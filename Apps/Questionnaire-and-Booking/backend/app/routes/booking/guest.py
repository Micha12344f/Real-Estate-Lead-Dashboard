"""Guest booking - port of the n8n "Parse Guest Booking / Create Contact + Token" flow.

Contract (from n8n "Respond - Booking Guest Stored"):
  {"status":"stored","contactId":...,"token":...,"propertyId":...,"propertyAddress":...}
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...services.questionnaire.tokens import make_questionnaire_token
from ...services.shared import settings_store
from ...services.shared.tokens_store import signing_key

router = APIRouter()


class GuestIdentity(BaseModel):
    firstName: str | None = None
    lastName: str | None = None
    email: str | None = None
    phone: str | None = None


class GuestBody(BaseModel):
    class Config:
        extra = "forbid"

    identity: GuestIdentity | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    propertyId: int | None = None
    propertyAddress: str | None = None
    message: str | None = None


@router.post("/webhook/thames-valley-estates/booking-guest")
async def guest(body: GuestBody):
    email = body.email or (body.identity.email if body.identity else None)
    phone = body.phone or (body.identity.phone if body.identity else None)
    name = body.name or (" ".join(filter(None, [body.identity.firstName, body.identity.lastName])) if body.identity else "")
    if not email and not phone:
        raise HTTPException(status_code=422, detail={"reason": "missing_contact", "message": "Email or phone required."})

    # Guest identity persists in the contacts container; never sent to a CRM.
    from ...services.shared.contacts import upsert_contact
    contact_id = await upsert_contact(name or "", "", email or "", phone)

    token = make_questionnaire_token(
        email=email or "",
        name=name or "",
        property_id=body.propertyId or 0,
        key=await signing_key(),
    )
    return {
        "status": "local_mock",
        "contactId": contact_id,
        "token": token,
        "propertyId": body.propertyId,
        "propertyAddress": body.propertyAddress,
        "localMock": True,
        "message": "",
    }
