"""Built-in pipeline scheduler (DECISIONS.md D1).

Runs inside the web app process on a dedicated thread with its own asyncio
event loop (Flask is sync — D2). Semantics ported from
legacy/news_scheduler.py:

- one scheduler loop waking every 60 seconds
- per-interest schedule: first run at start_time (HH:MM), then every
  interval_hours within the day
- catch-up: on startup, run the most recently missed run per interest
- an interest is "paused" when summary, script and brief are all disabled
- per-interest concurrency guard: never two runs of the same interest
"""

# version: 0.1.0

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_SECONDS_PER_HOUR = 3600


def _parse_hhmm(time_str: str) -> tuple[int, int]:
    """Parse "HH:MM"; fall back to 04:00 on malformed input (legacy behavior)."""
    try:
        parts = time_str.strip().split(":")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 4, 0


def compute_next_run_seconds(start_time: str, interval_hours: int, now: datetime) -> float:
    """Seconds until the next scheduled run (ported from legacy)."""
    start_h, start_m = _parse_hhmm(start_time)
    interval = max(1, interval_hours)

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_since_midnight = (now - midnight).total_seconds()

    possible_runs: list[float] = []
    for day in range(3):
        for k in range(0, 24, interval):
            run_hour = (start_h + k) % 24
            run_seconds = day * 86400 + run_hour * _SECONDS_PER_HOUR + start_m * 60
            if run_seconds > seconds_since_midnight:
                possible_runs.append(run_seconds)

    if not possible_runs:
        return 60.0

    return min(possible_runs) - seconds_since_midnight


def find_most_recent_missed(
    start_time: str,
    interval_hours: int,
    now: datetime,
) -> datetime | None:
    """Most recent scheduled run time that was missed (ported from legacy)."""
    start_h, start_m = _parse_hhmm(start_time)
    interval = max(1, min(interval_hours, 168))
    lookback = timedelta(hours=interval + 1)
    cutoff = now - lookback
    best: datetime | None = None

    for day_offset in range(2):
        check_day = now.date() - timedelta(days=day_offset)
        for k in range(0, 24, interval):
            run_hour = (start_h + k) % 24
            run_time = datetime(
                check_day.year, check_day.month, check_day.day,
                run_hour, start_m, tzinfo=now.tzinfo,
            )
            if run_time < now and run_time > cutoff:
                if best is None or run_time > best:
                    best = run_time

    if best is not None and now - best > timedelta(minutes=5):
        return best
    return None


def is_paused(interest: dict[str, Any]) -> bool:
    """An interest is paused when all deliverable types are disabled (legacy)."""
    return not any(
        interest.get(k) for k in ("enable_summary", "enable_script", "enable_brief")
    )


class SchedulerThread:
    """Background-thread scheduler hosting its own asyncio event loop.

    TODO(WP6): implement — thread lifecycle (start/stop/join), 60s check
    loop, catch-up on startup, per-interest run tasks with a concurrency
    guard, trigger_now() for the web UI's "run now" button.
    """

    def __init__(self, *, timezone: str = "Europe/Berlin") -> None:
        self._tz = ZoneInfo(timezone)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False

    def __bool__(self) -> bool:
        return self._started

    def start(self) -> None:
        raise NotImplementedError("WP6")

    def stop(self) -> None:
        raise NotImplementedError("WP6")

    def trigger_now(self, interest_name: str) -> str:
        """Trigger an immediate run. Returns "" on success, else an error message."""
        raise NotImplementedError("WP6")
