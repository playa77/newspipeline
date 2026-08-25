"""Guard test for invariant G9 (DECISIONS.md D10): serve startup sweeps
orphaned 'running' rows; one-shot CLI runs never do.

A kill -9 mid-run leaves a status='running' row that blocks the interest
forever. A fresh `serve` start implies the previous process died, so its
startup sweep marks those rows failed ('interrupted by restart') and the
interest becomes schedulable again. A concurrent serve process may
legitimately own a running row, so the one-shot `newspipeline run` path
must NOT sweep.
"""

# version: 0.3.0

import time

import newspipeline.scheduler as scheduler_mod
from newspipeline.scheduler import SchedulerThread


def _seed_running(store, interest="swept"):
    rid = store.create_run(interest)["id"]
    assert store.is_interest_running(interest)
    return rid


def test_serve_startup_sweeps_orphaned_rows(store, monkeypatch):
    rid = _seed_running(store)

    launched: list[str] = []

    class _FakePipeline:
        def __init__(self, **kwargs):
            pass

        async def run(self, interest):
            launched.append(interest["name"])
            new_rid = store.create_run(interest["name"])["id"]
            store.update_run(new_rid, status="completed")
            return new_rid

    monkeypatch.setattr(scheduler_mod, "Pipeline", _FakePipeline)
    scheduler = SchedulerThread(
        store=store,
        get_settings=lambda: {"interests": [{
            "name": "swept", "start_time": "04:00", "interval_hours": 24,
            "enable_summary": True, "enable_script": False, "enable_brief": False,
            "feeds": [],
        }]},
        get_api_key=lambda: "k",
    )
    try:
        scheduler.start()
        # start() returns only after the sweep has run (it precedes _ready).
        row = store.get_run(rid)
        assert row["status"] == "failed"
        assert row["error"] == "interrupted by restart"
        assert row["completed_at"]
        assert not store.is_interest_running("swept")

        # Schedulable again: triggering through the public path must get
        # past the stale-row concurrency guard and actually run.
        assert scheduler.trigger_now("swept") == ""
        deadline = time.monotonic() + 5
        while not launched and time.monotonic() < deadline:
            time.sleep(0.02)
        assert launched == ["swept"]
    finally:
        scheduler.stop()


def test_cli_run_does_not_sweep(isolated_config, store, monkeypatch, tmp_path):
    from newspipeline import cli, config

    rid = _seed_running(store)

    sweeps = []
    real_sweep = store.sweep_orphaned_runs

    def spy_sweep():
        sweeps.append(1)
        return real_sweep()

    monkeypatch.setattr(store, "sweep_orphaned_runs", spy_sweep)
    # The CLI builds its own Store() and Pipeline(); inject the fixture ones.
    monkeypatch.setattr(cli, "Store", lambda: store)
    # _cmd_run closes the store in its finally; keep it open for assertions.
    monkeypatch.setattr(store, "close", lambda: None)

    class _FakePipeline:
        def __init__(self, **kwargs):
            pass

        async def run(self, interest):
            new_rid = store.create_run(interest["name"])["id"]
            store.update_run(new_rid, status="completed")
            return new_rid

    monkeypatch.setattr(cli, "Pipeline", _FakePipeline)
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    config.save_settings({"global": {}, "interests": [
        {"name": "swept", "delivery": [], "feeds": []},
    ]})

    exit_code = cli._cmd_run("swept", False)
    assert exit_code == 0
    # The seeded row is untouched: a concurrent serve may legitimately own it.
    assert sweeps == []
    row = store.get_run(rid)
    assert row["status"] == "running"
    assert row["error"] is None
