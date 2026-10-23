"""Questionnaire submit - port of the n8n B. Detect Questionnaire submissions webhooks.

Two endpoints (from the workflow):
  POST /webhook/thames-valley-estates/questionnaire-submit
  POST /webhook/thames-valley-estates/questionnaire-submit-guest
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...services.questionnaire.tokens import verified_guest_token
from ...services.shared.database import get_pool
from ...services.enquiries.journey import submit_questionnaire

router = APIRouter()

class QuestionnaireBody(BaseModel):
    class Config:
        extra = "allow"

    token: str | None = None
    answers: dict | None = None
    consent: bool = False


@router.post("/webhook/thames-valley-estates/questionnaire-submit")
async def questionnaire_submit(body: QuestionnaireBody):
    """Authenticated (headerAuth) questionnaire submission."""
    # Store the submission. The n8n flow wrote rows into TVE data tables —
    # in 2.0 submissions land in Postgres via services/store.py.
    if body.token is not None:
        guest = await verified_guest_token(body.token)
        if guest is None:
            return await submit_questionnaire(await get_pool("contacts"), body.token, body.answers or {}, body.consent)
        from ...services.questionnaire.store import save_submission
        saved = await save_submission(source="guest", token=body.token, answers=body.answers or {}, contact_email=guest["e"])
        return {"status": "ok", "submissionId": saved.get("id"), "contactEmail": guest["e"]}
    from ...services.questionnaire.store import save_submission
    saved = await save_submission(source="submit", token=body.token, answers=body.answers or {})
    return {"status": "ok", "submissionId": saved.get("id")}


@router.post("/webhook/thames-valley-estates/questionnaire-submit-guest")
async def questionnaire_submit_guest(body: QuestionnaireBody):
    """Guest variant - validates the signed token instead of contact identity."""
    if not body.token:
        raise HTTPException(status_code=422, detail={"status": "error", "reason": "missing-token"})
    payload = await verified_guest_token(body.token)
    if not payload:
        raise HTTPException(status_code=401, detail={"status": "error", "reason": "bad-token"})
    from ...services.questionnaire.store import save_submission
    saved = await save_submission(source="guest", token=body.token, answers=body.answers or {}, contact_email=payload.get("e"))
    return {"status": "ok", "submissionId": saved.get("id"), "contactEmail": payload.get("e")}
