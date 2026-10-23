from datetime import date, datetime

import pytest

import app.services.enquiries.followup as followup
from app.services.booking.lead_email import build_lead_email, emit_lead_email, send_lead_email
from app.services.booking.tenant_email import (build_tenant_email, emit_tenant_email,
                                               resolve_tenants, send_tenant_email)


class FixtureClient:
    def __init__(self, tenancies, contacts):
        self.tenancies, self.contacts = tenancies, contacts

    async def list_tenancies(self, property_id):
        assert property_id == 42
        return self.tenancies

    async def get_contact(self, contact_id):
        return self.contacts[contact_id]


@pytest.mark.asyncio
async def test_only_current_typed_tenant_not_applicant_or_landlord():
    client = FixtureClient([
        {"propertyId": 42, "contactId": 1, "startDate": "2020-01-01"},
        {"propertyId": 42, "contactId": 9, "startDate": "2020-01-01"},
        {"property": {"id": 42}, "contactId": 10, "startDate": "2020-01-01"},
    ], {1: {"typeId": 1}, 9: {"typeId": 9}, 10: {"typeId": 10, "email": "tenant@example.test"}})
    result = await resolve_tenants(client, 42, date(2025, 1, 1))
    assert result == {"status": "resolved", "tenants": [{"id": 10, "email": "tenant@example.test"}], "count": 1}


@pytest.mark.asyncio
async def test_end_date_only_rule_includes_missing_end_and_multiple_tenants():
    client = FixtureClient([
        {"propertyId": 42, "contactId": 10}, {"propertyId": 42, "contactId": 11, "startDate": "2020-01-01"},
        {"propertyId": 42, "contactId": 12, "startDate": "2020-01-01"},
    ], {10: {"typeId": 10}, 11: {"typeId": 10}, 12: {"typeId": 10}})
    result = await resolve_tenants(client, 42, date(2025, 1, 1))
    assert result == {"status": "resolved", "tenants": [{"id": 10, "email": None}, {"id": 11, "email": None}, {"id": 12, "email": None}], "count": 3}


@pytest.mark.asyncio
async def test_tenant_end_date_cutoff_inclusive_future_start_and_deduplicated():
    viewing_day = date(2025, 1, 1)
    client = FixtureClient([
        {"propertyId": 42, "contactId": 1, "endDate": "2024-12-31"},
        {"propertyId": 42, "contactId": 2, "endDate": "2025-01-01"},
        {"propertyId": 42, "contactId": 3, "endDate": "2025-02-01"},
        {"propertyId": 42, "contactId": 4, "endDate": None},
        {"propertyId": 42, "contactId": 5, "startDate": "2030-01-01", "endDate": "2031-01-01"},
        {"propertyId": 42, "contactId": 2, "endDate": "2025-01-01"},
        {"propertyId": 99, "contactId": 6},
        {"contactId": 7},
    ], {i: {"typeId": 10, "email": f"{i}@example.test"} for i in range(1, 8)})
    result = await resolve_tenants(client, 42, viewing_day)
    assert [tenant["id"] for tenant in result["tenants"]] == [2, 3, 4, 5]
    assert result["count"] == 4


@pytest.mark.asyncio
async def test_tenants_deduplicated_across_linked_contacts_and_records():
    client = FixtureClient([
        {"propertyId": 42, "contactId": 10, "linkedTenants": [{"contactId": 11}, 10], "startDate": "2020-01-01"},
        {"propertyId": 42, "contactId": 11, "startDate": "2020-01-01"},
    ], {10: {"typeId": 10, "email": "a@example.test"}, 11: {"typeId": 10, "email": "b@example.test"}})
    result = await resolve_tenants(client, 42, date(2025, 1, 1))
    assert [tenant["id"] for tenant in result["tenants"]] == [10, 11]
    assert result["count"] == 2


