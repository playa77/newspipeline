"""Guard tests for invariant G3: next-run computation preserves the original
scheduler semantics (start_time + interval grid, 60s tick window), plus
launch-gate behavior (no run starts without an API key).

The reference outputs are frozen in reference_snapshots.py (captured from
the retired reference implementation before its removal on 2026-08-08).
"""

# version: 0.5.1

import asyncio

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import newspipeline.scheduler as sched_mod
from newspipeline.scheduler import (
    SchedulerThread,
    compute_next_run_seconds,
    find_most_recent_missed,
    is_paused,
)

from reference_snapshots import MALFORMED_NEXT_RUN, MISSED, NEXT_RUN

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 8, 8, 9, 37, 25, tzinfo=TZ)

MISSED_NOWS = (
    datetime(2026, 8, 8, 9, 37, tzinfo=TZ),
    datetime(2026, 8, 8, 0, 3, tzinfo=TZ),
    datetime(2026, 8, 8, 23, 59, tzinfo=TZ),
)


def test_next_run_matches_frozen_reference():
    for (start_time, interval), expected in NEXT_RUN.items():
        assert compute_next_run_seconds(start_time, interval, NOW) == pytest.approx(
            expected
        ), (start_time, interval)


def test_next_run_grid_semantics():
    # Start 04:00, every 6h -> grid 04:00, 10:00, 16:00, 22:00.
    assert compute_next_run_seconds("04:00", 6, datetime(2026, 8, 8, 9, 0, tzinfo=TZ)) == 3600
    # Exactly on a grid point the *next* slot counts (strictly greater).
    assert compute_next_run_seconds("04:00", 6, datetime(2026, 8, 8, 16, 0, tzinfo=TZ)) == 6 * 3600
    # Past the last slot of the day, the next run is tomorrow's first slot.
    assert compute_next_run_seconds("04:00", 24, datetime(2026, 8, 8, 23, 30, tzinfo=TZ)) == 4.5 * 3600


def test_next_run_tick_window():
    now = datetime(2026, 8, 8, 9, 0, 30, tzinfo=TZ)
    within = compute_next_run_seconds("09:01", 24, now)   # next slot in 30s
    outside = compute_next_run_seconds("09:05", 24, now)  # next slot in 270s
    assert 0 < within <= 65, "a run inside the 60s tick window must be due"
    assert outside > 65, "a run outside the 60s tick window must not be due"


def test_next_run_malformed_time_falls_back_to_0400():
    now = datetime(2026, 8, 8, 3, 0, tzinfo=TZ)
    for bad, expected in MALFORMED_NEXT_RUN.items():
        assert compute_next_run_seconds(bad, 24, now) == pytest.approx(expected)
    assert compute_next_run_seconds("xx", 24, now) == 3600  # 04:00 fallback


def test_find_most_recent_missed_matches_frozen_reference():
    for (start_time, interval, now_idx), expected in MISSED.items():
        result = find_most_recent_missed(start_time, interval, MISSED_NOWS[now_idx])
        expected_dt = datetime.fromisoformat(expected) if expected else None
        assert result == expected_dt, (start_time, interval, now_idx)


def test_find_most_recent_missed_grace_period():
    now = datetime(2026, 8, 8, 10, 2, tzinfo=TZ)
    # Two minutes ago: inside the 5-minute grace period -> not missed.
    assert find_most_recent_missed("10:00", 24, now) is None
    # One hour ago: missed, and the returned slot matches the schedule.
    missed = find_most_recent_missed("09:00", 24, now)
    assert missed == datetime(2026, 8, 8, 9, 0, tzinfo=TZ)


def test_is_paused():
    assert is_paused({"enable_summary": False, "enable_script": False, "enable_brief": False})
    assert is_paused({})
    assert not is_paused({"enable_summary": True, "enable_script": False, "enable_brief": False})
    assert not is_paused({"enable_brief": True})


def test_launch_refuses_without_api_key(store):
    # A scheduled/triggered launch with no API key must not create a doomed
    # run row (which would also fire a failure alert); it must return the
    # same guidance the CLI gives.
    scheduler = SchedulerThread(
        store=store,
        get_settings=lambda: {"interests": [{
            "name": "no-key", "start_time": "04:00", "interval_hours": 24,
            "enable_summary": True, "enable_script": False, "enable_brief": False,
            "feeds": [],
        }]},
        get_api_key=lambda: "",
    )
    error = asyncio.run(scheduler._launch(
        {"interests": []},
        {"name": "no-key", "feeds": []},
        reason="scheduled",
    ))
    assert error and "NEWS_API_KEY" in error
    assert store.list_runs("no-key") == []


