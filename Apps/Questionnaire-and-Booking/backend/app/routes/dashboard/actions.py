"""Dashboard actions over the stitched lead journey."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...services.dashboard import readmodel
from ...services.enquiries import followup
from ...services.enquiries.journey import catalogue_property, property_catalogue
from ...services.shared import acquaint
from ...services.shared import contacts as contacts_store
from ...services.shared import tokens_store
from ...services.shared.config import QUESTIONNAIRE_URL, fixture_properties, is_sandbox
from ...services.shared.config import journey_url

router = APIRouter(prefix="/dashboard")

# Last-resort "Viewing" appointment type id when Acquaint's appointment-types
# list is unavailable or has no viewing entry. Resolved dynamically by default.
VIEWING_APPOINTMENT_TYPE_FALLBACK_ID = 1
APPOINTMENT_TYPE_TIMEOUT_SECONDS = 5
APPOINTMENT_CREATE_TIMEOUT_SECONDS = 45
LEAD_REFRESH_TIMEOUT_SECONDS = 5
logger = logging.getLogger(__name__)


class AssignPropertyBody(BaseModel):
    class Config:
        extra = "forbid"

    contact_id: int
    property_id: int


class ResendInvitationBody(BaseModel):
    class Config:
        extra = "forbid"

    contact_id: int


class ViewingDecisionBody(BaseModel):
    class Config:
        extra = "forbid"

    contact_id: int
    decision: Literal["approve", "deny"]


async def _viewing_appointment_type_id() -> int:
    """Resolve the "Viewing" appointment type id from Acquaint's type list.

    Falls back to ``VIEWING_APPOINTMENT_TYPE_FALLBACK_ID`` when the list is
    unavailable or has no case-insensitive "viewing" description.
    """
    try:
        appointment_types = await asyncio.wait_for(
            acquaint.client.list_appointment_types(), APPOINTMENT_TYPE_TIMEOUT_SECONDS)
    except Exception:
        return VIEWING_APPOINTMENT_TYPE_FALLBACK_ID
    for item in appointment_types or []:
        if not isinstance(item, dict):
            continue
        if "viewing" in str(item.get("description", "")).lower():
            value = item.get("id")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return VIEWING_APPOINTMENT_TYPE_FALLBACK_ID


async def _appointment_payload(contact: dict) -> dict:
    """The Acquaint create-appointment body for a stored viewing request.

    Matches ``#/definitions/AppointmentWrite`` from the Acquaint Swagger 2.0
    spec used by the local Prism mock
    (``openapi/acquaint-spec.json``): only ``startDate``, ``endDate``,
    ``appointmentTypeId``, ``confirmed``, ``notes``, ``properties`` and
    ``attendees``. Live Viewing appointments require a CRM contact attendee;
    the local contact id must never be used as the CRM contact id.
    """
    email = contact.get("email")
    phone = contact.get("phone")
    return {
        "startDate": contact["viewing_slot_start"].isoformat(),
        "endDate": contact["viewing_slot_end"].isoformat(),
        "appointmentTypeId": await _viewing_appointment_type_id(),
        "confirmed": True,
        "notes": f"Approved from the dashboard. Lead email: {email or 'n/a'}; phone: {phone or 'n/a'}",
        "properties": [int(contact["acquaint_property_id"])],
        "attendees": [await _viewing_attendee_id(contact)],
    }


async def _viewing_attendee_id(contact: dict) -> int:
    return await asyncio.wait_for(acquaint.client.ensure_viewing_contact(contact), 30)


def _appointment_id(appointment) -> int | None:
    """The created appointment's id from the CRM response, or None."""
    if not isinstance(appointment, dict):
        return None
    for key in ("id", "appointmentId"):
        value = appointment.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


async def _refreshed_lead(contact_id: int) -> dict:
    refreshed = await readmodel.lead(contact_id)
    if refreshed is None:
        raise HTTPException(status_code=404, detail="Unknown contact")
    return refreshed


async def _decision_result(contact: dict, approved: bool) -> dict:
    """A successful decision must survive an unavailable catalogue refresh.

    The dashboard merges partial lead updates. If the stitched read (including
    Acquaint's property catalogue) fails, return the already committed outcome.
    """
    try:
        return await asyncio.wait_for(_refreshed_lead(contact["id"]), LEAD_REFRESH_TIMEOUT_SECONDS)
    except Exception as error:
        logger.warning("Viewing saved; lead refresh failed type=%s", type(error).__name__)
        status = "booked" if approved else "questionnaire_completed"
        def slot(key):
            value = contact.get(key) if approved else None
            return value.isoformat() if value is not None else None
        return {"id": contact["id"], "status": status,
                "stage_index": contacts_store.STAGES.index(status),
                "viewing_slot_start": slot("viewing_slot_start"),
                "viewing_slot_end": slot("viewing_slot_end"),
                "updated_at": datetime.now(timezone.utc).isoformat()}


