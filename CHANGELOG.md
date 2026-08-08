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
