"""Deterministic scoring and attention-flag rules for the lead dashboard."""
from datetime import datetime, timedelta, timezone

from app.services.dashboard import scoring

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _answers(**overrides):
    answers = {"move_timing": None, "monthly_budget": None, "rental_duration": None,
               "household_income": None, "income_proof": None, "occupants": None, "pets": None,
               "smoke_vape": None, "adverse_credit": None, "booking_opt_in": None}
    answers.update(overrides)
    return answers


def _flags(**overrides):
    params = dict(status="enquiried", stage_index=0, created_at=NOW, updated_at=NOW,
                  has_property=False, enquiry=None, questionnaire=None, questionnaire_token=None,
                  rent=None, now=NOW)
    params.update(overrides)
    return scoring.compute_flags(**params)


# ---------------------------------------------------------------- scoring ----

def test_no_questionnaire_is_unscored():
    expected = {"value": None, "band": "unscored", "reasons": ["Questionnaire not completed"]}
    assert scoring.compute_score(None, 1200) == expected
    assert scoring.compute_score({}, 1200) == expected


def test_move_timing_tiers():
    tiers = {"Within 1 week": 60, "Within 2 weeks": 53, "Within 3 weeks": 47,
             "Within 4 weeks": 41, "After a month": 35}
    for timing, value in tiers.items():
        result = scoring.compute_score(_answers(move_timing=timing), None)
        assert result["value"] == value, timing
    # An answer outside the tier list contributes nothing.
    assert scoring.compute_score(_answers(move_timing="Eventually"), None)["value"] == 35


def test_move_timing_reason_words():
    result = scoring.compute_score(_answers(move_timing="Within 1 week", pets="No"), None)
    assert result["reasons"] == ["Urgent move-in (+25)", "No pets (+2)"]
    result = scoring.compute_score(_answers(move_timing="After a month", pets="No"), None)
    assert result["reasons"] == ["No pets (+2)"]  # zero components are not explained


def test_affordability_component_uses_thirty_x_monthly_rent():
    strong = scoring.compute_score(_answers(household_income=30000), 1000)
    assert strong["value"] == 50
    assert "Income covers 30x monthly rent (+15)" in strong["reasons"]
    weak = scoring.compute_score(_answers(household_income=29999), 1000)
    assert weak["value"] == 25
    assert "Income below 30x monthly rent (-10)" in weak["reasons"]
    # Unknown income or rent skips the component entirely.
    assert scoring.compute_score(_answers(household_income=30000), None)["value"] == 35
    assert scoring.compute_score(_answers(), 1000)["value"] == 35


def test_budget_component_against_rent():
    assert scoring.compute_score(_answers(monthly_budget=1000), 1000)["value"] == 40
    assert scoring.compute_score(_answers(monthly_budget=999), 1000)["value"] == 30
    assert "Budget at or above rent (+5)" in scoring.compute_score(
        _answers(monthly_budget=1000), 1000)["reasons"]
    assert "Budget below rent (-5)" in scoring.compute_score(
        _answers(monthly_budget=999), 1000)["reasons"]


def test_income_proof_component():
    assert scoring.compute_score(_answers(income_proof="Yes"), None)["value"] == 45
    assert scoring.compute_score(_answers(income_proof="No"), None)["value"] == 20


def test_adverse_credit_component():
    assert scoring.compute_score(_answers(adverse_credit="No"), None)["value"] == 43
    assert scoring.compute_score(_answers(adverse_credit="Yes, settled"), None)["value"] == 25
    assert scoring.compute_score(_answers(adverse_credit="Yes, active"), None)["value"] == 15


def test_pets_smoke_and_opt_in_components():
    assert scoring.compute_score(_answers(pets="No"), None)["value"] == 42
    assert scoring.compute_score(_answers(pets="Yes - 1 pet"), None)["value"] == 35
    assert scoring.compute_score(_answers(smoke_vape="Yes"), None)["value"] == 30
    assert scoring.compute_score(_answers(booking_opt_in="opt_in"), None)["value"] == 40
    assert scoring.compute_score(_answers(booking_opt_in="opt_out"), None)["value"] == 25


