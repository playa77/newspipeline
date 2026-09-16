"""API surface tests for the JSON configuration API (D8): CRUD round-trips,
hand-rolled validation errors, trigger routing, and status reporting.

Uses the injected-fakes pattern: isolated config paths, fixture store, a
fake scheduler recording trigger calls. Tests never touch the real
config/settings.toml, .env, or data/news.db.
"""

# version: 0.3.0

import pytest

from newspipeline import config
from newspipeline.web.app import create_app

from conftest import make_settings


class FakeScheduler:
    """Records trigger_now calls; returns a canned error ("") = success."""

    def __init__(self, result: str = ""):
        self.result = result
        self.triggered: list[str] = []

    def __bool__(self):
        return True

    def trigger_now(self, name: str) -> str:
        self.triggered.append(name)
        return self.result


@pytest.fixture
def sched():
    return FakeScheduler()


@pytest.fixture
def api(isolated_config, store, sched, tmp_path):
    config.save_settings(make_settings(1, tmp_path / "out"))
    app = create_app(store=store, scheduler=sched)
    app.testing = True
    return app.test_client()


VALID_INTEREST = {
    "name": "Tech",
    "start_time": "06:30",
    "interval_hours": 12,
    "max_themes": 3,
    "enable_summary": True,
    "enable_script": False,
    "enable_script_de": False,
    "enable_brief": True,
    "delivery": ["file", "email"],
    "email_recipient": "me@example.com",
    "input_data_length_mode": "headers_only",
}


# ---- interests CRUD ----------------------------------------------------------


def test_interest_crud_round_trip(api):
    resp = api.post("/api/interests", json=VALID_INTEREST)
    assert resp.status_code == 201
    created = resp.get_json()["interest"]
    assert created["name"] == "Tech"
    assert created["paused"] is False
    assert created["target_summary_words"] == 600  # defaults applied

    listed = api.get("/api/interests").get_json()["interests"]
    assert [i["name"] for i in listed] == ["Tech"]

    one = api.get("/api/interests/Tech").get_json()["interest"]
    assert one["email_recipient"] == "me@example.com"
    assert api.get("/api/interests/Ghost").status_code == 404

    resp = api.put("/api/interests/Tech", json={
        "interval_hours": 4,
        "delivery": ["file"],
        "enable_brief": False,
        "feeds": [],  # explicit empty is fine; feeds are managed via routes
    })
    assert resp.status_code == 200
    updated = resp.get_json()["interest"]
    assert updated["interval_hours"] == 4
    assert updated["delivery"] == ["file"]
    assert updated["enable_brief"] is False
    # Untouched fields keep their stored values.
    assert updated["start_time"] == "06:30"

    # A fully-disabled interest reports paused.
    api.put("/api/interests/Tech", json={
        "enable_summary": False, "enable_brief": False,
    })
    assert api.get("/api/interests/Tech").get_json()["interest"]["paused"] is True

    assert api.delete("/api/interests/Tech").get_json() == {"deleted": "Tech"}
    assert api.get("/api/interests/Tech").status_code == 404
    assert config.load_settings()["interests"] == []


def test_created_interest_is_persisted_to_toml(api):
    api.post("/api/interests", json=VALID_INTEREST)
    stored = next(
        i for i in config.load_settings()["interests"] if i["name"] == "Tech"
    )
    assert stored["input_data_length_mode"] == "headers_only"
    assert stored["delivery"] == ["file", "email"]
    assert stored["feeds"] == []


def test_feed_add_remove_round_trip(api):
    api.post("/api/interests", json=VALID_INTEREST)
    resp = api.post("/api/interests/Tech/feeds", json={
        "name": "Hacker News", "url": "https://hnrss.org/frontpage",
    })
    assert resp.status_code == 201
    feeds = resp.get_json()["feeds"]
    assert len(feeds) == 1 and feeds[0]["category"] == ""

    api.post("/api/interests/Tech/feeds", json={
        "name": "Heise", "url": "https://heise.de/rss.xml", "category": "news",
    })
    stored = config.load_settings()["interests"][0]["feeds"]
    assert [f["name"] for f in stored] == ["Hacker News", "Heise"]

    resp = api.delete("/api/interests/Tech/feeds/0")
    assert [f["name"] for f in resp.get_json()["feeds"]] == ["Heise"]
    assert [f["name"] for f in config.load_settings()["interests"][0]["feeds"]] == \
        ["Heise"]

    assert api.delete("/api/interests/Tech/feeds/9").status_code == 404


def test_put_never_changes_the_name(api):
    api.post("/api/interests", json=VALID_INTEREST)
    resp = api.put("/api/interests/Tech", json={"name": "Renamed"})
    assert resp.status_code == 400
    assert "rename" in resp.get_json()["error"].lower()
    names = [i["name"] for i in config.load_settings()["interests"]]
    assert names == ["Tech"]


# ---- validation errors ---------------------------------------------------------


