"""Available-properties - port of the n8n "Normalise Properties" node.

Contract (from n8n "Respond - Available Properties"): passthrough of the
normalised property list.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from ...services.shared.acquaint import client as acquaint
from ...services.booking.properties import is_selectable_property, normalise_property

router = APIRouter()


@router.get("/webhook/thames-valley-estates/booking-available-properties")
async def available_properties(statusId: int | None = Query(None), status: int | None = Query(None), request: Request = None):
    status_id = statusId if statusId is not None else status
    try:
        props = await acquaint.list_properties(status_id=status_id)
    except httpx.HTTPStatusError as exc:
        from ...main import record_event
        record_event("WARNING", "backend", "integration_failure", "Integration request failed.", request_id=getattr(getattr(request, "state", None), "request_id", "unavailable"), integration="acquaint", operation="list_properties", error_code="upstream_http_error")
        message = "Acquaint rejected the properties request; check the Sandbox read-only API credentials and access."
        raise HTTPException(status_code=502, detail=message) from exc
    except httpx.HTTPError as exc:
        from ...main import record_event
        record_event("WARNING", "backend", "integration_failure", "Integration request failed.", request_id=getattr(getattr(request, "state", None), "request_id", "unavailable"), integration="acquaint", operation="list_properties", error_code="upstream_unavailable")
        raise HTTPException(status_code=502, detail="Acquaint properties service is unavailable.") from exc
    return {"status": "ok", "properties": [normalise_property(p) for p in props if isinstance(p, dict) and is_selectable_property(p)]}
