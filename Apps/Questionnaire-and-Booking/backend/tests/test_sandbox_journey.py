import hashlib
import json
from pathlib import Path
import uuid

import pytest
from fastapi import HTTPException

from app.services.shared import acquaint, config
from app.services.enquiries import followup, journey, simulator
from app.services.enquiries.parser import parse_email
from app.services.enquiries.matching import match_property


@pytest.mark.parametrize("scenario,review,matched", [
    ("rightmove", False, True), ("onthemarket", False, True), ("html", False, True),
    ("missing_email", True, True), ("unmatched", False, False),
])
def test_original_github_templates_drive_scenarios(scenario, review, matched):
    parsed = parse_email(simulator.scenario_message(scenario, 0, uuid.uuid4(), "<fixture@example.invalid>").as_bytes())
    match = match_property(parsed.property_query, parsed.property_reference, config.fixture_properties())
    assert parsed.is_enquiry
    assert parsed.needs_review == review
    assert (match["status"] == "matched") == matched


def test_fixture_provenance_checksums_match_original_downloads():
    root = Path(__file__).resolve().parents[2] / "Tools"
    manifest = json.loads((root / "fixture-manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["commit"]) == 40
    for entry in manifest["files"]:
        # Git's core.autocrlf may change the checkout bytes on Windows. The
        # manifest hashes the canonical upstream LF content, not host newlines.
        canonical = (root / entry["local"]).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(canonical).hexdigest() == entry["sha256"]


def test_e2e_questionnaire_fixtures_exercise_both_sides_of_real_score_gate(monkeypatch):
    from app.services.dashboard import scoring
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "Tools"))
    import run_sandbox_e2e as e2e

    properties = config.fixture_properties()
    emails = set()
    cases = [(e2e.QUALIFYING_FIXTURE_INDEX, e2e.QUALIFYING_ANSWERS, 100, 90),
             (e2e.LOW_SCORE_FIXTURE_INDEX, e2e.LOW_SCORE_ANSWERS, 31, 46)]
    with config.mode_scope(True):
        for index, answers, expected_score, unknown_rent_score in cases:
            parsed = parse_email(simulator.scenario_message("rightmove", index, uuid.UUID(int=0), "<e2e@example.invalid>").as_bytes())
            assert parsed.is_enquiry and not parsed.needs_review and parsed.sender_email
            emails.add(parsed.sender_email)
            match = match_property(parsed.property_query, parsed.property_reference, properties)
            assert match["status"] == "matched"
            matched = next(p for p in properties if p["id"] == match["property_id"])
            linked_id = matched.get("sourcePropertyId") or matched["id"]
            rent = scoring.monthly_rent(journey.catalogue_property(linked_id, properties))
            assert linked_id == 222 and rent == 2187
            journey.validate_answers(answers, True)
            assert scoring.compute_score(answers, rent)["value"] == expected_score
            assert scoring.compute_score(answers, None)["value"] == unknown_rent_score
            assert (expected_score > 80) == (unknown_rent_score > 80)
    assert len(emails) == 2, "The gated journey must use a separate, fresh contact"


def test_sandbox_selects_fixture_crm_even_with_live_configuration(monkeypatch):
    monkeypatch.setenv("sandbox", "TRUE")
    monkeypatch.setenv("TVE_ACQUAINT_MODE", "live-readonly")
    monkeypatch.setenv("TVE_ALLOW_LIVE_INTEGRATIONS", "false")
    monkeypatch.setenv("ACQUAINT_BASE", "https://real-provider.invalid")
    monkeypatch.setenv("ACQUAINT_API_KEY", "")
    client = acquaint.make_client()
    assert isinstance(client, acquaint.SandboxAcquaintClient)
    assert not hasattr(client, "api_key")


class _FakeLiveAppointments:
    def __init__(self):
        self.calls = []

    async def list_appointments(self, property_id):
        self.calls.append(property_id)
        return [{"startDate": "2099-01-01 10:00:00", "endDate": "2099-01-01 10:30:00"}]


@pytest.mark.asyncio
async def test_sandbox_maps_fixture_property_to_real_source_for_appointments(monkeypatch):
    monkeypatch.setenv("sandbox", "TRUE")
    live = _FakeLiveAppointments()
    client = acquaint.SandboxAcquaintClient(live_client=live)
    assert client.is_live
    appointments = await client.list_appointments(10001)
    assert live.calls == [242]
    assert appointments[0]["startDate"] == "2099-01-01 10:00:00"
    # A fixture with no real source property never reaches the provider.
    assert await client.list_appointments(10056) == []
    assert live.calls == [242]


