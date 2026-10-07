"""Inheritance, scoped API updates, and isolation of recipient configuration."""

# version: 0.6.0

from copy import deepcopy
import pytest
from newspipeline import config
from newspipeline.recipients import effective_interests, resolve_recipients
from newspipeline.web.app import create_app
from conftest import make_settings


def configuration():
    return {
        "interests": [{"name": "legacy", "enable_summary": True}],
        "recipient_defaults": {
            "interest_defaults": {"target_summary_words": 300, "delivery_schedule": {"start_time": "09:00", "timezone": "UTC"}},
            "interests": [{"name": "Energy", "feeds": [{"name": "A", "url": "https://example.com/a"}]}],
        },
        "recipients": [
            {"id": "alice", "email": "alice@example.com", "interest_defaults": {"summary_detail": "detailed"}},
            {"id": "bob", "parent": "alice", "email": "bob@example.com",
             "interests": [{"name": "Energy", "enable_summary": False, "target_brief_words": 100,
                            "delivery_schedule": {"start_time": "12:00"}, "feeds": []}]},
        ],
    }


def test_inheritance_is_sparse_recursive_and_independent():
    settings = configuration()
    before = deepcopy(settings)
    alice, bob = effective_interests(settings, "alice")[0], effective_interests(settings, "bob")[0]
    assert alice["target_summary_words"] == bob["target_summary_words"] == 300
    assert alice["summary_detail"] == bob["summary_detail"] == "detailed"
    assert alice["enable_summary"] is True and bob["enable_summary"] is False
    assert len(alice["feeds"]) == 1 and bob["feeds"] == []
    assert bob["delivery_schedule"] == {"start_time": "12:00", "timezone": "UTC"}
    assert alice["recipient_email"] == "alice@example.com"
    assert bob["recipient_email"] == "bob@example.com"
    alice["feeds"].clear()
    assert settings == before
    settings["recipient_defaults"]["interest_defaults"]["target_summary_words"] = 800
    assert effective_interests(settings, "bob")[0]["target_summary_words"] == 800
    settings["recipients"][1]["interests"][0]["delivery_schedule"] = {}
    assert effective_interests(settings, "bob")[0]["delivery_schedule"] == {}
    settings["recipients"][1]["interests"][0]["enabled"] = False
    assert effective_interests(settings, "bob") == []
    assert len(effective_interests(settings, "alice")) == 1
    assert effective_interests(settings, "")[0]["name"] == "legacy"


@pytest.mark.parametrize("recipients", [
    [{"id": "a", "parent": "missing"}],
    [{"id": "a", "parent": "b"}, {"id": "b", "parent": "a"}],
    [{"id": "a"}, {"id": "a"}],
    [{"id": "a", "interests": [{"name": "X"}, {"name": "X"}]}],
])
def test_invalid_inheritance_rejected(recipients):
    with pytest.raises(ValueError):
        resolve_recipients({"recipients": recipients})


@pytest.fixture
def api(isolated_config, store, tmp_path):
    settings = {**make_settings(1, tmp_path / "out"), **configuration()}
    config.save_settings(settings)
    app = create_app(store=store)
    app.testing = True
    return app.test_client()


def test_scoped_interest_override_reset_and_inherited_feed_isolation(api):
    path = "/api/recipients/bob/interests/Energy"
    assert api.put(path, json={"target_summary_words": 123}).status_code == 200
    assert api.get(path).json["interest"]["target_summary_words"] == 123
    assert api.get("/api/recipients/alice/interests/Energy").json["interest"]["target_summary_words"] == 300
    raw = config.load_settings()["recipients"][1]["interests"][0]
    assert "summary_detail" not in raw
    assert api.put(path, json={"inherit": ["target_summary_words", "feeds"]}).status_code == 200
    assert api.get(path).json["interest"]["target_summary_words"] == 300
    assert api.delete(path + "/feeds/0").status_code == 200
    assert api.get(path).json["interest"]["feeds"] == []
    assert len(api.get("/api/recipients/alice/interests/Energy").json["interest"]["feeds"]) == 1
    assert api.delete(path).status_code == 200
    assert api.get(path).status_code == 404
    assert api.post("/api/recipients/bob/interests", json={"name": "Energy"}).status_code == 201


