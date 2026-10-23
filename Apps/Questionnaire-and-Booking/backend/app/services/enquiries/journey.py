"""Durable purpose-scoped links and the contact journey, container-backed."""
from datetime import datetime

import httpx
from fastapi import HTTPException

from ..shared.config import BOOKING_URL, fixture_properties, is_sandbox, journey_url
from ..shared import tokens_store
from ..shared.contact_write_guard import assert_writable
from ..shared.contacts import contact as contact_row, advance_status
from ..shared.responses_store import save_responses, response as questionnaire_response


async def issue_token(sender_name: str, sender_email: str, purpose: str) -> str:
    """Issue (or reuse) a token for the sender, stored in the tokens container."""
    from ..shared.contacts import upsert_contact
    contact_id = await upsert_contact(sender_name or "", "", sender_email, None)
    if not contact_id:
        raise HTTPException(409, detail="Enquiry has no contact email")
    return await tokens_store.issue_token(contact_id, purpose)


async def verify_token(conn, token: str, purpose: str) -> dict:
    """Resolve a token: tokens → contacts, then attach the contact's journey.

    ``conn`` is accepted for backwards compatibility; the lookup spans
    separate containers, so it is not used.
    """
    row = await tokens_store.verify_token(token, purpose)
    if not row:
        raise HTTPException(401, detail="Invalid or expired journey token")
    identity = await contact_row(row["contact_id"])
    if not identity:
        raise HTTPException(401, detail="Invalid or expired journey token")
    result = dict(identity)
    # Legacy key compatibility: callers read sender_name/sender_email.
    result["sender_name"] = ((result.get("first_name") or "") + " " + (result.get("last_name") or "")).strip()
    result["sender_email"] = result["email"]
    result["enquiry_id"] = row["contact_id"]
    result["token_id"] = row["id"]
    result["contact_id"] = row["contact_id"]
    result["expires_at"] = row["expires_at"]
    result["consumed_at"] = row["consumed_at"]
    return result


def validate_answers(answers: dict, consent: bool):
    if not consent:
        raise HTTPException(422, detail="Please confirm your questionnaire details are accurate")
    choices = {
        "move_timing": {"Within 1 week", "Within 2 weeks", "Within 3 weeks", "Within 4 weeks", "After a month"},
        "rental_duration": {"1 - 3 months", "3 - 6 months", "6 months to 1 year", "Over 1 year"},
        "income_proof": {"Yes", "No"}, "pets": {"No", "Yes - 1 pet", "Yes - multiple"},
        "smoke_vape": {"Yes", "No"}, "adverse_credit": {"No", "Yes, settled", "Yes, active"},
        "booking_opt_in": {"opt_in", "opt_out"},
    }
    for key, allowed in choices.items():
        if not isinstance(answers.get(key), str) or answers[key] not in allowed:
            raise HTTPException(422, detail=f"Invalid answer: {key}")
    for key, low, high in (("monthly_budget", 0, 10000), ("household_income", 0, 1000000), ("occupants", 1, 20)):
        try:
            value = float(answers.get(key))
        except (ValueError, TypeError):
            raise HTTPException(422, detail=f"Invalid answer: {key}")
        if isinstance(answers.get(key), bool) or not low <= value <= high or (key == "occupants" and not value.is_integer()):
            raise HTTPException(422, detail=f"Invalid answer: {key}")


async def submit_questionnaire(pool, token: str, answers: dict, consent: bool) -> dict:
    enquiry = await verify_token(None, token, "questionnaire")
    validate_answers(answers, consent)
    contact_id = enquiry["contact_id"]
    newly_completed = False
    identity: dict | None = None
    # Serialize retries on the contact even though answers/tokens use separate DBs.
    async with pool.acquire() as conn:
        async with conn.transaction():
            identity = dict(await conn.fetchrow("SELECT * FROM contacts WHERE id=$1 FOR UPDATE", contact_id))
            if not identity:
                raise HTTPException(401, detail="Invalid or expired journey token")
            await assert_writable(conn, contact_id)
            already = identity.get("status") not in (None, "enquiried", "Questionnaire_sent")
            invited_by_staff = identity.get("status") == "booking_link_sent"
            if invited_by_staff:
                already = await questionnaire_response(contact_id) is not None
            if not already:
                await save_responses(contact_id, answers, _contact_locked=True)
                await tokens_store.consume_token(enquiry["token_id"], _contact_locked=True)
                if not invited_by_staff:
                    await conn.execute("UPDATE contacts SET status='questionnaire_completed', updated_at=NOW() WHERE id=$1", contact_id)
                newly_completed = True

    # Score gate: computed outside the row lock (it may read the property
    # catalogue). Only a score strictly above the threshold unlocks a booking
    # link; a gated applicant is left at questionnaire_completed with no link.
    # issue_token is idempotent, so retries return the same link without a
    # second email. Everything after booking is unchanged.
    score = await booking_score(answers, identity or {})
    booking_unlocked = (score is not None and score > BOOKING_SCORE_THRESHOLD) or invited_by_staff
    booking_token = None
    if booking_unlocked:
        booking_token = await tokens_store.issue_token(contact_id, "booking")
        if newly_completed and not invited_by_staff:
            from .followup import send_journey_email
            await send_journey_email(identity, "booking_link", token=booking_token)
    return {"status": "ok", "submissionId": contact_id, "enquiryId": contact_id,
            "bookingUnlocked": booking_unlocked,
            "bookingUrl": (journey_url("booking") + "/" + booking_token) if booking_unlocked else None,
            "localMock": is_sandbox()}