class _FakeLiveProperties:
    def __init__(self, by_status):
        self.by_status = by_status
        self.calls = []

    async def list_properties(self, status_id):
        self.calls.append(status_id)
        return list(self.by_status.get(status_id, []))


@pytest.mark.asyncio
async def test_sandbox_with_live_client_serves_union_of_live_selectable_properties():
    live = _FakeLiveProperties({0: [{"id": 240, "statusId": 0}, {"id": 241, "statusId": 0}],
                                1: [{"id": 240, "statusId": 0}, {"id": 242, "statusId": 1}]})
    client = acquaint.SandboxAcquaintClient(live_client=live)
    assert [p["id"] for p in await client.list_properties()] == [240, 241, 242]
    assert live.calls == [0, 1]


@pytest.mark.asyncio
async def test_sandbox_with_live_client_passes_explicit_property_status_through():
    live = _FakeLiveProperties({2: [{"id": 249, "statusId": 2}]})
    client = acquaint.SandboxAcquaintClient(live_client=live)
    assert [p["id"] for p in await client.list_properties(2)] == [249]
    assert live.calls == [2]


@pytest.mark.asyncio
async def test_sandbox_live_property_errors_are_never_swallowed_or_cached():
    import httpx

    class DownLive:
        async def list_properties(self, status_id):
            raise httpx.ConnectError("provider down")

    client = acquaint.SandboxAcquaintClient(live_client=DownLive())
    with pytest.raises(httpx.ConnectError):
        await client.list_properties()

    live = _FakeLiveProperties({0: [{"id": 240, "statusId": 0}], 1: [{"id": 242, "statusId": 1}]})
    client._live = live
    assert len(await client.list_properties()) == 2
    assert len(await client.list_properties()) == 2
    assert live.calls == [0, 1]  # the union stays cached for the TTL
    client._PROPERTIES_CACHE_TTL = 0.0
    assert len(await client.list_properties()) == 2
    assert live.calls == [0, 1, 0, 1]  # expiry refetches; the failure was never cached


@pytest.mark.asyncio
async def test_sandbox_without_live_client_keeps_fixture_properties():
    client = acquaint.SandboxAcquaintClient()
    assert await client.list_properties() == config.fixture_properties()
    assert await client.list_properties(0) == []
    assert await client.list_properties(1) == [p for p in config.fixture_properties() if p["statusId"] == 1]


@pytest.mark.asyncio
async def test_live_property_reads_use_documented_parameters_and_follow_pagination(monkeypatch):
    import httpx
    monkeypatch.setenv("sandbox", "TRUE")
    monkeypatch.setenv("ACQUAINT_BASE", "https://crm.example.test")
    monkeypatch.setenv("ACQUAINT_API_KEY", "test-key")
    monkeypatch.setenv("ACQUAINT_SITE_PREFIX", "TVES")
    calls = []

    def handle(request):
        if request.url.path == "/v1/auth":
            return httpx.Response(200, json={"token": "test-bearer", "expiresIn": 3600})
        params = request.url.params
        calls.append(dict(params))
        page = int(params["requestPropertyList.currentPage"])
        assert int(params["requestPropertyList.pageSize"]) <= 1000
        pages = {
            1: {"pagination": {"currentPage": 1, "pageSize": 1000, "totalPages": 2, "totalRecords": 3},
                "data": [{"id": 240, "statusId": 0}, {"id": 241, "statusId": 0}]},
            2: {"pagination": {"currentPage": 2, "pageSize": 1000, "totalPages": 2, "totalRecords": 3},
                "data": [{"id": 242, "statusId": 0}]},
        }
        return httpx.Response(200, json=pages[page])

    client = acquaint.make_client(transport=httpx.MockTransport(handle))
    assert client.is_live
    assert [p["id"] for p in await client.list_properties(0)] == [240, 241, 242]
    assert [call["requestPropertyList.currentPage"] for call in calls] == ["1", "2"]
    assert {call["requestPropertyList.statusId"] for call in calls} == {"0"}


@pytest.mark.asyncio
async def test_sandbox_property_catalogue_reads_through_the_live_client(monkeypatch):
    monkeypatch.setenv("sandbox", "TRUE")

    class LiveCatalogue:
        async def list_properties(self, status_id=None):
            return [{"id": 240, "statusId": 0}, {"id": 242, "statusId": 1}, {"id": 250, "statusId": 2}]

    monkeypatch.setattr(acquaint, "client", LiveCatalogue())
    assert [p["id"] for p in await journey.property_catalogue()] == [240, 242]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connect", "http_status"], ids=["unavailable", "http_error"])
