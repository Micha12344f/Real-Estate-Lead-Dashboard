"""Enquiries container: one row per contact; the parsed portal message."""

from .contact_write_guard import writable_contact
from .database import get_pool

_COLUMNS = ("property_reference", "platform", "move_in_date", "duration", "moving_with",
            "pets", "smokers", "employment", "affordability", "adverse_credit")


async def upsert_enquiry(contact_id: int, raw_email_body: str | None, extracted: dict) -> bool:
    """Store the parsed enquiry for a contact; latest message wins."""
    fields = {key: extracted.get(key) for key in _COLUMNS}
    async with writable_contact(contact_id):
        pool = await get_pool("enquiries")
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
            """INSERT INTO enquiries (contact_id, raw_email_body, property_reference, platform, move_in_date,
                                      duration, moving_with, pets, smokers, employment, affordability, adverse_credit)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
               ON CONFLICT (contact_id) DO UPDATE SET
                   raw_email_body = COALESCE(excluded.raw_email_body, enquiries.raw_email_body),
                   property_reference = COALESCE(excluded.property_reference, enquiries.property_reference),
                   platform = COALESCE(excluded.platform, enquiries.platform),
                   move_in_date = COALESCE(excluded.move_in_date, enquiries.move_in_date),
                   duration = COALESCE(excluded.duration, enquiries.duration),
                   moving_with = COALESCE(excluded.moving_with, enquiries.moving_with),
                   pets = COALESCE(excluded.pets, enquiries.pets),
                   smokers = COALESCE(excluded.smokers, enquiries.smokers),
                   employment = COALESCE(excluded.employment, enquiries.employment),
                   affordability = COALESCE(excluded.affordability, enquiries.affordability),
                   adverse_credit = COALESCE(excluded.adverse_credit, enquiries.adverse_credit),
                   updated_at = NOW()
               RETURNING (xmax = 0) AS inserted""",
            contact_id, raw_email_body, fields["property_reference"], fields["platform"],
            fields["move_in_date"], fields["duration"], fields["moving_with"], fields["pets"],
            fields["smokers"], fields["employment"], fields["affordability"], fields["adverse_credit"],
            )
    return bool(row["inserted"])


async def enquiry(contact_id: int) -> dict | None:
    """The stored enquiry row for a contact, or None."""
    pool = await get_pool("enquiries")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM enquiries WHERE contact_id=$1", contact_id)
    return dict(row) if row else None
