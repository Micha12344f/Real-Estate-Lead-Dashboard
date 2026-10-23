"""Outbound mail for the journey, driven by contact stages.

Each contact at `enquiried` owes a questionnaire invitation; sending it
advances the stage. Follow-up retries use simple per-attempt backoff with no
separate outbox table.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from urllib.parse import quote

from ..shared.config import (MOCK_INBOX_SMTP_HOST, MOCK_INBOX_SMTP_PORT, QUESTIONNAIRE_URL,
                             fixture_properties, is_sandbox)
from ..shared.database import get_pool
from ..shared import contacts as contacts_store
from ..shared import tokens_store
from ..shared import settings_store
from ..shared.contacts import clean_name_part
from .journey import issue_token
from ..shared.config import BOOKING_URL, journey_url

BACKOFF_SECONDS = [30, 120, 600, 1800]
EMAIL_DOMAIN = "thamesvalleyestates.co.uk"


def _set_professional_content(message: EmailMessage, text: str) -> None:
    """Keep customer email readable and structured as plain text only."""
    message.set_content(text)


def _branded_sender(sender: str) -> str:
    _, address = parseaddr(sender)
    return formataddr(("Thames Valley Estates", address or sender))


async def send_journey_email(identity: dict, kind: str, *, token: str | None = None,
                             raise_on_error: bool = False) -> None:
    """Send a customer-facing next step through the mode-selected SMTP adapter."""
    config = await mail_configuration()
    message = EmailMessage()
    message["To"] = identity["email"]
    message["From"] = _branded_sender(config["sender"])
    message["Message-ID"] = f"<tve-{kind}-{identity['id']}@{EMAIL_DOMAIN}>"
    if kind in ("booking_link", "manual_booking_link"):
        link = journey_url("booking") + "/" + quote(token or "", safe="")
        name = _identity(identity)[0] or "there"
        introduction = ("Our team would like to invite you to a viewing."
                        if kind == "manual_booking_link" else "Thank you for completing your questionnaire.")
        message["Subject"] = "Choose a viewing time | Thames Valley Estates"
        _set_professional_content(
            message,
            f"Dear {name},\n\n{introduction} You can now choose a suitable viewing time using the secure link below.\n\n{link}\n\nYour viewing request will be reviewed by our team; your appointment is not confirmed until we contact you.\n\nKind regards,\nThames Valley Estates",
        )
    else:
        from zoneinfo import ZoneInfo
        when = identity["viewing_slot_start"].astimezone(ZoneInfo("Europe/London")).strftime("%A %d %B %Y at %H:%M")
        name = _identity(identity)[0] or "there"
        message["Subject"] = "We’ve received your viewing request | Thames Valley Estates"
        _set_professional_content(
            message,
            f"Dear {name},\n\nThank you for your viewing request for {when} (UK time). We have received it, but the appointment is not confirmed yet. Our team will contact you with an update.\n\nKind regards,\nThames Valley Estates",
        )
    try:
        await asyncio.to_thread(_smtp_send, config, message)
    except Exception as error:
        logging.getLogger("tve.mail").warning("Journey email failed type=%s", type(error).__name__)
        if raise_on_error:
            raise
        return
    # Token creation alone is not proof that an invitation was sent. Record the
    # stage only once SMTP accepts the message, for automatic and staff sends.
    # advance_status never moves requested/confirmed viewings backwards.
    if kind in ("booking_link", "manual_booking_link"):
        await contacts_store.advance_status(identity["id"], "booking_link_sent")


def _required_mail_value(name: str, values) -> str:
    value = (values.get(name) or "").strip()
    if not value or value.startswith("<"):
        raise RuntimeError(f"{name}_not_configured")
    return value


def _mail_configuration(values=None) -> dict:
    """Build a snapshot; synchronous env fallback is retained for local callers."""
    if is_sandbox():
        return {"host": MOCK_INBOX_SMTP_HOST, "port": MOCK_INBOX_SMTP_PORT, "sender": "lettings@sandbox.invalid"}
    values = os.environ if values is None else values
    return {
        "host": _required_mail_value("TVE_SMTP_HOST", values),
        "port": int(values.get("TVE_SMTP_PORT") or "587"),
        "username": _required_mail_value("TVE_SMTP_USER", values),
        "password": _required_mail_value("TVE_SMTP_PASSWORD", values),
        "sender": _required_mail_value("TVE_ENQUIRY_FROM", values),
    }


async def mail_configuration() -> dict:
    """Resolve the current SMTP credentials immediately before each send."""
    if is_sandbox():
        config = _mail_configuration({})
        sender = await settings_store.get("TVE_ENQUIRY_FROM")
        if sender and settings_store.validate("TVE_ENQUIRY_FROM", sender) is None:
            config["sender"] = sender
        return config
    return _mail_configuration(await settings_store.get_many("smtp"))


def _smtp_send(config: dict, message: EmailMessage) -> None:
    if is_sandbox():
        with smtplib.SMTP(MOCK_INBOX_SMTP_HOST, MOCK_INBOX_SMTP_PORT, timeout=15) as smtp:
            smtp.send_message(message)
        return
    context = ssl.create_default_context()
    if config["port"] == 465:
        with smtplib.SMTP_SSL(config["host"], config["port"], timeout=20, context=context) as smtp:
            smtp.login(config["username"], config["password"])
            smtp.send_message(message)
    else:
        with smtplib.SMTP(config["host"], config["port"], timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(config["username"], config["password"])
            smtp.send_message(message)


async def deliver_booking_email(*, to: str, subject: str, body: str) -> bool:
    """Best-effort delivery of one plain-text booking email.

    Reuses the mode-selected SMTP adapter (Mailpit in Sandbox) and the branded
    from-address. Never raises: failures are logged by exception class only so
    the booking response is unaffected.
    """
    recipient = (to or "").strip()
    if not recipient:
        return False
    try:
        config = await mail_configuration()
        message = EmailMessage()
        message["To"] = recipient
        message["From"] = _branded_sender(config["sender"])
        message["Subject"] = subject
        _set_professional_content(message, body)
        await asyncio.to_thread(_smtp_send, config, message)
        return True
    except Exception as error:
        logging.getLogger("tve.mail").warning("Booking email failed type=%s", type(error).__name__)
        return False


def _identity(contact: dict) -> tuple[str, str]:
    name = (clean_name_part(contact.get("first_name")), clean_name_part(contact.get("last_name")))
    return (" ".join(part for part in name if part)).strip(), contact.get("email") or ""


def _invitation_message(contact_id: int, name: str, email: str, token: str, base_url: str, sender: str) -> EmailMessage:
    message = EmailMessage()
    message["To"] = email
    message["From"] = _branded_sender(sender)
    message["Message-ID"] = f"<tve-questionnaire-{contact_id}@{EMAIL_DOMAIN}>"
    message["Subject"] = "Next step: your property enquiry | Thames Valley Estates"
    link = base_url.rstrip("/") + "/" + quote(token, safe="")
    greeting = name or "there"
    _set_professional_content(
        message,
        f"Dear {greeting},\n\nThank you for your enquiry to Thames Valley Estates. To help us understand your requirements and recommend suitable next steps, please complete our short questionnaire using the secure link below.\n\n{link}\n\nThis link is valid for {tokens_store.TOKEN_TTL_DAYS} days. If you have any questions, please reply to this email and our lettings team will be happy to help.\n\nKind regards,\nThames Valley Estates",
    )
    return message


async def deliver_due_followups(pool, *, limit: int = 10) -> int:
    """Send the invitations owed to contacts; returns the number sent."""
    candidates_pool = await get_pool("contacts")
    async with candidates_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, first_name, last_name, email FROM contacts WHERE status='enquiried' AND acquaint_property_id IS NOT NULL ORDER BY id LIMIT $1",
            max(1, min(limit, 50)))
    sent = 0
    for row in rows:
        contact_id = int(row["id"])
        name, email = _identity(dict(row))
        if not email:
            continue
        try:
            token = await issue_token(name, email, "questionnaire")
            config = await mail_configuration()
            message = _invitation_message(contact_id, name, email, token, journey_url("questionnaire"), config["sender"])
            await asyncio.to_thread(_smtp_send, config, message)
            await contacts_store.advance_status(contact_id, "Questionnaire_sent")
            sent += 1
        except Exception:
            await asyncio.sleep(1)
    return sent


async def send_questionnaire_invitation(contact_id: int) -> bool:
    """Send exactly one questionnaire invitation for one contact.

    Used both by the follow-up worker and by the dashboard's manual assignment.
    The whole operation is guarded by a row lock and the status transition, so a
    concurrent worker pass or a double-click cannot send a second invitation:

    * the contact row is locked, its status re-read inside the transaction;
    * the invitation is only sent while the status is still ``enquiried``;
    * a fresh token is issued and the status advances to ``Questionnaire_sent``
      atomically before the send, so a second caller sees the new status and
      returns without sending again.

    Returns True when an invitation was sent by this call.
    """
    candidates_pool = await get_pool("contacts")
    identity: dict | None = None
    async with candidates_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, first_name, last_name, email, status, acquaint_property_id "
                "FROM contacts WHERE id=$1 FOR UPDATE", contact_id)
            if row is None:
                raise LookupError("unknown_contact")
            if row["status"] != "enquiried" or row["acquaint_property_id"] is None:
                # Already invited/advanced, or still not matched: nothing to send.
                return False
            # Claim the send by advancing the stage inside the lock.
            await conn.execute(
                "UPDATE contacts SET status='Questionnaire_sent', updated_at=NOW() WHERE id=$1",
                contact_id)
            identity = dict(row)

    assert identity is not None
    name, email = _identity(identity)
    if not email:
        # Revert the claim: no address means no invitation could be sent.
        async with candidates_pool.acquire() as conn:
            await conn.execute(
                "UPDATE contacts SET status='enquiried', updated_at=NOW() WHERE id=$1 AND status='Questionnaire_sent'",
                contact_id)
        return False
    try:
        token = await tokens_store.reissue_token(contact_id, "questionnaire")
        config = await mail_configuration()
        message = _invitation_message(contact_id, name, email, token,
                                      journey_url("questionnaire"), config["sender"])
        await asyncio.to_thread(_smtp_send, config, message)
        return True
    except Exception:
        # Fail closed: restore the previous stage so the worker/user can retry.
        async with candidates_pool.acquire() as conn:
            await conn.execute(
                "UPDATE contacts SET status='enquiried', updated_at=NOW() WHERE id=$1 AND status='Questionnaire_sent'",
                contact_id)
        raise
