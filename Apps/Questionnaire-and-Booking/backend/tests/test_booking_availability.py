"""Unit tests for the viewing notice policy (24h notice, no Sunday, Monday midday)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.booking import availability


def test_booking_day_start_hour_reserves_monday_morning():
    assert availability.booking_day_start_hour(date(2099, 1, 5)) == availability.MONDAY_START_HOUR  # Monday
    assert availability.booking_day_start_hour(date(2099, 1, 6)) == availability.WORKDAY_START  # Tuesday


def test_viewing_slot_error_classifies_instants():
    now = datetime(2099, 1, 5, 9, 0, tzinfo=timezone.utc)  # Monday 09:00 London (GMT)

    assert availability.viewing_slot_error(now - timedelta(hours=1), now) == "slot-in-past"
    assert availability.viewing_slot_error(now + timedelta(hours=12), now) == "min-notice"
    assert availability.viewing_slot_error(availability.london_to_utc(2099, 1, 11, 10, 0), now) == "sunday-blocked"
    assert availability.viewing_slot_error(availability.london_to_utc(2099, 1, 12, 10, 0), now) == "monday-morning-blocked"
    assert availability.viewing_slot_error(availability.london_to_utc(2099, 1, 12, 12, 0), now) is None


@pytest.mark.asyncio
async def test_build_slots_applies_notice_sunday_and_monday_rules(monkeypatch):
    from contextlib import asynccontextmanager
    from app.services.shared import database

    class Connection:
        async def fetch(self, *args):
            return []

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    async def get_pool(name):
        assert name == "contacts"
        return Pool()

    monkeypatch.setattr(database, "get_pool", get_pool)
    async def no_appointments(property_id):
        return []

    monkeypatch.setattr(availability.acquaint, "is_mock", False, raising=False)
    monkeypatch.setattr(availability.acquaint, "list_appointments", no_appointments)
    monkeypatch.setattr(availability, "is_sandbox", lambda: False)

    now = datetime.now(timezone.utc)
    slots = await availability.build_slots(1)

    assert slots, "expected at least one generated slot"
    today_london = now.astimezone(availability.TZ).date()
    for slot in slots:
        start = datetime.fromisoformat(slot["start"])
        local = start.astimezone(availability.TZ)
        assert start >= now + timedelta(hours=availability.MIN_NOTICE_HOURS)
        assert local.date() != today_london
        assert local.weekday() != 6
        assert not (local.weekday() == 0 and local.hour < availability.MONDAY_START_HOUR)
    assert any(datetime.fromisoformat(s["start"]).astimezone(availability.TZ).weekday() == 0 for s in slots)
