# Backfill: copy questionnaire answers from the legacy core DB into pg-responses.
"""One-off local backfill run inside the backend container."""
import asyncio, json, os, sys

sys.path.insert(0, "/app")

import asyncpg

DSN_CONTACTS = os.environ["TVE_DB_CONTACTS"]
DSN_RESPONSES = os.environ["TVE_DB_RESPONSES"]
DSN_CORE = os.environ["DATABASE_URL"]

_COLUMNS = ("move_timing", "monthly_budget", "rental_duration", "household_income", "income_proof",
            "occupants", "pets", "smoke_vape", "adverse_credit", "booking_opt_in")


async def main():
    contacts = await asyncpg.connect(DSN_CONTACTS)
    core = responses = None
    try:
        if await contacts.fetchval("SELECT to_regclass('lead_delete_operations')"):
            raise RuntimeError("Legacy backfill disabled after lead deletion migration")
        core = await asyncpg.connect(DSN_CORE)
        responses = await asyncpg.connect(DSN_RESPONSES)
        rows = await core.fetch(
            """SELECT e.sender_email, s.answers FROM tve_submissions s
               JOIN tve_enquiries e ON e.id = s.enquiry_id WHERE e.sender_email IS NOT NULL"""
        )
        n = 0
        for row in rows:
            async with contacts.transaction():
                identity = await contacts.fetchrow(
                    "SELECT id FROM contacts WHERE lower(email)=lower($1) FOR UPDATE", row["sender_email"])
                if await contacts.fetchval("SELECT to_regclass('lead_delete_operations')"):
                    raise RuntimeError("Legacy backfill disabled after lead deletion migration")
                if identity is None:
                    continue
                answers = row["answers"] if isinstance(row["answers"], dict) else json.loads(row["answers"] or "{}")
                await responses.execute(
                    """INSERT INTO questionnaire_responses
                           (contact_id, move_timing, monthly_budget, rental_duration, household_income,
                            income_proof, occupants, pets, smoke_vape, adverse_credit, booking_opt_in)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) ON CONFLICT (contact_id) DO UPDATE SET updated_at=NOW()""",
                    identity["id"], *(answers.get(c) for c in _COLUMNS),
                )
                n += 1
        print(f"backfilled {n} questionnaire responses")
    finally:
        if core is not None:
            await core.close()
        if responses is not None:
            await responses.close()
        await contacts.close()


if __name__ == "__main__":
    asyncio.run(main())
