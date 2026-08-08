"""Configuration loading and saving.

Two sources, strictly separated:

- ``config/settings.toml`` — non-secret global settings, interests, feeds.
  Human-editable; the web UI reads and writes it. Version-stable external
  contract (see DECISIONS.md D3).
- ``.env`` — secrets only (LLM API key, SMTP password). Parsed here without
  an extra dependency. Secrets are never written to settings.toml, never
  logged, and never returned to the UI (invariant G1).
"""

# version: 0.1.0

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.toml"
SETTINGS_EXAMPLE_PATH = PROJECT_ROOT / "config" / "settings.example.toml"
ENV_PATH = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "news.db"

DEFAULT_SETTINGS: dict[str, Any] = {
    "global": {
        "api_base_url": "https://openrouter.ai/api/v1",
        "default_model": "deepseek/deepseek-v4-flash-latest",
        "timezone": "Europe/Berlin",
        "output_dir": "output",
        "models": {"analysis": "", "generation": "", "brief": ""},
        "reasoning": {"effort": ""},
    },
    "smtp": {"host": "localhost", "port": 25, "user": "", "sender": "newspipeline@localhost"},
    "user": {"email": ""},
    "interests": [],
}


def load_settings() -> dict[str, Any]:
    """Load settings.toml, falling back to defaults for missing keys.

    Returns an empty-interests default structure when the file does not
    exist yet (first run before `newspipeline init`).
    """
    if not SETTINGS_PATH.exists():
        logger.info("settings.toml not found at %s — using defaults", SETTINGS_PATH)
        return {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in DEFAULT_SETTINGS.items()}
    with SETTINGS_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    merged = {**DEFAULT_SETTINGS, **data}
    for key, default_sub in DEFAULT_SETTINGS.items():
        if isinstance(default_sub, dict):
            merged[key] = {**default_sub, **data.get(key, {})}
    return merged


def save_settings(settings: dict[str, Any]) -> None:
    """Write settings.toml. Secrets are refused — G1 guard at the write path."""
    serialized = repr(settings)
    for secret in (get_api_key(), get_smtp_password()):
        if secret and secret in serialized:
            raise ValueError("Refusing to persist a secret value to settings.toml")
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(tomli_w.dumps(settings), encoding="utf-8")
    logger.info("settings.toml saved (%d interests)", len(settings.get("interests", [])))


def load_env() -> None:
    """Parse .env into os.environ (without overriding already-set variables)."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def get_api_key() -> str:
    """Return the LLM API key from the environment (.env), or "" if unset."""
    load_env()
    return os.environ.get("NEWS_API_KEY", "")


def get_smtp_password() -> str:
    """Return the SMTP password from the environment (.env), or "" if unset."""
    load_env()
    return os.environ.get("NEWS_SMTP_PASSWORD", "")


def set_api_key(key: str) -> None:
    """Write the API key to .env, creating the file if needed.

    Overwrites an existing NEWS_API_KEY line; preserves all other lines.
    """
    _upsert_env("NEWS_API_KEY", key)


def set_smtp_password(password: str) -> None:
    """Write the SMTP password to .env (see set_api_key)."""
    _upsert_env("NEWS_SMTP_PASSWORD", password)


def _upsert_env(key: str, value: str) -> None:
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            replaced = True
    if not replaced:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_PATH.chmod(0o600)
    # load_env() only setdefaults into os.environ, so without this the
    # running process would keep serving the stale value until restart.
    os.environ[key] = value
    logger.info(".env updated — key %s written (value not logged)", key)


def has_api_key() -> bool:
    """True when an API key is configured. Safe to expose in the UI."""
    return bool(get_api_key())
