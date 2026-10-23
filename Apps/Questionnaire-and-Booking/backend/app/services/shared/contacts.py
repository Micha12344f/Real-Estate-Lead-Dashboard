"""Contacts container: one row per person, keyed by email."""

import re

from .database import get_pool

_NAME_TRIM = " \t\r\n,;:.!?\"'`|/-"


def clean_name_part(value: str | None) -> str:
    """Collapse whitespace and strip surrounding punctuation from a name part."""
    return re.sub(r"\s+", " ", str(value or "")).strip(_NAME_TRIM)


async def upsert_contact(first_name: str, last_name: str, email: str, phone: str | None = None,
                         initial_status: str = "enquiried") -> int | None:
    """Insert or update a contact keyed by lowercase email; returns its id.

    Accepts the old single-string name as `first_name`; it is split on the
    first space when `last_name` is empty.

    ``initial_status`` is used only for a brand-new contact. An existing
    contact is never downgraded: an ``unmatched_enquiry`` contact is promoted to
    ``enquiried`` once a property matches, but a progressed contact is left
    untouched.
    """
    if not email:
        return None
    first, last = _split_name(first_name, last_name)
    pool = await get_pool("contacts")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO contacts (first_name, last_name, email, phone, status)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (lower(email)) DO UPDATE
               SET first_name = CASE WHEN excluded.first_name <> '' THEN excluded.first_name ELSE contacts.first_name END,
                   last_name = CASE WHEN excluded.last_name <> '' THEN excluded.last_name ELSE contacts.last_name END,
                   phone = COALESCE(excluded.phone, contacts.phone),
                   status = CASE
                       WHEN contacts.status IS NULL THEN excluded.status
                       WHEN contacts.status = 'unmatched_enquiry' AND excluded.status = 'enquiried' THEN 'enquiried'
                       ELSE contacts.status END,
                   updated_at = NOW()
               RETURNING id""",
            first, last, email, phone, initial_status,
        )
    return int(row["id"])


def _split_name(first_name: str, last_name: str) -> tuple[str, str]:
    first_name = clean_name_part(first_name)
    last_name = clean_name_part(last_name)
    if not last_name and " " in first_name:
        head, _, tail = first_name.partition(" ")
        return head, clean_name_part(tail)
    return first_name, last_name


async def find_contact(email: str) -> int | None:
    """Return the contact id for an email, or None."""
    if not email:
        return None
    pool = await get_pool("contacts")
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT id FROM contacts WHERE lower(email)=lower($1)", email)


async def contact_email(contact_id: int) -> str | None:
    pool = await get_pool("contacts")
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT email FROM contacts WHERE id=$1", contact_id)


async def link_property(contact_id: int, property_id: int) -> None:
    pool = await get_pool("contacts")
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE contacts SET acquaint_property_id=$2, updated_at=NOW() WHERE id=$1 AND viewing_slot_start IS NULL",
            contact_id, property_id)


async def promote_to_enquiried(contact_id: int) -> bool:
    """Move an ``unmatched_enquiry`` contact to ``enquiried`` once a property is
    linked. Returns True when the row was in ``unmatched_enquiry`` and changed.

    This is the only legal transition into the journey ladder from the
    off-ladder review state; progressed contacts are never touched.
    """
    pool = await get_pool("contacts")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE contacts SET status='enquiried', updated_at=NOW()
               WHERE id=$1 AND status=$2 RETURNING id""",
            contact_id, UNMATCHED_STATUS)
    return row is not None


async def contact(contact_id: int) -> dict | None:
    """The full contact row."""
    pool = await get_pool("contacts")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM contacts WHERE id=$1", contact_id)
    return dict(row) if row else None


async def list_contacts(limit: int | None = None) -> list[dict]:
    """All contacts, most recently updated first; an optional limit caps the rows."""
    pool = await get_pool("contacts")
    async with pool.acquire() as conn:
        if limit is None:
            rows = await conn.fetch("SELECT * FROM contacts ORDER BY updated_at DESC")
        else:
            rows = await conn.fetch("SELECT * FROM contacts ORDER BY updated_at DESC LIMIT $1", limit)
    return [dict(row) for row in rows]


STAGES = ("enquiried", "Questionnaire_sent", "questionnaire_completed",
          "booking_link_sent", "viewing_requested", "booked", "done")

# A genuine lettings enquiry that did not uniquely match a property. It is a
# real enquiry for audit but is not part of the journey ladder and is never
# invited, so it is tracked as a separate status.
UNMATCHED_STATUS = "unmatched_enquiry"


async def advance_status(contact_id: int, stage: str) -> None:
    """Move a contact forward through the journey ladder; never backwards."""
    if stage not in STAGES:
        raise ValueError(f"unknown journey stage: {stage}")
    pool = await get_pool("contacts")
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE contacts SET status=$2, updated_at=NOW()
               WHERE id=$1 AND (status IS NULL OR
                     ARRAY_POSITION($3::text[], status) < ARRAY_POSITION($3::text[], $2))""",
            contact_id, stage, list(STAGES))


async def contact_identity(contact_id: int) -> dict:
    """Return {first_name,last_name,email} for a contact, or {}."""
    pool = await get_pool("contacts")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT first_name, last_name, email FROM contacts WHERE id=$1", contact_id)
    return dict(row) if row else {}


async def save_viewing(contact_id: int, property_id: int, slot_start, slot_end) -> None:
    """Record the viewing on the contact; the ladder status carries the stage."""
    pool = await get_pool("contacts")
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE contacts
               SET acquaint_property_id = $2,
                   viewing_slot_start = $3, viewing_slot_end = $4, updated_at = NOW()
               WHERE id = $1""",
            contact_id, property_id, slot_start, slot_end)


async def decide_viewing(contact_id: int, approved: bool) -> bool:
    """Resolve a pending viewing request exactly once.

    Approval advances the journey to ``booked``. Denial returns it to the
    completed-questionnaire stage and releases the requested slot.
    """
    pool = await get_pool("contacts")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE contacts
               SET status=$2,
                   viewing_slot_start=CASE WHEN $3 THEN viewing_slot_start ELSE NULL END,
                   viewing_slot_end=CASE WHEN $3 THEN viewing_slot_end ELSE NULL END,
                   updated_at=NOW()
               WHERE id=$1 AND status='viewing_requested'
               RETURNING id""",
            contact_id, "booked" if approved else "questionnaire_completed", approved)
    return row is not None