@pytest.mark.parametrize("payload,fragment", [
    ({"name": ""}, "name"),
    ({"name": "a/b"}, "'/'"),
    ({"name": "X", "start_time": "6am"}, "HH:MM"),
    ({"name": "X", "start_time": "25:00"}, "HH:MM"),
    ({"name": "X", "interval_hours": 0}, "between"),
    ({"name": "X", "interval_hours": 200}, "between"),
    ({"name": "X", "interval_hours": "12"}, "integer"),
    ({"name": "X", "delivery": []}, "delivery"),
    ({"name": "X", "delivery": ["carrier-pigeon"]}, "delivery"),
    ({"name": "X", "enable_summary": "yes"}, "boolean"),
    ({"name": "X", "input_data_length_mode": "all_of_it"}, "input_data_length_mode"),
    ({"name": "X", "nonexistent_field": 1}, "unknown field"),
])
def test_create_interest_validation_errors(api, payload, fragment):
    resp = api.post("/api/interests", json=payload)
    assert resp.status_code == 400
    assert fragment.lower() in resp.get_json()["error"].lower()
    assert config.load_settings()["interests"] == []


def test_duplicate_interest_name_conflict(api):
    api.post("/api/interests", json=VALID_INTEREST)
    resp = api.post("/api/interests", json={**VALID_INTEREST, "start_time": "07:00"})
    assert resp.status_code == 409


def test_malformed_json_body_rejected(api):
    resp = api.post(
        "/api/interests", data="not json",
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_missing_content_type_rejected(api):
    resp = api.put("/api/settings")
    assert resp.status_code == 400


# ---- settings --------------------------------------------------------------------


def test_settings_round_trip(api):
    body = api.get("/api/settings").get_json()
    assert body["global"]["timezone"]  # from make_settings defaults
    assert body["scheduler_running"] is True

    resp = api.put("/api/settings", json={
        "global": {
            "default_model": "vendor/model-b",
            "models": {"analysis": "vendor/analyzer"},
            "reasoning": {"effort": "high"},
        },
        "smtp": {"host": "smtp.gmail.com", "port": 587, "user": "bot@gmail.com"},
        "user": {"email": "owner@private.example"},
    })
    assert resp.status_code == 200
    stored = config.load_settings()
    assert stored["global"]["default_model"] == "vendor/model-b"
    assert stored["global"]["models"]["analysis"] == "vendor/analyzer"
    assert stored["global"]["reasoning"]["effort"] == "high"
    assert stored["smtp"]["host"] == "smtp.gmail.com"
    assert stored["smtp"]["port"] == 587
    assert stored["user"]["email"] == "owner@private.example"
    # Partial update: untouched global fields survive.
    assert stored["global"]["timezone"] == body["global"]["timezone"]


@pytest.mark.parametrize("payload,fragment", [
    ({"global": {"default_model": 5}}, "string"),
    ({"smtp": {"port": 0}}, "between"),
    ({"smtp": {"port": "587"}}, "integer"),
    ({"global": {"reasoning": {"effort": "maximum"}}}, "effort"),
    ({"global": {"bogus": 1}}, "unknown global field"),
    ({"smtp": {"password": "x"}}, "unknown smtp field"),
    ({"secrets": {}}, "unknown section"),
    ({"global": "nope"}, "object"),
])
def test_settings_validation_errors(api, payload, fragment):
    resp = api.put("/api/settings", json=payload)
    assert resp.status_code == 400
    assert fragment.lower() in resp.get_json()["error"].lower()


# ---- trigger ---------------------------------------------------------------------


def test_trigger_success_and_error_routing(api, sched):
    api.post("/api/interests", json=VALID_INTEREST)

    resp = api.post("/api/interests/Tech/trigger", json={})
    assert resp.status_code == 202
    assert sched.triggered == ["Tech"]

    sched.result = "Interest 'Tech' is paused"
    assert api.post("/api/interests/Tech/trigger").status_code == 409

    sched.result = "Interest 'Ghost' not found"
    resp = api.post("/api/interests/Ghost/trigger")
    assert resp.status_code == 404


def test_trigger_without_scheduler_is_503(isolated_config, store, tmp_path):
    config.save_settings(make_settings(1, tmp_path / "out"))
    app = create_app(store=store, scheduler=None)
    app.testing = True
    resp = app.test_client().post("/api/interests/X/trigger")
    assert resp.status_code == 503
    assert "Scheduler" in resp.get_json()["error"]


# ---- status ------------------------------------------------------------------------


def test_status_reports_recent_runs_and_paused_state(api, store):
    api.post("/api/interests", json=VALID_INTEREST)
    rid = store.create_run("Tech")["id"]
    store.update_run(rid, status="failed", current_stage="analyze", error="boom")

    payload = api.get("/api/status").get_json()
    entry = next(i for i in payload["interests"] if i["name"] == "Tech")
    assert entry["paused"] is False
    assert len(entry["recent_runs"]) == 1
    run = entry["recent_runs"][0]
    assert run["id"] == rid
    assert run["status"] == "failed"
    assert run["current_stage"] == "analyze"
    assert run["error"] == "boom"


def test_status_limit_parameter(api, store):
    api.post("/api/interests", json=VALID_INTEREST)
    for _ in range(8):
        rid = store.create_run("Tech")["id"]
        store.update_run(rid, status="completed")
    runs = api.get("/api/status?limit=3").get_json()["interests"][0]["recent_runs"]
    assert len(runs) == 3