# Score from the questionnaire plus the linked property's rent. A booking link
# is only sent when this is strictly greater than BOOKING_SCORE_THRESHOLD.
BOOKING_SCORE_THRESHOLD = 80


async def booking_score(answers: dict, identity: dict) -> int | None:
    """The questionnaire score exactly as the dashboard computes it (0..100)."""
    from ..dashboard import scoring
    property_id = identity.get("acquaint_property_id")
    rent = None
    if property_id is not None:
        row = catalogue_property(property_id, await property_catalogue())
        rent = scoring.monthly_rent(row)
    return scoring.compute_score(answers or None, rent).get("value")


async def property_for(enquiry: dict) -> dict:
    """Resolve catalogue details while keeping the contact's provider property ID."""
    from ..booking.properties import normalise_property
    property_id = enquiry.get("acquaint_property_id")
    raw = catalogue_property(property_id, await property_catalogue())
    if not raw:
        raise HTTPException(409, detail="Linked property is not available for viewing")
    result = normalise_property(raw)
    result["id"] = property_id
    return result


async def property_catalogue() -> list[dict]:
    """The selectable property catalogue: live reads where available, fixtures otherwise."""
    from ..booking.properties import is_selectable_property
    from ..shared.acquaint import client
    raw_properties = None
    if is_sandbox():
        try:
            raw_properties = await client.list_properties()
        except httpx.HTTPStatusError:
            from ...main import record_event
            record_event("WARNING", "backend", "integration_failure", "Integration request failed.", integration="acquaint", operation="list_properties", error_code="upstream_http_error")
            raw_properties = fixture_properties()
        except httpx.HTTPError:
            from ...main import record_event
            record_event("WARNING", "backend", "integration_failure", "Integration request failed.", integration="acquaint", operation="list_properties", error_code="upstream_unavailable")
            raw_properties = fixture_properties()
    else:
        raw_properties = await client.list_properties()
    return [p for p in raw_properties if isinstance(p, dict) and is_selectable_property(p)]


def catalogue_property(property_id, catalogue: list[dict]) -> dict | None:
    """The catalogue entry for a linked property id (fixture id or sourcePropertyId)."""
    if property_id is None:
        return None
    return next((p for p in catalogue
                 if str(p.get("id")) == str(property_id)
                 or (is_sandbox() and str(p.get("sourcePropertyId")) == str(property_id))), None)


async def store_viewing(pool, token: str, start, supplied_property: int | None) -> dict:
    from ..booking.availability import build_slots, VIEWING_MINUTES
    from ..shared.contacts import contact
    from datetime import timedelta
    enquiry = await verify_token(None, token, "booking")
    # A verified booking-purpose token is the authorization: automatic issuance
    # applies the score gate, while staff may explicitly override it.
    contact_id = enquiry["contact_id"]
    if supplied_property is not None and supplied_property != enquiry.get("acquaint_property_id"):
        raise HTTPException(403, detail="Property does not match the booking token")
    identity = await contact(contact_id)
    if identity and identity.get("viewing_slot_start"):
        if identity["viewing_slot_start"] != start:
            raise HTTPException(409, detail={"status": "error", "reason": "already-requested",
                                             "message": "You have already requested a viewing for this property.",
                                             "booked": contact_viewing_response(identity)})
        return contact_viewing_response(identity)
    property_id = enquiry.get("acquaint_property_id")
    if not property_id:
        raise HTTPException(403, detail="Property does not match the booking token")
    await property_for(enquiry)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", int(property_id))
            saved = await conn.fetchrow("SELECT * FROM contacts WHERE id=$1 FOR UPDATE", contact_id)
            if not saved or saved["acquaint_property_id"] != property_id:
                raise HTTPException(403, detail="Property does not match the booking token")
            await assert_writable(conn, contact_id)
            if saved["viewing_slot_start"]:
                if saved["viewing_slot_start"] != start:
                    raise HTTPException(409, detail={"reason": "already-requested", "message": "You have already requested a viewing.", "booked": contact_viewing_response(saved)})
                return contact_viewing_response(saved)
            offered = await build_slots(property_id)
            if not any(datetime.fromisoformat(s["start"]) == start for s in offered):
                raise HTTPException(409, detail="That slot is no longer available")
            await conn.execute("UPDATE contacts SET acquaint_property_id=$2, viewing_slot_start=$3, viewing_slot_end=$4, status='viewing_requested', updated_at=NOW() WHERE id=$1",
                               contact_id, property_id, start, start + timedelta(minutes=VIEWING_MINUTES))
    await tokens_store.consume_token(enquiry["token_id"])
    await advance_status(contact_id, "viewing_requested")
    refreshed = await contact(contact_id)
    from .followup import send_journey_email
    await send_journey_email(refreshed, "viewing_acknowledgement")
    return contact_viewing_response(refreshed)


def contact_viewing_response(row) -> dict:
    """Acknowledgement built from the contact's viewing columns."""
    slot = {"start": row["viewing_slot_start"].isoformat(), "end": row["viewing_slot_end"].isoformat()}
    sandbox = is_sandbox()
    return {"status": "acknowledged", "requestId": row["id"], "slot": slot,
            "slotStart": slot["start"], "slotEnd": slot["end"],
            "localMock": sandbox, "message": (
                "Your simulated viewing request is saved. No real appointment has been created."
                if sandbox else
                "Your viewing request is saved. Your appointment is not confirmed yet; our team will contact you with an update.")}


async def contact_viewing(contact_id: int) -> dict | None:
    """The contact's saved viewing from the contacts container, if any."""
    row = await contact_row(contact_id)
    if row and row.get("viewing_slot_start"):
        return contact_viewing_response(row)
    return None