@router.post("/actions/assign-property")
async def assign_property(body: AssignPropertyBody):
    """Manually link a property to an enquiry and start the journey.

    When the contact is an unmatched enquiry, link the property, move it from
    ``unmatched_enquiry`` to ``enquiried`` and send exactly one questionnaire
    invitation. A contact already on the journey is only (re-)linked.
    """
    contact = await contacts_store.contact(body.contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Unknown contact")
    if contact.get("viewing_slot_start"):
        raise HTTPException(status_code=422, detail="Contact already has a viewing")
    catalogue = await property_catalogue()
    if catalogue_property(body.property_id, catalogue) is None:
        raise HTTPException(status_code=404, detail="Property is not available for assignment")
    property_id = body.property_id
    if is_sandbox():
        # Fixtures carry the real Acquaint id to link against, exactly like intake.
        fixture = next((p for p in fixture_properties() if p["id"] == body.property_id), {})
        property_id = fixture.get("sourcePropertyId") or property_id

    was_unmatched = contact.get("status") == contacts_store.UNMATCHED_STATUS
    await contacts_store.link_property(body.contact_id, property_id)

    sent = False
    if was_unmatched:
        # Promote the off-ladder review enquiry into the journey, then send
        # exactly one invitation (a second call is a no-op once advanced).
        await contacts_store.promote_to_enquiried(body.contact_id)
        try:
            sent = await followup.send_questionnaire_invitation(body.contact_id)
        except Exception as error:
            raise HTTPException(status_code=502, detail="Invitation email could not be sent") from error

    return {**await _refreshed_lead(body.contact_id), "invitationSent": sent}


@router.post("/actions/resend-invitation")
async def resend_invitation(body: ResendInvitationBody):
    """Re-issue the questionnaire invitation with a fresh token expiry."""
    contact = await contacts_store.contact(body.contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Unknown contact")
    email = contact.get("email")
    if not email:
        raise HTTPException(status_code=422, detail="Contact has no email address")
    status = contact.get("status")
    if status not in ("enquiried", "Questionnaire_sent"):
        raise HTTPException(status_code=422, detail="Contact is not awaiting a questionnaire")
    token = await tokens_store.reissue_token(body.contact_id, "questionnaire")
    name = " ".join(part for part in (contact.get("first_name"), contact.get("last_name")) if part).strip()
    try:
        config = await followup.mail_configuration()
        message = followup._invitation_message(body.contact_id, name, email, token,
                                               journey_url("questionnaire"), config["sender"])
        await asyncio.to_thread(followup._smtp_send, config, message)
    except Exception as error:
        # Fail closed: no fallback adapter is ever attempted.
        raise HTTPException(status_code=502, detail="Invitation email could not be sent") from error
    if status == "enquiried":
        await contacts_store.advance_status(body.contact_id, "Questionnaire_sent")
    return {**await _refreshed_lead(body.contact_id), "sent": True}


@router.post("/actions/send-booking")
async def send_booking(body: ResendInvitationBody):
    """Staff override: issue a booking link regardless of score or questionnaire."""
    contact = await contacts_store.contact(body.contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Unknown contact")
    if not contact.get("email"):
        raise HTTPException(status_code=422, detail="Contact has no email address")
    if not contact.get("acquaint_property_id"):
        raise HTTPException(status_code=422, detail="Assign a property before sending a booking link")
    token = await tokens_store.reissue_token(body.contact_id, "booking")
    try:
        await followup.send_journey_email(contact, "manual_booking_link", token=token, raise_on_error=True)
    except Exception as error:
        raise HTTPException(status_code=502, detail="Booking email could not be sent") from error
    return {**await _refreshed_lead(body.contact_id), "sent": True}


@router.post("/actions/viewing-decision")
async def viewing_decision(body: ViewingDecisionBody):
    """Approve a pending viewing (creating the CRM appointment) or deny it.

    Only a still-pending request can be resolved. Approval is fail-closed: the
    lead is marked ``booked`` only after Acquaint returns an appointment, and a
    CRM error leaves the status untouched so the decision can be retried.
    """
    contact = await contacts_store.contact(body.contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Unknown contact")
    if contact.get("status") != "viewing_requested":
        raise HTTPException(status_code=422, detail="Contact does not have a pending viewing request")
    if body.decision == "approve":
        if (contact.get("acquaint_property_id") is None
                or contact.get("viewing_slot_start") is None
                or contact.get("viewing_slot_end") is None):
            raise HTTPException(status_code=422, detail="Contact has no viewing request to approve")
        try:
            payload = await _appointment_payload(contact)
            appointment = await asyncio.wait_for(
                acquaint.client.create_appointment(payload), APPOINTMENT_CREATE_TIMEOUT_SECONDS)
        except (TimeoutError, httpx.TimeoutException) as error:
            logger.warning("Viewing appointment result timed out")
            raise HTTPException(status_code=504, detail=(
                "Acquaint did not confirm the result in time. Check its appointments before retrying approval."
            )) from error
        except acquaint.AcquaintContactError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            logger.warning("Viewing appointment rejected upstream_status=%s", status)
            if status in (400, 422):
                raise HTTPException(status_code=422, detail=(
                    "Acquaint rejected the viewing details. Check the applicant contact, property and appointment settings."
                )) from error
            if status in (401, 403):
                raise HTTPException(status_code=422, detail=(
                    "Acquaint denied access. Check that the configured integration can create applicants and appointments."
                )) from error
            raise HTTPException(status_code=503, detail="Acquaint is temporarily unavailable. Check the viewing before retrying.") from error
        except Exception as error:
            # Fail closed: never advance the journey when the CRM write fails.
            logger.warning("Viewing appointment failed type=%s", type(error).__name__)
            raise HTTPException(status_code=502, detail="Viewing appointment could not be created") from error
        # The CRM must confirm the appointment before the lead is advanced.
        if _appointment_id(appointment) is None:
            raise HTTPException(status_code=502, detail="Viewing appointment could not be created")
    changed = await contacts_store.decide_viewing(body.contact_id, body.decision == "approve")
    if not changed:
        raise HTTPException(status_code=409, detail="Viewing request was already resolved")
    return await _decision_result(contact, body.decision == "approve")