def test_tenant_email_is_fully_rendered_without_recipient_data():
    email = build_tenant_email(property_address="8 Sample Road, Reading, RG1 2AB",
                               slot_start=datetime(2030, 1, 2, 10, 30))
    assert email == {
        "subject": "Viewing request for 8 Sample Road, Reading, RG1 2AB",
        "body": "Hello,\n\nWe have received a viewing request for 8 Sample Road, Reading, RG1 2AB on Wednesday, 2 January 2030 at 10:30 AM. Would this proposed date and time be convenient for you? Please reply to let us know.\n\nPlease note that the appointment is not confirmed until Thames Valley Estates has reconfirmed the arrangements with you.\n\nKind regards,\nThames Valley Estates",
    }


@pytest.mark.asyncio
async def test_99_account_wide_records_excluded_by_explicit_property_and_deduplicated():
    records = [{"propertyId": 100 + i, "contactId": 500 + i, "startDate": "2020-01-01"} for i in range(99)]
    records += [
        {"propertyId": "42", "contactId": 701, "linkedTenants": [{"contactId": 702}, 701], "startDate": "2020-01-01"},
        {"property": {"id": 42}, "contactId": 702, "startDate": "2020-01-01"},
        {"contactId": 703, "startDate": "2020-01-01"},
        {"propertyId": None, "contactId": 704, "startDate": "2020-01-01"},
        {"property": {}, "contactId": 705, "startDate": "2020-01-01"},
    ]
    contacts = {i: {"typeId": 10, "email": f"{i}@example.test"} for i in range(500, 599)}
    contacts.update({701: {"typeId": 10}, 702: {"typeId": 10}, 703: {"typeId": 10}, 704: {"typeId": 10}, 705: {"typeId": 10}})
    client = FixtureClient(records, contacts)
    result = await resolve_tenants(client, 42, date(2025, 1, 1))
    assert result == {"status": "resolved", "tenants": [{"id": 701, "email": None}, {"id": 702, "email": None}], "count": 2}
    client.tenancies = records[:99] + records[-5:]
    result = await resolve_tenants(client, 42, date(2025, 1, 1))
    assert result == {"status": "resolved", "tenants": [{"id": 701, "email": None}, {"id": 702, "email": None}], "count": 2}


def test_lead_email_is_distinct_and_says_request_not_confirmation():
    lead = build_lead_email(property_address="8 Sample Road", slot_start=datetime(2030, 1, 2, 10, 30))
    tenant = build_tenant_email(property_address="8 Sample Road", slot_start=datetime(2030, 1, 2, 10, 30))
    assert lead != tenant
    assert "received your request" in lead["body"]
    assert "agency confirms" in lead["body"]
    assert "update you" in lead["body"]


def test_recipient_modules_own_distinct_simulated_email_events():
    events = []
    def record_event(*args, **kwargs):
        events.append((args, kwargs))

    slot = datetime(2030, 1, 2, 10, 30)
    emit_lead_email(record_event=record_event, contact_id=70, property_id=42, request_id="request",
                    property_address="8 Sample Road", slot_start=slot)
    emit_tenant_email(record_event=record_event, tenant_id=10, property_id=42, request_id="request",
                      property_address="8 Sample Road", slot_start=slot)

    lead_fields, tenant_fields = (event[1] for event in events)
    assert lead_fields["operation"] == "lead_email_prepared"
    assert lead_fields["recipient_role"] == "lead" and lead_fields["contact_id"] == 70
    assert tenant_fields["operation"] == "tenant_email_prepared"
    assert tenant_fields["recipient_role"] == "tenant" and tenant_fields["contact_id"] == 10
    assert "subject" not in lead_fields and "body" not in lead_fields
    assert "subject" not in tenant_fields and "body" not in tenant_fields
    assert all(fields["simulation_status"] == "simulated" for fields in (lead_fields, tenant_fields))
    assert all("email" not in fields and "name" not in fields for fields in (lead_fields, tenant_fields))


