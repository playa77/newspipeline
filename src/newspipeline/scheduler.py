"""Built-in pipeline scheduler (DECISIONS.md D1).

Runs inside the server process on a dedicated thread with its own asyncio
event loop (Flask is sync — D2). Semantics:

- one scheduler loop waking every 60 seconds
- per-interest schedule: first run at start_time (HH:MM), then every
  interval_hours within the day
- catch-up: on startup, run the most recently missed run per interest — unless
  a completed or in-flight run already covers that slot (a restart must not
  repeat a run that happened; D16)
- startup sweep: orphaned 'running' rows are marked failed (D10, serve only)
- an interest is "paused" when all four deliverable types are disabled
- scheduled delivery jobs are drained independently of collection/LLM access
- per-interest concurrency guard: never two runs of the same interest
"""

# version: 0.6.0

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Callable
from zoneinfo import ZoneInfo

from newspipeline import config, delivery
from newspipeline.recipients import effective_interests, interest_key
from newspipeline.pipeline import Pipeline
from newspipeline.store import Store

logger = logging.getLogger(__name__)

_SECONDS_PER_HOUR = 3600
_CHECK_INTERVAL_SECONDS = 60
_TICK_WINDOW_SECONDS = 65  # a run within this window fires on this tick
_CATCH_UP_DELAY_SECONDS = 5
# A run started within this much before a slot counts as that slot's run: the
# tick window fires up to 65s early, and a run may also start a little late.
_SLOT_TOLERANCE = timedelta(minutes=5)


def _parse_hhmm(time_str: str) -> tuple[int, int]:
    """Parse "HH:MM"; fall back to 04:00 on malformed input."""
    try:
        parts = time_str.strip().split(":")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 4, 0


def compute_next_run_seconds(start_time: str, interval_hours: int, now: datetime) -> float:
    """Seconds until the next scheduled run."""
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
    """Most recent scheduled run time that was missed."""
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
    """An interest is paused when all deliverable types are disabled."""
    return not interest.get("enabled", True) or not any(
        interest.get(k) for k in ("enable_summary", "enable_script", "enable_script_de", "enable_brief")
    )


