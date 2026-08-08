"""Guard test for invariant G1: the API key is never written to
settings.toml, never logged, and never returned by any web endpoint.
"""

# version: 0.1.0

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

    # The web UI shows only the configured state — never the value, on any
    # page render, including after re-entering a new key.
    client = create_app(store=store, scheduler=None).test_client()
    assert key not in client.get("/").get_data(as_text=True)
    new_key = "sk-g1-replacement-key-9876543210"
    html = client.post(
        "/settings/api-key", data={"api_key": new_key}, follow_redirects=True,
    ).get_data(as_text=True)
    assert new_key not in html
    assert "configured" in html
    assert new_key not in settings_path.read_text()
    assert config.get_api_key() == new_key


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
