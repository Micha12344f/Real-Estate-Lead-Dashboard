from datetime import datetime, timedelta
from email.message import EmailMessage
import importlib.util
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import app.main as main
from app.main import app
import app.routes.booking.confirm as confirm_route
import app.services.enquiries.followup as followup_service
from app.services.shared import contacts as contacts_store

VIEWER_PATH = Path(__file__).resolve().parents[2] / "Tools" / "live-log-viewer.py"
viewer_spec = importlib.util.spec_from_file_location("sandbox_live_log_viewer", VIEWER_PATH)
viewer_module = importlib.util.module_from_spec(viewer_spec)
viewer_spec.loader.exec_module(viewer_module)


client = TestClient(app)


def _capture_mail(monkeypatch):
    """Capture booking emails instead of opening any SMTP transport."""
    sent = []

    async def fake_config():
        return {"host": "mock-inbox", "port": 1025, "sender": "lettings@sandbox.invalid"}

    def fake_send(config, message):
        sent.append(message)

    monkeypatch.setattr(followup_service, "mail_configuration", fake_config)
    monkeypatch.setattr(followup_service, "_smtp_send", fake_send)
    return sent


def test_missing_slot_is_safe_422_with_request_id(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    response = client.post(
        "/webhook/thames-valley-estates/booking-confirm",
        json={"propertyId": 42},
        headers={"X-Request-ID": "12345678-1234-4234-8234-123456789abc"},
    )

    assert response.status_code == 422
    assert response.headers["x-request-id"] == "12345678-1234-4234-8234-123456789abc"
    assert response.json()["error"]["code"] == "missing-slot"
    assert response.json()["error"]["requestId"] == response.headers["x-request-id"]


def test_selected_slot_alias_returns_simulated_acknowledgement(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    response = client.post(
        "/webhook/thames-valley-estates/booking-confirm",
        json={"propertyId": 42, "slotStart": "2099-01-01T10:00:00Z"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "acknowledged"
    assert body["localMock"] is True
    assert "requested time" in body["message"]
    assert "appointmentId" not in body


def test_confirm_simulates_lead_and_tenant_without_preview_or_smtp(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    sent = _capture_mail(monkeypatch)
    async def no_properties():
        return []
    async def no_tenancies(property_id):
        return []
    async def no_contact(contact_id):
        raise AssertionError("unexpected contact lookup")
    monkeypatch.setattr(confirm_route.acquaint, "list_properties", no_properties)
    monkeypatch.setattr(confirm_route.acquaint, "list_tenancies", no_tenancies)
    monkeypatch.setattr(confirm_route.acquaint, "get_contact", no_contact)
    monkeypatch.setattr(confirm_route.acquaint, "list_tenancies", no_tenancies)
    response = client.post("/webhook/thames-valley-estates/booking-confirm", json={
        "propertyId": 42, "slotStart": "2099-01-01T10:00:00Z", "name": "Lead", "email": "lead@example.test"
    })
    assert response.status_code == 200
    payload = response.json()
    assert "emailPreview" not in payload
    assert "emailStatus" not in payload
    assert "isn’t confirmed yet" in payload["message"]
    email_event = next(event for event in reversed(main._events) if event["event"] == "email_simulated")
    assert email_event["operation"] == "tenant_not_identified"
    assert "lead@example.test" not in repr(email_event)


def test_confirm_resolved_tenant_event_and_public_payload_has_no_recipient(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    sent = _capture_mail(monkeypatch)
    async def tenant_list(property_id):
        return [{"propertyId": 42, "contactId": 10, "startDate": "2020-01-01"}]
    async def get_contact(contact_id):
        return {"typeId": 10, "email": "tenant@example.test"}
    monkeypatch.setattr(confirm_route.acquaint, "list_tenancies", tenant_list)
    monkeypatch.setattr(confirm_route.acquaint, "get_contact", get_contact)
    response = client.post("/webhook/thames-valley-estates/booking-confirm", json={
        "propertyId": 42, "slotStart": "2099-01-01T10:00:00Z", "email": "lead@example.test"
    })
    assert response.status_code == 200
    tenant_event = next(event for event in reversed(main._events) if event["event"] == "tenant_resolution")
    email_event = next(event for event in reversed(main._events) if event["event"] == "email_simulated")
    assert tenant_event["operation"] == "resolved"
    assert email_event["operation"] == "tenant_email_prepared"
    assert email_event["recipient_role"] == "tenant"
    assert email_event["contact_id"] == 10
    assert email_event["simulation_status"] == "simulated"
    assert "tenant@example.test" not in repr(email_event)


def test_confirm_logs_each_simulated_recipient_contact_id_and_null_lead_id(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    main._events.clear()
    sent = _capture_mail(monkeypatch)
    async def tenant_list(property_id):
        return [{"propertyId": 42, "contactId": 991, "startDate": "2020-01-01"},
                {"propertyId": 42, "contactId": 992, "startDate": "2020-01-01"}]
    async def get_contact(contact_id):
        return {"typeId": 10, "email": f"tenant{contact_id}@example.test"}
    async def properties():
        return []
    monkeypatch.setattr(confirm_route.acquaint, "list_tenancies", tenant_list)
    monkeypatch.setattr(confirm_route.acquaint, "get_contact", get_contact)
    monkeypatch.setattr(confirm_route.acquaint, "list_properties", properties)
    response = client.post("/webhook/thames-valley-estates/booking-confirm", json={
        "propertyId": 42, "slotStart": "2099-01-01T10:00:00Z", "email": "private@example.test"
    })
    assert response.status_code == 200
    events = [e for e in main._events if e["event"] == "email_simulated"]
    recipients = [e for e in events if e.get("recipient_role")]
    assert [(e["recipient_role"], e["contact_id"]) for e in recipients] == [
        ("lead", None), ("tenant", 991), ("tenant", 992)]
    assert all(e["simulation_status"] == "simulated" for e in recipients)
    assert "private@example.test" not in repr(recipients)
    assert all("subject" not in e and "body" not in e for e in recipients)
    # Exercise the actual backend feed record through the viewer's production allowlist.
    feed_recipients = [viewer_module.safe_backend_event(e) for e in recipients]
    assert [(e["recipient_role"], e["contact_id"]) for e in feed_recipients] == [
        ("lead", None), ("tenant", 991), ("tenant", 992)]
    assert all("private@example.test" not in repr(e) for e in feed_recipients)
    assert all(e["property_id"] == 42 and e["request_id"] for e in recipients)
    assert all("subject" not in e and "body" not in e for e in feed_recipients)
    debug_response = client.get("/debug/events")
    assert debug_response.status_code == 200
    debug_json = debug_response.text
    for private_value in ("private@example.test", "tenant991@example.test", "tenant992@example.test",
                          "Viewing request received", "8 Sample Road", "2099-01-01T10:00:00"):
        assert private_value not in debug_json


def test_shared_room_tenants_delivered_and_missing_email_skipped(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    main._events.clear()
    sent = _capture_mail(monkeypatch)
    async def two_tenants(property_id):
        return [{"propertyId": 42, "contactId": 10, "linkedTenants": [11], "startDate": "2020-01-01"},
                {"propertyId": 42, "contactId": 11, "startDate": "2020-01-01"}, {"propertyId": 42, "contactId": 12, "startDate": "2020-01-01"},
                {"propertyId": 42, "contactId": 13, "startDate": "2020-01-01"}]
    async def get_tenant(contact_id):
        types = {10: 10, 11: 10, 12: 1, 13: 9}
        return {"typeId": types[contact_id], "email": "tenant@example.test" if contact_id == 10 else None}
    monkeypatch.setattr(confirm_route.acquaint, "list_tenancies", two_tenants)
    monkeypatch.setattr(confirm_route.acquaint, "get_contact", get_tenant)
    async def properties():
        return [{"id": 42, "street": "8 Sample Road", "town": "Reading", "postcode": "RG1 2AB"}]
    monkeypatch.setattr(confirm_route.acquaint, "list_properties", properties)
    response = client.post("/webhook/thames-valley-estates/booking-confirm", json={
        "propertyId": 42, "slotStart": "2099-01-01T10:00:00Z", "email": "lead@example.test"
    })
    assert response.status_code == 200
    # Tenant 10 and the applicant are really delivered; tenant 11 has no address.
    assert sorted(m["To"] for m in sent) == ["lead@example.test", "tenant@example.test"]
    tenant_msg = next(m for m in sent if m["To"] == "tenant@example.test")
    assert "8 Sample Road" in tenant_msg.get_content()
    assert "lead@example.test" not in tenant_msg.get_content()
    assert next(e for e in reversed(main._events)
                if e.get("operation") == "tenant_email_skipped_no_address")["contact_id"] == 11
    resolution = next(e for e in reversed(main._events) if e["event"] == "tenant_resolution")
    email_events = [e for e in main._events if e["event"] == "email_simulated" and e.get("operation") == "tenant_email_prepared"]
    email_event = next(e for e in email_events if e["contact_ids"] == [10])
    assert resolution["operation"] == "resolved" and resolution["contact_ids"] == [10, 11]
    assert email_event["operation"] == "tenant_email_prepared"
    assert email_event["contact_ids"] == [10]
    assert email_event["tenant_count"] == 1 and email_event["simulation_status"] == "simulated"
    assert {tuple(e["contact_ids"]) for e in email_events} == {(10,), (11,)}
    assert "Email prepared/simulated — not sent" in email_event["message"]
    assert "subject" not in email_event and "body" not in email_event
    assert "tenant@example.test" not in repr(email_event)
    assert "lead@example.test" not in repr(email_event)
    assert "emailPreview" not in response.json()

    async def failed_tenancies(property_id):
        raise RuntimeError("private provider response must not be logged")
    monkeypatch.setattr(confirm_route.acquaint, "list_tenancies", failed_tenancies)
    response = client.post("/webhook/thames-valley-estates/booking-confirm", json={
        "propertyId": 42, "slotStart": "2099-01-01T10:00:00Z", "email": "lead@example.test"
    })
    assert response.status_code == 200
    assert next(e for e in reversed(main._events) if e["event"] == "integration_error")["operation"] == "tenant_lookup"
    assert next(e for e in reversed(main._events) if e["event"] == "email_simulated")["operation"] == "tenant_not_identified"
    assert "private provider response" not in repr(main._events)


def test_slot_within_min_notice_is_rejected(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    soon = datetime.now(ZoneInfo("Europe/London")) + timedelta(hours=12)
    response = client.post(
        "/webhook/thames-valley-estates/booking-confirm",
        json={"propertyId": 42, "slotStart": soon.replace(microsecond=0).isoformat()},
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "min-notice"


def test_sunday_slot_is_rejected(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    today = datetime.now(ZoneInfo("Europe/London")).date()
    days_until_sunday = (6 - today.weekday()) % 7
    if days_until_sunday < 2:
        days_until_sunday += 7
    sunday = today + timedelta(days=days_until_sunday)
    response = client.post(
        "/webhook/thames-valley-estates/booking-confirm",
        json={"propertyId": 42, "slotStart": f"{sunday.isoformat()}T10:00:00"},
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "sunday-blocked"


def test_monday_morning_slot_is_rejected(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    response = client.post(
        "/webhook/thames-valley-estates/booking-confirm",
        json={"propertyId": 42, "slotStart": "2099-01-05T10:00:00"},
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "monday-morning-blocked"


def test_monday_afternoon_slot_is_allowed(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    response = client.post(
        "/webhook/thames-valley-estates/booking-confirm",
        json={"propertyId": 42, "slotStart": "2099-01-05T14:00:00"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "acknowledged"


def _resolved_tenant_property(monkeypatch, *, address="8 Sample Road, Reading, RG1 2AB"):
    async def tenant_list(property_id):
        return [{"propertyId": 42, "contactId": 10, "startDate": "2020-01-01"}]

    async def get_contact(contact_id):
        return {"typeId": 10, "email": "tenant@example.test"}

    async def properties():
        return [{"id": 42, "street": "8 Sample Road", "town": "Reading", "postcode": "RG1 2AB"}]

    monkeypatch.setattr(confirm_route.acquaint, "list_tenancies", tenant_list)
    monkeypatch.setattr(confirm_route.acquaint, "get_contact", get_contact)
    monkeypatch.setattr(confirm_route.acquaint, "list_properties", properties)


def test_contact_bound_confirm_emails_tenant_and_applicant_once(monkeypatch):
    """The contact-bound path notifies the resolved tenant and keeps a single
    applicant acknowledgement (sent by store_viewing)."""
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    sent = _capture_mail(monkeypatch)

    async def not_guest(token):
        return None

    async def fake_store(pool, token, start, supplied_property):
        # store_viewing sends the applicant acknowledgement in production.
        applicant = EmailMessage()
        applicant["To"] = "amara@example.invalid"
        applicant["Subject"] = "We’ve received your viewing request"
        sent.append(applicant)
        return {"status": "acknowledged", "requestId": 7, "localMock": False,
                "slot": {"start": start.isoformat(), "end": start.isoformat()}}

    async def fake_contact(contact_id):
        return {"id": 7, "acquaint_property_id": 42}

    async def pool(name="contacts"):
        return object()

    monkeypatch.setattr(confirm_route, "verified_guest_token", not_guest)
    monkeypatch.setattr(confirm_route, "store_viewing", fake_store)
    monkeypatch.setattr(confirm_route, "get_pool", pool)
    monkeypatch.setattr(contacts_store, "contact", fake_contact)
    _resolved_tenant_property(monkeypatch)

    response = client.post("/webhook/thames-valley-estates/booking-confirm",
                           json={"token": "a" * 43, "slotStart": "2099-01-01T10:00:00Z"})
    assert response.status_code == 200
    assert response.json()["status"] == "acknowledged"
    recipients = [m["To"] for m in sent]
    assert recipients.count("tenant@example.test") == 1
    assert recipients.count("amara@example.invalid") == 1
    assert len(sent) == 2
    tenant_msg = next(m for m in sent if m["To"] == "tenant@example.test")
    body = tenant_msg.get_content()
    assert "8 Sample Road" in body and "not confirmed" in body
    assert "amara@example.invalid" not in body


def test_confirm_without_tenants_emails_only_the_applicant(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    sent = _capture_mail(monkeypatch)
    async def no_tenancies(property_id):
        return []
    async def properties():
        return []
    monkeypatch.setattr(confirm_route.acquaint, "list_tenancies", no_tenancies)
    monkeypatch.setattr(confirm_route.acquaint, "list_properties", properties)
    response = client.post("/webhook/thames-valley-estates/booking-confirm", json={
        "propertyId": 42, "slotStart": "2099-01-01T10:00:00Z", "email": "lead@example.test"})
    assert response.status_code == 200
    assert [m["To"] for m in sent] == ["lead@example.test"]


def test_confirm_email_failure_does_not_break_response(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)

    async def fake_config():
        return {"host": "mock-inbox", "port": 1025, "sender": "lettings@sandbox.invalid"}

    def boom(config, message):
        raise OSError("smtp down")

    monkeypatch.setattr(followup_service, "mail_configuration", fake_config)
    monkeypatch.setattr(followup_service, "_smtp_send", boom)
    _resolved_tenant_property(monkeypatch)

    response = client.post("/webhook/thames-valley-estates/booking-confirm", json={
        "propertyId": 42, "slotStart": "2099-01-01T10:00:00Z", "email": "lead@example.test"})
    assert response.status_code == 200
    assert response.json()["status"] == "acknowledged"
    assert response.json()["occupancyStatus"] == "resolved"