def test_score_clamps_to_zero_and_hundred():
    strong = scoring.compute_score(_answers(
        move_timing="Within 1 week", monthly_budget=2000, household_income=100000,
        income_proof="Yes", adverse_credit="No", pets="No", smoke_vape="No",
        booking_opt_in="opt_in"), 1000)
    assert strong["value"] == 100
    assert strong["band"] == "high"
    assert strong["reasons"] == [
        "Urgent move-in (+25)", "Income covers 30x monthly rent (+15)", "Budget at or above rent (+5)",
        "Income proof provided (+10)", "Clean credit history (+8)", "No pets (+2)",
        "Opted in to booking (+5)"]

    weak = scoring.compute_score(_answers(
        move_timing="After a month", monthly_budget=500, household_income=10000,
        income_proof="No", adverse_credit="Yes, active", pets="Yes - multiple",
        smoke_vape="Yes", booking_opt_in="opt_out"), 1000)
    assert weak["value"] == 0
    assert weak["band"] == "low"
    assert weak["reasons"] == [
        "Income below 30x monthly rent (-10)", "Budget below rent (-5)", "No income proof (-15)",
        "Active adverse credit (-20)", "Pets (-5)", "Smoker or vaper (-5)", "Opted out of booking (-10)"]


def test_bands_change_at_seventy_and_forty():
    assert scoring.compute_score(_answers(move_timing="Within 2 weeks", income_proof="Yes",
                                          pets="No"), None)["band"] == "high"  # exactly 70
    assert scoring.compute_score(_answers(move_timing="Within 3 weeks", household_income=30000,
                                           pets="No"), 1000)["band"] == "medium"  # exactly 69
    assert scoring.compute_score(_answers(monthly_budget=1000), 1000)["band"] == "medium"  # exactly 40
    assert scoring.compute_score(_answers(smoke_vape="Yes", monthly_budget=999),
                                 1000)["band"] == "low"  # 30


def test_monthly_rent_parses_acquaint_price_strings():
    assert scoring.monthly_rent({"price": "£1,351 pcm (£312 pw)"}) == 1351
    assert scoring.monthly_rent({"price": "£1,660 pcm"}) == 1660
    assert scoring.monthly_rent({"price": "£317 pw"}) == 1374  # weekly converts at 52/12
    assert scoring.monthly_rent({"price": "£950"}) == 950
    assert scoring.monthly_rent({"price": 1250}) == 1250
    assert scoring.monthly_rent({"price": None}) is None
    assert scoring.monthly_rent({"price": "POA"}) is None
    assert scoring.monthly_rent(None) is None


# ------------------------------------------------------------------ flags ----

def test_unmatched_property_needs_enquiry_signal_and_open_status():
    assert "unmatched_property" in _flags(enquiry={"platform": "rightmove"})
    assert "unmatched_property" in _flags(enquiry={"property_reference": "REF-1"})
    assert "unmatched_property" not in _flags(enquiry={"platform": None, "property_reference": None})
    assert "unmatched_property" not in _flags(enquiry={"platform": "rightmove"}, has_property=True)
    assert "unmatched_property" not in _flags(enquiry={"platform": "rightmove"}, status="booked")
    assert "unmatched_property" not in _flags(enquiry={"platform": "rightmove"}, status="done")


def test_stalled_flags_use_strict_day_boundaries():
    assert "stalled_enquiried" not in _flags(updated_at=NOW - timedelta(days=1))
    assert "stalled_enquiried" in _flags(updated_at=NOW - timedelta(days=1, seconds=1))
    assert "stalled_questionnaire" not in _flags(status="Questionnaire_sent", stage_index=1,
                                                  updated_at=NOW - timedelta(days=3))
    assert "stalled_questionnaire" in _flags(status="Questionnaire_sent", stage_index=1,
                                             updated_at=NOW - timedelta(days=3, seconds=1))
    # Only the matching status stalls; the other ladder positions never do.
    assert "stalled_enquiried" not in _flags(status="Questionnaire_sent", stage_index=1,
                                             updated_at=NOW - timedelta(days=2))
    assert "stalled_questionnaire" not in _flags(updated_at=NOW - timedelta(days=2))


