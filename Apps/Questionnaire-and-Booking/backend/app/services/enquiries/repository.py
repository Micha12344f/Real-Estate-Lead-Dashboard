"""Intake persistence against the contacts and enquiries containers.

Deduplication: the contact row is unique by email, so a repeated message
updates the same contact. Mailpit messages are deleted after processing.
"""
from __future__ import annotations

from ..shared.contacts import upsert_contact, link_property, UNMATCHED_STATUS
from ..shared.enquiries_store import upsert_enquiry
from ..shared.config import fixture_properties, is_sandbox


async def record_message(pool, *, mailbox_key: str, mailbox_name: str, uid_validity: int, uid: int,
                         parsed, property_match: dict | None, lookup_error: bool = False) -> int | None:
    """Persist one parsed message; returns the contact id or None for non-enquiries.

    Only a genuine lettings enquiry that uniquely matches a property is labelled
    ``enquiried`` (the journey start). A genuine lettings enquiry that does not
    match is recorded as ``unmatched_enquiry`` for review and is never invited.
    """
    if not parsed.is_enquiry:
        return None
    match = property_match or {"status": "unmatched", "property_id": None, "candidates": []}
    matched = not lookup_error and not parsed.needs_review and match.get("status") == "matched"
    contact_id = await upsert_contact(parsed.sender_name or "", "", parsed.sender_email or "",
                                      parsed.sender_phone,
                                      initial_status="enquiried" if matched else UNMATCHED_STATUS)
    if not contact_id:
        return None
    await upsert_enquiry(contact_id, parsed.body or None, dict(parsed.extracted_fields))
    if matched:
        property_id = match["property_id"]
        if is_sandbox():
            fixture = next((p for p in fixture_properties() if p["id"] == property_id), {})
            property_id = fixture.get("sourcePropertyId") or property_id
        await link_property(contact_id, property_id)
    return contact_id
