"""Tenant resolution and tenant-facing booking email delivery."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from ..enquiries import followup

TENANT_TYPE_ID = 10


def _id(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def tenancy_contains(tenancy: dict, on_date: date) -> bool:
    """Eligibility is the inclusive end-date cutoff; a missing end is allowed.

    Start dates are deliberately ignored, including future start dates.
    """
    end = _as_date(tenancy.get("endDate") or tenancy.get("toDate") or tenancy.get("tenancyEndDate"))
    return end is None or on_date <= end


def _associated_property_id(tenancy: dict) -> int | None:
    for path in (("propertyId",), ("propertyID",), ("property", "id"),
                 ("property", "propertyId"), ("property", "propertyID")):
        value: Any = tenancy
        for part in path:
            value = value.get(part) if isinstance(value, dict) else None
        parsed = _id(value)
        if parsed is not None:
            return parsed
    return None


async def resolve_tenants(acquaint, property_id: int, on_date: date) -> dict:
    """Return distinct type-10 contacts from explicitly associated eligible tenancies."""
    tenancies = await acquaint.list_tenancies(property_id)
    candidates: set[int] = set()
    for tenancy in tenancies:
        if not isinstance(tenancy, dict) or _associated_property_id(tenancy) != property_id:
            continue
        if not tenancy_contains(tenancy, on_date):
            continue
        linked = tenancy.get("linkedTenants") or []
        if isinstance(linked, (str, int)):
            linked = [linked]
        ids = [_id(tenancy.get("contactId"))]
        for item in linked if isinstance(linked, list) else []:
            ids.append(_id(item.get("contactId") or item.get("id")) if isinstance(item, dict) else _id(item))
        candidates.update(contact_id for contact_id in ids if contact_id)

    tenants = []
    for contact_id in sorted(candidates):
        contact = await acquaint.get_contact(contact_id)
        if isinstance(contact, dict) and _id(contact.get("typeId")) == TENANT_TYPE_ID:
            tenants.append({"id": contact_id, "email": contact.get("email")})
    return {"status": "resolved", "tenants": tenants, "count": len(tenants)} if tenants else {
        "status": "undetermined", "tenants": [], "count": 0
    }


def build_tenant_email(*, property_address: str, slot_start: datetime) -> dict:
    address = property_address.strip() or "the selected property"
    when = slot_start.strftime("%A, %d %B %Y at %I:%M %p").replace(" 0", " ")
    return {
        "subject": f"Viewing request for {address}",
        "body": (f"Hello,\n\nWe have received a viewing request for {address} on {when}. "
                 "Would this proposed date and time be convenient for you? Please reply to let us know.\n\n"
                 "Please note that the appointment is not confirmed until Thames Valley Estates has reconfirmed "
                 "the arrangements with you.\n\nKind regards,\nThames Valley Estates"),
    }


async def send_tenant_email(*, to: str | None, record_event: Callable, tenant_id: int,
                            property_id: int, request_id: str | None,
                            property_address: str, slot_start: datetime) -> bool:
    """Deliver the non-PII tenant email to one tenant; best-effort.

    Returns False and records ``tenant_email_skipped_no_address`` when the
    tenant has no address, or when delivery fails.
    """
    if not (to or "").strip():
        record_event("WARNING", "backend", "email_simulated", "Tenant email skipped — no address",
                     integration="email", operation="tenant_email_skipped_no_address",
                     recipient_role="tenant", contact_id=tenant_id, contact_ids=[tenant_id],
                     property_id=property_id, request_id=request_id)
        return False
    rendered = build_tenant_email(property_address=property_address, slot_start=slot_start)
    return await followup.deliver_booking_email(to=to, subject=rendered["subject"], body=rendered["body"])


def emit_tenant_email(*, record_event: Callable, tenant_id: int, property_id: int,
                      request_id: str | None, property_address: str, slot_start: datetime) -> None:
    """Record one recipient-specific tenant email audit event, with no recipient PII."""
    record_event("INFO", "backend", "email_simulated", "Email prepared/simulated — not sent",
                 integration="email", operation="tenant_email_prepared", recipient_role="tenant",
                 contact_id=tenant_id, contact_ids=[tenant_id], tenant_count=1,
                 simulation_status="simulated",
                 property_id=property_id, request_id=request_id)
