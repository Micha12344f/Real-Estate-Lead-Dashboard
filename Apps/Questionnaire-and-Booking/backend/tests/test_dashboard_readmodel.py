"""Lead read-model stitching across the four containers."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app
import app.services.dashboard.readmodel as readmodel
from app.services.shared import contacts as contacts_store, enquiries_store, responses_store, tokens_store
from app.services.shared.config import QUESTIONNAIRE_URL

client = TestClient(app)
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
CATALOGUE = [{
    "id": 10001, "statusId": 1, "street": "77 Riverbank Walk", "town": "Henley-on-Thames",
    "postcode": "RG7 2EF", "price": "£1,351 pcm (£312 pw)", "bedrooms": 2, "sourcePropertyId": 242,
}]
LEAD_KEYS = {"id", "contact_uid", "first_name", "last_name", "email", "phone", "status", "stage_index", "property_id",
             "catalogue_property_id", "property_address", "property_rent", "property_bedrooms", "created_at", "updated_at",
             "viewing_slot_start", "viewing_slot_end", "enquiry", "questionnaire", "tokens", "links",
             "score", "flags", "age_days"}


def _patch(monkeypatch, contacts, *, enquiries=None, responses=None, tokens=None, catalogue=None):
    # This catalogue deliberately uses fixture IDs and sourcePropertyId aliases.
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.setenv("sandbox", "TRUE")
    async def fake_list_contacts(limit=None):
        return contacts[:limit] if limit is not None else contacts

    async def fake_contact(contact_id):
        return next((dict(row) for row in contacts if row["id"] == contact_id), None)

    async def fake_enquiry(contact_id):
        return dict(enquiries[contact_id]) if enquiries and contact_id in enquiries else None

    async def fake_response(contact_id):
        return dict(responses[contact_id]) if responses and contact_id in responses else None

    async def fake_tokens(contact_id):
        return [dict(row) for row in (tokens or {}).get(contact_id, [])]

    async def fake_catalogue():
        return CATALOGUE if catalogue is None else catalogue

    monkeypatch.setattr(contacts_store, "list_contacts", fake_list_contacts)
    monkeypatch.setattr(contacts_store, "contact", fake_contact)
    monkeypatch.setattr(enquiries_store, "enquiry", fake_enquiry)
    monkeypatch.setattr(responses_store, "response", fake_response)
    monkeypatch.setattr(tokens_store, "tokens_for_contact", fake_tokens)
    monkeypatch.setattr(readmodel, "property_catalogue", fake_catalogue)


def _contact(contact_id, **overrides):
    row = {"id": contact_id, "first_name": "Amara", "last_name": "Winslow",
           "email": "amara@example.invalid", "phone": "07700 900123", "status": "Questionnaire_sent",
           "acquaint_property_id": 242, "viewing_slot_start": None, "viewing_slot_end": None,
           "created_at": NOW - timedelta(days=2), "updated_at": NOW - timedelta(hours=6)}
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_full_lead_stitches_all_four_containers(monkeypatch):
    enquiry = {"contact_id": 7, "raw_email_body": "raw", "property_reference": "REF-1",
               "platform": "rightmove", "move_in_date": "ASAP", "duration": "6 months",
               "moving_with": "Partner", "pets": "No", "smokers": "No", "employment": "Employed",
               "affordability": "£30,000", "adverse_credit": None,
               "created_at": NOW - timedelta(days=2)}
    response = {"contact_id": 7, "move_timing": "Within 2 weeks", "monthly_budget": 1500,
                "rental_duration": "6 months to 1 year", "household_income": 60000,
                "income_proof": "Yes", "occupants": 2, "pets": "No", "smoke_vape": "No",
                "adverse_credit": "No", "booking_opt_in": "opt_in",
                "completed_at": NOW - timedelta(days=1)}
    tokens = [{"purpose": "questionnaire", "token": "sbx_q",
               "expires_at": NOW + timedelta(days=5), "consumed_at": None},
              {"purpose": "booking", "token": "sbx_b",
               "expires_at": NOW - timedelta(days=1), "consumed_at": None}]
    _patch(monkeypatch, [_contact(7)], enquiries={7: enquiry}, responses={7: response}, tokens={7: tokens})

    leads = await readmodel.list_leads(now=NOW)
    assert len(leads) == 1
    lead = leads[0]
    assert set(lead) == LEAD_KEYS
    assert lead["id"] == 7 and lead["stage_index"] == 1
    assert lead["age_days"] == 2.0
    assert lead["property_id"] == 242
    assert lead["catalogue_property_id"] == 10001  # catalogue filters use fixture ids, not CRM ids
    assert lead["property_address"] == "77 Riverbank Walk, Henley-on-Thames, RG7 2EF"
    assert lead["property_rent"] == 1351
    assert lead["property_bedrooms"] == 2
    assert set(lead["enquiry"]) == {"platform", "property_reference", "move_in_date", "duration",
                                    "moving_with", "pets", "smokers", "employment", "affordability",
                                    "adverse_credit", "received_at"}
    assert lead["enquiry"]["received_at"] == (NOW - timedelta(days=2)).isoformat()
    assert set(lead["questionnaire"]) == {"move_timing", "monthly_budget", "rental_duration",
                                          "household_income", "income_proof", "occupants", "pets",
                                          "smoke_vape", "adverse_credit", "booking_opt_in",
                                          "completed_at"}
    assert lead["questionnaire"]["completed_at"] == (NOW - timedelta(days=1)).isoformat()
    assert lead["tokens"]["questionnaire"] == {"expires_at": (NOW + timedelta(days=5)).isoformat(),
                                              "consumed_at": None}
    assert lead["tokens"]["booking"]["expires_at"] == (NOW - timedelta(days=1)).isoformat()
    assert lead["links"] == {"questionnaire": "http://127.0.0.1:3001/questionnaire/sbx_q", "booking": None}
    assert lead["score"]["value"] == 100
    assert lead["score"]["band"] == "high"
    assert lead["score"]["reasons"] == [
        "Moving soon (+18)", "Income covers 30x monthly rent (+15)", "Budget at or above rent (+5)",
        "Income proof provided (+10)", "Clean credit history (+8)", "No pets (+2)",
        "Opted in to booking (+5)"]
    assert lead["flags"] == []


@pytest.mark.asyncio
async def test_lead_without_enquiry_questionnaire_tokens_or_property(monkeypatch):
    contact = _contact(8, status="enquiried", acquaint_property_id=None,
                       created_at=NOW - timedelta(hours=1), updated_at=NOW - timedelta(minutes=30))
    _patch(monkeypatch, [contact], catalogue=[])
    lead = (await readmodel.list_leads(now=NOW))[0]
    assert lead["enquiry"] is None
    assert lead["questionnaire"] is None
    assert lead["tokens"] == {"questionnaire": None, "booking": None}
    assert lead["links"] == {"questionnaire": None, "booking": None}
    assert lead["score"] == {"value": None, "band": "unscored",
                             "reasons": ["Questionnaire not completed"]}
    assert lead["flags"] == []
    assert lead["property_id"] is None
    assert lead["catalogue_property_id"] is None
    assert lead["property_address"] is None
    assert lead["property_rent"] is None
    assert lead["property_bedrooms"] is None
    assert lead["age_days"] == pytest.approx(1 / 24)


@pytest.mark.asyncio
async def test_legacy_zoopla_platform_is_inferred_without_relabeling_other_email(monkeypatch):
    contacts = [_contact(7), _contact(8)]
    enquiries = {
        7: {"platform": "email", "raw_email_body": "A new enquiry from Zoopla for this property."},
        8: {"platform": "email", "raw_email_body": "I would like to arrange a viewing."},
    }
    _patch(monkeypatch, contacts, enquiries=enquiries)

    leads = await readmodel.list_leads(now=NOW)
    platforms = {lead["id"]: lead["enquiry"]["platform"] for lead in leads}

    assert platforms == {7: "zoopla", 8: "email"}


@pytest.mark.asyncio
async def test_linked_property_missing_from_catalogue_keeps_id_only(monkeypatch):
    _patch(monkeypatch, [_contact(9, acquaint_property_id=555)], catalogue=[])
    lead = (await readmodel.list_leads(now=NOW))[0]
    assert lead["property_id"] == 555
    assert lead["catalogue_property_id"] is None
    assert lead["property_address"] is None
    assert lead["property_rent"] is None
    assert lead["property_bedrooms"] is None


@pytest.mark.asyncio
async def test_unknown_contact_returns_none(monkeypatch):
    _patch(monkeypatch, [])
    assert await readmodel.lead(999, now=NOW) is None
    assert await readmodel.lead_detail(999, now=NOW) is None


@pytest.mark.asyncio
async def test_lead_detail_adds_raw_email_body(monkeypatch):
    _patch(monkeypatch, [_contact(7), _contact(8, status="enquiried", acquaint_property_id=None)],
           enquiries={7: {"contact_id": 7, "raw_email_body": "Hello, I would like to view...",
                         "created_at": NOW - timedelta(days=2)}})
    with_body = await readmodel.lead_detail(7, now=NOW)
    assert with_body["raw_email_body"] == "Hello, I would like to view..."
    without_body = await readmodel.lead_detail(8, now=NOW)
    assert without_body["raw_email_body"] is None


@pytest.mark.asyncio
async def test_expired_questionnaire_token_flags_but_keeps_history(monkeypatch):
    tokens = [{"purpose": "questionnaire", "token": "sbx_old",
               "expires_at": NOW - timedelta(days=1), "consumed_at": None}]
    _patch(monkeypatch, [_contact(7, status="Questionnaire_sent", updated_at=NOW)], tokens={7: tokens})
    lead = (await readmodel.list_leads(now=NOW))[0]
    assert lead["flags"] == ["expired_questionnaire_token"]
    assert lead["links"]["questionnaire"] is None
    assert lead["tokens"]["questionnaire"]["expires_at"] == (NOW - timedelta(days=1)).isoformat()


def test_leads_route_returns_wrapped_list(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    _patch(monkeypatch, [_contact(7), _contact(8, status="enquiried", acquaint_property_id=None,
                                              created_at=NOW - timedelta(days=9),
                                              updated_at=NOW - timedelta(days=9))])
    response = client.get("/dashboard/leads")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"leads", "generated_at"}
    assert [lead["id"] for lead in body["leads"]] == [7, 8]  # updated_at DESC from the store
    # No questionnaire rows here, so both leads are honestly unscored.
    assert body["leads"][0]["score"]["band"] == "unscored"
    assert body["leads"][0]["property_rent"] == 1351  # the catalogue property still resolves


def test_lead_route_reports_unknown_contact_as_404(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)

    async def no_detail(contact_id):
        return None

    monkeypatch.setattr(readmodel, "lead_detail", no_detail)
    response = client.get("/dashboard/leads/999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_dashboard_routes_reject_missing_auth_header(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setattr(main, "TVE_WEBHOOK_SECRET", "test-secret")
    for path in ("/dashboard/leads", "/dashboard/funnel", "/dashboard/metrics"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
