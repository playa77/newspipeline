# Changelog

All notable changes to newspipeline are documented here, appended as work
happens. Format: version heading, date, grouped bullet points.

## 0.1.0 — 2026-08-08

### Added

- Project scaffolding for the standalone news pipeline agent, extracted from
  the legacy Workbench monolith (`legacy/` is read-only reference material):
  - `pyproject.toml` (src layout, pinned deps: flask, httpx, feedparser,
    trafilatura, tomli-w; pytest as dev extra; `newspipeline` CLI entry point)
  - `.gitignore`, `.env.example`, `config/settings.example.toml`
  - Package skeleton under `src/newspipeline/`: `config.py`, `store.py`,
    `llm.py`, `pipeline.py`, `emailer.py`, `fileout.py`, `scheduler.py`,
    `cli.py`, `web/` (Flask app, template, stylesheet)
  - Pure helpers ported from legacy and functional at scaffold level:
    language detection, RSS date parsing, scheduler time math, SMTP sender
  - `tests/` skeleton with a smoke test
  - `README.md`, `WORKPACKAGES.md`, `DECISIONS.md`, this changelog
- WP4: pipeline stages implemented in `pipeline.py` — `Pipeline.run()` and
  the four stages (scrape → analyze → generate → brief), ported from
  `legacy/news_pipeline.py` with the planned adaptations: sync sqlite store,
  feeds from the interest dict, per-stage model overrides
  (`global.models.{analysis,generation,brief}` with fallback to
  `default_model`), reasoning effort forwarded per LLM call, and run
  status/stage transitions recorded in the store. Verified by a full run
  against a fixture RSS feed and a mock chat-completions endpoint (local
  HTTP server): 2 articles scraped with full-content extraction, 2 themes,
  summary+script deliverables per theme, a brief, run marked completed;
  second run confirmed URL dedupe (no re-scraped articles). `pytest` smoke
  suite: 3 passed. Formal G4/G5 guard tests land in WP9 as planned.
- WP5: delivery implemented —
  - `emailer.py`: `send_pipeline_results` (brief email + per-theme emails
    with the enabled deliverables) and `send_failure_alert`, ported from
    `legacy/news_emailer.py`; SMTP password from .env via
    `config.get_smtp_password()` (G1)
  - `fileout.py`: `write_run_output` writes `brief.md` and one markdown file
    per theme under `<output_dir>/<interest-slug>/<run-date>/`
  - `delivery.py` (new): `deliver_run` dispatch driven by
    `interest.delivery`, recipient resolution (interest `email_recipient`,
    else `[user] email`), and `deliver_failure_alert`
  - Verified end-to-end against the fixture feed + mock LLM plus a
    hand-rolled local SMTP server: file output (brief + theme files,
    correct headings, disabled script_de omitted), 2 result emails + 1
    failure alert captured with correct recipients/subjects/bodies,
    no-recipient paths skip with a warning. `pytest` smoke suite: 3 passed.
    Authenticated SMTP (STARTTLS+LOGIN) untested locally — T2.
- WP6: `SchedulerThread` implemented — daemon thread hosting its own asyncio
  loop; 60s check loop with the legacy 65s tick window; startup catch-up via
  `find_most_recent_missed`; two-layer per-interest concurrency guard
  (in-memory task registry + `store.is_interest_running`); thread-safe
  `trigger_now(name)` returning "" or an error message; settings (including
  timezone) re-read every tick so UI edits apply without restart; pipeline
  and delivery wired per run, with blocking delivery dispatched to an
  executor; idempotent `start()`/`stop()` (signal handlers are installed by
  the CLI in WP8). Verified live against the fixture feed + mock LLM:
  catch-up-only fire (1 run), tick-window fire (+1 catch-up = 2 runs),
  `trigger_now` success and all guard messages (duplicate trigger, unknown
  interest, paused interest, stale 'running' row), file delivery executed
  from the scheduler path, clean stop with joined threads. `pytest` smoke
  suite: 3 passed. G3 time-math guard tests land in WP9 as planned.