def _parse_started_at(value: str | None) -> datetime | None:
    """Parse a run row's ISO ``started_at`` into an aware datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class SchedulerThread:
    """Background-thread scheduler hosting its own asyncio event loop.

    Interests are re-read from settings on every tick so edits made
    through the API take effect without a restart. Runs are keyed by
    (recipient id, interest name), with legacy names in the empty scope.

    The concurrency guard is two-layered: an in-memory task registry
    (guards within this process) plus `store.is_interest_running` (guards
    against a second process running the same interest). Residual
    limitation: this sweep runs at serve STARTUP only (D10), so a row
    orphaned by a killed one-shot `run` while a serve is already up stays
    'running' until the next serve restart.
    """

    def __init__(
        self,
        *,
        store: Store,
        get_settings: Callable[[], dict[str, Any]] = config.load_settings,
        get_api_key: Callable[[], str] = config.get_api_key,
    ) -> None:
        self._store = store
        self._get_settings = get_settings
        self._get_api_key = get_api_key
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._started = False
        # The members below live on the scheduler thread's event loop.
        self._lock: asyncio.Lock | None = None
        self._running: dict[str | tuple[str, str], asyncio.Task[None]] = {}

    def __bool__(self) -> bool:
        return self._started

    # ---- thread lifecycle (called from the main/Flask thread) ----

    def start(self) -> None:
        """Start the daemon thread and its event loop. Idempotent."""
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._thread_main, name="newspipeline-scheduler", daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("Scheduler thread did not come up within 10s")
        logger.info("Scheduler thread started")

    def stop(self) -> None:
        """Signal the loop to stop and join the thread. Idempotent.

        In-flight pipeline runs are NOT cancelled: closing the loop abandons
        them, leaving their run rows in 'running' state (the WP6 limitation).
        The caller (CLI `serve`) installs the signal handlers that invoke
        this.
        """
        if not self._started:
            return
        self._started = False
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout=15)
            if self._thread.is_alive():
                logger.warning("Scheduler thread did not stop within 15s")
        logger.info("Scheduler thread stopped")

    def trigger_now(self, interest_name: str, recipient_id: str = "") -> str:
        """Trigger an immediate run. Returns "" on success, else an error message."""
        if not self._started or self._loop is None:
            return "Scheduler is not running"
        future = asyncio.run_coroutine_threadsafe(
            self._trigger_now(interest_name, recipient_id), self._loop,
        )
        try:
            return future.result(timeout=30)
        except Exception as exc:
            logger.exception("trigger_now(%r) failed", interest_name)
            return f"Trigger failed: {exc}"

    # ---- event-loop side (scheduler thread only) ----

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()
            logger.info("Scheduler event loop closed")

    async def _main(self) -> None:
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        # Startup sweep (D10): a fresh serve start implies the previous
        # process died, so 'running' rows can only be orphans. Runs before
        # catch-up so a swept interest is immediately schedulable again.
        # One-shot `newspipeline run` never constructs a SchedulerThread,
        # so it cannot clobber a concurrent serve's legitimately running
        # row. Synchronous on purpose: done before _ready is set, so
        # callers of start() can rely on the sweep having happened.
        self._store.sweep_orphaned_runs()
        self._store.recover_deliveries()
        catch_up = asyncio.create_task(self._run_catch_up())
        checker = asyncio.create_task(self._check_loop())
        self._ready.set()
        await self._stop_event.wait()
        for task in (catch_up, checker):
            task.cancel()
        for task in (catch_up, checker):
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._running:
            logger.info(
                "Shutdown with %d run(s) still in flight: %s",
                len(self._running), ", ".join(sorted(map(str, self._running))),
            )

    def _tz(self, settings: dict[str, Any]) -> ZoneInfo:
        tz_name = settings.get("global", {}).get("timezone", "Europe/Berlin")
        try:
            return ZoneInfo(tz_name)
        except Exception:
            logger.warning("Unknown timezone %r — falling back to Europe/Berlin", tz_name)
            return ZoneInfo("Europe/Berlin")

    async def _check_loop(self) -> None:
        """Wake every 60s and fire interests whose next run is near."""
        while True:
            try:
                drain = asyncio.create_task(asyncio.to_thread(delivery.deliver_due, self._store))
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    # Finish the claimed job before Store is closed on shutdown.
                    await drain
                    raise
                await self._check_schedules()
                await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler check-loop error")
                await asyncio.sleep(_CHECK_INTERVAL_SECONDS)

    async def _check_schedules(self) -> None:
        settings = self._get_settings()
        now = datetime.now(self._tz(settings))
        for interest in effective_interests(settings):
            if is_paused(interest):
                continue
            next_run = compute_next_run_seconds(
                interest.get("start_time", "04:00"),
                interest.get("interval_hours", 24),
                now,
            )
            if next_run <= _TICK_WINDOW_SECONDS:
                await self._launch(settings, interest, reason="scheduled")

    async def _run_catch_up(self) -> None:
        """On startup, run the most recently missed run per interest."""
        await asyncio.sleep(_CATCH_UP_DELAY_SECONDS)
        settings = self._get_settings()
        now = datetime.now(self._tz(settings))
        for interest in effective_interests(settings):
            if is_paused(interest):
                continue
            missed = find_most_recent_missed(
                interest.get("start_time", "04:00"),
                interest.get("interval_hours", 24),
                now,
            )
            if missed is None:
                continue
            name = interest.get("name", "?")
            covered_by = self._covering_run(name, missed, interest.get("recipient_id", ""))
            if covered_by is not None:
                logger.info(
                    "Catch-up skipped — interest %r already served the %s slot "
                    "(run %s): a restart must not repeat a run that happened",
                    name, missed.strftime("%Y-%m-%d %H:%M"), covered_by,
                )
                continue
            logger.info(
                "Catch-up: interest %r missed run at %s — executing now",
                name, missed.strftime("%Y-%m-%d %H:%M"),
            )
            await self._launch(settings, interest, reason="catch-up")

    def _covering_run(self, interest_name: str, slot: datetime, recipient_id: str = "") -> int | None:
        """Id of the run that already covers ``slot``, or None.

        A run started within ``_SLOT_TOLERANCE`` before the slot counts as that
        slot's run (the tick window can fire early). Failed runs deliberately do
        NOT cover a slot: a run interrupted by a crash is swept to 'failed' at
        serve startup and must still be retried here (D10). Only the most recent
        20 runs of the interest are examined (``Store.list_runs``) — far more
        than the single slot in question.
        """
        earliest = slot - _SLOT_TOLERANCE
        for run in self._store.list_runs(interest_name, recipient_id=recipient_id):
            started = _parse_started_at(run.get("started_at"))
            if started is None or started < earliest:
                continue
            if run.get("status") in ("completed", "running"):
                return int(run["id"])
        return None

    async def _trigger_now(self, interest_name: str, recipient_id: str = "") -> str:
        settings = self._get_settings()
        interest = next(
            (i for i in effective_interests(settings, recipient_id) if i.get("name") == interest_name),
            None,
        )
        if interest is None:
            return f"Interest {interest_name!r} not found"
        if is_paused(interest):
            return f"Interest {interest_name!r} is paused"
        return await self._launch(settings, interest, reason="manual") or ""

    async def _launch(
        self, settings: dict[str, Any], interest: dict[str, Any], *, reason: str,
    ) -> str | None:
        """Start a run task unless one is in flight. Returns an error or None."""
        name = interest.get("name", "?")
        api_key = self._get_api_key()
        if not api_key:
            # Consistent with the CLI's `run` command (cli.py): without a
            # key the run would scrape everything, then fail at analyze and
            # fire a failure alert for a known-missing precondition.
            logger.warning(
                "Run not launched — NEWS_API_KEY is not set (interest=%r, reason=%s)",
                name, reason,
            )
            return "NEWS_API_KEY is not set — add it to .env first"
        assert self._lock is not None
        async with self._lock:
            key = interest_key(interest)
            task = self._running.get(key)
            if task is not None and not task.done():
                return f"A pipeline run for {name!r} is already in progress"
            if self._store.is_interest_running(name, interest.get("recipient_id", "")):
                return f"A pipeline run for {name!r} is already in progress"
            self._running[key] = asyncio.create_task(
                self._run_interest_safe(settings, interest, api_key)
            )
        logger.info("Run launched — interest=%r reason=%s", name, reason)
        return None

    async def _run_interest_safe(
        self, settings: dict[str, Any], interest: dict[str, Any],
        api_key: str,
    ) -> None:
        name = interest.get("name", "?")
        try:
            logger.info("Pipeline run starting — interest=%r", name)
            # The key checked at launch is passed through rather than
            # re-read here, so the gate and the pipeline see one value.
            pipeline = Pipeline(
                store=self._store, settings=settings, api_key=api_key,
            )
            run_id = await pipeline.run(interest)
            # Delivery (smtplib, file I/O) is blocking — keep it off the loop.
            loop = asyncio.get_running_loop()
            status, _run = await loop.run_in_executor(None, partial(
                delivery.dispatch_finished_run,
                run_id=run_id, store=self._store,
                settings=settings, interest=interest,
            ))
            logger.info("Pipeline run finished — interest=%r status=%s", name, status)
        except Exception:
            logger.exception("Pipeline run failed — interest=%r", name)
        finally:
            if self._lock is not None:
                async with self._lock:
                    self._running.pop(interest_key(interest), None)
