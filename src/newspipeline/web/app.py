"""Flask settings UI — configuration only, no dashboard (kept lean by design).

Pages:
  GET  /                         settings forms + recent-run status per interest
  POST /settings/global          save global + smtp + user sections
  POST /settings/api-key         save API key to .env (never echoed back — G1)
  POST /interests/new            add an interest
  POST /interests/<name>/save    update an interest (schedule, toggles, delivery)
  POST /interests/<name>/delete  remove an interest
  POST /interests/<name>/feeds/new         add a feed
  POST /interests/<name>/feeds/<i>/delete  remove feed i
  POST /run/<name>               trigger a run now (via the scheduler thread)

Recent-run status is rendered inline on the index page (read from the
store) — there is deliberately no separate status page or dashboard.

Bound to 127.0.0.1 by default; single-user local app, no auth (documented
in README — do not expose to a network).
"""

# version: 0.1.0

from __future__ import annotations

import secrets
from typing import Any

from flask import Flask, flash, redirect, render_template, request, url_for

from newspipeline import __version__, config
from newspipeline.scheduler import is_paused
from newspipeline.store import Store

# Fields an interest form submits; everything else keeps its stored value.
_INTEREST_TEXT_FIELDS = (
    "name", "start_time", "email_recipient", "input_data_length_mode",
)
_INTEREST_INT_FIELDS = (
    "interval_hours", "max_themes", "target_summary_words", "target_script_words",
    "target_script_de_words", "target_brief_words", "input_word_count",
)
_INTEREST_BOOL_FIELDS = (
    "enable_summary", "enable_script", "enable_script_de", "enable_brief",
)
_VALID_LENGTH_MODES = ("full_article", "headers_only", "word_count")