def test_launch_starts_with_api_key(store, monkeypatch):
    started = asyncio.Event()
    seen_kwargs = {}
    delivered_runs = []

    class _FakePipeline:
        def __init__(self, **kwargs):
            seen_kwargs.update(kwargs)

        async def run(self, interest):
            # A real completed run row so the completion (not failure-alert)
            # branch runs; the interest has no delivery modes, so delivery
            # is a no-op.
            rid = store.create_run(interest["name"])["id"]
            store.update_run(rid, status="completed")
            started.set()
            return rid

    def _spy_deliver_run(**kwargs):
        delivered_runs.append(kwargs["run_id"])

    monkeypatch.setattr(sched_mod, "Pipeline", _FakePipeline)
    monkeypatch.setattr(sched_mod.delivery, "deliver_run", _spy_deliver_run)
    scheduler = SchedulerThread(
        store=store,
        get_settings=lambda: {"interests": []},
        get_api_key=lambda: "k",
    )

    async def scenario():
        # The real scheduler creates its lock inside _main(); mirror that
        # here since we drive _launch directly.
        scheduler._lock = asyncio.Lock()
        error = await scheduler._launch(
            {"interests": []}, {"name": "with-key", "feeds": []}, reason="manual",
        )
        assert error is None
        task = scheduler._running.get("with-key")
        if task is not None:
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    assert started.is_set()
    # The key gated at launch must be the one handed to the pipeline —
    # no independent re-read between gate and use (TOCTOU pin).
    assert seen_kwargs.get("api_key") == "k"
    # The completion branch must actually have dispatched delivery.
    assert len(delivered_runs) == 1


def test_launch_failed_run_dispatches_failure_alert(store, monkeypatch):
    alerts = []

    class _FailingPipeline:
        def __init__(self, **kwargs):
            pass

        async def run(self, interest):
            rid = store.create_run(interest["name"])["id"]
            store.update_run(
                rid, status="failed", current_stage="analyze", error="boom",
            )
            return rid

    def _spy_alert(**kwargs):
        alerts.append(kwargs)

    monkeypatch.setattr(sched_mod, "Pipeline", _FailingPipeline)
    monkeypatch.setattr(sched_mod.delivery, "deliver_failure_alert", _spy_alert)
    scheduler = SchedulerThread(
        store=store,
        get_settings=lambda: {"interests": []},
        get_api_key=lambda: "k",
    )

    async def scenario():
        scheduler._lock = asyncio.Lock()
        await scheduler._launch(
            {"interests": []}, {"name": "failing", "feeds": []}, reason="manual",
        )
        task = scheduler._running.get("failing")
        if task is not None:
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    assert len(alerts) == 1
    assert alerts[0]["stage_name"] == "analyze"
    assert alerts[0]["error_message"] == "boom"


# ---- catch-up must not repeat a slot that already ran (D16) ------------------


def _slot_start_time(now: datetime) -> str:
    """A start_time whose today's slot lies ~6h in the past (not a full day)."""
    return (now - timedelta(hours=6)).strftime("%H:%M")


def _catch_up_round(store, monkeypatch, name: str, start_time: str) -> list[str]:
    """Run one catch-up pass; return the interest names it decided to launch."""
    launched: list[str] = []
    monkeypatch.setattr(sched_mod, "_CATCH_UP_DELAY_SECONDS", 0)
    scheduler = SchedulerThread(
        store=store,
        get_settings=lambda: {"interests": [{
            "name": name, "start_time": start_time, "interval_hours": 24,
            "enable_summary": True, "enable_brief": True, "feeds": [],
        }]},
        get_api_key=lambda: "k",
    )

    async def _record_launch(_settings, interest, *, reason):
        launched.append(interest["name"])
        return None

    monkeypatch.setattr(scheduler, "_launch", _record_launch)
    asyncio.run(scheduler._run_catch_up())
    return launched


def test_catch_up_does_not_repeat_a_slot_that_already_ran(store, monkeypatch):
    """A restart must not re-run an interest whose slot already ran.

    Live evidence for this guard: with interval_hours = 24 every interest had
    two runs a day on 2026-09-16/17/18 and the journal showed 24 catch-up
    launches in 72h — each restart added a second full round, doubling both
    LLM spend and delivered mail.
    """
    now = datetime.now(TZ)
    run_id = store.create_run("catch-up-covered")["id"]
    store.update_run(run_id, status="completed")

    launched = _catch_up_round(
        store, monkeypatch, "catch-up-covered", _slot_start_time(now),
    )

    assert launched == []
    assert store.get_run(run_id)["status"] == "completed"  # left untouched


def test_catch_up_still_runs_a_genuinely_missed_slot(store, monkeypatch):
    now = datetime.now(TZ)
    launched = _catch_up_round(
        store, monkeypatch, "catch-up-missed", _slot_start_time(now),
    )
    assert launched == ["catch-up-missed"]


def test_catch_up_retries_a_slot_whose_run_was_interrupted(store, monkeypatch):
    """Crash recovery stays intact: a swept run must not block its slot.

    The run row exists and sits inside the slot's window, but it failed — only
    a completed (or in-flight) run may suppress a catch-up, otherwise an
    interrupted run would silently lose its slot.
    """
    now = datetime.now(TZ)
    store.create_run("catch-up-interrupted")  # status 'running'
    assert store.sweep_orphaned_runs() == 1  # what serve startup does (D10)

    launched = _catch_up_round(
        store, monkeypatch, "catch-up-interrupted", _slot_start_time(now),
    )
    assert launched == ["catch-up-interrupted"]
