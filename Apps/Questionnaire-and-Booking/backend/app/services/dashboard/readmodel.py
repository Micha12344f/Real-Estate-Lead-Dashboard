"""Lead read models stitched across the four containers by contact_id."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..booking.properties import normalise_property
from ..enquiries.journey import catalogue_property, property_catalogue
from ..shared import contacts as contacts_store
from ..shared import enquiries_store, responses_store, tokens_store
from ..shared.config import BOOKING_URL, QUESTIONNAIRE_URL, journey_url
from ..shared.contacts import STAGES, clean_name_part
from . import scoring

LEADS_LIMIT = 200


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _age_days(created_at, now: datetime) -> float:
    if not isinstance(created_at, datetime):
        return 0.0
    return (now - _as_utc(created_at)).total_seconds() / 86400


def _stage_index(status) -> int | None:
    return STAGES.index(status) if status in STAGES else None


def _property_address(raw_property: dict | None) -> str | None:
    if not raw_property:
        return None
    return normalise_property(raw_property).get("address") or None


def _property_bedrooms(raw_property: dict | None):
    if not isinstance(raw_property, dict):
        return None
    bedrooms = raw_property.get("bedrooms")
    return None if isinstance(bedrooms, bool) else bedrooms


def _platform(enquiry_row: dict) -> str | None:
    platform = enquiry_row.get("platform")
    body = enquiry_row.get("raw_email_body")
    if platform == "email" and isinstance(body, str) and re.search(r"\bzoopla\b", body, re.IGNORECASE):
        return "zoopla"
    return platform


async def list_leads(limit: int | None = LEADS_LIMIT, *, now: datetime | None = None) -> list[dict]:
    """Stitched lead read models, most recently updated first."""
    now = _as_utc(now or datetime.now(timezone.utc))
    catalogue = await property_catalogue()
    contacts = await contacts_store.list_contacts(limit)
    return [await _stitch_lead(contact, now=now, catalogue=catalogue) for contact in contacts]


async def lead(contact_id: int, *, now: datetime | None = None) -> dict | None:
    """One stitched lead read model, or None when the contact is unknown."""
    contact = await contacts_store.contact(contact_id)
    if contact is None:
        return None
    now = _as_utc(now or datetime.now(timezone.utc))
    return await _stitch_lead(contact, now=now, catalogue=await property_catalogue())


async def lead_detail(contact_id: int, *, now: datetime | None = None) -> dict | None:
    """The lead read model plus the raw enquiry email body for the detail view."""
    model = await lead(contact_id, now=now)
    if model is None:
        return None
    row = await enquiries_store.enquiry(contact_id)
    model["raw_email_body"] = (row or {}).get("raw_email_body")
    return model


async def _stitch_lead(contact: dict, *, now: datetime, catalogue: list[dict]) -> dict:
    """Combine one contact with its enquiry, answers, tokens and property."""
    contact_id = contact["id"]
    enquiry_row = await enquiries_store.enquiry(contact_id)
    response_row = await responses_store.response(contact_id)
    token_rows = await tokens_store.tokens_for_contact(contact_id)
    status = contact.get("status")
    stage_index = _stage_index(status)
    property_id = contact.get("acquaint_property_id")
    property_row = catalogue_property(property_id, catalogue)
    rent = scoring.monthly_rent(property_row)

    enquiry = None
    if enquiry_row:
        enquiry = {
            "platform": _platform(enquiry_row),
            "property_reference": enquiry_row.get("property_reference"),
            "move_in_date": enquiry_row.get("move_in_date"),
            "duration": enquiry_row.get("duration"),
            "moving_with": enquiry_row.get("moving_with"),
            "pets": enquiry_row.get("pets"),
            "smokers": enquiry_row.get("smokers"),
            "employment": enquiry_row.get("employment"),
            "affordability": enquiry_row.get("affordability"),
            "adverse_credit": enquiry_row.get("adverse_credit"),
            "received_at": _iso(enquiry_row.get("created_at")),
        }
    questionnaire = None
    if response_row:
        questionnaire = {
            "move_timing": response_row.get("move_timing"),
            "monthly_budget": response_row.get("monthly_budget"),
            "rental_duration": response_row.get("rental_duration"),
            "household_income": response_row.get("household_income"),
            "income_proof": response_row.get("income_proof"),
            "occupants": response_row.get("occupants"),
            "pets": response_row.get("pets"),
            "smoke_vape": response_row.get("smoke_vape"),
            "adverse_credit": response_row.get("adverse_credit"),
            "booking_opt_in": response_row.get("booking_opt_in"),
            "completed_at": _iso(response_row.get("completed_at")),
        }

    tokens = {"questionnaire": None, "booking": None}
    links = {"questionnaire": None, "booking": None}
    for row in token_rows:
        purpose = row.get("purpose")
        if purpose not in tokens:
            continue
        tokens[purpose] = {"expires_at": _iso(row.get("expires_at")),
                           "consumed_at": _iso(row.get("consumed_at"))}
        expires_at = row.get("expires_at")
        if expires_at is not None and _as_utc(expires_at) > now:
            base = journey_url(purpose)
            links[purpose] = base + "/" + row["token"]

    questionnaire_token = next((row for row in token_rows if row.get("purpose") == "questionnaire"), None)
    created_at = contact.get("created_at")
    return {
        "id": contact_id,
        "contact_uid": str(contact.get("contact_uid")) if contact.get("contact_uid") is not None else None,
        "first_name": clean_name_part(contact.get("first_name")),
        "last_name": clean_name_part(contact.get("last_name")),
        "email": contact.get("email"),
        "phone": contact.get("phone"),
        "status": status,
        "stage_index": stage_index,
        "property_id": property_id,
        "catalogue_property_id": property_row.get("id") if property_row else None,
        "property_address": _property_address(property_row),
        "property_rent": rent,
        "property_bedrooms": _property_bedrooms(property_row),
        "created_at": _iso(created_at),
        "updated_at": _iso(contact.get("updated_at")),
        "viewing_slot_start": _iso(contact.get("viewing_slot_start")),
        "viewing_slot_end": _iso(contact.get("viewing_slot_end")),
        "enquiry": enquiry,
        "questionnaire": questionnaire,
        "tokens": tokens,
        "links": links,
        "score": scoring.compute_score(questionnaire, rent),
        "flags": scoring.compute_flags(
            status=status, stage_index=stage_index, created_at=created_at,
            updated_at=contact.get("updated_at"), has_property=property_id is not None,
            enquiry=enquiry_row, questionnaire=response_row, questionnaire_token=questionnaire_token,
            rent=rent, now=now),
        "age_days": _age_days(created_at, now),
    }