def _find_interest(settings: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next(
        (i for i in settings.get("interests", []) if i.get("name") == name), None,
    )


def _parse_interest_form(form: Any, base: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Merge form data into ``base``. Returns (interest, error_message)."""
    interest = dict(base)
    for field in _INTEREST_TEXT_FIELDS:
        interest[field] = form.get(field, "").strip()
    for field in _INTEREST_INT_FIELDS:
        raw = form.get(field, "").strip()
        try:
            interest[field] = int(raw)
        except ValueError:
            return base, f"Field {field!r} must be a whole number (got {raw!r})"
    for field in _INTEREST_BOOL_FIELDS:
        interest[field] = field in form
    interest["delivery"] = [
        mode for mode in ("file", "email") if f"delivery_{mode}" in form
    ]

    if not interest["name"]:
        return base, "Interest name must not be empty"
    try:
        hour, minute = (int(p) for p in interest["start_time"].split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        return base, f"start_time must be HH:MM (got {interest['start_time']!r})"
    if not 1 <= interest["interval_hours"] <= 168:
        return base, "interval_hours must be between 1 and 168"
    if interest["input_data_length_mode"] not in _VALID_LENGTH_MODES:
        return base, f"input_data_length_mode must be one of {_VALID_LENGTH_MODES}"
    return interest, None


def create_app(store: Store | None = None, scheduler: Any = None) -> Flask:
    app = Flask(__name__)
    # Signs flash-message cookies; per-process random is fine for a local app.
    app.secret_key = secrets.token_hex(32)
    app.config["STORE"] = store if store is not None else Store()
    app.config["SCHEDULER"] = scheduler

    @app.get("/")
    def index():
        settings = config.load_settings()
        runs_by_interest = {
            i.get("name", ""): app.config["STORE"].list_runs(i.get("name", ""), limit=5)
            for i in settings.get("interests", [])
        }
        return render_template(
            "index.html",
            settings=settings,
            runs_by_interest=runs_by_interest,
            is_paused=is_paused,
            has_api_key=config.has_api_key(),
            scheduler_running=bool(app.config["SCHEDULER"]),
            version=__version__,
        )

    @app.post("/settings/global")
    def save_global():
        form = request.form
        try:
            smtp_port = int(form.get("smtp_port", "25").strip())
        except ValueError:
            flash("SMTP port must be a number", "error")
            return redirect(url_for("index"))
        # Load→mutate→save cycles hold the lock so overlapping POSTs from
        # two browser tabs cannot lose each other's changes.
        with config.mutation_lock:
            settings = config.load_settings()
            settings["global"].update({
                "api_base_url": form.get("api_base_url", "").strip(),
                "default_model": form.get("default_model", "").strip(),
                "timezone": form.get("timezone", "").strip(),
                "output_dir": form.get("output_dir", "").strip(),
                "models": {
                    stage: form.get(f"model_{stage}", "").strip()
                    for stage in ("analysis", "generation", "brief")
                },
                "reasoning": {"effort": form.get("reasoning_effort", "").strip()},
            })
            settings["smtp"].update({
                "host": form.get("smtp_host", "").strip(),
                "port": smtp_port,
                "user": form.get("smtp_user", "").strip(),
                "sender": form.get("smtp_sender", "").strip(),
            })
            settings["user"]["email"] = form.get("user_email", "").strip()
            config.save_settings(settings)
        flash("Global settings saved")
        return redirect(url_for("index"))

    @app.post("/settings/api-key")
    def save_api_key():
        # Write-only: the key goes straight to .env and is never rendered (G1).
        key = request.form.get("api_key", "").strip()
        if not key:
            flash("No API key entered — nothing saved", "error")
        else:
            config.set_api_key(key)
            flash("API key saved")
        return redirect(url_for("index"))

    @app.post("/interests/new")
    def interest_new():
        with config.mutation_lock:
            settings = config.load_settings()
            interest, error = _parse_interest_form(request.form, {"feeds": []})
            if error:
                flash(error, "error")
            elif _find_interest(settings, interest["name"]) is not None:
                flash(f"An interest named {interest['name']!r} already exists", "error")
            else:
                settings["interests"].append(interest)
                config.save_settings(settings)
                flash(f"Interest {interest['name']!r} added")
        return redirect(url_for("index"))

    @app.post("/interests/<path:name>/save")
    def interest_save(name: str):
        with config.mutation_lock:
            settings = config.load_settings()
            old = _find_interest(settings, name)
            if old is None:
                flash(f"Interest {name!r} not found", "error")
            else:
                interest, error = _parse_interest_form(request.form, old)
                if error:
                    flash(error, "error")
                elif interest["name"] != name and _find_interest(settings, interest["name"]):
                    flash(f"An interest named {interest['name']!r} already exists", "error")
                else:
                    settings["interests"][settings["interests"].index(old)] = interest
                    config.save_settings(settings)
                    if interest["name"] != name:
                        flash(
                            f"Interest renamed to {interest['name']!r} — note: run history "
                            "stays under the old name (DECISIONS.md D5)",
                        )
                    else:
                        flash(f"Interest {name!r} saved")
        return redirect(url_for("index"))

    @app.post("/interests/<path:name>/delete")
    def interest_delete(name: str):
        with config.mutation_lock:
            settings = config.load_settings()
            old = _find_interest(settings, name)
            if old is None:
                flash(f"Interest {name!r} not found", "error")
            else:
                settings["interests"].remove(old)
                config.save_settings(settings)
                flash(f"Interest {name!r} deleted")
        return redirect(url_for("index"))

    @app.post("/interests/<path:name>/feeds/new")
    def feed_new(name: str):
        with config.mutation_lock:
            settings = config.load_settings()
            interest = _find_interest(settings, name)
            feed_name = request.form.get("feed_name", "").strip()
            feed_url = request.form.get("feed_url", "").strip()
            if interest is None:
                flash(f"Interest {name!r} not found", "error")
            elif not feed_name or not feed_url:
                flash("Feed name and URL are required", "error")
            else:
                interest.setdefault("feeds", []).append({
                    "name": feed_name,
                    "url": feed_url,
                    "category": request.form.get("feed_category", "").strip(),
                })
                config.save_settings(settings)
                flash(f"Feed {feed_name!r} added to {name!r}")
        return redirect(url_for("index"))

    @app.post("/interests/<path:name>/feeds/<int:index>/delete")
    def feed_delete(name: str, index: int):
        with config.mutation_lock:
            settings = config.load_settings()
            interest = _find_interest(settings, name)
            if interest is None or not 0 <= index < len(interest.get("feeds", [])):
                flash("Feed not found", "error")
            else:
                removed = interest["feeds"].pop(index)
                config.save_settings(settings)
                flash(f"Feed {removed.get('name', '?')!r} removed from {name!r}")
        return redirect(url_for("index"))

    @app.post("/run/<path:name>")
    def run_now(name: str):
        scheduler = app.config["SCHEDULER"]
        if scheduler is None or not scheduler:
            flash("Scheduler is not running — cannot trigger a run", "error")
        else:
            error = scheduler.trigger_now(name)
            flash(error, "error") if error else flash(f"Run triggered for {name!r}")
        return redirect(url_for("index"))

    return app
