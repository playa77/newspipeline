"""Guard test for invariant G8: first-class rename preserves run history
(DECISIONS.md D11, amending D5).

Renaming through the API must (a) repoint every runs row to the new name,
(b) atomically rewrite settings.toml, in that order; a simulated crash
between the two stores leaves a half-applied rename from which RE-RUNNING
the rename converges.
"""

# version: 0.3.0

import pytest

from newspipeline import config
from newspipeline.web.app import create_app

from conftest import make_settings


@pytest.fixture
def env(isolated_config, store, tmp_path):
    config.save_settings({
        **make_settings(1, tmp_path / "out"),
        "interests": [{
            "name": "Old Name", "start_time": "04:00", "interval_hours": 24,
            "enable_summary": True, "enable_script": False, "enable_brief": False,
            "delivery": ["file"], "feeds": [],
        }],
    })
    # History under the old name — the join key is runs.interest_name (D5).
    for stage in ("scrape", "analyze"):
        rid = store.create_run("Old Name")["id"]
        store.update_run(rid, status="failed" if stage == "analyze" else "completed",
                         current_stage=stage)

    class _Sched:
        def __bool__(self):
            return True

        def trigger_now(self, name):
            return ""

    app = create_app(store=store, scheduler=_Sched())
    app.testing = True
    return {"client": app.test_client(), "store": store}


def _toml_names():
    return [i["name"] for i in config.load_settings()["interests"]]


def test_rename_preserves_run_history(env):
    client, store = env["client"], env["store"]

    resp = client.post("/api/interests/Old Name/rename", json={"to": "New Name"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["renamed"] is True
    assert body["runs_repointed"] == 2

    # (a) Every runs row now points at the new name; nothing under the old.
    assert sorted(r["id"] for r in store.list_runs("New Name")) == [1, 2]
    assert store.list_runs("Old Name") == []
    assert store.get_run(1)["current_stage"] == "scrape"  # rows intact

    # (b) settings.toml carries the renamed interest only.
    assert _toml_names() == ["New Name"]

    # New runs land under the new name (the scheduler self-corrects on its
    # next tick since settings are re-read per tick).
    rid = store.create_run("New Name")["id"]
    assert len(store.list_runs("New Name")) == 3
    assert rid == 3  # history was repointed, not recreated


def test_crash_between_stores_converges_on_rerun(env):
    """Simulate a crash after the SQLite write but before the settings.toml
    rewrite: state is half-applied; re-running the rename converges."""
    client, store = env["client"], env["store"]

    real_save = config.save_settings

    def crashing_save(settings):
        raise RuntimeError("simulated crash between step 2 and step 3")

    config.save_settings = crashing_save
    try:
        with pytest.raises(RuntimeError):
            client.post("/api/interests/Old Name/rename", json={"to": "New Name"})
    finally:
        config.save_settings = real_save

    # Half-applied: SQLite repointed, TOML still under the old name.
    assert store.list_runs("Old Name") == []
    assert sorted(r["id"] for r in store.list_runs("New Name")) == [1, 2]
    assert _toml_names() == ["Old Name"]

    # Documented remedy (D11): re-run the rename. Step 2 is idempotent
    # (zero rows matched this time), then the TOML write lands.
    resp = client.post("/api/interests/Old Name/rename", json={"to": "New Name"})
    assert resp.status_code == 200
    assert resp.get_json()["runs_repointed"] == 0
    assert _toml_names() == ["New Name"]
    assert sorted(r["id"] for r in store.list_runs("New Name")) == [1, 2]


def test_retry_after_completed_rename_reports_convergence(env):
    client, _ = env["client"], env["store"]
    first = client.post("/api/interests/Old Name/rename", json={"to": "New Name"})
    assert first.status_code == 200
    # A lost response or double click retries the same operation: not an
    # error — report the already-converged state.
    retry = client.post("/api/interests/Old Name/rename", json={"to": "New Name"})
    assert retry.status_code == 200
    assert retry.get_json()["already_renamed"] is True
    assert _toml_names() == ["New Name"]


def test_rename_conflicts_and_validation(env):
    client, store = env["client"], env["store"]
    config.save_settings({
        **{k: v for k, v in config.load_settings().items() if k != "interests"},
        "interests": [
            *config.load_settings()["interests"],
            {"name": "Taken", "feeds": []},
        ],
    })

    # Target exists -> conflict, and NOTHING changes.
    resp = client.post("/api/interests/Old Name/rename", json={"to": "Taken"})
    assert resp.status_code == 409
    assert _toml_names() == ["Old Name", "Taken"]
    assert sorted(r["id"] for r in store.list_runs("Old Name")) == [1, 2]

    for bad in ({}, {"to": ""}, {"to": "   "}):
        assert client.post("/api/interests/Old Name/rename", json=bad).status_code == 400
    assert client.post("/api/interests/Ghost/rename", json={"to": "X"}).status_code == 404
    assert client.post("/api/interests/Old Name/rename",
                       json={"to": "Old Name"}).status_code == 400
    # Nothing moved.
    assert sorted(r["id"] for r in store.list_runs("Old Name")) == [1, 2]
