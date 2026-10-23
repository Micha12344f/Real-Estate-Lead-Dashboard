"""Funnel and metrics math over the stitched lead read models."""
from datetime import datetime, timedelta, timezone

import pytest

import app.services.dashboard.analytics as analytics
import app.services.dashboard.readmodel as readmodel
from app.services.shared import contacts as contacts_store, enquiries_store, responses_store, tokens_store
from app.services.shared.contacts import STAGES

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
CATALOGUE = [{
    "id": 10001, "statusId": 1, "street": "77 Riverbank Walk", "town": "Henley-on-Thames",
    "postcode": "RG7 2EF", "price": "£1,351 pcm (£312 pw)", "bedrooms": 2, "sourcePropertyId": 242,
}]


def _patch(monkeypatch, contacts, *, responses=None, catalogue=None):
    async def fake_list_contacts(limit=None):
        assert limit is None  # analytics always aggregates every contact
        return contacts

    async def fake_contact(contact_id):
        return next((dict(row) for row in contacts if row["id"] == contact_id), None)

    async def fake_enquiry(contact_id):
        return None

    async def fake_response(contact_id):
        return dict(responses[contact_id]) if responses and contact_id in responses else None

    async def fake_tokens(contact_id):
        return []

    async def fake_catalogue():
        return CATALOGUE if catalogue is None else catalogue

    monkeypatch.setattr(contacts_store, "list_contacts", fake_list_contacts)
    monkeypatch.setattr(contacts_store, "contact", fake_contact)
    monkeypatch.setattr(enquiries_store, "enquiry", fake_enquiry)
    monkeypatch.setattr(responses_store, "response", fake_response)
    monkeypatch.setattr(tokens_store, "tokens_for_contact", fake_tokens)
    monkeypatch.setattr(readmodel, "property_catalogue", fake_catalogue)


