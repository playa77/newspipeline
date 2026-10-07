"""Validated per-interest delivery slots, independently of collection slots."""

# version: 0.6.0

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any
import re


def validate_delivery_schedule(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("delivery_schedule must be an object (empty means immediate)")
    if not value:
        return {}
    if set(value) - {"start_time", "interval_hours", "timezone"}:
        raise ValueError("unknown delivery_schedule field")
    time = value.get("start_time", "04:00")
    if not isinstance(time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time):
        raise ValueError("delivery_schedule.start_time must be HH:MM")
    interval = value.get("interval_hours", 24)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 24:
        raise ValueError("delivery_schedule.interval_hours must be between 1 and 24")
    if "timezone" in value:
        try:
            if not isinstance(value["timezone"], str):
                raise ValueError("delivery_schedule.timezone must be an IANA timezone")
            ZoneInfo(value["timezone"])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("delivery_schedule.timezone must be an IANA timezone") from exc
    return dict(value)


def next_delivery_at(schedule: dict, now: datetime, default_timezone: str) -> datetime:
    """Next wall-clock slot at/after completion, returned in UTC.

    Slots repeat each day. DST gaps are skipped; an ambiguous wall time
    runs once at its first occurrence. UTC comparisons avoid fold errors.
    """
    validate_delivery_schedule(schedule)
    tz = ZoneInfo(schedule.get("timezone", default_timezone))
    local = now.astimezone(tz)
    hour, minute = map(int, schedule.get("start_time", "04:00").split(":"))
    interval = schedule.get("interval_hours", 24)
    candidates = []
    for day in range(4):
        date = local.date() + timedelta(days=day)
        for offset in range(0, 24, interval):
            slot = datetime(date.year, date.month, date.day, (hour + offset) % 24, minute, tzinfo=tz)
            utc = slot.astimezone(UTC)
            if utc.astimezone(tz).replace(tzinfo=None) != slot.replace(tzinfo=None):
                continue
            if utc >= now.astimezone(UTC):
                candidates.append(utc)
    return min(candidates)
