"""News Pipeline Scheduler — asyncio-based interest scheduling.

Adapted from ai_news_scraper/src/scheduler.py. Manages per-interest scheduled runs
using asyncio instead of APScheduler for lighter resource usage.  Supports:
- Computed run times from start_time + interval within each day
- Skipping paused interests (all deliverables disabled)
- Catch-up: on startup, runs the most recently missed run per interest
- "Run now" trigger with concurrent-run prevention
- Thread-safe via asyncio.Lock
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_SECONDS_PER_HOUR = 3600


def _parse_hhmm(time_str: str) -> tuple[int, int]:
    try:
        parts = time_str.strip().split(":")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 4, 0


def _find_most_recent_missed(
    start_time: str,
    interval_hours: int,
    now: datetime,
) -> datetime | None:
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


def _compute_next_run_seconds(start_time: str, interval_hours: int, now: datetime) -> float:
    """Compute seconds until the next scheduled run."""
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


class NewsScheduler:
    """Asyncio-based scheduler for multi-interest pipeline runs.

    Each interest runs independently on its own schedule.  Uses a single
    background task that re-evaluates every 60 seconds.

    Parameters
    ----------
    get_interests:
        Async callable returning list of interest dicts (must have: id, name,
        start_time, interval_hours, enable_summary, enable_script,
        enable_brief).
    is_running:
        Callable(interest_id: int) -> bool.
    run_interest:
        Async callable(user_id: str, interest_id: int) -> None.
    timezone:
        IANA timezone string.
    """

    def __init__(
        self,
        *,
        get_interests: Any,
        is_running: Any,
        run_interest: Any,
        timezone: str = "Europe/Berlin",
    ) -> None:
        self._get_interests = get_interests
        self._is_running = is_running
        self._run_interest = run_interest
        self._tz = ZoneInfo(timezone)
        self._lock = asyncio.Lock()
        self._running: dict[int, asyncio.Task[None]] = {}
        self._task: asyncio.Task[None] | None = None
        self._started = False

    def __bool__(self) -> bool:
        return self._started

    async def start(self) -> None:
        """Start the scheduler background loop."""
        if self._started:
            return
        self._started = True
        self._task = asyncio.create_task(self._loop())
        logger.info("News scheduler started — timezone=%s", self._tz)
        asyncio.create_task(self._run_catch_up())

    async def stop(self) -> None:
        """Stop the scheduler."""
        self._started = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("News scheduler stopped")

    async def trigger_now(self, user_id: str, interest_id: int) -> str:
        """Trigger an immediate pipeline run. Returns empty string on success,
        or an error message string on failure."""
        interests = await self._get_interests()
        interest = next((i for i in interests if i.get("id") == interest_id), None)
        if interest is None:
            return f"Interest {interest_id} not found"

        name = interest.get("name", str(interest_id))
        paused = not any(
            interest.get(k) for k in ("enable_summary", "enable_script", "enable_brief")
        )
        if paused:
            return f"Interest '{name}' is paused"

        if self._is_running(interest_id):
            return f"A pipeline run for '{name}' is already in progress"

        async with self._lock:
            if interest_id in self._running and not self._running[interest_id].done():
                return f"A pipeline run for '{name}' is already in progress"

            task = asyncio.create_task(self._run_interest_safe(user_id, interest_id, name))
            self._running[interest_id] = task

        logger.info("Manual run triggered for interest '%s' (id=%d)", name, interest_id)
        return ""

    async def _loop(self) -> None:
        """Main scheduler loop — wakes every 60s to check schedules."""
        while self._started:
            try:
                await self._check_schedules()
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Scheduler loop error")
                await asyncio.sleep(60)

    async def _check_schedules(self) -> None:
        """Check all interests for due scheduled runs."""
        now = datetime.now(self._tz)
        interests = await self._get_interests()

        for interest in interests:
            paused = not any(
                interest.get(k)
                for k in ("enable_summary", "enable_script", "enable_brief")
            )
            if paused:
                continue

            next_run = _compute_next_run_seconds(
                interest.get("start_time", "04:00"),
                interest.get("interval_hours", 24),
                now,
            )
            if next_run <= 65:  # within the 60s sleep window
                iid = interest.get("id")
                if iid and not self._is_running(iid):
                    async with self._lock:
                        if iid not in self._running or self._running[iid].done():
                            name = interest.get("name", str(iid))
                            user_id = interest.get("user_id", "")
                            task = asyncio.create_task(
                                self._run_interest_safe(user_id, iid, name)
                            )
                            self._running[iid] = task

    async def _run_catch_up(self) -> None:
        """On startup, run missed scheduled runs for each interest."""
        await asyncio.sleep(5)  # Let the system stabilize
        now = datetime.now(self._tz)
        interests = await self._get_interests()

        for interest in interests:
            paused = not any(
                interest.get(k)
                for k in ("enable_summary", "enable_script", "enable_brief")
            )
            if paused:
                continue

            iid = interest.get("id")
            if iid is None or self._is_running(iid):
                continue

            missed = _find_most_recent_missed(
                interest.get("start_time", "04:00"),
                interest.get("interval_hours", 24),
                now,
            )
            if missed is None:
                continue

            name = interest.get("name", str(iid))
            user_id = interest.get("user_id", "")
            logger.info(
                "Catch-up: interest '%s' missed run at %s — executing now",
                name, missed.strftime("%Y-%m-%d %H:%M"),
            )
            async with self._lock:
                if iid not in self._running or self._running[iid].done():
                    task = asyncio.create_task(
                        self._run_interest_safe(user_id, iid, name)
                    )
                    self._running[iid] = task

    async def _run_interest_safe(self, user_id: str, interest_id: int, name: str) -> None:
        try:
            logger.info("Pipeline run starting for interest '%s'", name)
            await self._run_interest(user_id, interest_id)
            logger.info("Pipeline run completed for interest '%s'", name)
        except Exception:
            logger.exception("Pipeline run failed for interest '%s'", name)
        finally:
            async with self._lock:
                self._running.pop(interest_id, None)
