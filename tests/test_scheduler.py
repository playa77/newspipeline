"""Guard tests for invariant G3: next-run computation preserves the original
scheduler semantics (start_time + interval grid, 60s tick window).

The reference outputs are frozen in reference_snapshots.py (captured from
the retired reference implementation before its removal on 2026-08-08).
"""

# version: 0.2.0

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from newspipeline.scheduler import (
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
