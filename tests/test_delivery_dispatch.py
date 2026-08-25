"""Guard tests for the shared run-completion dispatch path
(deduplicated from the CLI and scheduler during the refactor).

dispatch_finished_run must route a completed run to output delivery, and
anything else (failed / missing row) to the failure-alert path with stage
and error taken from the run row.
"""

# version: 0.1.1

import newspipeline.delivery as delivery_mod
from newspipeline.delivery import dispatch_finished_run


def _settings():
    return {"global": {"output_dir": "/nonexistent"}, "user": {}, "smtp": {}}


def test_completed_run_goes_to_output_delivery(store, monkeypatch):
    calls = []
    monkeypatch.setattr(delivery_mod, "deliver_run", lambda **kw: calls.append(kw))
    monkeypatch.setattr(
        delivery_mod, "deliver_failure_alert",
        lambda **kw: calls.append(("alert", kw)),  # must NOT be called
    )
    rid = store.create_run("i")
    store.update_run(rid["id"], status="completed")
    status, run = dispatch_finished_run(
        run_id=rid["id"], store=store, settings=_settings(),
        interest={"name": "interest-i"},
    )
    assert status == "completed"
    assert run["status"] == "completed"
    assert len(calls) == 1
    # The dispatch decision's inputs must reach output delivery intact.
    assert calls[0]["run_id"] == rid["id"]
    assert calls[0]["store"] is store
    assert calls[0]["settings"] == _settings()
    assert calls[0]["interest"] == {"name": "interest-i"}


def test_failed_run_goes_to_failure_alert(store, monkeypatch):
    alerts = []
    monkeypatch.setattr(
        delivery_mod, "deliver_run",
        lambda **kw: alerts.append(("deliver", kw)),  # must NOT be called
    )
    monkeypatch.setattr(delivery_mod, "deliver_failure_alert", lambda **kw: alerts.append(kw))
    rid = store.create_run("i")
    store.update_run(rid["id"], status="failed", current_stage="brief", error="x")
    status, run = dispatch_finished_run(
        run_id=rid["id"], store=store, settings=_settings(),
        interest={"name": "i"},
    )
    assert status == "failed"
    assert run["error"] == "x"
    assert len(alerts) == 1
    assert alerts[0]["stage_name"] == "brief"
    assert alerts[0]["error_message"] == "x"


def test_missing_row_routes_to_failure_alert_with_unknowns(store, monkeypatch):
    alerts = []
    monkeypatch.setattr(delivery_mod, "deliver_run", lambda **kw: alerts.append(("deliver", kw)))
    monkeypatch.setattr(delivery_mod, "deliver_failure_alert", lambda **kw: alerts.append(kw))
    status, run = dispatch_finished_run(
        run_id=9999, store=store, settings=_settings(), interest={"name": "i"},
    )
    assert status == "unknown"
    assert run == {}
    assert len(alerts) == 1
    assert alerts[0]["stage_name"] == "unknown"
    assert alerts[0]["error_message"] == "unknown error"


def test_scheduler_and_cli_route_through_shared_dispatch(store, monkeypatch):
    """The dedup guard: both entry points must go through
    dispatch_finished_run. Reverting either caller to inline duplicated
    routing makes the recorded call count wrong."""
    import asyncio

    from newspipeline.cli import _run_selected

    real = delivery_mod.dispatch_finished_run
    routes: list[str] = []

    def spy(**kwargs):
        routes.append(kwargs["interest"].get("name", "?"))
        return real(**kwargs)

    monkeypatch.setattr(delivery_mod, "dispatch_finished_run", spy)
    # Also neutralize output delivery so no files are written.
    monkeypatch.setattr(delivery_mod, "deliver_run", lambda **kw: {"files": [], "emails_sent": 0})

    async def cli_scenario():
        rid = store.create_run("via-cli")["id"]
        store.update_run(rid, status="completed")
        await _run_selected(store, _settings(), [{"name": "via-cli"}], "k")

    asyncio.run(cli_scenario())

    from newspipeline.scheduler import SchedulerThread

    class _DonePipeline:
        def __init__(self, **kwargs):
            pass

        async def run(self, interest):
            rid = store.create_run("via-scheduler")["id"]
            store.update_run(rid, status="completed")
            return rid

    monkeypatch.setattr("newspipeline.scheduler.Pipeline", _DonePipeline)
    scheduler = SchedulerThread(
        store=store, get_settings=lambda: {"interests": []}, get_api_key=lambda: "k",
    )

    async def sched_scenario():
        scheduler._lock = asyncio.Lock()
        await scheduler._launch(
            {"interests": []}, {"name": "via-scheduler"}, reason="manual",
        )
        task = scheduler._running.get("via-scheduler")
        if task is not None:
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(sched_scenario())
    assert routes == ["via-cli", "via-scheduler"]
