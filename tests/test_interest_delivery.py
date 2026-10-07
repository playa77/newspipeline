"""Independent wall-clock delivery schedules, persistent jobs and scoped routing."""

# version: 0.6.0

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import pytest
from newspipeline import config, delivery
from newspipeline.pipeline import Pipeline
from newspipeline.recipients import effective_interests
from newspipeline.scheduler import SchedulerThread, is_paused
from newspipeline.store import Store
from newspipeline.timing import next_delivery_at
from newspipeline.web.app import create_app
from conftest import make_interest, make_settings


def test_delivery_slots_are_independent_and_handle_dst():
    now = datetime(2026, 10, 7, 7, 30, tzinfo=UTC)
    assert next_delivery_at({"start_time": "09:00", "timezone": "UTC"}, now, "UTC") == now.replace(hour=9, minute=0)
    assert next_delivery_at({"start_time": "12:00", "timezone": "UTC"}, now, "UTC") == now.replace(hour=12, minute=0)
    assert next_delivery_at({"start_time": "09:00", "timezone": "Europe/Berlin"}, now, "UTC") == datetime(2026, 10, 8, 7, tzinfo=UTC)
    gap = datetime(2026, 3, 29, 0, tzinfo=UTC)
    assert next_delivery_at({"start_time": "02:30", "timezone": "Europe/Berlin"}, gap, "UTC") == datetime(2026, 3, 30, 0, 30, tzinfo=UTC)
    fold = datetime(2026, 10, 25, 0, tzinfo=UTC)
    assert next_delivery_at({"start_time": "02:30", "timezone": "Europe/Berlin"}, fold, "UTC") == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    after_first_fold = datetime(2026, 10, 25, 0, 45, tzinfo=UTC)
    assert next_delivery_at({"start_time": "02:30", "timezone": "Europe/Berlin"}, after_first_fold, "UTC") == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)


def test_completed_run_is_queued_and_delivered_only_when_due(store, monkeypatch):
    calls = []
    monkeypatch.setattr(delivery, "deliver_run", lambda **kw: calls.append(kw))
    settings = {"global": {"output_dir": "output", "timezone": "UTC", "api_key": "must-not-be-saved"}, "smtp": {}, "user": {"email": "legacy@example.com"}}
    interest = {"name": "Energy", "recipient_id": "alice", "recipient_email": "alice@example.com",
                "delivery": ["email"], "delivery_schedule": {"start_time": "09:00", "timezone": "UTC"}}
    run = store.create_run("Energy", "alice")
    store.update_run(run["id"], status="completed")
    status, dispatched = delivery.dispatch_finished_run(run_id=run["id"], store=store, settings=settings, interest=interest)
    assert status == "completed" and dispatched["delivery_status"] == "pending"
    assert calls == []
    job = store.list_deliveries("alice")[0]
    due = datetime.fromisoformat(job["due_at"])
    payload = store._execute("SELECT payload FROM pending_deliveries").fetchone()[0]
    assert "must-not-be-saved" not in payload
    assert "legacy@example.com" not in payload
    assert delivery.deliver_due(store, due - timedelta(seconds=1)) == 0
    # Recipient/email edits after generation cannot redirect already queued mail.
    interest["recipient_email"] = "different@example.com"
    assert delivery.deliver_due(store, due) == 1
    assert calls[0]["interest"]["email_recipient"] == "alice@example.com"
    assert store.list_deliveries("alice")[0]["status"] == "completed"
    assert delivery.deliver_due(store, due + timedelta(days=1)) == 0
    # Re-dispatching completion is idempotent for the durable queue.
    delivery.dispatch_finished_run(run_id=run["id"], store=store, settings=settings, interest=interest)
    assert len(store.list_deliveries("alice")) == 1


