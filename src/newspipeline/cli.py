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
import logging
import shutil
import sys

from newspipeline import __version__
from newspipeline import config


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


def _cmd_run(interest: str | None, run_all: bool) -> int:
    # TODO(WP4/WP8): load settings, resolve interest(s), run the pipeline
    # via asyncio.run(), then dispatch delivery (email/file).
    raise NotImplementedError("WP8")


def _cmd_serve(host: str, port: int, no_scheduler: bool) -> int:
    # TODO(WP6/WP7/WP8): create the Flask app, start SchedulerThread,
    # run the development server bound to 127.0.0.1 by default.
    raise NotImplementedError("WP7")


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
