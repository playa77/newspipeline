"""Guard test for invariant G1: the API key is never written to
settings.toml, never logged, and never returned by any web endpoint.

Extended for the JSON API (D8): NO response body of ANY endpoint may ever
contain a secret value (LLM API key or SMTP password).
"""

# version: 0.3.0

import asyncio
import logging

import pytest

from newspipeline.pipeline import Pipeline
from newspipeline.web.app import create_app

from conftest import make_interest, make_settings


def test_api_key_not_persisted_or_exposed(isolated_config, store, tmp_path):
    config = isolated_config
    env_path = tmp_path / ".env"
    settings_path = tmp_path / "settings.toml"

    key = "sk-g1-guard-key-0123456789"
    config.set_api_key(key)
    assert key in env_path.read_text()
    assert oct(env_path.stat().st_mode)[-3:] == "600"
    assert config.get_api_key() == key

    # The write-path guard refuses to persist a secret into settings.toml.
    settings = config.load_settings()
    settings["global"]["default_model"] = key  # simulated leak attempt
    with pytest.raises(ValueError, match="secret"):
        config.save_settings(settings)
    assert not settings_path.exists() or key not in settings_path.read_text()

    # A normal save never contains the key.
    config.save_settings(config.load_settings())
    assert key not in settings_path.read_text()


def test_no_api_response_body_ever_contains_a_secret(
    isolated_config, store, tmp_path,
):
    """G1 over the whole API surface: seed both secrets, then exercise
    every endpoint — reads and mutations — and scan every response body."""
    config = isolated_config
    api_key = "sk-g1-api-surface-key-abcdef"
    smtp_password = "g1-app-password-xyzzy-9931"
    config.set_api_key(api_key)
    config.set_smtp_password(smtp_password)
    secrets = (api_key, smtp_password)

    settings_path = tmp_path / "settings.toml"
    config.save_settings({
        **make_settings(1, tmp_path / "out"),
        "interests": [make_interest(1, ["file"], name="surf")],
    })

    class _FakeScheduler:
        def __bool__(self):
            return True

        def trigger_now(self, name):
            return ""

    app = create_app(store=store, scheduler=_FakeScheduler())
    app.testing = True
    client = app.test_client()

    # Reads...
    reads = [
        ("get", "/api/settings", None),
        ("get", "/api/interests", None),
        ("get", "/api/interests/surf", None),
        ("get", "/api/status", None),
    ]
    # ...and every mutating route that returns a body (errors included).
    writes = [
        ("put", "/api/settings", {"global": {"default_model": "m"}}),
        ("post", "/api/interests",
         {"name": "surf2", "start_time": "05:00", "interval_hours": 2}),
        ("put", "/api/interests/surf", {"max_themes": 3}),
        ("post", "/api/interests/surf/feeds",
         {"name": "f", "url": "http://example.com/rss.xml"}),
        ("delete", "/api/interests/surf/feeds/0", None),
        ("post", "/api/interests/surf/rename", {"to": "surf-renamed"}),
        ("post", "/api/interests/surf/trigger", {}),
        ("delete", "/api/interests/surf-renamed", None),
        # Error paths must not leak either.
        ("post", "/api/interests", {"name": ""}),
        ("put", "/api/interests/ghost", {"max_themes": 3}),
        ("post", "/api/interests/surf-rename-again/rename", {"to": ""}),
    ]
    for method, path, payload in reads + writes:
        kwargs = {} if payload is None else {"json": payload}
        resp = getattr(client, method)(path, **kwargs)
        body = resp.get_data(as_text=True)
        for secret in secrets:
            assert secret not in body, f"secret leaked via {method.upper()} {path}"
        # Sanity: the exercised routes actually ran (not blocked at the
        # gate, which would make the scan vacuous).
        assert resp.status_code < 500, f"{method.upper()} {path} -> {resp.status_code}"

    # The configured-state flag is exposed instead of the value itself.
    body = client.get("/api/settings").get_json()
    assert body["api_key_configured"] is True

    # And neither secret ever landed in settings.toml through any route.
    assert api_key not in settings_path.read_text()
    assert smtp_password not in settings_path.read_text()


def test_api_key_only_in_authorization_header(
    isolated_config, fixture_server, store, tmp_path, caplog,
):
    """The key reaches the provider exclusively as a Bearer header (G1):
    never in the URL, never in the request body, never in logs."""
    key = "sk-g1-header-key-abcdefghij"
    settings = make_settings(fixture_server.server_port, tmp_path / "out")
    interest = make_interest(fixture_server.server_port, ["file"])

    with caplog.at_level(logging.INFO):
        pipeline = Pipeline(store=store, settings=settings, api_key=key)
        run_id = asyncio.run(pipeline.run(interest))
    assert store.get_run(run_id)["status"] == "completed"

    assert fixture_server.captured_llm_requests, "expected LLM calls"
    for request in fixture_server.captured_llm_requests:
        assert request["headers"].get("Authorization") == f"Bearer {key}"
        assert key not in request["path"]
        assert key not in str(request["body"])
    for record in caplog.records:
        assert key not in record.getMessage()
