"""Mail worker: listens for inbound emails and sends outbound mail.

Two modes:
- sandbox=TRUE  → the local Mailpit inbox (mock SMTP), messages deleted after intake.
- sandbox=FALSE with credentials → a real IMAP mailbox over TLS,
  read-only selection, durable bounded replay checkpoints, and outbound SMTP.
"""
from __future__ import annotations

import asyncio
import hashlib
import imaplib
import logging
import os
import ssl

from app.services.enquiries.followup import deliver_due_followups
from app.services.enquiries.parser import parse_email
from app.services.enquiries.simulator import poll_mailpit, ingest_email
from app.services.shared.config import is_sandbox, mode_scope
from app.services.shared import settings_store
from app.services.shared import runtime_mode
from app.workers.imap_checkpoint import CheckpointError, CheckpointStore, identity_hash

logging.basicConfig(level=os.environ.get("TVE_LOG_LEVEL", "INFO"))
logger = logging.getLogger("tve.sandbox.imap")


def _required(name: str, values) -> str:
    value = (values.get(name) or "").strip()
    if not value or value.startswith("<set-"):
        raise RuntimeError(f"{name}_not_configured")
    return value


def _build_imap_config(values) -> dict:
    host = _required("TVE_IMAP_HOST", values)
    username = _required("TVE_IMAP_USERNAME", values)
    password = _required("TVE_IMAP_PASSWORD", values)
    port = int(values.get("TVE_IMAP_PORT") or "993")
    if not 1 <= port <= 65535:
        raise RuntimeError("TVE_IMAP_PORT_invalid")
    mailbox = (values.get("TVE_IMAP_MAILBOX") or "INBOX").strip() or "INBOX"
    mailbox_key = hashlib.sha256(f"{host.lower()}\n{username}".encode()).hexdigest()
    return {"host": host, "username": username, "password": password, "port": port,
            "mailbox": mailbox, "mailbox_key": mailbox_key}


def _positive_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(1, min(value, maximum))


def email_intake_mode() -> str:
    """Which inbound-email transport is active.

    ``webhook`` (default) - the mail provider POSTs raw messages to
    /webhook/thames-valley-estates/email-intake; the worker does not poll.
    ``imap``    - legacy mailbox polling (opt-in via TVE_EMAIL_INTAKE=imap).
    """
    mode = (os.environ.get("TVE_EMAIL_INTAKE") or "webhook").strip().lower()
    return "imap" if mode == "imap" else "webhook"


def inbound_configuration() -> dict | None:
    """Synchronous bootstrap snapshot; simulation bypasses credential parsing."""
    return None if is_sandbox() else _build_imap_config(os.environ)


async def _imap_config() -> dict | None:
    """Resolve credentials each poll using the settings store's short TTL."""
    return None if is_sandbox() else _build_imap_config(await settings_store.get_many("imap"))


def _cursor_identity(config: dict) -> str:
    return identity_hash(config)


def _imap_fetch_batch(config: dict, checkpoint: CheckpointStore, limit: int) -> tuple[int, int, list[tuple[int, bytes]]]:
    """Baseline/resume this selected mailbox, then fetch only subsequent UIDs."""
    connection = imaplib.IMAP4_SSL(
        config["host"], config["port"], ssl_context=ssl.create_default_context(), timeout=20,
    )
    try:
        connection.login(config["username"], config["password"])
        status, _ = connection.select(config["mailbox"], readonly=True)
        if status != "OK":
            raise RuntimeError("imap_mailbox_select_failed")

        def metadata(name):
            _, values = connection.response(name)
            if not values or len(values) != 1 or not isinstance(values[0], bytes) or not values[0].isdigit():
                raise CheckpointError("imap_checkpoint_metadata_invalid")
            return int(values[0])

        uid_validity, uid_next = metadata("UIDVALIDITY"), metadata("UIDNEXT")
        last_uid, baseline = checkpoint.prepare(_cursor_identity(config), uid_validity, uid_next)
        if baseline:
            logger.info("imap checkpoint baselined reason=%s", baseline)
            return uid_validity, last_uid, []
        if limit <= 0:
            return uid_validity, last_uid, []
        status, data = connection.uid("search", None, "UID", f"{last_uid + 1}:*")
        if status != "OK" or not data or not isinstance(data[0], bytes):
            raise RuntimeError("imap_uid_search_failed")
        values = data[0].split()
        if any(not value.isdigit() or not 1 <= int(value) <= 2**32 - 1 for value in values):
            raise RuntimeError("imap_uid_search_failed")
        uids = sorted({int(value) for value in values if int(value) > last_uid})
        items: list[tuple[int, bytes]] = []
        for uid in uids[:limit]:
            status, parts = connection.uid("fetch", str(uid), "(BODY.PEEK[])")
            raw = next((part[1] for part in parts or [] if isinstance(part, tuple) and isinstance(part[1], bytes)), None)
            if status != "OK" or raw is None:
                raise RuntimeError("imap_message_fetch_failed")
            items.append((uid, raw))
        return uid_validity, last_uid, items
    finally:
        try:
            connection.logout()
        except Exception:
            pass


