"""JSON configuration API — the server's management surface (D8).

The desktop client (`client/newspipeline-client`) reaches these endpoints
through an SSH local forward; the trust boundary is the loopback bind plus
the tunnel (D7). The 0.2.0 Host/Origin gates are kept as defense-in-depth.

Endpoints (all JSON; errors are ``{"error": "<message>"}`` with a matching
status code):
  GET    /api/settings                      global + smtp + user sections
  PUT    /api/settings                      partial update of those sections
  GET    /api/status                        recent runs per interest
  GET    /api/interests                     all interests (+ paused flag)
  POST   /api/interests                     create an interest
  GET    /api/interests/<name>              one interest
  PUT    /api/interests/<name>              update (never renames — D11)
  DELETE /api/interests/<name>              remove (orphans history — D11)
  POST   /api/interests/<name>/rename       first-class rename (D11)
  POST   /api/interests/<name>/trigger      run now via the scheduler thread
  POST   /api/interests/<name>/feeds        add a feed
  DELETE /api/interests/<name>/feeds/<i>    remove feed i

Feeds are add/remove only (WP7 convention); editing a feed is remove+add.
Every mutating route holds ``config.mutation_lock`` across its
load→mutate→save cycle. Secrets never appear in any request or response
body (G1): SMTP host/port/user live in settings.toml, the passwords stay
in .env on the VPS and are managed over SSH, not HTTP.
"""

# version: 0.5.0

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, jsonify, request

from newspipeline import __version__, config
from newspipeline.scheduler import is_paused
from newspipeline.store import Store

logger = logging.getLogger(__name__)

# Hosts this API may be reached under. The server binds 127.0.0.1 behind
# an SSH tunnel and has no auth; enforcing these names on every request
# closes drive-by CSRF and DNS rebinding (reads included) from web pages
# open in a browser on the VPS or tunneled desktop.
_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_LENGTH_MODES = ("full_article", "headers_only", "word_count")
_DELIVERY_MODES = ("file", "email")
_EFFORT_LEVELS = ("", "low", "medium", "high")
_STAGES = ("analysis", "generation", "brief")

# Defaults applied on creation; every key stays individually editable.
_INTEREST_DEFAULTS: dict[str, Any] = {
    "start_time": "04:00",
    "interval_hours": 24,
    "max_themes": 5,
    "enable_summary": True,
    "enable_script": False,
    "enable_script_de": False,
    "enable_brief": True,
    # Per-theme result emails. False = the daily brief is this interest's
    # only mail; its themes are still generated and archived either way.
    "email_themes": True,
    "target_summary_words": 600,
    "target_script_words": 1250,
    "target_script_de_words": 1250,
    "target_brief_words": 690,
    "delivery": ["file"],
    "email_recipient": "",
    "input_data_length_mode": "full_article",
    "input_word_count": 256,
    "feeds": [],
}

_INT_RANGES: dict[str, tuple[int, int]] = {
    "interval_hours": (1, 168),
    "max_themes": (1, 100),
    "target_summary_words": (1, 100_000),
    "target_script_words": (1, 100_000),
    "target_script_de_words": (1, 100_000),
    "target_brief_words": (1, 100_000),
    "input_word_count": (1, 100_000),
}


