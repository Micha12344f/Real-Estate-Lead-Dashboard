"""Add synthetic dashboard/journey examples without copying or resetting live data."""
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from .config import is_sandbox
from .database import get_pool, _DSNS, verify_sandbox_storage
from .tokens_store import issue_token
from .responses_store import save_responses, is_completed
from .contact_write_guard import writable_contact

EXAMPLES = (
    ("Alex", "New", "enquiried"),
    ("Sam", "Invited", "Questionnaire_sent"),
    ("Taylor", "Completed", "questionnaire_completed"),
    ("Jordan", "Viewing", "viewing_requested"),
    ("Morgan", "Booked", "booked"),
    ("Casey", "Done", "done"),
)
ANSWERS = {"move_timing": "Within 4 weeks", "monthly_budget": 1800,
           "rental_duration": "Over 1 year", "household_income": 55000,
           "income_proof": "Yes", "occupants": 2, "pets": "No", "smoke_vape": "No",
           "adverse_credit": "No", "booking_opt_in": "opt_in"}


async def seed() -> dict:
    if not is_sandbox():
        raise HTTPException(409, "Test data can only be loaded in Sandbox.")
    # All stores must exist before the first write; never attempt live fallback.
    await verify_sandbox_storage()
    pools = {name: await get_pool(name) for name in _DSNS}
    from ..enquiries.journey import property_catalogue
    catalogue = await property_catalogue()
    if not catalogue:
        raise HTTPException(503, "No selectable properties are available for test data.")
    added = 0
    for index, (first, last, status) in enumerate(EXAMPLES):
        property_id = int(catalogue[index % len(catalogue)]["id"])
        email = f"demo.{last.lower()}@sandbox.invalid"
        start = datetime.now(timezone.utc).replace(hour=14, minute=0, second=0, microsecond=0) + timedelta(days=3 + index)
        viewing = status in {"viewing_requested", "booked", "done"}
        async with pools["contacts"].acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO contacts (first_name, last_name, email, acquaint_property_id,
                       status, viewing_slot_start, viewing_slot_end)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT DO NOTHING RETURNING id""",
                first, last, email, property_id, status,
                start if viewing else None, start + timedelta(minutes=30) if viewing else None)
            if row:
                added += 1
            else:
                row = await conn.fetchrow("SELECT * FROM contacts WHERE lower(email)=lower($1)", email)
                status = row["status"]
                property_id = row["acquaint_property_id"]
        contact_id = row["id"]
        async with writable_contact(contact_id):
            async with pools["enquiries"].acquire() as conn:
                await conn.execute(
                    """INSERT INTO enquiries (contact_id, raw_email_body, platform, property_reference)
                       VALUES ($1,$2,'rightmove',$3) ON CONFLICT DO NOTHING""",
                    contact_id, f"Synthetic test enquiry from {first} {last}.\nEmail: {email}\nNo real applicant data.", str(property_id))
        if status == "Questionnaire_sent":
            await issue_token(contact_id, "questionnaire")
        elif status != "enquiried":
            if not await is_completed(contact_id):
                await save_responses(contact_id, ANSWERS)
            await issue_token(contact_id, "booking")
    return {"added": added, "sandbox": True, "message": "Test leads loaded. Existing test journeys were preserved."}
