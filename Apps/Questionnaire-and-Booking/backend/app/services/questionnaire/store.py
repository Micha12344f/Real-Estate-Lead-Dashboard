"""Postgres-backed persistence - replaces the five TVE n8n data tables.

Tables (schemas from backend/data-tables/schemas.json):
  tve_enquiries (22 cols), tve_contacts (8), tve_emails (6),
  tve_lead_context_email (2), tve_actions (7), tve_submissions (new, questionnaires)
"""
from __future__ import annotations

import json
import hashlib
import os
from urllib.parse import quote

DB_DSN = os.environ.get("DATABASE_URL", "").strip()
if not DB_DSN:
    # Derive the connection URI from the Compose-injected password. Encoding
    # prevents reserved URI characters in a randomly generated password from
    # changing how the DSN is parsed.
    password = os.environ.get("POSTGRES_PASSWORD", "")
    if password:
        DB_DSN = f"postgresql://n8n:{quote(password, safe='')}@postgres:5432/tve"


async def save_submission(source: str, token: str | None, answers: dict,
                          contact_email: str | None = None) -> dict:
    """Persist a questionnaire submission; returns {id}."""
    from ..shared.config import is_sandbox
    if is_sandbox():
        from ..shared.database import get_pool
        pool = await get_pool("responses")
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO tve_submissions (source, token, contact_email, answers) VALUES ($1,$2,$3,$4) RETURNING id",
                source, token, contact_email, json.dumps(answers))
        return {"id": row["id"], "stored": True}
    import asyncpg
    if not DB_DSN:
        # No DB configured (tests / dry runs) - in-memory ack
        return {"id": None, "stored": False}
    conn = await asyncpg.connect(DB_DSN)
    try:
        row = await conn.fetchrow(
            """INSERT INTO tve_submissions (source, token, contact_email, answers)
               VALUES ($1, $2, $3, $4)
               RETURNING id""",
            source, token, contact_email, json.dumps(answers),
        )
        if token:
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            completed = await conn.fetchrow(
                """UPDATE tve_enquiries SET questionnaire_completed_at=NOW(), status='questionnaire_completed', updated_at=NOW()
                   WHERE questionnaire_token_hash=$1 AND questionnaire_completed_at IS NULL RETURNING id""",
                token_hash,
            )
            if completed:
                await conn.execute(
                    """UPDATE tve_enquiry_followups SET status='cancelled', updated_at=NOW()
                       WHERE enquiry_id=$1 AND followup_type='questionnaire_reminder' AND status IN ('queued','waiting_for_invitation')""",
                    completed["id"],
                )
        return {"id": row["id"], "stored": True}
    finally:
        await conn.close()
