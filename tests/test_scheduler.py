"""Guard tests for invariant G3: next-run computation preserves the original
scheduler semantics (start_time + interval grid, 60s tick window), plus
launch-gate behavior (no run starts without an API key).

The reference outputs are frozen in reference_snapshots.py (captured from
the retired reference implementation before its removal on 2026-08-08).
"""

# version: 0.3.0

import asyncio

from datetime import datetime
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
