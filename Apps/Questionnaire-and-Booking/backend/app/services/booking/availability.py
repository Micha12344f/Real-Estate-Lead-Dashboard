"""Booking slot and Acquaint appointment-time operations."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..shared.acquaint import client as acquaint
from ..shared.config import is_sandbox

TZ = ZoneInfo("Europe/London")
SLOT_MINUTES = int(os.environ.get("TVE_SLOT_MINUTES", "30"))
VIEWING_MINUTES = int(os.environ.get("TVE_VIEWING_MINUTES", "30"))
DAYS_AHEAD = int(os.environ.get("TVE_DAYS_AHEAD", "14"))
WORKDAY_START = 9
WORKDAY_END = 18
MIN_NOTICE_HOURS = int(os.environ.get("TVE_MIN_NOTICE_HOURS", "24"))
MONDAY_START_HOUR = int(os.environ.get("TVE_MONDAY_START_HOUR", "12"))


def london_to_utc(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    """Convert a London wall-clock time to a true UTC instant (DST-aware)."""
    return datetime(y, mo, d, h, mi, tzinfo=TZ).astimezone(timezone.utc)


def parse_appt_instant(s: str) -> datetime | None:
    """Parse an Acquaint appointment time string into a UTC instant."""
    if not s:
        return None
    s = str(s)
    if s.endswith("Z") or (len(s) > 6 and (s[-6] == "+" or s[-6] == "-") and s[-3] == ":"):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")) if s.endswith("Z") else datetime.fromisoformat(s)
        except ValueError:
            return None
    try:
        dt = datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.strptime(s[:16].replace("T", " "), "%Y-%m-%d %H:%M")
        except ValueError:
            return None
    return london_to_utc(dt.year, dt.month, dt.day, dt.hour, dt.minute)


def booking_day_start_hour(day_date: date) -> int:
    """Return the first viewable hour of a day.

    Monday morning is reserved for the paperwork the office could not do at the
    weekend, so Monday viewings start at midday.
    """
    return MONDAY_START_HOUR if day_date.weekday() == 0 else WORKDAY_START


def is_within_min_notice(start: datetime, now: datetime | None = None) -> bool:
    """Return whether a booking is inside the rolling minimum-notice window."""
    current = now or datetime.now(timezone.utc)
    return start < current + timedelta(hours=MIN_NOTICE_HOURS)


def is_sunday_booking_blocked(start: datetime) -> bool:
    """Return whether a booking falls on Sunday in the booking timezone."""
    return start.astimezone(TZ).weekday() == 6


def is_monday_morning_booking_blocked(start: datetime) -> bool:
    """Return whether a booking falls before midday on Monday in the booking timezone."""
    local = start.astimezone(TZ)
    return local.weekday() == 0 and local.hour < MONDAY_START_HOUR


def viewing_slot_error(start: datetime, now: datetime | None = None) -> str | None:
    """Return the rejection reason for a requested viewing instant, or None when bookable."""
    current = now or datetime.now(timezone.utc)
    if start < current:
        return "slot-in-past"
    if is_within_min_notice(start, current):
        return "min-notice"
    if is_sunday_booking_blocked(start):
        return "sunday-blocked"
    if is_monday_morning_booking_blocked(start):
        return "monday-morning-blocked"
    return None


async def build_slots(property_id: int) -> list[dict]:
    """Generate available slots for a property, minus existing appointments.

    Read-only Acquaint appointments and locally saved viewing requests both
    block the calendar in either mode.
    """
    if getattr(acquaint, "is_mock", False) and not is_sandbox():
        return []
    appointments = list(await acquaint.list_appointments(property_id))
    from ..shared.database import get_pool
    contacts_pool = await get_pool("contacts")
    async with contacts_pool.acquire() as conn:
        saved = await conn.fetch(
            "SELECT viewing_slot_start, viewing_slot_end FROM contacts WHERE viewing_slot_start IS NOT NULL AND acquaint_property_id=$1",
            property_id)
    appointments += [{"startDate": r["viewing_slot_start"].isoformat(), "endDate": r["viewing_slot_end"].isoformat()} for r in saved]
    busy: list[tuple[datetime, datetime]] = []
    for appointment in appointments:
        if not isinstance(appointment, dict):
            continue
        start = parse_appt_instant(appointment.get("startDate", ""))
        end = parse_appt_instant(appointment.get("endDate", ""))
        if start and end and start < end:
            busy.append((start, end))

    now = datetime.now(timezone.utc)
    slots: list[dict] = []
    for day in range(DAYS_AHEAD):
        day_date = (now.astimezone(TZ) + timedelta(days=day)).date()
        if day_date.weekday() == 6:
            continue
        for hour in range(booking_day_start_hour(day_date), WORKDAY_END):
            slot_start = london_to_utc(day_date.year, day_date.month, day_date.day, hour, 0)
            slot_end = slot_start + timedelta(minutes=VIEWING_MINUTES)
            if is_within_min_notice(slot_start, now):
                continue
            if any(slot_start < busy_end and slot_end > busy_start for busy_start, busy_end in busy):
                continue
            slots.append({
                "start": slot_start.isoformat(),
                "end": slot_end.isoformat(),
                "label": slot_start.astimezone(TZ).strftime("%A %d %B, %H:%M"),
            })
    return slots


async def is_slot_available(property_id: int, start: datetime) -> bool:
    """Recheck a requested start against live Acquaint appointments."""
    for appointment in await acquaint.list_appointments(property_id):
        if not isinstance(appointment, dict):
            continue
        appt_start = parse_appt_instant(appointment.get("startDate", ""))
        appt_end = parse_appt_instant(appointment.get("endDate", ""))
        if appt_start and appt_end and start < appt_end and (start + (appt_end - appt_start)) > appt_start:
            return False
    return True