async def test_sandbox_property_catalogue_falls_back_to_fixtures_when_acquaint_is_down(monkeypatch, failure):
    import httpx
    import app.main as main
    from app.services.booking.properties import is_selectable_property
    monkeypatch.setenv("sandbox", "TRUE")

    class DownCatalogue:
        async def list_properties(self, status_id=None):
            if failure == "connect":
                raise httpx.ConnectError("provider down")
            request = httpx.Request("GET", "https://crm.example.test/v1/Properties/list")
            raise httpx.HTTPStatusError("Unauthorized", request=request, response=httpx.Response(401, request=request))

    monkeypatch.setattr(acquaint, "client", DownCatalogue())
    known_event_ids = {event["id"] for event in main._events}
    catalogue = await journey.property_catalogue()
    assert catalogue == [p for p in config.fixture_properties() if is_selectable_property(p)]
    recorded = [event for event in main._events if event["id"] not in known_event_ids]
    assert [event.get("error_code") for event in recorded] == [
        "upstream_unavailable" if failure == "connect" else "upstream_http_error"]


@pytest.mark.asyncio
async def test_sandbox_without_complete_credentials_uses_synthetic_calendar(monkeypatch):
    monkeypatch.setenv("sandbox", "TRUE")
    monkeypatch.setenv("TVE_ALLOW_LIVE_INTEGRATIONS", "false")
    monkeypatch.setenv("ACQUAINT_BASE", "https://real-provider.invalid")
    monkeypatch.setenv("ACQUAINT_API_KEY", "live-secret")
    monkeypatch.setenv("ACQUAINT_SITE_PREFIX", "")
    client = acquaint.make_client()
    assert isinstance(client, acquaint.SandboxAcquaintClient)
    assert not client.is_live
    assert await client.list_appointments(10001) == []
    assert acquaint._sandbox_live_client() is None


def test_sandbox_appointment_reads_require_only_credentials(monkeypatch):
    monkeypatch.setenv("sandbox", "TRUE")
    monkeypatch.setenv("TVE_ALLOW_LIVE_INTEGRATIONS", "true")
    monkeypatch.setenv("ACQUAINT_BASE", "https://real-provider.invalid")
    monkeypatch.setenv("ACQUAINT_SITE_PREFIX", "ABCD")
    # Missing API key fails safe to None rather than contacting the provider.
    monkeypatch.setenv("ACQUAINT_API_KEY", "")
    assert acquaint._sandbox_live_client() is None
    assert not acquaint.make_client().is_live

    monkeypatch.setenv("ACQUAINT_API_KEY", "live-secret")
    # Obsolete flags cannot prevent read-only appointments when credentials exist.
    monkeypatch.setenv("TVE_ACQUAINT_MODE", "mock")
    monkeypatch.setenv("TVE_ALLOW_LIVE_INTEGRATIONS", "false")
    live = acquaint._sandbox_live_client()
    assert isinstance(live, acquaint.AcquaintReadOnlyClient)
    assert live.api_key == "live-secret"


def test_mock_smtp_ignores_real_smtp_configuration(monkeypatch):
    monkeypatch.setenv("sandbox", "TRUE")
    monkeypatch.setenv("TVE_SMTP_HOST", "real-smtp.invalid")
    assert followup._mail_configuration()["host"] == "mock-inbox"
    assert followup._mail_configuration()["port"] == 1025


@pytest.mark.asyncio
async def test_sandbox_acquaint_reads_and_mutations_never_write_crm(monkeypatch):
    import httpx
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.setenv("sandbox", "TRUE")
    monkeypatch.setenv("ACQUAINT_BASE", "https://crm.example.test")
    monkeypatch.setenv("ACQUAINT_API_KEY", "test-key")
    monkeypatch.setenv("ACQUAINT_SITE_PREFIX", "TVES")
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/auth":
            return httpx.Response(200, json={"token": "test-bearer", "expiresIn": 3600})
        assert request.method == "GET"
        assert request.url.params["request.propertyId"] == "242"
        return httpx.Response(200, json=[{"startDate": "2099-01-01T10:00:00Z"}])
    client = acquaint.make_client(transport=httpx.MockTransport(handle))
    assert len(await client.list_appointments(10001)) == 1
    for operation in (client.create_appointment, client.create_note, client.create_task):
        assert (await operation({}))["localMock"]
    with pytest.raises(acquaint.AcquaintReadOnlyError):
        await client._live.request("POST", "/v1/Appointments/list")
    assert calls == [("POST", "/v1/auth"), ("GET", "/v1/Appointments/list")]


