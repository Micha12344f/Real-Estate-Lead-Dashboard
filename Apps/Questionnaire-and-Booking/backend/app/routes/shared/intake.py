"""Webhook email intake: receive a raw inbound message and process it.

The mail provider (or a mail rule / inbound-parse service) POSTs the raw
RFC822 message here instead of the backend polling a mailbox. The message is
parsed, matched and persisted exactly like the other intake adapters, then the
follow-up worker sends any owed questionnaire invitation.

Endpoints:
    POST /webhook/thames-valley-estates/email-intake
        - Content-Type: message/rfc822  -> the raw message is the request body
        - Content-Type: application/json -> {"raw": "<base64 RFC822>"}
    GET  /webhook/thames-valley-estates/email-intake/health

Auth: the standard X-TVE-Auth header (see verify_webhook_auth in main).
"""

from __future__ import annotations

import base64
import binascii
from email import policy
from email.parser import BytesParser

from fastapi import APIRouter, HTTPException, Request

from ...services.enquiries.followup import deliver_due_followups
from ...services.enquiries.intake import ingest_email
from ...services.shared.database import get_pool

router = APIRouter(prefix="/webhook/thames-valley-estates")

MAX_RAW_BYTES = 2_000_000


async def _read_raw(request: Request) -> bytes:
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    body = await request.body()
    if len(body) > MAX_RAW_BYTES:
        raise HTTPException(status_code=413, detail="Message too large")
    if content_type == "application/json":
        try:
            payload = await request.json()
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Invalid JSON body") from error
        encoded = payload.get("raw") if isinstance(payload, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise HTTPException(status_code=422, detail="JSON body must contain a base64 'raw' field")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise HTTPException(status_code=422, detail="'raw' is not valid base64") from error
        return raw
    # Default: the body itself is the raw RFC822 message.
    return body


@router.get("/email-intake/health")
async def intake_health() -> dict:
    return {"status": "ok", "service": "tve-email-intake", "mode": "webhook"}


@router.post("/email-intake")
async def email_intake(request: Request) -> dict:
    """Parse, match and persist one inbound message; send owed invitations."""
    raw = await _read_raw(request)
    if not raw.strip():
        raise HTTPException(status_code=422, detail="Empty message")

    # Reject anything that is not a parseable RFC822 message.
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        sender = message.get("From")
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="Unparseable message") from error
    if not sender:
        raise HTTPException(status_code=422, detail="Message has no From header")

    message_id = (message.get("Message-ID") or "").strip()
    pool = await get_pool()
    contact_id = await ingest_email(
        pool, raw, uid=0, mailbox_key="webhook", mailbox_name="INBOX", uid_validity=0)

    # Only a genuine, matched enquiry produces a contact id here.
    sent = 0
    if contact_id is not None:
        try:
            sent = await deliver_due_followups(None, limit=5)
        except Exception:  # noqa: BLE001
            # Invitations are retried by the worker; intake itself succeeded.
            sent = 0

    return {"status": "accepted", "contactId": contact_id,
            "isEnquiry": contact_id is not None,
            "messageId": message_id or None, "invitationsSent": sent}