async def poll_once(pool, config: dict) -> dict:
    """Persist each completed intake before moving to a later UID.

    The poll lock also prevents two workers consuming the same checkpoint.
    Failed fetch/parser/intake/write aborts the batch, leaving the failed UID
    retryable. Successful non-enquiry classification is a completed no-op.
    """
    with CheckpointStore() as checkpoint:
        uid_validity, newest, messages = await asyncio.to_thread(
            _imap_fetch_batch, config, checkpoint, _positive_int("TVE_IMAP_MAX_MESSAGES_PER_POLL", 25, 100))
        processed = 0
        contact_pool = pool
        for uid, raw in messages:
            parsed = parse_email(raw)
            if not checkpoint.contains(parsed.dedupe_key) and parsed.is_enquiry and parsed.sender_email:
                if contact_pool is None:
                    from app.services.shared.database import get_pool
                    contact_pool = await get_pool("contacts")
                contact_id = await ingest_email(contact_pool, raw, uid=uid,
                                                mailbox_key=config["mailbox_key"],
                                                mailbox_name=config["mailbox"], uid_validity=uid_validity)
                if not contact_id:
                    raise RuntimeError("imap_intake_not_committed")
                processed += 1
            checkpoint.complete(_cursor_identity(config), uid, parsed.dedupe_key)
            newest = uid
        return {"fetched": len(messages), "new_enquiries": processed, "last_uid": newest}


async def run() -> None:
    from app.services.shared.database import close_pool
    os.environ.setdefault("TVE_SETTINGS_TTL_SECONDS", "5")
    if is_sandbox():
        logger.info("sandbox mode: mock Mailpit intake selected; IMAP bypassed")
    elif email_intake_mode() == "imap":
        logger.info("imap intake enabled; credentials resolved each poll")
    else:
        logger.info("webhook intake selected; worker sends follow-ups only")
    interval = 2 if is_sandbox() else _positive_int("TVE_IMAP_POLL_SECONDS", 60, 3600)
    try:
        while True:
            await runtime_mode.refresh()
            interval = 2 if is_sandbox() else _positive_int("TVE_IMAP_POLL_SECONDS", 60, 3600)
            with mode_scope(is_sandbox()):
                await run_batch()
            await asyncio.sleep(interval)
    finally:
        await close_pool()


async def run_batch() -> None:
    """One pinned intake/follow-up batch; sandbox storage errors pause the batch."""
    checkpoint_failed = False
    if is_sandbox():
        try:
            # Preflight every store before touching Mailpit or issuing mail.
            from app.services.shared.database import verify_sandbox_storage
            await verify_sandbox_storage()
            await poll_mailpit(None)
        except Exception as exc:
            logger.warning("sandbox intake paused error_type=%s", type(exc).__name__)
            return
    elif email_intake_mode() == "imap":
        # Legacy polling remains available but is off by default.
        try:
            config = await _imap_config()
            result = await poll_once(None, config)
            if result["fetched"]:
                logger.info("imap poll completed fetched=%s new_enquiries=%s",
                            result["fetched"], result["new_enquiries"])
        except Exception as exc:
            checkpoint_failed = isinstance(exc, CheckpointError)
            logger.warning("imap poll failed error_type=%s", type(exc).__name__)
    # webhook mode: intake happens on the HTTP route; the worker only sends
    # follow-up invitations owed to already-matched enquiries.
    try:
        if not checkpoint_failed:
            await deliver_due_followups(None)
    except Exception as exc:
        logger.warning("follow-up worker paused error_type=%s", type(exc).__name__)


if __name__ == "__main__":
    asyncio.run(run())
