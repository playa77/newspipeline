"""Flask settings UI — configuration only, no dashboard (kept lean by design).

Pages:
  GET  /                  settings form (global, SMTP, user, interests+feeds)
  POST /settings/global   save global + smtp + user sections
  POST /settings/api-key  save API key to .env (never echoed back — G1)
  POST /interests/...     interest + feed CRUD
  POST /run/<name>        trigger a run now (via the scheduler thread)
  GET  /status/<name>     recent runs for an interest (read from the store)

Bound to 127.0.0.1 by default; single-user local app, no auth (documented
in README — do not expose to a network).

TODO(WP7): implement routes and template wiring.
"""

# version: 0.1.0

from __future__ import annotations

from flask import Flask, render_template

from newspipeline import __version__, config


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        settings = config.load_settings()
        return render_template(
            "index.html",
            settings=settings,
            has_api_key=config.has_api_key(),
            version=__version__,
        )

    return app
