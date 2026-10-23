"""Dashboard actions: assign-property and resend-invitation."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app
import app.routes.dashboard.actions as actions_route
import app.services.dashboard.readmodel as readmodel
from app.services.enquiries import followup
from app.services.shared import acquaint
from app.services.shared import contacts as contacts_store, enquiries_store, responses_store, tokens_store
from app.services.shared.config import QUESTIONNAIRE_URL

client = TestClient(app)
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
CATALOGUE = [{
    "id": 10001, "statusId": 1, "street": "77 Riverbank Walk", "town": "Henley-on-Thames",
    "postcode": "RG7 2EF", "price": "£1,351 pcm", "bedrooms": 2, "sourcePropertyId": 242,
}]


def _auth(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)


def _sandbox(monkeypatch):
    # Windows env vars are case-insensitive: delete any spelling first, then set one.
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.delenv("sandbox", raising=False)
    monkeypatch.setenv("sandbox", "TRUE")


def _contact(**overrides):
    row = {"id": 7, "first_name": "Amara", "last_name": "Winslow", "email": "amara@example.invalid",
           "phone": "07700 900123", "status": "enquiried", "acquaint_property_id": None,
           "viewing_slot_start": None, "viewing_slot_end": None,
           "created_at": NOW - timedelta(days=1), "updated_at": NOW}
    row.update(overrides)
    return row


def _patch_refresh(monkeypatch, state, catalogue=CATALOGUE):
    """Patch the stitched read model the refreshed lead response is built from."""
    async def fake_contact(contact_id):
        return dict(state["contact"]) if contact_id == 7 else None

    async def fake_enquiry(contact_id):
        return None

    async def fake_response(contact_id):
        return None

    async def fake_tokens(contact_id):
        return []

    async def fake_catalogue():
        return catalogue

    async def fake_attendee(contact):
        return 700

    monkeypatch.setattr(contacts_store, "contact", fake_contact)
    monkeypatch.setattr(enquiries_store, "enquiry", fake_enquiry)
    monkeypatch.setattr(responses_store, "response", fake_response)
    monkeypatch.setattr(tokens_store, "tokens_for_contact", fake_tokens)
    monkeypatch.setattr(readmodel, "property_catalogue", fake_catalogue)
    monkeypatch.setattr(actions_route, "property_catalogue", fake_catalogue)
    monkeypatch.setattr(actions_route, "fixture_properties", lambda: catalogue)
    monkeypatch.setattr(actions_route, "_viewing_attendee_id", fake_attendee)


def test_dashboard_actions_require_auth_header(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setattr(main, "TVE_WEBHOOK_SECRET", "test-secret")
    for body in ({"contact_id": 7, "property_id": 10001}, {"contact_id": 7}):
        response = client.post("/dashboard/actions/assign-property", json=body)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        response = client.post("/dashboard/actions/resend-invitation", json=body,
                               headers={"X-TVE-Auth": "wrong-secret"})
        assert response.status_code == 401


def test_action_bodies_are_validated(monkeypatch):
    _auth(monkeypatch)
    for body in ({}, {"contact_id": 7}, {"property_id": 10001},
                 {"contact_id": "seven", "property_id": 10001},
                 {"contact_id": 7, "property_id": 10001, "unexpected": True}):
        response = client.post("/dashboard/actions/assign-property", json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
    response = client.post("/dashboard/actions/resend-invitation", json={})
    assert response.status_code == 422


def test_assign_property_maps_sandbox_fixture_to_source_property(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _contact()}
    links = []

    async def fake_link(contact_id, property_id):
        links.append((contact_id, property_id))
        state["contact"]["acquaint_property_id"] = property_id

    monkeypatch.setattr(contacts_store, "link_property", fake_link)
    _patch_refresh(monkeypatch, state)

    response = client.post("/dashboard/actions/assign-property",
                           json={"contact_id": 7, "property_id": 10001})
    assert response.status_code == 200
    assert links == [(7, 242)]  # fixture id becomes the real Acquaint sourcePropertyId
    lead = response.json()
    assert lead["id"] == 7
    assert lead["property_id"] == 242
    assert lead["property_address"] == "77 Riverbank Walk, Henley-on-Thames, RG7 2EF"
    assert lead["property_rent"] == 1351
    assert lead["property_bedrooms"] == 2
    assert "sent" not in lead


def test_assign_property_production_links_the_acquaint_id_directly(monkeypatch):
    _auth(monkeypatch)
    monkeypatch.setattr(actions_route, "is_sandbox", lambda: False)
    catalogue = [{"id": 42, "statusId": 1, "street": "1 Real Road", "town": "Reading",
                  "postcode": "RG1 1AA", "price": "£950 pcm", "bedrooms": 1}]
    state = {"contact": _contact()}
    links = []

    async def fake_link(contact_id, property_id):
        links.append((contact_id, property_id))
        state["contact"]["acquaint_property_id"] = property_id

    monkeypatch.setattr(contacts_store, "link_property", fake_link)
    _patch_refresh(monkeypatch, state, catalogue=catalogue)

    response = client.post("/dashboard/actions/assign-property",
                           json={"contact_id": 7, "property_id": 42})
    assert response.status_code == 200
    assert links == [(7, 42)]
    assert response.json()["property_rent"] == 950


def test_assign_property_promotes_unmatched_enquiry_and_sends_one_invitation(monkeypatch):
    """An unmatched enquiry assigned a property is linked, promoted to
    'enquiried' and sent exactly one questionnaire invitation."""
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _contact(status="unmatched_enquiry", acquaint_property_id=None)}
    links, promotions, sent = [], [], []

    async def fake_link(contact_id, property_id):
        links.append((contact_id, property_id))
        state["contact"]["acquaint_property_id"] = property_id

    async def fake_promote(contact_id):
        promotions.append(contact_id)
        state["contact"]["status"] = "enquiried"
        return True

    async def fake_send(contact_id):
        sent.append(contact_id)
        # The real helper advances the stage; mirror that so a second call no-ops.
        state["contact"]["status"] = "Questionnaire_sent"
        return True

    monkeypatch.setattr(contacts_store, "link_property", fake_link)
    monkeypatch.setattr(contacts_store, "promote_to_enquiried", fake_promote)
    monkeypatch.setattr(followup, "send_questionnaire_invitation", fake_send)
    _patch_refresh(monkeypatch, state)

    response = client.post("/dashboard/actions/assign-property",
                           json={"contact_id": 7, "property_id": 10001})
    assert response.status_code == 200
    assert links == [(7, 242)]
    assert promotions == [7]
    assert sent == [7]
    assert response.json()["invitationSent"] is True

    # A second assign must not send again (status advanced).
    sent.clear()
    response = client.post("/dashboard/actions/assign-property",
                           json={"contact_id": 7, "property_id": 10001})
    assert response.status_code == 200
    assert sent == []


def test_assign_property_rejects_unknown_states(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)

    async def unknown(contact_id):
        return None

    monkeypatch.setattr(contacts_store, "contact", unknown)
    response = client.post("/dashboard/actions/assign-property",
                           json={"contact_id": 7, "property_id": 10001})
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Unknown contact"

    state = {"contact": _contact(viewing_slot_start=NOW + timedelta(days=1),
                                 viewing_slot_end=NOW + timedelta(days=1, minutes=30))}
    _patch_refresh(monkeypatch, state)
    response = client.post("/dashboard/actions/assign-property",
                           json={"contact_id": 7, "property_id": 10001})
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Contact already has a viewing"

    state = {"contact": _contact()}
    _patch_refresh(monkeypatch, state, catalogue=[])  # nothing selectable matches
    response = client.post("/dashboard/actions/assign-property",
                           json={"contact_id": 7, "property_id": 10001})
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Property is not available for assignment"


def _patch_resend(monkeypatch, state, *, smtp_result=None):
    reissues, advances, sent = [], [], []

    async def fake_reissue(contact_id, purpose):
        reissues.append((contact_id, purpose))
        return "fresh_token"

    async def fake_advance(contact_id, stage):
        advances.append((contact_id, stage))
        if contacts_store.STAGES.index(state["contact"]["status"]) < contacts_store.STAGES.index(stage):
            state["contact"]["status"] = stage

    def fake_smtp(config, message):
        sent.append((config, message))
        if smtp_result is not None:
            raise smtp_result

    monkeypatch.setattr(tokens_store, "reissue_token", fake_reissue)
    monkeypatch.setattr(contacts_store, "advance_status", fake_advance)
    monkeypatch.setattr(followup, "_smtp_send", fake_smtp)
    async def settings_get(key):
        return None
    monkeypatch.setattr(followup.settings_store, "get", settings_get)
    _patch_refresh(monkeypatch, state, catalogue=[])
    return reissues, advances, sent


def test_resend_invitation_reissues_token_sends_and_advances(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _contact()}
    reissues, advances, sent = _patch_resend(monkeypatch, state)

    response = client.post("/dashboard/actions/resend-invitation", json={"contact_id": 7})
    assert response.status_code == 200
    assert reissues == [(7, "questionnaire")]
    assert advances == [(7, "Questionnaire_sent")]
    assert len(sent) == 1
    config, message = sent[0]
    assert config == {"host": "mock-inbox", "port": 1025, "sender": "lettings@sandbox.invalid"}
    assert message["To"] == "amara@example.invalid"
    assert ("http://127.0.0.1:3001/questionnaire/fresh_token") in message.get_body(preferencelist=("plain",)).get_content()
    body = response.json()
    assert body["sent"] is True
    assert body["status"] == "Questionnaire_sent"
    assert body["score"]["band"] == "unscored"


def test_resend_invitation_keeps_questionnaire_sent_status(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _contact(status="Questionnaire_sent")}
    reissues, advances, sent = _patch_resend(monkeypatch, state)

    response = client.post("/dashboard/actions/resend-invitation", json={"contact_id": 7})
    assert response.status_code == 200
    assert reissues == [(7, "questionnaire")]
    assert advances == []
    assert response.json()["sent"] is True


def test_resend_invitation_fails_closed_on_send_error(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _contact()}
    reissues, advances, sent = _patch_resend(monkeypatch, state, smtp_result=OSError("smtp down"))

    response = client.post("/dashboard/actions/resend-invitation", json={"contact_id": 7})
    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Invitation email could not be sent"
    # The token was refreshed, but the journey never advanced after a failed send.
    assert reissues == [(7, "questionnaire")]
    assert advances == []
    assert len(sent) == 1


def test_resend_invitation_rejects_unknown_or_ineligible_contacts(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)

    async def unknown(contact_id):
        return None

    monkeypatch.setattr(contacts_store, "contact", unknown)
    response = client.post("/dashboard/actions/resend-invitation", json={"contact_id": 7})
    assert response.status_code == 404

    state = {"contact": _contact(email="")}
    _patch_resend(monkeypatch, state)
    response = client.post("/dashboard/actions/resend-invitation", json={"contact_id": 7})
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Contact has no email address"

    for status in ("questionnaire_completed", "viewing_requested", "booked", "done", None):
        state = {"contact": _contact(status=status)}
        _patch_resend(monkeypatch, state)
        response = client.post("/dashboard/actions/resend-invitation", json={"contact_id": 7})
        assert response.status_code == 422, status
        assert response.json()["error"]["message"] == "Contact is not awaiting a questionnaire"


@pytest.mark.parametrize("stage", ["enquiried", "Questionnaire_sent", "questionnaire_completed", "viewing_requested", "booked"])
def test_send_booking_records_sent_stage_without_regressing_viewings(monkeypatch, stage):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _contact(status=stage, acquaint_property_id=242)}
    reissues, advances, sent = _patch_resend(monkeypatch, state)
    response = client.post("/dashboard/actions/send-booking", json={"contact_id": 7})
    assert response.status_code == 200
    assert response.json()["sent"] is True
    expected = stage if stage in ("viewing_requested", "booked") else "booking_link_sent"
    assert response.json()["status"] == expected
    assert reissues == [(7, "booking")]
    assert advances == [(7, "booking_link_sent")]
    assert len(sent) == 1
    assert sent[0][1]["To"] == "amara@example.invalid"
    assert "http://127.0.0.1:3000/booking/fresh_token" in sent[0][1].get_content()
    assert "Thank you for completing your questionnaire" not in sent[0][1].get_content()


def test_send_booking_reports_smtp_failure(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _contact(status="questionnaire_completed", acquaint_property_id=242)}
    _, advances, _ = _patch_resend(monkeypatch, state, smtp_result=OSError("smtp down"))
    response = client.post("/dashboard/actions/send-booking", json={"contact_id": 7})
    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Booking email could not be sent"
    assert advances == []


@pytest.mark.parametrize("overrides", [
    {"email": ""}, {"acquaint_property_id": None},
])
def test_send_booking_requires_email_and_property(monkeypatch, overrides):
    _auth(monkeypatch)
    state = {"contact": _contact(**{"acquaint_property_id": 242, **overrides})}
    reissues, _, sent = _patch_resend(monkeypatch, state)
    response = client.post("/dashboard/actions/send-booking", json={"contact_id": 7})
    assert response.status_code == 422
    assert reissues == sent == []


def test_send_booking_requires_auth_and_known_contact(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setattr(main, "TVE_WEBHOOK_SECRET", "test-secret")
    assert client.post("/dashboard/actions/send-booking", json={"contact_id": 7}).status_code == 401
    _auth(monkeypatch)
    state = {"contact": _contact(status="questionnaire_completed")}
    _patch_resend(monkeypatch, state)
    assert client.post("/dashboard/actions/send-booking", json={"contact_id": 999}).status_code == 404


def test_viewing_decision_approves_pending_request_once(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _contact(status="viewing_requested", acquaint_property_id=242,
                                 viewing_slot_start=NOW + timedelta(days=2),
                                 viewing_slot_end=NOW + timedelta(days=2, minutes=30))}
    calls = []

    async def decide(contact_id, approved):
        calls.append((contact_id, approved))
        if state["contact"]["status"] != "viewing_requested":
            return False
        state["contact"]["status"] = "booked" if approved else "questionnaire_completed"
        return True

    monkeypatch.setattr(contacts_store, "decide_viewing", decide)
    _patch_refresh(monkeypatch, state)
    response = client.post("/dashboard/actions/viewing-decision",
                           json={"contact_id": 7, "decision": "approve"})
    assert response.status_code == 200
    assert response.json()["status"] == "booked"
    assert calls == [(7, True)]


def test_viewing_decision_denies_pending_request_and_rejects_replays(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _contact(status="viewing_requested")}
    calls = []

    async def decide(contact_id, approved):
        calls.append((contact_id, approved))
        if state["contact"]["status"] != "viewing_requested":
            return False
        state["contact"]["status"] = "booked" if approved else "questionnaire_completed"
        return True

    monkeypatch.setattr(contacts_store, "decide_viewing", decide)
    _patch_refresh(monkeypatch, state)
    payload = {"contact_id": 7, "decision": "deny"}
    response = client.post("/dashboard/actions/viewing-decision", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "questionnaire_completed"
    response = client.post("/dashboard/actions/viewing-decision", json=payload)
    assert response.status_code == 422
    assert calls == [(7, False)]


def test_viewing_decision_validates_contact_and_body(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    for payload in ({}, {"contact_id": 7, "decision": "maybe"},
                    {"contact_id": 7, "decision": "approve", "extra": True}):
        assert client.post("/dashboard/actions/viewing-decision", json=payload).status_code == 422

    async def missing(contact_id):
        return None

    monkeypatch.setattr(contacts_store, "contact", missing)
    response = client.post("/dashboard/actions/viewing-decision",
                           json={"contact_id": 7, "decision": "approve"})
    assert response.status_code == 404


# --- Approved viewings create the Acquaint appointment before booking ---

VIEWING_UID = "11111111-1111-1111-1111-111111111111"


def _pending_contact(**overrides):
    return _contact(status="viewing_requested", acquaint_property_id=242,
                    viewing_slot_start=NOW + timedelta(days=2),
                    viewing_slot_end=NOW + timedelta(days=2, minutes=30),
                    contact_uid=VIEWING_UID, **overrides)


def _recording_decide(state, calls):
    async def decide(contact_id, approved):
        calls.append((contact_id, approved))
        if state["contact"]["status"] != "viewing_requested":
            return False
        state["contact"]["status"] = "booked" if approved else "questionnaire_completed"
        if not approved:
            state["contact"]["viewing_slot_start"] = None
            state["contact"]["viewing_slot_end"] = None
        return True
    return decide


def test_viewing_decision_approve_creates_acquaint_appointment_then_books(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _pending_contact()}
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/v1/auth":
            return httpx.Response(200, json={"token": "test-bearer", "expiresIn": 3600})
        if request.url.path == "/v1/appointmenttypes/list":
            return httpx.Response(200, json={"data": [
                {"id": 3, "description": "Valuation", "inactive": False},
                {"id": 7, "description": "Viewing", "inactive": False},
            ]})
        return httpx.Response(200, json={"id": 555})

    crm = acquaint.AcquaintReadOnlyClient("https://crm.example.test", "test-key", "TVES",
                                          transport=httpx.MockTransport(handle))
    monkeypatch.setattr(acquaint, "client", crm)
    calls = []
    monkeypatch.setattr(contacts_store, "decide_viewing", _recording_decide(state, calls))
    _patch_refresh(monkeypatch, state)

    response = client.post("/dashboard/actions/viewing-decision",
                           json={"contact_id": 7, "decision": "approve"})
    assert response.status_code == 200
    assert response.json()["status"] == "booked"
    assert calls == [(7, True)]

    post = next(request for request in requests if request.url.path == "/v1/Appointments")
    assert post.method == "POST"
    assert post.headers["Authorization"] == "Bearer test-bearer"
    body = json.loads(post.content)
    # Exact AppointmentWrite body; the "Viewing" type id is resolved from Acquaint.
    assert body == {
        "startDate": (NOW + timedelta(days=2)).isoformat(),
        "endDate": (NOW + timedelta(days=2, minutes=30)).isoformat(),
        "appointmentTypeId": 7,
        "confirmed": True,
        "notes": "Approved from the dashboard. Lead email: amara@example.invalid; phone: 07700 900123",
        "properties": [242],
        "attendees": [700],
    }
    assert "amara@example.invalid" in body["notes"] and "07700 900123" in body["notes"]
    # No non-spec (assumed) field names may be sent.
    for forbidden in ("propertyId", "startDateTime", "endDateTime", "subject",
                      "contactName", "contactEmail", "contactTelephone", "externalReference"):
        assert forbidden not in body


def test_viewing_decision_notes_fall_back_when_contact_details_are_absent(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _pending_contact(email=None, phone=None)}
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/v1/auth":
            return httpx.Response(200, json={"token": "test-bearer", "expiresIn": 3600})
        if request.url.path == "/v1/appointmenttypes/list":
            return httpx.Response(200, json={"data": [{"id": 7, "description": "Viewing"}]})
        return httpx.Response(200, json={"id": 1})

    crm = acquaint.AcquaintReadOnlyClient("https://crm.example.test", "test-key", "TVES",
                                          transport=httpx.MockTransport(handle))
    monkeypatch.setattr(acquaint, "client", crm)
    monkeypatch.setattr(contacts_store, "decide_viewing", _recording_decide(state, []))
    _patch_refresh(monkeypatch, state)

    response = client.post("/dashboard/actions/viewing-decision",
                           json={"contact_id": 7, "decision": "approve"})
    assert response.status_code == 200
    post = next(request for request in requests if request.url.path == "/v1/Appointments")
    notes = json.loads(post.content)["notes"]
    assert notes == "Approved from the dashboard. Lead email: n/a; phone: n/a"


def test_viewing_decision_falls_back_to_default_appointment_type(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _pending_contact()}
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/v1/auth":
            return httpx.Response(200, json={"token": "test-bearer", "expiresIn": 3600})
        if request.url.path == "/v1/appointmenttypes/list":
            return httpx.Response(200, json={"data": [{"id": 3, "description": "Valuation"}]})
        return httpx.Response(200, json={"id": 9})

    crm = acquaint.AcquaintReadOnlyClient("https://crm.example.test", "test-key", "TVES",
                                          transport=httpx.MockTransport(handle))
    monkeypatch.setattr(acquaint, "client", crm)
    monkeypatch.setattr(contacts_store, "decide_viewing", _recording_decide(state, []))
    _patch_refresh(monkeypatch, state)

    response = client.post("/dashboard/actions/viewing-decision",
                           json={"contact_id": 7, "decision": "approve"})
    assert response.status_code == 200
    post = next(request for request in requests if request.url.path == "/v1/Appointments")
    assert json.loads(post.content)["appointmentTypeId"] == \
        actions_route.VIEWING_APPOINTMENT_TYPE_FALLBACK_ID


def test_viewing_decision_fails_closed_when_crm_write_fails(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _pending_contact()}

    class DownCRM:
        async def create_appointment(self, body):
            raise httpx.ConnectError("provider down")

    monkeypatch.setattr(acquaint, "client", DownCRM())
    decided = []

    async def decide(*args):
        decided.append(args)
        return True

    monkeypatch.setattr(contacts_store, "decide_viewing", decide)
    _patch_refresh(monkeypatch, state)

    response = client.post("/dashboard/actions/viewing-decision",
                           json={"contact_id": 7, "decision": "approve"})
    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Viewing appointment could not be created"
    assert decided == []
    assert state["contact"]["status"] == "viewing_requested"


def test_viewing_decision_surfaces_acquaint_validation_error_without_booking(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _pending_contact()}
    request = httpx.Request("POST", "https://crm.example.test/v1/Appointments")
    upstream = httpx.Response(422, request=request)

    class InvalidAppointmentCRM:
        async def create_appointment(self, body):
            raise httpx.HTTPStatusError("validation failed", request=request, response=upstream)

    monkeypatch.setattr(acquaint, "client", InvalidAppointmentCRM())
    decided = []
    monkeypatch.setattr(contacts_store, "decide_viewing", _recording_decide(state, decided))
    _patch_refresh(monkeypatch, state)

    response = client.post("/dashboard/actions/viewing-decision",
                           json={"contact_id": 7, "decision": "approve"})

    assert response.status_code == 422
    assert response.json()["error"]["message"] == (
        "Acquaint rejected the viewing details. Check the applicant contact, property and appointment settings."
    )
    assert decided == []
    assert state["contact"]["status"] == "viewing_requested"


@pytest.mark.parametrize("slow", [False, True])
def test_viewing_decision_returns_saved_result_when_catalogue_refresh_fails(monkeypatch, slow):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _pending_contact()}
    _patch_refresh(monkeypatch, state)
    monkeypatch.setattr(contacts_store, "decide_viewing", _recording_decide(state, []))
    monkeypatch.setattr(actions_route, "LEAD_REFRESH_TIMEOUT_SECONDS", 0.01)

    async def unavailable(_contact_id):
        if slow:
            await asyncio.sleep(1)
        raise httpx.ConnectError("catalogue unavailable")

    monkeypatch.setattr(readmodel, "lead", unavailable)
    response = client.post("/dashboard/actions/viewing-decision", json={"contact_id": 7, "decision": "approve"})
    assert response.status_code == 200
    assert response.json()["status"] == "booked"
    assert response.json()["viewing_slot_start"] == state["contact"]["viewing_slot_start"].isoformat()


def test_viewing_decision_times_out_without_advancing_or_repeating_write(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _pending_contact()}
    _patch_refresh(monkeypatch, state)
    writes, decisions = [], []

    class SlowCRM:
        async def list_appointment_types(self):
            return []

        async def create_appointment(self, body):
            writes.append(body)
            await asyncio.sleep(1)

    monkeypatch.setattr(acquaint, "client", SlowCRM())
    monkeypatch.setattr(actions_route, "APPOINTMENT_CREATE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(contacts_store, "decide_viewing", _recording_decide(state, decisions))
    response = client.post("/dashboard/actions/viewing-decision", json={"contact_id": 7, "decision": "approve"})
    assert response.status_code == 504
    assert "Check its appointments before retrying" in response.json()["error"]["message"]
    assert len(writes) == 1 and decisions == []
    assert state["contact"]["status"] == "viewing_requested"


def test_viewing_decision_fails_closed_when_crm_returns_no_id(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _pending_contact()}

    class NoIdCRM:
        async def create_appointment(self, body):
            return {"localMock": False}

    monkeypatch.setattr(acquaint, "client", NoIdCRM())
    decided = []
    monkeypatch.setattr(contacts_store, "decide_viewing", _recording_decide(state, decided))
    _patch_refresh(monkeypatch, state)

    response = client.post("/dashboard/actions/viewing-decision",
                           json={"contact_id": 7, "decision": "approve"})
    assert response.status_code == 502
    assert decided == []
    assert state["contact"]["status"] == "viewing_requested"


def test_viewing_decision_denies_and_reverts_without_crm_write(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _pending_contact()}

    class NoWriteCRM:
        async def create_appointment(self, body):
            raise AssertionError("deny must not create a CRM appointment")

    monkeypatch.setattr(acquaint, "client", NoWriteCRM())
    calls = []
    monkeypatch.setattr(contacts_store, "decide_viewing", _recording_decide(state, calls))
    _patch_refresh(monkeypatch, state)

    response = client.post("/dashboard/actions/viewing-decision",
                           json={"contact_id": 7, "decision": "deny"})
    assert response.status_code == 200
    assert response.json()["status"] == "questionnaire_completed"
    assert calls == [(7, False)]


def test_viewing_decision_sandbox_uses_the_deterministic_mock(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _pending_contact()}
    recorded = []

    async def decide(contact_id, approved):
        recorded.append((contact_id, approved))
        state["contact"]["status"] = "booked"
        return True

    monkeypatch.setattr(contacts_store, "decide_viewing", decide)
    _patch_refresh(monkeypatch, state)

    assert acquaint.client.is_mock is True
    response = client.post("/dashboard/actions/viewing-decision",
                           json={"contact_id": 7, "decision": "approve"})
    assert response.status_code == 200
    assert response.json()["status"] == "booked"
    assert recorded == [(7, True)]


def test_viewing_decision_atomic_guard_returns_conflict(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)
    state = {"contact": _pending_contact()}

    async def loses_race(contact_id, approved):
        return False

    monkeypatch.setattr(contacts_store, "decide_viewing", loses_race)
    _patch_refresh(monkeypatch, state)

    response = client.post("/dashboard/actions/viewing-decision",
                           json={"contact_id": 7, "decision": "approve"})
    assert response.status_code == 409


def test_viewing_decision_rejects_a_non_pending_stage(monkeypatch):
    _auth(monkeypatch)
    _sandbox(monkeypatch)

    async def wrong_stage(contact_id):
        return _contact(status="enquiried")

    monkeypatch.setattr(contacts_store, "contact", wrong_stage)
    response = client.post("/dashboard/actions/viewing-decision",
                           json={"contact_id": 7, "decision": "approve"})
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Contact does not have a pending viewing request"

    async def no_viewing(contact_id):
        return _contact(status="viewing_requested", acquaint_property_id=None,
                        viewing_slot_start=None, viewing_slot_end=None)

    monkeypatch.setattr(contacts_store, "contact", no_viewing)
    response = client.post("/dashboard/actions/viewing-decision",
                           json={"contact_id": 7, "decision": "approve"})
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Contact has no viewing request to approve"