def test_expired_questionnaire_token_flag():
    expired = {"expires_at": NOW - timedelta(seconds=1), "consumed_at": None}
    assert "expired_questionnaire_token" in _flags(questionnaire_token=expired)
    # Exactly now is not yet expired; consumed tokens and live ones never flag.
    assert "expired_questionnaire_token" not in _flags(
        questionnaire_token={"expires_at": NOW, "consumed_at": None})
    assert "expired_questionnaire_token" not in _flags(
        questionnaire_token={**expired, "consumed_at": NOW})
    assert "expired_questionnaire_token" not in _flags(
        questionnaire_token={"expires_at": NOW + timedelta(hours=1), "consumed_at": None})
    # Once the questionnaire stage is reached the stale token stops mattering.
    assert "expired_questionnaire_token" not in _flags(
        questionnaire_token=expired, stage_index=2, status="questionnaire_completed")
    # A NULL legacy status has not reached the stage, so the token still flags.
    assert "expired_questionnaire_token" in _flags(
        questionnaire_token=expired, stage_index=None, status=None)


def test_budget_and_affordability_risk_flags():
    answers = {"monthly_budget": 900, "household_income": 29999}
    flags = _flags(questionnaire=answers, rent=1000)
    assert "budget_mismatch" in flags
    assert "affordability_risk" in flags
    # Equality on either rule means no risk; unknown values never flag.
    flags = _flags(questionnaire={"monthly_budget": 1000, "household_income": 30000}, rent=1000)
    assert flags == []
    assert _flags(questionnaire={"monthly_budget": 900}, rent=None) == []
    assert _flags(questionnaire={"household_income": 29999}, rent=None) == []


def test_adverse_credit_flag_from_questionnaire_or_enquiry():
    assert "adverse_credit" in _flags(questionnaire={"adverse_credit": "Yes, settled"})
    assert "adverse_credit" in _flags(questionnaire={"adverse_credit": "Yes, active"})
    assert "adverse_credit" in _flags(enquiry={"adverse_credit": "Yes"})
    assert "adverse_credit" not in _flags(questionnaire={"adverse_credit": "No"})
    assert "adverse_credit" not in _flags(enquiry={"adverse_credit": None})


def test_income_proof_and_opt_out_flags():
    assert "no_income_proof" in _flags(questionnaire={"income_proof": "No"})
    assert "no_income_proof" not in _flags(questionnaire={"income_proof": "Yes"})
    assert "opted_out" in _flags(questionnaire={"booking_opt_in": "opt_out"})
    assert "opted_out" not in _flags(questionnaire={"booking_opt_in": "opt_in"})


def test_unresponsive_uses_age_over_seven_days():
    assert "unresponsive" not in _flags(created_at=NOW - timedelta(days=7))
    assert "unresponsive" in _flags(created_at=NOW - timedelta(days=7, seconds=1))
    assert "unresponsive" in _flags(status="Questionnaire_sent", stage_index=1,
                                    created_at=NOW - timedelta(days=8))
    assert "unresponsive" not in _flags(status="questionnaire_completed", stage_index=2,
                                        created_at=NOW - timedelta(days=8))


def test_flags_keep_their_spec_order():
    flags = scoring.compute_flags(
        status="Questionnaire_sent", stage_index=1,
        created_at=NOW - timedelta(days=8), updated_at=NOW - timedelta(days=4),
        has_property=False,
        enquiry={"platform": "rightmove", "adverse_credit": "Yes"},
        questionnaire={"monthly_budget": 900, "household_income": 10000, "adverse_credit": "Yes, active",
                       "income_proof": "No", "booking_opt_in": "opt_out"},
        questionnaire_token={"expires_at": NOW - timedelta(seconds=1), "consumed_at": None},
        rent=1000, now=NOW)
    assert flags == ["unmatched_property", "stalled_questionnaire", "expired_questionnaire_token",
                     "budget_mismatch", "affordability_risk", "adverse_credit", "no_income_proof",
                     "opted_out", "unresponsive"]


def test_clean_lead_has_no_flags():
    assert _flags() == []
    assert _flags(status="booked", stage_index=4, has_property=True,
                  enquiry={"platform": "rightmove"}) == []