"""Funnel and metrics aggregations for the lead dashboard."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from . import readmodel
from ..shared import contacts as contacts_store
from ..shared.contacts import STAGES, UNMATCHED_STATUS

DAILY_WINDOW_DAYS = 14
NEEDS_REVIEW_FLAGS = frozenset({"unmatched_property", "expired_questionnaire_token", "unresponsive"})


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _stage_index(status) -> int | None:
    return STAGES.index(status) if status in STAGES else None


def _instant(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _average_hours(pairs: list[tuple[datetime, datetime]]) -> float | None:
    if not pairs:
        return None
    return round(sum((end - start).total_seconds() / 3600 for start, end in pairs) / len(pairs), 2)


async def funnel(*, now: datetime | None = None) -> dict:
    """Journey-ladder counts, conversion shares, stage dwell times and daily volume.

    ``unmatched_enquiry`` contacts are genuine enquiries without a property
    match; they are reported separately and excluded from the journey ladder
    (they are never invited and cannot progress).
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    all_rows = await contacts_store.list_contacts(None)
    unmatched_count = sum(1 for row in all_rows if row.get("status") == UNMATCHED_STATUS)
    rows = [row for row in all_rows if row.get("status") != UNMATCHED_STATUS]
    total = len(rows)
    status_counts = Counter(row.get("status") for row in rows)
    indexes = [_stage_index(row.get("status")) for row in rows]

    stages = [{"stage": stage, "count": status_counts.get(stage, 0)} for stage in STAGES]
    conversion = {}
    for index, stage in enumerate(STAGES):
        reached = sum(1 for value in indexes if value is not None and value >= index)
        conversion[stage] = round(reached / total, 3) if total else 0.0

    hours: dict[str, list[float]] = {}
    for row in rows:
        status, updated_at = row.get("status"), row.get("updated_at")
        if status and updated_at is not None:
            hours.setdefault(status, []).append((now - _as_utc(updated_at)).total_seconds() / 3600)
    ordered = [status for status in STAGES if status in hours]
    ordered += sorted(status for status in hours if status not in STAGES)
    avg_hours_in_stage = {status: round(sum(hours[status]) / len(hours[status]), 2) for status in ordered}

    daily_counts = Counter(
        _as_utc(row["created_at"]).date().isoformat()
        for row in all_rows if isinstance(row.get("created_at"), datetime))
    today = now.date()
    days = [today - timedelta(days=offset) for offset in range(DAILY_WINDOW_DAYS - 1, -1, -1)]
    daily_new = [{"date": day.isoformat(), "count": daily_counts.get(day.isoformat(), 0)} for day in days]

    return {
        "stages": stages,
        "conversion": conversion,
        "avg_hours_in_stage": avg_hours_in_stage,
        "daily_new": daily_new,
        "unmatched_count": unmatched_count,
        "generated_at": now.isoformat(),
    }


async def metrics(*, now: datetime | None = None) -> dict:
    """Lead-volume and journey health metrics over the stitched read models.

    ``unmatched_enquiry`` rows are reported separately and excluded from the
    lead/conversion denominators, so "leads" means matched lettings enquiries.
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    all_leads = await readmodel.list_leads(limit=None, now=now)
    leads = [lead for lead in all_leads if lead["status"] != UNMATCHED_STATUS]
    unmatched_count = len(all_leads) - len(leads)
    total_leads = len(leads)

    new_7d = 0
    for lead in leads:
        created = _instant(lead["created_at"])
        if created is not None and _as_utc(created) >= now - timedelta(days=7):
            new_7d += 1

    completed = [lead for lead in leads if lead["questionnaire"]]
    completion_rate = round(len(completed) / total_leads, 3) if total_leads else 0.0
    with_viewing = [lead for lead in leads if lead["viewing_slot_start"]]
    viewing_rate = round(len(with_viewing) / total_leads, 3) if total_leads else 0.0

    completion_hours = []
    for lead in completed:
        start, end = _instant(lead["created_at"]), _instant(lead["questionnaire"]["completed_at"])
        if start is not None and end is not None:
            completion_hours.append((_as_utc(start), _as_utc(end)))
    viewing_hours = []
    for lead in with_viewing:
        start, end = _instant(lead["created_at"]), _instant(lead["viewing_slot_start"])
        if start is not None and end is not None:
            viewing_hours.append((_as_utc(start), _as_utc(end)))

    stalled_count = sum(1 for lead in leads if any(flag.startswith("stalled_") for flag in lead["flags"]))
    needs_review_count = sum(1 for lead in leads if NEEDS_REVIEW_FLAGS.intersection(lead["flags"]))
    high_intent_count = sum(
        1 for lead in leads
        if lead["score"]["band"] == "high" or (lead["questionnaire"] or {}).get("booking_opt_in") == "opt_in")
    viewings_upcoming = sum(
        1 for lead in with_viewing if _as_utc(_instant(lead["viewing_slot_start"])) > now)

    return {
        "total_leads": total_leads,
        "unmatched_count": unmatched_count,
        "new_7d": new_7d,
        "completion_rate": completion_rate,
        "viewing_rate": viewing_rate,
        "avg_hours_to_completion": _average_hours(completion_hours),
        "avg_hours_to_viewing": _average_hours(viewing_hours),
        "stalled_count": stalled_count,
        "needs_review_count": needs_review_count,
        "high_intent_count": high_intent_count,
        "viewings_upcoming": viewings_upcoming,
        "generated_at": now.isoformat(),
    }