def test_queue_survives_restart_recovers_inflight_and_retries_failures(tmp_path, isolated_config, monkeypatch):
    path = tmp_path / "persistent.db"
    store = Store(path)
    rid = store.create_run("Energy", "alice")["id"]
    payload = {"interest": {"name": "Energy", "recipient_id": "alice"}, "settings": {}}
    store.queue_delivery(rid, "2026-01-01T00:00:00+00:00", payload)
    assert store.claim_due_delivery("2026-01-02T00:00:00+00:00")["run_id"] == rid
    store.close()
    store = Store(path)
    try:
        assert store.list_deliveries("alice")[0]["status"] == "sending"
        store.recover_deliveries()
        def fail(**kwargs):
            raise RuntimeError("SMTP unavailable")
        monkeypatch.setattr(delivery, "deliver_run", fail)
        assert delivery.deliver_due(store) == 0
        assert store.list_deliveries("alice")[0]["status"] == "failed"
        api = create_app(store=store).test_client()
        assert api.post(f"/api/deliveries/{rid}/retry?recipient=bob").status_code == 404
        assert api.post(f"/api/deliveries/{rid}/retry?recipient=alice").status_code == 202
        sent = []
        monkeypatch.setattr(delivery, "deliver_run", lambda **kw: sent.append(kw))
        assert delivery.deliver_due(store) == 1
        assert len(sent) == 1
        assert store.list_deliveries("alice")[0]["attempts"] == 3
    finally:
        store.close()


def test_two_recipients_scrape_group_generate_and_deliver_independently(store, fixture_server, smtp_server, isolated_config, tmp_path):
    settings = make_settings(fixture_server.server_port, tmp_path / "out")
    settings["smtp"].update(host="127.0.0.1", port=smtp_server.server_address[1])
    settings["recipient_defaults"] = {"interests": [make_interest(fixture_server.server_port, ["email", "file"], name="Energy", enable_script=False, enable_script_de=False)]}
    settings["recipients"] = [
        {"id": "alice", "email": "alice@example.com", "interests": [{"name": "Energy", "target_summary_words": 150, "summary_detail": "concise"}]},
        {"id": "bob", "email": "bob@example.com", "interests": [{"name": "Energy", "enable_summary": False, "enable_script_de": True, "target_script_de_words": 750, "script_de_detail": "detailed", "delivery_schedule": {"start_time": "11:00", "timezone": "UTC"}}]},
    ]
    interests = effective_interests(settings)
    async def run():
        return [await Pipeline(store=store, settings=settings, api_key="fixture-key").run(i) for i in interests]
    runs = asyncio.run(run())
    for rid in runs:
        assert store.get_run(rid)["status"] == "completed"
        assert len(store.get_articles_for_run(rid)) == 2
        assert len(store.get_themes_for_run(rid)) == 2
    for rid, interest in zip(runs, interests):
        delivery.dispatch_finished_run(run_id=rid, store=store, settings=settings, interest=interest)
    assert len(smtp_server.captured_emails) == 3
    assert all(e["rcpt"] == ["alice@example.com"] for e in smtp_server.captured_emails)
    assert list((tmp_path / "out/recipients/alice").rglob("*.md"))
    assert not (tmp_path / "out/recipients/bob").exists()
    due = datetime.fromisoformat(store.list_deliveries("bob")[0]["due_at"])
    assert delivery.deliver_due(store, due) == 1
    assert len(smtp_server.captured_emails) == 6
    assert all(e["rcpt"] == ["bob@example.com"] for e in smtp_server.captured_emails[3:])
    assert list((tmp_path / "out/recipients/bob").rglob("*.md"))
    prompts = [r["body"]["messages"][-1]["content"] for r in fixture_server.captured_llm_requests]
    assert any("Detail level: concise" in p and "150" in p for p in prompts)
    assert any("Detail level: detailed" in p and "750" in p for p in prompts)
    assert {r["recipient_id"] for r in (store.get_run(rid) for rid in runs)} == {"alice", "bob"}
    alice_theme = store.get_themes_for_run(runs[0])[0]
    bob_theme = store.get_themes_for_run(runs[1])[0]
    assert {d["deliverable_type"] for d in store.get_deliverables_for_theme(alice_theme["id"])} == {"summary"}
    assert {d["deliverable_type"] for d in store.get_deliverables_for_theme(bob_theme["id"])} == {"script_de"}


