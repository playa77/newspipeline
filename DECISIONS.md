# DECISIONS.md — newspipeline decision ledger

Append-only. Entries are written when a decision is made, tagged by
reversibility: R1 (reversible < 1h), R2 (bounded cost to reverse),
R3 (one-way door: persisted data formats, external contracts, security model,
core framework).

---

## 2026-08-08 — D1: Scheduling architecture — built-in scheduler in the web app process (R3, human-decided)

Context: the task requires local orchestration/scheduling of pipeline runs on
an Ubuntu 24.04 desktop. Branches presented with downstream consequences:
(a) built-in asyncio scheduler inside the web settings app process,
(b) systemd user timer / cron invoking a CLI run command,
(c) a separate scheduler daemon.
The human chose (a). Downstream: the app must run continuously (an optional
systemd *user* service is documented for autostart); startup catch-up of
missed runs is ported from the legacy scheduler; the sync Flask process hosts
the async scheduler on a dedicated thread with its own event loop.

## 2026-08-08 — D2: Web framework — Flask, sync (R3, human-decided)

Branches presented: FastAPI+uvicorn / Flask / stdlib http.server.
The human chose Flask. Downstream: the async pipeline is executed via a
dedicated event loop on the scheduler thread; manual CLI runs use
`asyncio.run()`; no pydantic validation — request validation is hand-rolled
in the web layer.

## 2026-08-08 — D3: Persistence — SQLite via stdlib sqlite3 for run data, TOML for configuration (R3, human-decided)

Branches presented: (a) sqlite3 + settings.toml, (b) SQLAlchemy with all
config in the DB, (c) JSON files only. The human chose (a).
External contract: `config/settings.toml` is the stable, human-editable
configuration format and must stay version-compatible from the first release.
Pipeline data (runs, articles, themes, deliverables, briefs) lives in
`data/news.db` (SQLite, WAL mode). No ORM; SQL is hand-written in `store.py`.

## 2026-08-08 — D4: tomli-w added as a dependency for writing settings.toml (R1)

stdlib `tomllib` is read-only; the web UI must write configuration back.
Rejected alternative: a hand-rolled TOML writer (error-prone string escaping).

## 2026-08-08 — D5: Runs reference interests by name, not by surrogate id (R1)

Interests live in TOML and have no stable integer id as in the legacy
Postgres schema. `runs.interest_name` is the join key. Consequence: renaming
an interest orphans its run history — accepted for a lean single-user app;
documented in README.

## 2026-08-08 — Task invariants (derived from the task specification and legacy behavior)

Guarded (each requires a named, green guard test before the task is reported
complete):

- G1: The API key is never written to settings.toml, never logged, and never
  returned by any web endpoint → `tests/test_secrets.py::test_api_key_not_persisted_or_exposed`
- G2: Article URL dedupe holds across runs → `tests/test_store.py::test_article_url_dedupe`
- G3: Next-run computation preserves legacy semantics (start_time + interval
  grid, 60s tick window) → `tests/test_scheduler.py::test_next_run_*`
- G4: Language-detection heuristic parity with legacy → `tests/test_pipeline.py::test_detect_language_*`
- G5: End-to-end pipeline run against a fixture RSS feed with a mocked LLM
  endpoint produces themes, deliverables, a brief, and file output →
  `tests/test_e2e.py::test_pipeline_end_to_end`

Triggered (not auto-testable; flag any diff touching these paths before
commit):

- T1: `.env` parsing / secret handling in `config.py` and `cli.py`
- T2: SMTP authentication paths in `emailer.py` (no live SMTP available in
  this environment)
- T3: Live OpenRouter request/response handling in `llm.py` (no API key in
  this environment)
