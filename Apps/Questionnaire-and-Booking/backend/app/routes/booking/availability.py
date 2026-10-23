"""Booking availability - port of the n8n "Check Completed + Generate Slots" node.

Key logic preserved from the workflow JS:
- Acquaint returns NAIVE wall-clock times in Europe/London. Parse them as
  London wall-clock and convert to true UTC instants for overlap checks.
- Slots are generated as UTC instants aligned to London wall-clock times.
- Existing appointments block overlapping slots.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Query, HTTPException
from pydantic import BaseModel

from ...services.booking.availability import build_slots, parse_appt_instant
from ...services.shared.config import is_sandbox
from ...services.questionnaire.tokens import verified_guest_token
from ...services.enquiries.journey import verify_token, property_for, contact_viewing

router = APIRouter()

class SlotWindow(BaseModel):
    start: str  # ISO UTC
    end: str


@router.get("/webhook/thames-valley-estates/booking-availability")
async def availability(request: Request, propertyId: int | None = Query(None), contactId: int | None = Query(None), token: str | None = Query(None)):
    """GET availability — mirrors the n8n webhook contract."""
    # Signed guest links retain their own validation; stored booking-purpose
    # tokens authorize either a qualified applicant or an explicit staff invite.
    guest = await verified_guest_token(token) if token is not None else None
    if token is not None and guest is None:
        enquiry = await verify_token(None, token, "booking")
        # Booking tokens are issued after qualification or by a staff override.
        if propertyId is not None and propertyId != enquiry.get("acquaint_property_id"):
            raise HTTPException(403, detail="Property does not match token")
        booked = await contact_viewing(enquiry["contact_id"])
        property_id = enquiry.get("acquaint_property_id")
        if not property_id:
            raise HTTPException(409, detail="No property linked to this booking")
        property_data = await property_for(enquiry)
        return {"status": "ok", "slots": [] if booked else await build_slots(property_id), "property": property_data,
                "lead": {"name": enquiry.get("sender_name"), "email": enquiry.get("sender_email")}, "booked": booked, "localMock": is_sandbox()}
    if guest is not None:
        if propertyId is not None and propertyId != guest["p"]:
            raise HTTPException(403, detail="Property does not match token")
        propertyId = guest["p"] or None
    if propertyId is None:
        raise HTTPException(422, detail="Property ID required")
    slots = await build_slots(propertyId)
    return {"status": "ok", "slots": slots, "contactId": contactId, "property": {"id": propertyId}}
