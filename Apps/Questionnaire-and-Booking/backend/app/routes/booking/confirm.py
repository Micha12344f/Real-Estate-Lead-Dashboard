"""Persist contact-bound viewing requests; retain legacy guest acknowledgements.

Contract (from n8n "Respond - Booking Confirmed"):
  { "status": "confirmed", "appointmentId": ..., "contactId": ... }
Failure contract ("Respond - Confirm Rejected"):
  { "status": "error", "message": ..., "reason": ... }
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...services.booking.availability import parse_appt_instant, viewing_slot_error
from ...services.booking.lead_email import emit_lead_email, send_lead_email
from ...services.booking.tenant_email import emit_tenant_email, resolve_tenants, send_tenant_email
from ...services.booking.properties import normalise_property
from ...services.shared import contacts as contacts_store
from ...services.shared.acquaint import client as acquaint
from ...services.shared.database import get_pool
from ...services.enquiries.journey import store_viewing
from ...services.questionnaire.tokens import verified_guest_token
router = APIRouter()

TLE_MIN_LEAD_HOURS = 2

SLOT_ERROR_MESSAGES = {
    "slot-in-past": "That slot has passed",
    "min-notice": "Bookings need at least 24 hours' notice",
    "sunday-blocked": "Bookings are unavailable on Sundays",
    "monday-morning-blocked": "Monday viewings start at midday",
}


class ConfirmBody(BaseModel):
    """Mirrors what the booking.html frontend posts to n8n booking-confirm."""

    class Config:
        extra = "allow"

    propertyId: int | None = None
    contactId: int | None = None
    token: str | None = None
    slotStart: str | None = None
    slot_start: str | None = None
    startTime: str | None = None
    date: str | None = None
    name: str | None = None
    email: str | None = None
    appointmentType: str | None = None
    notes: str | None = None


def _field(body: ConfirmBody, *names: str) -> str | None:
    extras = body.model_extra or {}
    for n in names:
        v = getattr(body, n, None)
        if v:
            return v
    for n in names:
        if n in extras:
            return str(extras[n])
    return None


async def _property_address(property_id: int) -> str:
    """The catalogue address for a property, or "" when unavailable."""
    try:
        for raw_property in await acquaint.list_properties():
            if isinstance(raw_property, dict) and str(raw_property.get("id")) == str(property_id):
                return normalise_property(raw_property)["address"]
    except Exception:
        pass
    return ""


async def _notify_tenants(*, record_event, property_id: int, start, request_id: str | None,
                          property_address: str) -> dict:
    """Resolve the property's current tenants and deliver each the non-PII email.

    Best-effort: a lookup or send failure never blocks the booking response.
    """
    try:
        tenancy = await resolve_tenants(acquaint, property_id, start.date())
        record_event("INFO", "backend", "tenant_resolution", "Tenant resolution completed.", operation=tenancy["status"],
                     contact_ids=[tenant["id"] for tenant in tenancy.get("tenants", [])], tenant_count=tenancy.get("count", 0),
                     property_id=property_id, request_id=request_id)
    except Exception as exc:
        record_event("ERROR", "backend", "integration_error", "Tenant lookup failed.", integration="acquaint", operation="tenant_lookup", exception_class=type(exc).__name__)
        tenancy = {"status": "undetermined", "tenants": [], "count": 0}

    tenants = tenancy.get("tenants", []) if tenancy["status"] == "resolved" else []
    for tenant in tenants:
        emit_tenant_email(record_event=record_event, tenant_id=tenant["id"], property_id=property_id,
                          request_id=request_id, property_address=property_address, slot_start=start)
        await send_tenant_email(to=tenant.get("email"), record_event=record_event, tenant_id=tenant["id"],
                                property_id=property_id, request_id=request_id,
                                property_address=property_address, slot_start=start)
    if not tenants:
        record_event("INFO", "backend", "email_simulated", "No verified tenant contact; email not prepared",
                     integration="email", operation="tenant_not_identified", tenant_count=0, simulation_status="no_targets",
                     property_id=property_id, request_id=request_id)
    return tenancy


@router.post("/webhook/thames-valley-estates/booking-confirm")
async def confirm(body: ConfirmBody, request: Request):
    """A viewing request needs staff confirmation; Acquaint remains read-only."""
    from ...main import record_event
    request_id = getattr(request.state, "request_id", None)
    # Validate the same local input shape; only allowlisted safe metadata enters the event service.
    start_s = _field(body, "slotStart", "slot_start", "startTime", "date")
    if not start_s:
        raise HTTPException(status_code=422, detail={"status": "error", "reason": "missing-slot", "message": "No slot supplied"})
    start = parse_appt_instant(start_s)
    if not start:
        raise HTTPException(status_code=422, detail={"status": "error", "reason": "bad-slot", "message": "Slot unparseable"})

    if body.token is not None:
        guest = await verified_guest_token(body.token)
        if guest is None:
            # Contact-bound request: store_viewing persists the viewing and sends
            # the applicant's acknowledgement; tenants are notified here too.
            result = await store_viewing(await get_pool("contacts"), body.token, start, body.propertyId)
            property_id = None
            try:
                contact_id = result.get("requestId") if isinstance(result, dict) else None
                contact = await contacts_store.contact(contact_id) if contact_id else None
                property_id = (contact or {}).get("acquaint_property_id")
            except Exception:
                property_id = None
            if property_id:
                property_address = await _property_address(int(property_id))
                await _notify_tenants(record_event=record_event, property_id=int(property_id),
                                      start=start, request_id=request_id, property_address=property_address)
            return result
        if body.propertyId is not None and body.propertyId != guest["p"]:
            raise HTTPException(403, detail="Property does not match token")
        body.propertyId = guest["p"]
        body.name, body.email = guest["n"], guest["e"]

    reason = viewing_slot_error(start)
    if reason:
        return {"status": "error", "reason": reason, "message": SLOT_ERROR_MESSAGES[reason]}

    property_id = body.propertyId or int(_field(body, "propertyId") or 0)
    if not property_id:
        raise HTTPException(status_code=422, detail={"status": "error", "reason": "missing-property", "message": "No property supplied"})

    property_address = await _property_address(property_id)
    if body.email:
        emit_lead_email(record_event=record_event, contact_id=body.contactId, property_id=property_id,
                        request_id=request_id, property_address=property_address, slot_start=start)
        await send_lead_email(to=body.email, property_address=property_address, slot_start=start)
    tenancy = await _notify_tenants(record_event=record_event, property_id=property_id, start=start,
                                    request_id=request_id, property_address=property_address)
    return {
        "status": "acknowledged",
        "slotStart": start.isoformat(),
        "localMock": True,
        "occupancyStatus": tenancy["status"],
        "message": "Thanks — we’ve received your viewing request. We’ll do our best to arrange it for your requested time, but it isn’t confirmed yet. We’ll contact you to let you know whether it’s available.",
    }
