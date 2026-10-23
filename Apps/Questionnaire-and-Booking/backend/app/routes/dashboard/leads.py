"""Lead list and detail read models for the authenticated dashboard."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from ...services.dashboard import deletion, readmodel
from ...services.shared.config import is_sandbox

router = APIRouter(prefix="/dashboard")


@router.get("/leads")
async def leads():
    """Up to 200 stitched leads, most recently updated first."""
    return {"leads": await readmodel.list_leads(readmodel.LEADS_LIMIT),
            "generated_at": datetime.now(timezone.utc).isoformat()}


@router.get("/leads/{contact_id}")
async def lead(contact_id: int):
    """One lead read model plus the raw enquiry email body."""
    detail = await readmodel.lead_detail(contact_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Unknown contact")
    return detail


@router.delete("/leads/{contact_id}")
async def delete_lead(contact_id: int, request: Request):
    """Delete one lead's journey rows; never infer a target mode from defaults."""
    sandbox = is_sandbox()  # pinned for the request by RequestAuditMiddleware
    mode = "sandbox" if sandbox else "production"
    if request.headers.get("X-TVE-Mode") != mode:
        raise HTTPException(status_code=409, detail={"reason": "mode_changed", "message": "Refresh the dashboard before deleting."})
    if contact_id <= 0:
        raise HTTPException(status_code=404, detail="Unknown contact")
    try:
        operation_id = UUID(request.headers.get("X-TVE-Delete-Operation", ""))
        contact_uid = UUID(request.headers.get("X-TVE-Delete-Contact-UID", ""))
        initiated_at = datetime.fromisoformat(request.headers.get("X-TVE-Delete-Initiated-At", ""))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Deletion operation reference required") from error
    try:
        await deletion.delete_lead(contact_id, sandbox=sandbox, operation_id=operation_id,
                                   contact_uid=contact_uid, initiated_at=initiated_at)
    except deletion.LeadNotFound as error:
        raise HTTPException(status_code=404, detail="Unknown contact") from error
    except deletion.LeadConflict as error:
        raise HTTPException(status_code=409, detail="Deletion operation conflicts with this contact") from error
    except Exception as error:
        # Independent DB commits may already have succeeded; retry with the
        # same ID. Never turn a partial failure into an apparent success.
        raise HTTPException(status_code=503, detail="Lead deletion incomplete; retry or investigate") from error
    return {"deleted": True, "id": contact_id}
