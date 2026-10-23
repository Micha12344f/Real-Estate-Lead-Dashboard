"""Deterministic, explainable lead scoring and attention flags (v1)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..shared.contacts import STAGES

BASE_SCORE = 40
QUESTIONNAIRE_COMPLETED_INDEX = STAGES.index("questionnaire_completed")
MOVE_TIMING_POINTS = {
    "Within 1 week": (25, "Urgent move-in"),
    "Within 2 weeks": (18, "Moving soon"),
    "Within 3 weeks": (12, "Moving within 3 weeks"),
    "Within 4 weeks": (6, "Moving within 4 weeks"),
}
_MONTHLY_RENT_THRESHOLD = 30  # 30x monthly rent ~= 2.5x annual income rule
_PCM = re.compile(r"([\d,]+(?:\.\d+)?)\s*pcm", re.IGNORECASE)
_WEEKLY = re.compile(r"([\d,]+(?:\.\d+)?)\s*p\.?w", re.IGNORECASE)
_ANY_NUMBER = re.compile(r"([\d,]+(?:\.\d+)?)")


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _number(value):
    """Numeric answer values only; None and text count as unknown."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def monthly_rent(raw_property: dict | None) -> int | None:
    """Monthly rent in whole pounds from a catalogue property's price field.

    Acquaint prices read like "£1,351 pcm (£312 pw)": the pcm figure wins, a
    weekly figure converts at 52/12, and anything unparseable is unknown.
    """
    if not isinstance(raw_property, dict):
        return None
    price = raw_property.get("price")
    if price is None or isinstance(price, bool):
        return None
    if isinstance(price, (int, float)):
        return int(round(price))
    if not isinstance(price, str):
        return None
    for pattern, factor in ((_PCM, 1.0), (_WEEKLY, 52 / 12)):
        match = pattern.search(price)
        if match:
            return int(round(float(match.group(1).replace(",", "")) * factor))
    match = _ANY_NUMBER.search(price)
    if match:
        return int(round(float(match.group(1).replace(",", ""))))
    return None


def compute_score(questionnaire: dict | None, rent: int | None) -> dict:
    """v1: base 40 plus questionnaire components, clamped to 0..100.

    ``rent`` is the linked property's monthly rent; the affordability and
    budget components are skipped while either side is unknown.
    """
    if not questionnaire:
        return {"value": None, "band": "unscored", "reasons": ["Questionnaire not completed"]}
    points = BASE_SCORE
    reasons: list[str] = []

    def apply(label: str, delta: int) -> None:
        nonlocal points
        points += delta
        reasons.append(f"{label} ({'+' if delta >= 0 else ''}{delta})")

    move_timing = MOVE_TIMING_POINTS.get(questionnaire.get("move_timing"))
    if move_timing:
        apply(move_timing[1], move_timing[0])

    income = _number(questionnaire.get("household_income"))
    if income is not None and rent is not None:
        if income >= rent * _MONTHLY_RENT_THRESHOLD:
            apply("Income covers 30x monthly rent", 15)
        else:
            apply("Income below 30x monthly rent", -10)

    budget = _number(questionnaire.get("monthly_budget"))
    if budget is not None and rent is not None:
        if budget >= rent:
            apply("Budget at or above rent", 5)
        else:
            apply("Budget below rent", -5)

    if questionnaire.get("income_proof") == "Yes":
        apply("Income proof provided", 10)
    elif questionnaire.get("income_proof") == "No":
        apply("No income proof", -15)

    adverse_credit = questionnaire.get("adverse_credit")
    if adverse_credit == "No":
        apply("Clean credit history", 8)
    elif adverse_credit == "Yes, settled":
        apply("Settled adverse credit", -10)
    elif adverse_credit == "Yes, active":
        apply("Active adverse credit", -20)

    if questionnaire.get("pets") == "No":
        apply("No pets", 2)
    else:
        apply("Pets", -5)

    if questionnaire.get("smoke_vape") == "Yes":
        apply("Smoker or vaper", -5)

    booking_opt_in = questionnaire.get("booking_opt_in")
    if booking_opt_in == "opt_in":
        apply("Opted in to booking", 5)
    elif booking_opt_in == "opt_out":
        apply("Opted out of booking", -10)

    value = max(0, min(100, points))
    band = "high" if value >= 70 else "medium" if value >= 40 else "low"
    return {"value": value, "band": band, "reasons": reasons}


def compute_flags(*, status, stage_index, created_at, updated_at, has_property,
                  enquiry, questionnaire, questionnaire_token, rent, now: datetime) -> list[str]:
    """Attention flags in a fixed order; ``now`` is timezone-aware UTC."""
    flags: list[str] = []
    enquiry = enquiry or {}
    questionnaire = questionnaire or {}
    now = _as_utc(now)

    if status == "unmatched_enquiry":
        flags.append("unmatched_enquiry")

    if (not has_property and (enquiry.get("property_reference") or enquiry.get("platform"))
            and status not in ("booked", "done", "unmatched_enquiry")):
        flags.append("unmatched_property")

    if updated_at is not None:
        updated_at = _as_utc(updated_at)
        if status == "Questionnaire_sent" and updated_at < now - timedelta(days=3):
            flags.append("stalled_questionnaire")
        if status == "enquiried" and updated_at < now - timedelta(days=1):
            flags.append("stalled_enquiried")

    if questionnaire_token and questionnaire_token.get("consumed_at") is None:
        expires_at = questionnaire_token.get("expires_at")
        if expires_at is not None and _as_utc(expires_at) < now and (
                stage_index is None or stage_index < QUESTIONNAIRE_COMPLETED_INDEX):
            flags.append("expired_questionnaire_token")

    budget = _number(questionnaire.get("monthly_budget"))
    if budget is not None and rent is not None and budget < rent:
        flags.append("budget_mismatch")

    income = _number(questionnaire.get("household_income"))
    if income is not None and rent is not None and income < rent * _MONTHLY_RENT_THRESHOLD:
        flags.append("affordability_risk")

    if questionnaire.get("adverse_credit") in ("Yes, settled", "Yes, active") or enquiry.get("adverse_credit"):
        flags.append("adverse_credit")

    if questionnaire.get("income_proof") == "No":
        flags.append("no_income_proof")

    if questionnaire.get("booking_opt_in") == "opt_out":
        flags.append("opted_out")

    if status in ("enquiried", "Questionnaire_sent") and created_at is not None:
        if (now - _as_utc(created_at)).total_seconds() / 86400 > 7:
            flags.append("unresponsive")

    return flags