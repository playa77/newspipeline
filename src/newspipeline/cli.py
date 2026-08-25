"""newspipeline command-line interface.

Commands:
  init   — create config/settings.toml from the example template, data/,
           and remind about .env
  run    — run the pipeline manually (--interest NAME | --all)
  serve  — start the settings web UI + built-in scheduler
"""

# version: 0.1.0

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import signal
import sys
from typing import Any

from newspipeline import __version__
from newspipeline import config
from newspipeline import delivery
from newspipeline.pipeline import Pipeline
from newspipeline.scheduler import SchedulerThread, is_paused
from newspipeline.store import Store
from newspipeline.web.app import create_app

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    # ISO 8601 timestamps so failures can be diagnosed from logs alone.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def _cmd_init() -> int:
    created: list[str] = []
    if not config.SETTINGS_PATH.exists():
        config.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.SETTINGS_EXAMPLE_PATH, config.SETTINGS_PATH)
        created.append(str(config.SETTINGS_PATH))
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not config.ENV_PATH.exists():
        shutil.copy(config.PROJECT_ROOT / ".env.example", config.ENV_PATH)
        config.ENV_PATH.chmod(0o600)
        created.append(str(config.ENV_PATH))
    for path in created:
        print(f"created: {path}")
    if not created:
        print("already initialized")
    if not config.has_api_key():
        print("NOTE: set NEWS_API_KEY in .env before running the pipeline")
    return 0


async def _run_selected(
    store: Store, settings: dict[str, Any], interests: list[dict[str, Any]], api_key: str,
) -> int:
    """Run the pipeline for each interest sequentially. Returns a count of failures."""
    failures = 0
    for interest in interests:
        name = interest.get("name", "?")
        try:
            pipeline = Pipeline(store=store, settings=settings, api_key=api_key)
            run_id = await pipeline.run(interest)
            status, run = delivery.dispatch_finished_run(
                run_id=run_id, store=store, settings=settings, interest=interest,
            )
            if status == "completed":
                print(f"{name}: run {run_id} completed")
            else:
                failures += 1
                error = run.get("error") or "unknown error"
                print(f"{name}: run {run_id} FAILED — {error}", file=sys.stderr)
        except Exception as exc:
            failures += 1
            logger.exception("Run failed — interest=%r", name)
            print(f"{name}: FAILED — {exc}", file=sys.stderr)
    return failures


def _cmd_run(interest: str | None, run_all: bool) -> int:
    settings = config.load_settings()
    interests = settings.get("interests", [])

    if run_all:
        selected = [i for i in interests if not is_paused(i)]
        if not selected:
            print("no non-paused interests configured")
            return 0
    else:
        selected = [i for i in interests if i.get("name") == interest]
        if not selected:
            names = ", ".join(i.get("name", "?") for i in interests) or "(none)"
            print(f"interest {interest!r} not found — configured: {names}", file=sys.stderr)
            return 1

    api_key = config.get_api_key()
    if not api_key:
        print("NEWS_API_KEY is not set — add it to .env first", file=sys.stderr)
        return 1

    store = Store()
    try:
        failures = asyncio.run(_run_selected(store, settings, selected, api_key))
    finally:
        store.close()
    return 1 if failures else 0


def _cmd_serve(host: str, port: int, no_scheduler: bool) -> int:
    if host not in ("127.0.0.1", "localhost", "::1"):
        # The web UI rejects non-loopback Host headers (web/app.py origin
        # gate), so a non-loopback bind would serve pages no browser can
        # use — warn loudly instead of failing silently.
        logger.warning(
            "Binding to %s — the settings UI rejects non-local Host "
            "headers; browsers on other machines will get 403s.", host,
        )
    store = Store()
    scheduler: SchedulerThread | None = None
    if not no_scheduler:
        scheduler = SchedulerThread(store=store)
        scheduler.start()

    # SIGTERM: unwind through app.run() so the finally block stops the
    # scheduler cleanly (systemd sends SIGTERM on `systemctl stop`).
    def _sigterm(signum: int, frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm)

    app = create_app(store=store, scheduler=scheduler)
    logger.info("Serving settings UI on http://%s:%d (scheduler %s)",
                host, port, "running" if scheduler else "disabled")
    try:
        app.run(host=host, port=port)
    except SystemExit:
        pass  # SIGTERM handler — fall through to the clean-shutdown path
    finally:
        if scheduler is not None:
            scheduler.stop()
        store.close()
    return 0


def main() -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="newspipeline", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create settings.toml, data/ and .env from templates")

    run_p = sub.add_parser("run", help="run the pipeline manually")
    group = run_p.add_mutually_exclusive_group(required=True)
    group.add_argument("--interest", metavar="NAME", help="run a single interest by name")
    group.add_argument("--all", action="store_true", help="run all non-paused interests")

    serve_p = sub.add_parser("serve", help="start the settings UI + scheduler")
    serve_p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    serve_p.add_argument("--port", type=int, default=8421, help="listen port (default 8421)")
    serve_p.add_argument("--no-scheduler", action="store_true", help="run UI without the scheduler")

    args = parser.parse_args()
    if args.command == "init":
        return _cmd_init()
    if args.command == "run":
        return _cmd_run(args.interest, args.all)
    if args.command == "serve":
        return _cmd_serve(args.host, args.port, args.no_scheduler)
    return 2


if __name__ == "__main__":
    sys.exit(main())