def test_recipient_crud_and_invalid_updates_leave_config_unchanged(api):
    assert api.post("/api/recipients", json={"id": "carol", "parent": "bob"}).status_code == 201
    before = config.load_settings()
    assert api.put("/api/recipients/alice", json={"parent": "carol"}).status_code == 400
    assert config.load_settings() == before
    assert api.put("/api/recipients/alice", json={"id": "new"}).status_code == 400
    assert api.delete("/api/recipients/bob").status_code == 409
    assert api.delete("/api/recipients/carol").status_code == 200
    assert api.post("/api/recipients", json={"id": "alice"}).status_code == 409
    assert api.post("/api/recipients", json={"id": "../outside"}).status_code == 400
    assert api.put("/api/recipient-defaults", json={"NEWS_API_KEY": "not-allowed"}).status_code == 400


@pytest.mark.parametrize("payload", [
    {"summary_detail": "unknown"}, {"delivery_schedule": "09:00"},
    {"delivery_schedule": {"start_time": "25:00"}},
    {"delivery_schedule": {"timezone": "Nowhere/Invalid"}},
    {"delivery_schedule": {"interval_hours": True}},
    {"delivery_schedule": {"interval_hours": 25}},
    {"inherit": ["name"]},
])
def test_scoped_configuration_validation(api, payload):
    before = config.load_settings()
    response = api.put("/api/recipients/alice/interests/Energy", json=payload)
    assert response.status_code == 400
    assert config.load_settings() == before


def test_scoped_history_trigger_and_rename(api, store):
    a = store.create_run("Energy", "alice")
    b = store.create_run("Energy", "bob")
    store.update_run(a["id"], status="completed")
    store.update_run(b["id"], status="completed")
    assert [r["id"] for r in api.get("/api/recipients/bob/status").json["interests"][0]["recent_runs"]] == [b["id"]]
    assert api.post("/api/recipients/bob/interests/Energy/trigger").status_code == 503
    response = api.post("/api/recipients/bob/interests/Energy/rename", json={"to": "Power"})
    assert response.status_code == 200
    assert store.list_runs("Energy", recipient_id="alice")[0]["id"] == a["id"]
    assert store.list_runs("Power", recipient_id="bob")[0]["id"] == b["id"]
    assert api.get("/api/recipients/bob/interests/Energy").status_code == 404
    assert api.get("/api/recipients/bob/interests/Power").status_code == 200


def test_parent_rename_is_atomic_when_descendant_has_destination_history(api, store):
    for rid in ("alice", "bob"):
        run = store.create_run("Energy", rid)
        store.update_run(run["id"], status="completed")
    retained = store.create_run("Power", "bob")
    store.update_run(retained["id"], status="completed")
    before = config.load_settings()
    response = api.post("/api/recipients/alice/interests/Energy/rename", json={"to": "Power"})
    assert response.status_code == 409
    assert config.load_settings() == before
    assert len(store.list_runs("Energy", recipient_id="alice")) == 1
    assert len(store.list_runs("Energy", recipient_id="bob")) == 1
    assert store.list_runs("Power", recipient_id="alice") == []


def test_parent_rename_moves_inherited_child_history(api, store):
    for rid in ("alice", "bob"):
        run = store.create_run("Energy", rid)
        store.update_run(run["id"], status="completed")
    response = api.post("/api/recipients/alice/interests/Energy/rename", json={"to": "Power"})
    assert response.status_code == 200
    assert response.json["runs_repointed"] == 2
    assert api.get("/api/recipients/bob/interests/Energy").status_code == 404
    assert api.get("/api/recipients/bob/interests/Power").status_code == 200
    assert len(store.list_runs("Power", recipient_id="bob")) == 1
    renamed = api.get("/api/recipients/bob/interests/Power").json["interest"]
    assert renamed["enable_summary"] is False
    assert renamed["target_brief_words"] == 100
    assert renamed["feeds"] == []


def test_cannot_rename_interest_during_run(api, store):
    store.create_run("Energy", "bob")
    response = api.post("/api/recipients/bob/interests/Energy/rename", json={"to": "Power"})
    assert response.status_code == 409
    assert api.get("/api/recipients/bob/interests/Energy").status_code == 200