def _find_interest(settings: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next(
        (i for i in settings.get("interests", []) if i.get("name") == name), None,
    )


def _host_is_loopback(host_header: str) -> bool:
    """True when a Host/Origin netloc is this app's own loopback binding."""
    name = (urlsplit(f"//{host_header}", scheme="http").hostname or "").lower()
    return name in _LOOPBACK_NAMES


def _err(message: str, status: int):
    return jsonify({"error": message}), status


def _json_body() -> dict[str, Any] | None:
    """Parse the request body as a JSON object, or None when malformed."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _validate_feed(data: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(data, dict):
        return None, "each feed must be an object with non-empty 'name' and 'url'"
    unknown = set(data) - {"name", "url", "category"}
    if unknown:
        return None, f"unknown feed field(s): {', '.join(sorted(unknown))}"
    name = data.get("name")
    url = data.get("url")
    if not isinstance(name, str) or not name.strip():
        return None, "feed 'name' must be a non-empty string"
    if not isinstance(url, str) or not url.strip():
        return None, "feed 'url' must be a non-empty string"
    category = data.get("category", "")
    if not isinstance(category, str):
        return None, "feed 'category' must be a string"
    return {"name": name.strip(), "url": url.strip(), "category": category.strip()}, None


def _validate_interest(
    data: Any, *, creating: bool, base: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate an interest payload into a full interest dict.

    Creation validates over the defaults; updates merge over the stored
    interest. Returns (interest, error_message).
    """
    if not isinstance(data, dict):
        return None, "request body must be a JSON object"
    allowed = set(_INTEREST_DEFAULTS) | {"name"}
    unknown = set(data) - allowed
    if unknown:
        return None, f"unknown field(s): {', '.join(sorted(unknown))}"

    interest = dict(base if base is not None else _INTEREST_DEFAULTS)

    if creating or "name" in data:
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            return None, "'name' must be a non-empty string"
        if "/" in name:
            # Names are URL path segments in the API; a slash would make
            # the interest unreachable through its own routes.
            return None, "'name' must not contain '/'"
        interest["name"] = name.strip()

    for key in ("start_time", "email_recipient"):
        if key in data:
            if not isinstance(data[key], str):
                return None, f"{key!r} must be a string"
            interest[key] = data[key].strip()
    if creating or "start_time" in data:
        m = _TIME_RE.match(interest["start_time"])
        if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59):
            return None, f"'start_time' must be HH:MM (got {interest['start_time']!r})"
    if "input_data_length_mode" in data:
        if data["input_data_length_mode"] not in _LENGTH_MODES:
            return None, f"'input_data_length_mode' must be one of {_LENGTH_MODES}"
        interest["input_data_length_mode"] = data["input_data_length_mode"]
    if "delivery" in data:
        modes = data["delivery"]
        if (
            not isinstance(modes, list)
            or not modes
            or any(m not in _DELIVERY_MODES for m in modes)
            or len(set(modes)) != len(modes)
        ):
            return None, f"'delivery' must be a non-empty list drawn from {_DELIVERY_MODES}"
        interest["delivery"] = [m for m in _DELIVERY_MODES if m in modes]
    for key in _INT_RANGES:
        if key in data:
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int):
                return None, f"{key!r} must be an integer"
            low, high = _INT_RANGES[key]
            if not low <= value <= high:
                return None, f"{key!r} must be between {low} and {high}"
            interest[key] = value
    for key in (
        "enable_summary", "enable_script", "enable_script_de", "enable_brief",
        "email_themes",
    ):
        if key in data:
            if not isinstance(data[key], bool):
                return None, f"{key!r} must be a boolean"
            interest[key] = data[key]
    if "feeds" in data:
        if not isinstance(data["feeds"], list):
            return None, "'feeds' must be a list"
        feeds: list[dict[str, Any]] = []
        for raw in data["feeds"]:
            feed, err = _validate_feed(raw)
            if err:
                return None, err
            feeds.append(feed)
        interest["feeds"] = feeds
    return interest, None