# --- Real delivery through the shared SMTP adapter (captured, never sent) ---

def _capture_transport(monkeypatch):
    sent = []

    async def fake_config():
        return {"host": "mock-inbox", "port": 1025, "sender": "lettings@sandbox.invalid"}

    def fake_send(config, message):
        sent.append((config, message))

    monkeypatch.setattr(followup, "mail_configuration", fake_config)
    monkeypatch.setattr(followup, "_smtp_send", fake_send)
    return sent


@pytest.mark.asyncio
async def test_send_tenant_email_delivers_non_pii_body(monkeypatch):
    sent = _capture_transport(monkeypatch)
    events = []
    ok = await send_tenant_email(
        to="tenant@example.test", record_event=lambda *args, **kwargs: events.append(kwargs),
        tenant_id=10, property_id=42, request_id="req",
        property_address="8 Sample Road, Reading, RG1 2AB",
        slot_start=datetime(2030, 1, 2, 10, 30))
    assert ok is True
    assert len(sent) == 1
    config, message = sent[0]
    assert config["host"] == "mock-inbox"
    assert message["To"] == "tenant@example.test"
    assert message["From"] == "Thames Valley Estates <lettings@sandbox.invalid>"
    body = message.get_content()
    assert "8 Sample Road, Reading, RG1 2AB" in body
    assert "Wednesday, 2 January 2030 at 10:30 AM" in body
    assert "not confirmed" in body
    assert events == []  # a delivered email is not a skip


@pytest.mark.asyncio
async def test_send_tenant_email_skips_when_no_address(monkeypatch):
    sent = _capture_transport(monkeypatch)
    events = []
    ok = await send_tenant_email(
        to=None, record_event=lambda *args, **kwargs: events.append(kwargs),
        tenant_id=11, property_id=42, request_id="req",
        property_address="8 Sample Road", slot_start=datetime(2030, 1, 2, 10, 30))
    assert ok is False
    assert sent == []
    assert events and events[0]["operation"] == "tenant_email_skipped_no_address"
    assert events[0]["contact_id"] == 11


@pytest.mark.asyncio
async def test_send_lead_email_delivers(monkeypatch):
    sent = _capture_transport(monkeypatch)
    ok = await send_lead_email(to="lead@example.test", property_address="8 Sample Road",
                               slot_start=datetime(2030, 1, 2, 10, 30))
    assert ok is True
    assert len(sent) == 1
    assert sent[0][1]["To"] == "lead@example.test"


@pytest.mark.asyncio
async def test_deliver_booking_email_is_best_effort(monkeypatch):
    async def fake_config():
        return {"host": "mock-inbox", "port": 1025, "sender": "lettings@sandbox.invalid"}

    def boom(config, message):
        raise OSError("smtp down")

    monkeypatch.setattr(followup, "mail_configuration", fake_config)
    monkeypatch.setattr(followup, "_smtp_send", boom)
    assert await followup.deliver_booking_email(to="lead@example.test", subject="S", body="B") is False

    # An empty recipient never opens a transport.
    monkeypatch.setattr(followup, "_smtp_send", lambda *args, **kwargs: pytest.fail("no transport"))
    assert await followup.deliver_booking_email(to="", subject="S", body="B") is False


@pytest.mark.asyncio
async def test_booking_email_uses_sandbox_mailpit(monkeypatch):
    import smtplib
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.setenv("sandbox", "TRUE")
    calls = []

    class SMTP:
        def __init__(self, host, port, **kwargs):
            calls.append((host, port))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def send_message(self, message):
            calls.append(("sent", message["To"]))

    async def no_sender(key):
        return None
    monkeypatch.setattr(followup.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(followup.smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("real TLS"))
    monkeypatch.setattr(followup.settings_store, "get", no_sender)
    assert await followup.deliver_booking_email(to="lead@example.test", subject="S", body="B") is True
    assert calls == [("mock-inbox", 1025), ("sent", "lead@example.test")]