def test_sandbox_ignores_all_real_mail_credentials_even_invalid_ports(monkeypatch):
    from app.workers import imap_intake
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.setenv("sandbox", "TRUE")
    for key in ("TVE_IMAP_HOST", "TVE_IMAP_USERNAME", "TVE_IMAP_PASSWORD", "TVE_SMTP_HOST",
                "TVE_SMTP_USER", "TVE_SMTP_PASSWORD", "TVE_ENQUIRY_FROM"):
        monkeypatch.setenv(key, "real-value-ignored")
    monkeypatch.setenv("TVE_IMAP_PORT", "invalid")
    monkeypatch.setenv("TVE_SMTP_PORT", "invalid")
    assert imap_intake.inbound_configuration() is None
    assert followup._mail_configuration() == {
        "host": "mock-inbox", "port": 1025, "sender": "lettings@sandbox.invalid"}
    calls = []
    class MockSMTP:
        def __init__(self, host, port, **kwargs):
            calls.append((host, port))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def send_message(self, message): calls.append("sent")
    monkeypatch.setattr(followup.smtplib, "SMTP", MockSMTP)
    monkeypatch.setattr(followup.smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("Real SMTP used"))
    followup._smtp_send({"host": "real-host", "port": 465}, object())
    assert calls == [("mock-inbox", 1025), "sent"]


def test_mode_switch_rejects_conflicts_and_typos(monkeypatch):
    # A normal dict models Linux's case-sensitive container environment on Windows.
    environment = {"sandbox": "TRUE", "SANDBOX": "FALSE"}
    monkeypatch.setattr(config.os, "environ", environment)
    with pytest.raises(ValueError): config.is_sandbox()
    del environment["sandbox"]
    environment["SANDBOX"] = "ture"
    with pytest.raises(ValueError): config.is_sandbox()
    environment["SANDBOX"] = " TRUE "
    assert config.is_sandbox()


@pytest.mark.asyncio
async def test_production_acquaint_uses_credentials_without_mode_flags(monkeypatch):
    import httpx
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.setenv("sandbox", "FALSE")
    for key, value in {"ACQUAINT_BASE": "https://crm.example.test", "ACQUAINT_API_KEY": "test-key",
                       "ACQUAINT_SITE_PREFIX": "TVES"}.items():
        monkeypatch.setenv(key, value)
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/auth":
            return httpx.Response(200, json={"token": "test-bearer"})
        assert request.url.params["request.propertyId"] == "42"
        return httpx.Response(200, json=[])
    client = acquaint.make_client(transport=httpx.MockTransport(handle))
    assert not client.is_mock
    assert await client.list_appointments(42) == []
    assert calls == [("POST", "/v1/auth"), ("GET", "/v1/Appointments/list")]


def test_token_format_is_opaque_and_scoped():
    from app.services.shared import tokens_store
    q = tokens_store._new_token()
    assert not q.startswith("sbx_")
    assert len(q) == 43
    assert q != tokens_store._new_token()


@pytest.mark.asyncio
async def test_invalid_or_missing_token_is_rejected(monkeypatch):
    from app.services.shared import tokens_store
    async def missing(token, purpose):
        return None
    monkeypatch.setattr(tokens_store, "verify_token", missing)
    with pytest.raises(HTTPException) as error:
        await journey.verify_token(None, "a" * 43, "booking")
    assert error.value.status_code == 401


def test_incomplete_address_never_auto_matches():
    assert match_property("Reading", None, [{"id":1,"street":"1 Example Road","town":"Reading"}])["status"] == "unmatched"


def test_server_validates_questionnaire_answers():
    with pytest.raises(HTTPException) as error:
        journey.validate_answers({}, True)
    assert error.value.status_code == 422
    with pytest.raises(HTTPException):
        journey.validate_answers({}, False)


@pytest.mark.asyncio
async def test_intake_links_matched_crm_property_and_does_not_link_unknown(monkeypatch):
    from types import SimpleNamespace
    from app.services.enquiries import repository
    from app.services.shared import enquiries_store
    stored = []
    statuses = []
    async def upsert(*args, initial_status="enquiried"):
        statuses.append(initial_status)
        return 42
    async def save(*args): pass
    async def link(contact_id, property_id): stored.append((contact_id, property_id))
    monkeypatch.setattr(repository, "upsert_contact", upsert)
    monkeypatch.setattr(repository, "upsert_enquiry", save)
    monkeypatch.setattr(repository, "link_property", link)
    monkeypatch.setattr(repository, "is_sandbox", lambda: True)
    monkeypatch.setattr(repository, "fixture_properties", lambda: [{"id": 10001, "sourcePropertyId": 242}])
    parsed = SimpleNamespace(is_enquiry=True, sender_name="Test Lead", sender_email="test@example.invalid",
                             sender_phone=None, body="Test enquiry", extracted_fields={}, needs_review=False)
    kwargs = dict(mailbox_key="mailpit", mailbox_name="INBOX", uid_validity=1, uid=1, parsed=parsed)
    assert await repository.record_message(None, **kwargs, property_match={"status": "matched", "property_id": 10001}) == 42
    assert stored == [(42, 242)]
    assert statuses == ["enquiried"]
    await repository.record_message(None, **kwargs, property_match={"status": "unmatched", "property_id": None})
    assert stored == [(42, 242)]
    # A genuine lettings enquiry with no match is labelled for review, not enquiry.
    assert statuses == ["enquiried", "unmatched_enquiry"]


