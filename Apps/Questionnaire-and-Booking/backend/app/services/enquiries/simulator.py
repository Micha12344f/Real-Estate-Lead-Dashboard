"""Scenario generator and Mailpit adapter; all mail is sandbox-local."""
import asyncio
import hashlib
import json
import re
import smtplib
import uuid
from email.message import EmailMessage
from email import policy
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
from fastapi import HTTPException

from ..shared.config import fixtures_dir, fixture_properties, INBOX_ADDRESS, MOCK_INBOX_API, MOCK_INBOX_SMTP_HOST, MOCK_INBOX_SMTP_PORT, is_sandbox, QUESTIONNAIRE_URL, BOOKING_URL
from .parser import parse_email
from .intake import ingest_email
from ..shared.config import journey_url

SCENARIOS = {
    "rightmove": {"label": "Rightmove · complete enquiry", "source": "rightmove"},
    "onthemarket": {"label": "OnTheMarket · complete enquiry", "source": "onthemarket"},
    "missing_email": {"label": "Missing applicant email · review", "source": "onthemarket"},
    "unmatched": {"label": "Unknown property · review", "source": "onthemarket"},
    "html": {"label": "HTML-only portal enquiry", "source": "onthemarket"},
}


def fixture(scenario: str, index: int) -> dict:
    if scenario not in SCENARIOS:
        raise HTTPException(422, detail="Unknown scenario")
    records = json.loads((fixtures_dir() / "emails" / (SCENARIOS[scenario]["source"] + ".json")).read_text(encoding="utf-8"))
    if not 0 <= index < len(records):
        raise HTTPException(422, detail="Fixture index out of range")
    return records[index]


def scenario_message(scenario: str, index: int, run_id: uuid.UUID, message_id: str) -> EmailMessage:
    row = fixture(scenario, index)
    body = row["Raw_Body"]
    if scenario == "missing_email":
        body = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "", body)
    if scenario == "unmatched":
        body = re.sub(r"(?im)^PropAddress:.*$", "PropAddress:999 Unknown Lane, Nowhere, ZZ9 9ZZ;", body)
        body = re.sub(r"(?im)^(?:PropReference:|Your agency ref:).*$", "PropReference:UNKNOWN-999;", body)
    message = EmailMessage()
    message["From"] = row["From"]
    message["To"] = INBOX_ADDRESS
    message["Subject"] = row["Subject"]
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = message_id
    message["X-TVE-Sandbox-Run"] = str(run_id)
    if scenario == "html":
        import html
        message.set_content("<html><body>" + "".join("<p>" + html.escape(line) + "</p>" for line in body.splitlines()) + "</body></html>", subtype="html")
    else:
        message.set_content(body)
    return message


def send_mock(message: EmailMessage):
    if not is_sandbox():
        raise RuntimeError("Mock email requires sandbox=TRUE")
    # Fixed local hostname: configured real SMTP credentials are ignored.
    with smtplib.SMTP(MOCK_INBOX_SMTP_HOST, MOCK_INBOX_SMTP_PORT, timeout=15) as smtp:
        smtp.send_message(message)


async def start_scenario(pool, scenario: str, index: int) -> dict:
    fixture(scenario, index)
    run_id = uuid.uuid4()
    message_id = f"<sandbox-{run_id}@sandbox.invalid>"
    message = scenario_message(scenario, index, run_id, message_id)
    try:
        await asyncio.to_thread(send_mock, message)
    except Exception:
        raise HTTPException(502, detail="Mock SMTP is unavailable; no real SMTP fallback was attempted")
    return {"runId": str(run_id), "status": "smtp_captured", "contactEmail": parse_email(message.as_bytes()).sender_email}


async def ingest_email(pool, raw: bytes, *, uid: int, mailbox_key: str, mailbox_name: str, uid_validity: int):
    """Backwards-compatible re-export of the shared intake pipeline.

    The implementation lives in ``.intake``; the name is kept here so existing
    imports (the worker, tests and the webhook route) do not change.
    """
    from .intake import ingest_email as _ingest
    return await _ingest(pool, raw, uid=uid, mailbox_key=mailbox_key,
                         mailbox_name=mailbox_name, uid_validity=uid_validity)


async def poll_mailpit(pool):
    if not is_sandbox():
        raise RuntimeError("Mailpit intake is only available in sandbox mode")
    count = 0
    async with httpx.AsyncClient(base_url=MOCK_INBOX_API, timeout=15) as http:
        start = 0
        while True:
            response = await http.get("/api/v1/search", params={"query": f"to:{INBOX_ADDRESS}", "start": start, "limit": 100})
            response.raise_for_status()
            data = response.json()
            messages = data.get("messages", [])
            # Process and then read/delete each message: no ledger needed — the
            # contact upsert is idempotent, and the message leaves the inbox.
            for message in reversed(messages):
                mail_id = message["ID"]
                raw_response = await http.get(f"/api/v1/message/{mail_id}/raw")
                raw_response.raise_for_status()
                raw = raw_response.content
                uid = int.from_bytes(hashlib.sha256(mail_id.encode()).digest()[:7], "big")
                await ingest_email(pool, raw, uid=uid, mailbox_key="mailpit", mailbox_name=INBOX_ADDRESS, uid_validity=1)
                deleted = await http.request("DELETE", "/api/v1/messages", json={"IDs": [mail_id]})
                deleted.raise_for_status()
                count += 1
            # The inbox is drained page by page; deleting shifts the list.
            break
    return count


async def dashboard_runs(pool):
    """The dashboard's activity list, sourced from the contacts container."""
    from ..shared.database import get_pool as _get_pool
    contacts_pool = await _get_pool("contacts")
    async with contacts_pool.acquire() as cconn:
        rows = await cconn.fetch("SELECT * FROM contacts ORDER BY updated_at DESC LIMIT 100")
    results = []
    for row in rows:
        item = dict(row)
        item["links"] = {}
        if contacts_pool is None:
            continue
        token_pool = await _get_pool("tokens")
        async with token_pool.acquire() as tconn:
            token_rows = await tconn.fetch(
                "SELECT purpose, token FROM tokens WHERE contact_id=$1 AND expires_at > NOW()",
                row["id"])
        for token in token_rows:
            base = journey_url(token["purpose"])
            item["links"][token["purpose"]] = base + "/" + token["token"]
        results.append(item)
    return results


async def _tokens_pool():
    from ..shared.database import get_pool
    return await get_pool("tokens")