def test_scheduler_concurrency_and_catchup_use_recipient_scope(store, monkeypatch):
    barrier = None
    class BlockedPipeline:
        def __init__(self, **kwargs):
            pass
        async def run(self, interest):
            rid = store.create_run(interest["name"], interest["recipient_id"])["id"]
            await barrier.wait()
            store.update_run(rid, status="completed")
            return rid
    monkeypatch.setattr("newspipeline.scheduler.Pipeline", BlockedPipeline)
    monkeypatch.setattr(delivery, "dispatch_finished_run", lambda **kw: ("completed", {}))
    scheduler = SchedulerThread(store=store, get_api_key=lambda: "fixture-key")
    async def scenario():
        nonlocal barrier
        barrier = asyncio.Event()
        scheduler._lock = asyncio.Lock()
        alice = {"name": "Energy", "recipient_id": "alice"}
        bob = {"name": "Energy", "recipient_id": "bob"}
        assert await scheduler._launch({}, alice, reason="manual") is None
        assert await scheduler._launch({}, bob, reason="manual") is None
        assert "already in progress" in await scheduler._launch({}, alice, reason="manual")
        await asyncio.sleep(0)
        slot = datetime.now(UTC)
        assert scheduler._covering_run("Energy", slot, "alice") != scheduler._covering_run("Energy", slot, "bob")
        tasks = list(scheduler._running.values())
        barrier.set()
        await asyncio.gather(*tasks)
        assert not scheduler._running
    asyncio.run(scenario())
    assert not is_paused({"enable_script_de": True})
    assert is_paused({"enabled": False, "enable_summary": True})


def test_cli_requires_recipient_for_ambiguous_name(isolated_config, store, tmp_path, monkeypatch, capsys):
    from newspipeline.cli import _cmd_run
    settings = make_settings(1, tmp_path / "out")
    settings["recipients"] = [{"id": rid, "interests": [{"name": "Energy"}]} for rid in ("alice", "bob")]
    config.save_settings(settings)
    assert _cmd_run("Energy", False) == 1
    assert "ambiguous" in capsys.readouterr().err
    assert _cmd_run(None, True, "missing") == 1


def test_two_interest_delivery_times_are_independent_within_one_recipient(store, monkeypatch):
    seen = []
    monkeypatch.setattr(delivery, "deliver_run", lambda **kw: seen.append(kw["interest"]["name"]))
    now = datetime(2026, 10, 7, 8, tzinfo=UTC)
    class Clock:
        @staticmethod
        def now(tz):
            return now
    monkeypatch.setattr(delivery, "datetime", Clock)
    for name, time in (("Energy", "09:00"), ("Space", "12:00")):
        rid = store.create_run(name, "alice")["id"]
        store.update_run(rid, status="completed")
        delivery.dispatch_finished_run(run_id=rid, store=store, settings={"global": {"timezone": "UTC"}},
            interest={"name": name, "recipient_id": "alice", "delivery_schedule": {"start_time": time}})
    assert delivery.deliver_due(store, now.replace(hour=10)) == 1
    assert seen == ["Energy"]
    assert delivery.deliver_due(store, now.replace(hour=12)) == 1
    assert seen == ["Energy", "Space"]


def test_scheduler_drains_delivery_queue_without_llm_key(store, monkeypatch):
    import threading
    done = threading.Event()
    rid = store.create_run("Energy", "alice")["id"]
    store.update_run(rid, status="completed")
    store.queue_delivery(rid, "2026-01-01T00:00:00+00:00", {"interest": {"name": "Energy", "recipient_id": "alice"}, "settings": {}})
    store.claim_due_delivery("2026-01-02T00:00:00+00:00")  # interrupted previous process
    monkeypatch.setattr(delivery, "deliver_run", lambda **kwargs: done.set())
    scheduler = SchedulerThread(store=store, get_settings=lambda: {"interests": []}, get_api_key=lambda: "")
    scheduler.start()
    try:
        assert done.wait(5)
    finally:
        scheduler.stop()
    assert store.list_deliveries("alice")[0]["status"] == "completed"
