"""Lead-facing booking email: rendering and best-effort delivery."""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from ..enquiries import followup


def build_lead_email(*, property_address: str, slot_start: datetime) -> dict:
    address = property_address.strip() or "the selected property"
    when = slot_start.strftime("%A, %d %B %Y at %I:%M %p").replace(" 0", " ")
    return {
        "subject": f"Viewing request received — {address}",
        "body": (f"Hello,\n\nWe have received your request to view {address} on {when}. "
                 "Your viewing is not confirmed until our agency confirms the arrangements. "
                 "We will update you as soon as we can.\n\nKind regards,\nThames Valley Estates"),
    }


async def send_lead_email(*, to: str | None, property_address: str, slot_start: datetime) -> bool:
    """Deliver the applicant's booking acknowledgement; best-effort.

    Returns False when there is no address or delivery fails.
    """
    if not (to or "").strip():
        return False
    rendered = build_lead_email(property_address=property_address, slot_start=slot_start)
    return await followup.deliver_booking_email(to=to, subject=rendered["subject"], body=rendered["body"])


def emit_lead_email(*, record_event: Callable, contact_id: int | None, property_id: int,
                    request_id: str | None, property_address: str, slot_start: datetime) -> None:
    """Record the simulated lead email without retaining submitted recipient PII."""
    rendered = build_lead_email(property_address=property_address, slot_start=slot_start)
    record_event("INFO", "backend", "email_simulated", "Lead email prepared/simulated — not sent",
                 integration="email", operation="lead_email_prepared", recipient_role="lead",
                 contact_id=contact_id, simulation_status="simulated",
                 property_id=property_id, request_id=request_id)
