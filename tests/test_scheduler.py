"""Guard tests for invariant G3: next-run computation preserves legacy
semantics (start_time + interval grid, 60s tick window).

Parity is tested against the actual legacy module (imported from legacy/),
not a reimplementation.
"""

# version: 0.1.0

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from newspipeline.scheduler import (
    compute_next_run_seconds,
    find_most_recent_missed,
    is_paused,
)

from conftest import load_legacy_module

TZ = ZoneInfo("Europe/Berlin")


@pytest.fixture(scope="module")
def legacy():
    return load_legacy_module("legacy_news_scheduler", "news_scheduler.py")


def test_next_run_matches_legacy(legacy):
    now = datetime(2026, 8, 8, 9, 37, 25, tzinfo=TZ)
    for start_time in ("04:00", "06:30", "00:00", "23:45", "12:05"):
        for interval in (1, 2, 6, 12, 24, 48, 168, 0, 200):
            assert compute_next_run_seconds(start_time, interval, now) == pytest.approx(
                legacy._compute_next_run_seconds(start_time, interval, now)
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


def test_next_run_malformed_time_falls_back_to_0400(legacy):
    now = datetime(2026, 8, 8, 3, 0, tzinfo=TZ)
    for bad in ("", "xx", "4", "25:99"):
        assert compute_next_run_seconds(bad, 24, now) == pytest.approx(
            legacy._compute_next_run_seconds(bad, 24, now)
        )
    assert compute_next_run_seconds("xx", 24, now) == 3600  # 04:00 fallback


def test_find_most_recent_missed_matches_legacy(legacy):
    for start_time in ("04:00", "06:30", "23:45"):
        for interval in (1, 6, 24, 168):
            for now in (
                datetime(2026, 8, 8, 9, 37, tzinfo=TZ),
                datetime(2026, 8, 8, 0, 3, tzinfo=TZ),
                datetime(2026, 8, 8, 23, 59, tzinfo=TZ),
            ):
                assert find_most_recent_missed(start_time, interval, now) == \
                    legacy._find_most_recent_missed(start_time, interval, now), \
                    (start_time, interval, now)


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