def test_repeat_confirm_error_carries_already_requested_booking(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    import app.routes.booking.confirm as confirm_route
    monkeypatch.setattr("app.main.AUTH_DISABLED", True)
    monkeypatch.setenv("sandbox", "TRUE")
    booked = {"status": "acknowledged", "requestId": 10, "localMock": True,
              "slot": {"start": "2026-10-06T12:00:00+00:00", "end": "2026-10-06T12:30:00+00:00"},
              "slotStart": "2026-10-06T12:00:00+00:00", "slotEnd": "2026-10-06T12:30:00+00:00"}
    async def fake_get_pool(name: str = "contacts"):
        return object()
    async def not_guest(token):
        return None
    async def fake_store(pool, token, start, supplied_property):
        raise HTTPException(409, detail={"status": "error", "reason": "already-requested",
                                         "message": "You have already requested a viewing for this property.",
                                         "booked": booked})
    monkeypatch.setattr(confirm_route, "get_pool", fake_get_pool)
    monkeypatch.setattr(confirm_route, "verified_guest_token", not_guest)
    monkeypatch.setattr(confirm_route, "store_viewing", fake_store)
    client = TestClient(app)
    response = client.post("/webhook/thames-valley-estates/booking-confirm",
                           json={"token": "a" * 43, "slotStart": "2026-10-06T14:00:00+01:00", "propertyId": 10054})
    assert response.status_code == 409
    payload = response.json()
    assert payload["error"]["message"] == "You have already requested a viewing for this property."
    assert payload["error"]["booked"] == booked


@pytest.mark.asyncio
async def test_live_acquaint_create_appointment_posts_the_write_endpoint(monkeypatch):
    import httpx
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.setenv("sandbox", "FALSE")
    for key, value in {"ACQUAINT_BASE": "https://crm.example.test", "ACQUAINT_API_KEY": "test-key",
                       "ACQUAINT_SITE_PREFIX": "TVES"}.items():
        monkeypatch.setenv(key, value)
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/auth":
            return httpx.Response(200, json={"token": "test-bearer", "expiresIn": 3600})
        assert request.method == "POST"
        return httpx.Response(200, json={"id": 321})

    assert acquaint.WRITE_ENDPOINTS["appointments"] == "/v1/Appointments"
    client = acquaint.make_client(transport=httpx.MockTransport(handle))
    assert not client.is_mock
    # Body uses only AppointmentWrite field names (Swagger spec).
    body = {"startDate": "2099-01-01T10:00:00+00:00", "endDate": "2099-01-01T10:30:00+00:00",
            "appointmentTypeId": 7, "confirmed": True, "notes": "Approved", "properties": [42]}
    created = await client.create_appointment(body)
    assert created["id"] == 321
    assert calls == [("POST", "/v1/auth"), ("POST", "/v1/Appointments")]
    # The read-only request path still rejects the write endpoint.
    with pytest.raises(acquaint.AcquaintReadOnlyError):
        await client.request("POST", acquaint.WRITE_ENDPOINTS["appointments"])


@pytest.mark.asyncio
async def test_live_acquaint_create_appointment_propagates_provider_failure(monkeypatch):
    import httpx
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.setenv("sandbox", "FALSE")
    for key, value in {"ACQUAINT_BASE": "https://crm.example.test", "ACQUAINT_API_KEY": "test-key",
                       "ACQUAINT_SITE_PREFIX": "TVES"}.items():
        monkeypatch.setenv(key, value)

    def handle(request):
        if request.url.path == "/v1/auth":
            return httpx.Response(200, json={"token": "test-bearer", "expiresIn": 3600})
        return httpx.Response(500, json={"error": "failed"})

    client = acquaint.make_client(transport=httpx.MockTransport(handle))
    with pytest.raises(httpx.HTTPStatusError):
        await client.create_appointment({"propertyId": 42})
