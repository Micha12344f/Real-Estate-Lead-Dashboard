# Backfill: copy parsed enquiry fields from the legacy core DB into pg-enquiries.
"""One-off local backfill run inside the backend container."""
import asyncio, json, os, sys

sys.path.insert(0, "/app")

import asyncpg

DSN_CONTACTS = os.environ["TVE_DB_CONTACTS"]
DSN_ENQUIRIES = os.environ["TVE_DB_ENQUIRIES"]
DSN_CORE = os.environ["DATABASE_URL"]


async def main():
    contacts = await asyncpg.connect(DSN_CONTACTS)
    core = enquiries = None
    try:
        if await contacts.fetchval("SELECT to_regclass('lead_delete_operations')"):
            raise RuntimeError("Legacy backfill disabled after lead deletion migration")
        core = await asyncpg.connect(DSN_CORE)
        enquiries = await asyncpg.connect(DSN_ENQUIRIES)
        rows = await core.fetch(
            "SELECT sender_email, extracted_fields, property_match_status FROM tve_enquiries WHERE sender_email IS NOT NULL"
        )
        n = 0
        for row in rows:
            # No stale email-to-ID snapshot. Lock the exact current contact
            # through the child commit, and refuse if migration starts mid-run.
            async with contacts.transaction():
                identity = await contacts.fetchrow(
                    "SELECT id FROM contacts WHERE lower(email)=lower($1) FOR UPDATE", row["sender_email"])
                if await contacts.fetchval("SELECT to_regclass('lead_delete_operations')"):
                    raise RuntimeError("Legacy backfill disabled after lead deletion migration")
                if identity is None:
                    continue
                ex = row["extracted_fields"] if isinstance(row["extracted_fields"], dict) else json.loads(row["extracted_fields"] or "{}")
                await enquiries.execute(
                    """INSERT INTO enquiries (contact_id, property_reference, platform, move_in_date, duration,
                                              moving_with, pets, smokers, employment, affordability, adverse_credit)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                       ON CONFLICT (contact_id) DO UPDATE SET updated_at=NOW()""",
                    identity["id"], ex.get("property_reference"), ex.get("platform"), ex.get("move_in_date"),
                    ex.get("duration"), ex.get("moving_with"), ex.get("pets"), ex.get("smokers"),
                    ex.get("employment"), ex.get("affordability"), ex.get("adverse_credit"),
                )
                n += 1
        print(f"backfilled {n} enquiry rows")
    finally:
        if core is not None:
            await core.close()
        if enquiries is not None:
            await enquiries.close()
        await contacts.close()


if __name__ == "__main__":
    asyncio.run(main())