def _validate_settings_update(
    data: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a PUT /api/settings payload into a section->patch mapping."""
    patch: dict[str, dict[str, Any]] = {}
    unknown = set(data) - {"global", "smtp", "user"}
    if unknown:
        return None, f"unknown section(s): {', '.join(sorted(unknown))}"

    if "global" in data:
        g = data["global"]
        if not isinstance(g, dict):
            return None, "'global' must be an object"
        bad = set(g) - {
            "api_base_url", "default_model", "timezone", "output_dir",
            "models", "reasoning",
        }
        if bad:
            return None, f"unknown global field(s): {', '.join(sorted(bad))}"
        for key in ("api_base_url", "default_model", "timezone", "output_dir"):
            if key in g and not isinstance(g[key], str):
                return None, f"global.{key} must be a string"
        if "models" in g:
            models = g["models"]
            if not isinstance(models, dict):
                return None, "global.models must be an object"
            bad = set(models) - set(_STAGES)
            if bad:
                return None, f"unknown model stage(s): {', '.join(sorted(bad))}"
            for stage in models:
                if not isinstance(models[stage], str):
                    return None, f"global.models.{stage} must be a string"
        if "reasoning" in g:
            reasoning = g["reasoning"]
            if not isinstance(reasoning, dict) or set(reasoning) - {"effort"}:
                return None, "global.reasoning must be an object with only 'effort'"
            effort = reasoning.get("effort", "")
            if effort not in _EFFORT_LEVELS:
                return None, f"global.reasoning.effort must be one of {[e for e in _EFFORT_LEVELS if e]}"
        patch["global"] = g

    if "smtp" in data:
        s = data["smtp"]
        if not isinstance(s, dict):
            return None, "'smtp' must be an object"
        bad = set(s) - {"host", "port", "user", "sender"}
        if bad:
            return None, f"unknown smtp field(s): {', '.join(sorted(bad))}"
        for key in ("host", "user", "sender"):
            if key in s and not isinstance(s[key], str):
                return None, f"smtp.{key} must be a string"
        if "port" in s:
            port = s["port"]
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                return None, "smtp.port must be an integer between 1 and 65535"
        patch["smtp"] = s

    if "user" in data:
        u = data["user"]
        if not isinstance(u, dict) or set(u) - {"email"}:
            return None, "'user' must be an object with only 'email'"
        if "email" in u and not isinstance(u["email"], str):
            return None, "user.email must be a string"
        patch["user"] = u
    return patch, None


def create_app(store: Store | None = None, scheduler: Any = None) -> Flask:
    app = Flask(__name__)
    app.config["STORE"] = store if store is not None else Store()
    app.config["SCHEDULER"] = scheduler

    # JSON-only surface, errors included: Flask's default HTML error pages
    # would leak a web-shaped endpoint to clients expecting structured
    # responses.
    @app.errorhandler(404)
    def _not_found(_e):
        return _err("No such endpoint", 404)

    @app.errorhandler(405)
    def _method_not_allowed(_e):
        return _err("Method not allowed on this endpoint", 405)

    @app.errorhandler(500)
    def _internal_error(_e):
        return _err("Internal server error", 500)

    @app.before_request
    def _reject_cross_site_requests():
        """Every request must arrive under a loopback Host; mutations must
        additionally carry a loopback Origin when one is present.

        Defense-in-depth behind the SSH tunnel (D7): a malicious web page
        can always SEND a request to 127.0.0.1 — what it cannot do is forge
        the Host or Origin headers. A non-loopback Host is a DNS-rebinding
        attempt and is rejected for reads and writes alike. No Origin
        header is allowed (curl, tooling); a loopback Origin hostname with
        any port is accepted. Safe methods are exempt from the Origin
        check; anything else is origin-gated by default.
        """
        if not _host_is_loopback(request.host):
            return _err("Rejected: non-local Host header", 403)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        origin = request.headers.get("Origin", "")
        if origin:
            o = urlsplit(origin)
            if (o.hostname or "").lower() not in _LOOPBACK_NAMES:
                return _err("Rejected: cross-site request", 403)
        return None

    # ---- settings ----------------------------------------------------------

    @app.get("/api/settings")
    def get_settings():
        with config.mutation_lock:
            settings = config.load_settings()
        return jsonify({
            "global": settings.get("global", {}),
            "smtp": settings.get("smtp", {}),
            "user": settings.get("user", {}),
            "api_key_configured": config.has_api_key(),
            "scheduler_running": bool(app.config["SCHEDULER"]),
            "version": __version__,
        })

    @app.put("/api/settings")
    def put_settings():
        body = _json_body()
        if body is None:
            return _err("request body must be a JSON object", 400)
        patch, error = _validate_settings_update(body)
        if error:
            return _err(error, 400)
        with config.mutation_lock:
            settings = config.load_settings()
            for section, changes in patch.items():
                if section == "global":
                    for key in ("api_base_url", "default_model", "timezone", "output_dir"):
                        if key in changes:
                            settings["global"][key] = changes[key].strip()
                    if "models" in changes:
                        for stage in _STAGES:
                            if stage in changes["models"]:
                                settings["global"]["models"][stage] = \
                                    changes["models"][stage].strip()
                    if "reasoning" in changes and "effort" in changes["reasoning"]:
                        settings["global"]["reasoning"]["effort"] = \
                            changes["reasoning"]["effort"].strip()
                elif section == "smtp":
                    for key in ("host", "user", "sender"):
                        if key in changes:
                            settings["smtp"][key] = changes[key].strip()
                    if "port" in changes:
                        settings["smtp"]["port"] = changes["port"]
                else:
                    if "email" in changes:
                        settings["user"]["email"] = changes["email"].strip()
            config.save_settings(settings)
        logger.info("API: settings updated (sections=%s)", ", ".join(patch))
        return _settings_response(app)

    # ---- status --------------------------------------------------------------

    @app.get("/api/status")
    def status():
        limit = request.args.get("limit", default=5, type=int)
        limit = max(1, min(limit or 5, 50))
        with config.mutation_lock:
            settings = config.load_settings()
        interests = []
        for interest in settings.get("interests", []):
            name = interest.get("name", "")
            interests.append({
                "name": name,
                "paused": is_paused(interest),
                "recent_runs": [
                    {
                        "id": r["id"],
                        "status": r["status"],
                        "current_stage": r["current_stage"],
                        "error": r["error"],
                        "run_date": r["run_date"],
                        "started_at": r["started_at"],
                        "completed_at": r["completed_at"],
                    }
                    for r in app.config["STORE"].list_runs(name, limit=limit)
                ],
            })
        return jsonify({
            "scheduler_running": bool(app.config["SCHEDULER"]),
            "api_key_configured": config.has_api_key(),
            "interests": interests,
        })

    # ---- interests -------------------------------------------------------------

    @app.get("/api/interests")
    def list_interests():
        with config.mutation_lock:
            settings = config.load_settings()
        return jsonify({
            "interests": [
                {**i, "paused": is_paused(i)} for i in settings.get("interests", [])
            ]
        })

    @app.post("/api/interests")
    def create_interest():
        body = _json_body()
        if body is None:
            return _err("request body must be a JSON object", 400)
        interest, error = _validate_interest(body, creating=True)
        if error:
            return _err(error, 400)
        with config.mutation_lock:
            settings = config.load_settings()
            if _find_interest(settings, interest["name"]) is not None:
                return _err(f"An interest named {interest['name']!r} already exists", 409)
            settings["interests"].append(interest)
            config.save_settings(settings)
        logger.info("API: interest %r created", interest["name"])
        return jsonify({"interest": {**interest, "paused": is_paused(interest)}}), 201

    def _get_or_404(settings: dict[str, Any], name: str):
        interest = _find_interest(settings, name)
        if interest is None:
            return None, _err(f"Interest {name!r} not found", 404)
        return interest, None

    @app.get("/api/interests/<path:name>")
    def get_interest(name: str):
        with config.mutation_lock:
            settings = config.load_settings()
        interest, error = _get_or_404(settings, name)
        if error:
            return error
        return jsonify({"interest": {**interest, "paused": is_paused(interest)}})

    @app.put("/api/interests/<path:name>")
    def update_interest(name: str):
        body = _json_body()
        if body is None:
            return _err("request body must be a JSON object", 400)
        # Renaming happens exclusively through the rename endpoint (D11):
        # a name in the body must match the path, otherwise the silent
        # mismatch would look like a successful edit while splitting
        # config from history.
        if "name" in body and body["name"] != name:
            return _err(
                "PUT cannot rename an interest — use POST "
                f"/api/interests/{name}/rename", 400,
            )
        with config.mutation_lock:
            settings = config.load_settings()
            old, error = _get_or_404(settings, name)
            if error:
                return error
            interest, error = _validate_interest(body, creating=False, base=old)
            if error:
                return _err(error, 400)
            settings["interests"][settings["interests"].index(old)] = interest
            config.save_settings(settings)
        logger.info("API: interest %r updated", name)
        return jsonify({"interest": {**interest, "paused": is_paused(interest)}})

    @app.delete("/api/interests/<path:name>")
    def delete_interest(name: str):
        with config.mutation_lock:
            settings = config.load_settings()
            old, error = _get_or_404(settings, name)
            if error:
                return error
            settings["interests"].remove(old)
            config.save_settings(settings)
        logger.info(
            "API: interest %r deleted (run history stays under this name — D11)", name,
        )
        return jsonify({"deleted": name})

    @app.post("/api/interests/<path:name>/rename")
    def rename_interest(name: str):
        body = _json_body()
        if body is None:
            return _err("request body must be a JSON object", 400)
        new_name = body.get("to")
        if not isinstance(new_name, str) or not new_name.strip():
            return _err("'to' must be a non-empty string", 400)
        new_name = new_name.strip()
        if "/" in new_name:
            return _err("'to' must not contain '/'", 400)
        if new_name == name:
            return _err("'to' must differ from the current name", 400)
        with config.mutation_lock:
            settings = config.load_settings()
            old, error = _get_or_404(settings, name)
            if error:
                # Convergence aid (D11 recovery contract): a retry after a
                # crash AFTER both stores committed lands here — report the
                # converged state instead of a bare 404.
                already = _find_interest(settings, new_name)
                if already is not None:
                    return jsonify({
                        "renamed": True, "already_renamed": True,
                        "from": name, "to": new_name,
                        "runs_repointed": 0,
                    })
                return error
            if _find_interest(settings, new_name) is not None:
                return _err(f"An interest named {new_name!r} already exists", 409)
            # D11 order: SQLite FIRST, then atomically rewritten settings.
            # Step 2 is idempotent, so re-running a half-applied rename
            # converges.
            repointed = app.config["STORE"].rename_interest(name, new_name)
            old["name"] = new_name
            config.save_settings(settings)
        logger.info("API: interest renamed %r -> %r (%d run row(s) repointed)",
                    name, new_name, repointed)
        return jsonify({
            "renamed": True, "from": name, "to": new_name,
            "runs_repointed": repointed,
        })

    @app.post("/api/interests/<path:name>/trigger")
    def trigger_interest(name: str):
        scheduler = app.config["SCHEDULER"]
        if scheduler is None or not scheduler:
            return _err("Scheduler is not running — cannot trigger a run", 503)
        with config.mutation_lock:
            error = scheduler.trigger_now(name)
        if not error:
            return jsonify({"triggered": True, "interest": name}), 202
        status = 404 if "not found" in error else 409
        return _err(error, status)

    # ---- feeds (add/remove only — WP7 convention) -----------------------------

    @app.post("/api/interests/<path:name>/feeds")
    def add_feed(name: str):
        body = _json_body()
        if body is None:
            return _err("request body must be a JSON object", 400)
        feed, error = _validate_feed(body)
        if error:
            return _err(error, 400)
        with config.mutation_lock:
            settings = config.load_settings()
            interest, error = _get_or_404(settings, name)
            if error:
                return error
            interest.setdefault("feeds", []).append(feed)
            config.save_settings(settings)
        logger.info("API: feed %r added to interest %r", feed["name"], name)
        return jsonify({"feeds": interest["feeds"]}), 201

    @app.delete("/api/interests/<path:name>/feeds/<int:index>")
    def delete_feed(name: str, index: int):
        with config.mutation_lock:
            settings = config.load_settings()
            interest, error = _get_or_404(settings, name)
            if error:
                return error
            feeds = interest.get("feeds", [])
            if not 0 <= index < len(feeds):
                return _err(f"No feed at index {index}", 404)
            removed = feeds.pop(index)
            config.save_settings(settings)
        logger.info("API: feed %r removed from interest %r", removed.get("name"), name)
        return jsonify({"feeds": feeds})

    return app


def _settings_response(app: Flask):
    """Shared body for GET and PUT /api/settings."""
    with config.mutation_lock:
        settings = config.load_settings()
    return jsonify({
        "global": settings.get("global", {}),
        "smtp": settings.get("smtp", {}),
        "user": settings.get("user", {}),
        "api_key_configured": config.has_api_key(),
        "scheduler_running": bool(app.config["SCHEDULER"]),
        "version": __version__,
    })