@pytest.mark.asyncio
async def test_funnel_counts_conversion_dwell_and_daily_volume(monkeypatch):
    rows = [
        {"id": 1, "status": "enquiried", "created_at": NOW - timedelta(days=1), "updated_at": NOW - timedelta(hours=2)},
        {"id": 2, "status": "enquiried", "created_at": NOW - timedelta(days=2), "updated_at": NOW - timedelta(hours=4)},
        {"id": 3, "status": "Questionnaire_sent", "created_at": NOW - timedelta(days=3), "updated_at": NOW - timedelta(hours=6)},
        {"id": 4, "status": "booked", "created_at": NOW - timedelta(days=1), "updated_at": NOW - timedelta(hours=8)},
        {"id": 5, "status": "done", "created_at": NOW - timedelta(days=13), "updated_at": NOW - timedelta(hours=10)},
        {"id": 6, "status": None, "created_at": NOW - timedelta(days=20), "updated_at": NOW - timedelta(hours=12)},
    ]
    _patch(monkeypatch, rows)
    result = await analytics.funnel(now=NOW)

    assert result["stages"] == [
        {"stage": "enquiried", "count": 2}, {"stage": "Questionnaire_sent", "count": 1},
        {"stage": "questionnaire_completed", "count": 0}, {"stage": "booking_link_sent", "count": 0},
        {"stage": "viewing_requested", "count": 0},
        {"stage": "booked", "count": 1}, {"stage": "done", "count": 1},
    ]
    # Conversion counts every contact that reached each stage or beyond it.
    assert result["conversion"] == {
        "enquiried": 0.833, "Questionnaire_sent": 0.5, "questionnaire_completed": 0.333,
        "booking_link_sent": 0.333, "viewing_requested": 0.333, "booked": 0.333, "done": 0.167,
    }
    assert result["avg_hours_in_stage"] == {"enquiried": 3.0, "Questionnaire_sent": 6.0,
                                            "booked": 8.0, "done": 10.0}
    assert len(result["daily_new"]) == 14
    assert result["daily_new"][0] == {"date": "2026-09-22", "count": 1}  # 13 days back, still in window
    assert result["daily_new"][-1] == {"date": "2026-10-05", "count": 0}
    assert sum(item["count"] for item in result["daily_new"]) == 5  # the 20-day-old row is outside
    assert result["generated_at"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_funnel_with_no_contacts_is_all_zero(monkeypatch):
    _patch(monkeypatch, [])
    result = await analytics.funnel(now=NOW)
    assert result["stages"] == [{"stage": stage, "count": 0} for stage in STAGES]
    assert set(result["conversion"]) == set(STAGES)
    assert all(value == 0.0 for value in result["conversion"].values())
    assert result["avg_hours_in_stage"] == {}
    assert len(result["daily_new"]) == 14
    assert all(item["count"] == 0 for item in result["daily_new"])


@pytest.mark.asyncio
async def test_metrics_aggregate_stitched_leads(monkeypatch):
    contacts = [
        # Old, still waiting: unresponsive (needs review).
        {"id": 1, "status": "enquiried", "created_at": NOW - timedelta(days=8),
         "updated_at": NOW - timedelta(hours=8), "acquaint_property_id": None},
        # Invitation sent four days ago: stalled.
        {"id": 2, "status": "Questionnaire_sent", "created_at": NOW - timedelta(days=5),
         "updated_at": NOW - timedelta(days=4), "acquaint_property_id": None},
        # Completed questionnaire with a high score and an upcoming viewing.
        {"id": 3, "status": "questionnaire_completed", "created_at": NOW - timedelta(days=3),
         "updated_at": NOW - timedelta(days=2), "acquaint_property_id": 242,
         "viewing_slot_start": NOW + timedelta(days=2), "viewing_slot_end": NOW + timedelta(days=2, minutes=30)},
    ]
    responses = {3: {"contact_id": 3, "move_timing": "Within 2 weeks", "monthly_budget": 1500,
                     "rental_duration": "6 months to 1 year", "household_income": 60000,
                     "income_proof": "Yes", "occupants": 2, "pets": "No", "smoke_vape": "No",
                     "adverse_credit": "No", "booking_opt_in": "opt_in",
                     "completed_at": NOW - timedelta(days=2)}}
    _patch(monkeypatch, contacts, responses=responses)
    result = await analytics.metrics(now=NOW)
    assert result == {
        "total_leads": 3,
        "unmatched_count": 0,
        "new_7d": 2,
        "completion_rate": 0.333,
        "viewing_rate": 0.333,
        "avg_hours_to_completion": 24.0,
        "avg_hours_to_viewing": 120.0,
        "stalled_count": 1,
        "needs_review_count": 1,
        "high_intent_count": 1,
        "viewings_upcoming": 1,
        "generated_at": NOW.isoformat(),
    }


@pytest.mark.asyncio
async def test_metrics_and_funnel_exclude_unmatched_enquiries(monkeypatch):
    """A genuine lettings enquiry with no matched property is reported
    separately and excluded from the lead/conversion denominators."""
    contacts = [
        {"id": 1, "status": "enquiried", "created_at": NOW - timedelta(days=1),
         "updated_at": NOW - timedelta(hours=1), "acquaint_property_id": 242},
        {"id": 2, "status": "unmatched_enquiry", "created_at": NOW - timedelta(hours=2),
         "updated_at": NOW - timedelta(hours=1), "acquaint_property_id": None},
    ]
    _patch(monkeypatch, contacts)
    result = await analytics.metrics(now=NOW)
    assert result["total_leads"] == 1
    assert result["unmatched_count"] == 1
    funnel = await analytics.funnel(now=NOW)
    assert funnel["unmatched_count"] == 1
    assert sum(stage["count"] for stage in funnel["stages"]) == 1


@pytest.mark.asyncio
async def test_metrics_with_no_leads_report_null_averages(monkeypatch):
    _patch(monkeypatch, [])
    result = await analytics.metrics(now=NOW)
    assert result == {
        "total_leads": 0, "unmatched_count": 0, "new_7d": 0, "completion_rate": 0.0, "viewing_rate": 0.0,
        "avg_hours_to_completion": None, "avg_hours_to_viewing": None,
        "stalled_count": 0, "needs_review_count": 0, "high_intent_count": 0,
        "viewings_upcoming": 0, "generated_at": NOW.isoformat(),
    }
