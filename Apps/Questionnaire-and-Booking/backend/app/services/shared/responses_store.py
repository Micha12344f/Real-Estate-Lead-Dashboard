"""Questionnaire responses container: the answers, one row per contact."""

from contextlib import asynccontextmanager

from .contact_write_guard import writable_contact
from .database import get_pool


@asynccontextmanager
async def _existing_lock():
    yield


async def save_responses(contact_id: int, answers: dict, *, _contact_locked: bool = False) -> bool:
    """Store a contact's questionnaire answers; returns True if newly created."""
    async with _existing_lock() if _contact_locked else writable_contact(contact_id):
        pool = await get_pool("responses")
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
            """INSERT INTO questionnaire_responses
                   (contact_id, move_timing, monthly_budget, rental_duration, household_income,
                    income_proof, occupants, pets, smoke_vape, adverse_credit, booking_opt_in)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
               ON CONFLICT (contact_id) DO UPDATE SET
                   move_timing = excluded.move_timing,
                   monthly_budget = excluded.monthly_budget,
                   rental_duration = excluded.rental_duration,
                   household_income = excluded.household_income,
                   income_proof = excluded.income_proof,
                   occupants = excluded.occupants,
                   pets = excluded.pets,
                   smoke_vape = excluded.smoke_vape,
                   adverse_credit = excluded.adverse_credit,
                   booking_opt_in = excluded.booking_opt_in,
                   updated_at = NOW()
               RETURNING (xmax = 0) AS inserted""",
            contact_id, answers.get("move_timing"), _int_or_none(answers.get("monthly_budget")),
            answers.get("rental_duration"), _int_or_none(answers.get("household_income")),
            answers.get("income_proof"), _int_or_none(answers.get("occupants")), answers.get("pets"),
            answers.get("smoke_vape"), answers.get("adverse_credit"), answers.get("booking_opt_in"),
            )
    return bool(row["inserted"])


def _int_or_none(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


async def is_completed(contact_id: int) -> bool:
    pool = await get_pool("responses")
    async with pool.acquire() as conn:
        return bool(await conn.fetchval("SELECT 1 FROM questionnaire_responses WHERE contact_id=$1", contact_id))


async def response(contact_id: int) -> dict | None:
    """The stored questionnaire answers for a contact, or None."""
    pool = await get_pool("responses")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM questionnaire_responses WHERE contact_id=$1", contact_id)
    return dict(row) if row else None
