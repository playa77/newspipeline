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
