# WORKPACKAGES.md — newspipeline implementation plan

Each work package ends in a reviewable, testable state. Invariants G1–G5 and
trigger paths T1–T3 are defined in [DECISIONS.md](DECISIONS.md); a task is
complete only when its guarded invariants have green guard tests.

| WP | Title | Status |
|----|-------|--------|
| WP1 | Scaffolding, config & secrets handling | ✅ done (0.1.0) |
| WP2 | SQLite store | ✅ done (0.1.0, scaffold level — guard tests in WP9) |
| WP3 | LLM client | ✅ done (0.1.0, scaffold level — live calls untestable, T3) |
| WP4 | Pipeline stages | ✅ done (0.1.0 — guard tests G4/G5 land in WP9) |
| WP5 | Delivery: email + local file | ✅ done (0.1.0 — live SMTP paths T2) |
| WP6 | Scheduler thread | ✅ done (0.1.0 — G3 guard tests land in WP9) |
| WP7 | Web settings UI | ⬜ pending |
| WP8 | CLI wiring, systemd unit, README finalization | ⬜ pending |
| WP9 | Guard tests + end-to-end verification | ⬜ pending |

---

## WP1 — Scaffolding, config & secrets handling ✅

Project layout (src layout, pyproject, .gitignore, .env.example,
settings.example.toml), `config.py` with TOML load/save, .env parsing, and
the G1 write-path guard (secrets refused in `save_settings`).

**Done when:** package installs editable; `newspipeline init` creates
settings.toml, data/, .env.

## WP2 — SQLite store ✅ (scaffold)

`store.py`: schema (runs, articles, themes, deliverables, briefs), thread-safe
single connection, WAL, URL dedupe via UNIQUE + ON CONFLICT. RSS date parsing
ported from legacy.

**Done when:** all methods implemented; guard tests for G2 land in WP9.

## WP3 — LLM client ✅ (scaffold)

`llm.py`: async httpx chat-completions client, retries with exponential
backoff on 429/5xx, per-call model override, optional `reasoning.effort`
forwarded only when set. API key only in the Authorization header (G1).

**Done when:** implemented; live provider calls are out of scope for local
verification (T3) — behavior verified against a mock endpoint in WP9.

## WP4 — Pipeline stages ⬜

`pipeline.py`: port `_scrape`, `_analyze`, `_generate`, `_brief` from
`legacy/news_pipeline.py`. Adaptations: sync sqlite store; feeds from the
interest dict; per-stage models and reasoning effort from settings; run
status/stage transitions recorded in the store.

**Done when:** a full run executes against a fixture feed + mock LLM; G4,
G5 guard tests green (with WP9).

## WP5 — Delivery: email + local file ⬜

`emailer.py`: port `send_pipeline_results` and `send_failure_alert` from
legacy (brief email + per-theme emails; failure alerts). `fileout.py`:
write brief + per-theme markdown under `output/<interest>/<date>/`.
Dispatch driven by `interest.delivery` (`["file"]`, `["email"]`, or both).
Recipient resolution: interest `email_recipient`, else `[user] email`.

**Done when:** file output verified end-to-end; SMTP covered by a local
debug server test where possible (T2 flagged for live paths).

## WP6 — Scheduler thread ⬜

`scheduler.py`: `SchedulerThread` hosting an asyncio loop on a daemon
thread; 60s check loop over settings.toml interests; catch-up via
`find_most_recent_missed` on startup; per-interest concurrency guard;
`trigger_now(name)` for the UI/CLI; clean shutdown on signal.

**Done when:** G3 guard tests green; a short-interval interest fires in a
live smoke run.

## WP7 — Web settings UI ⬜

`web/`: Flask routes + templates for global settings (endpoint, models,
reasoning effort), API key entry (write-only, shows "configured" state —
G1), SMTP, user email, interest + feed CRUD, schedule editing, "run now",
and recent-run status read from the store. No dashboard beyond that.

**Done when:** every settings.toml field editable via UI; API key never
rendered back; manual browser check documented.

## WP8 — CLI wiring, systemd, README ⬜

Wire `run` (asyncio.run over selected interests + delivery dispatch) and
`serve` (create_app + SchedulerThread lifecycle). Ship the systemd user
unit (already sketched in README). Finalize README against actual behavior.

**Done when:** `newspipeline run --all` and `newspipeline serve` work
end-to-end from a fresh clone following the README alone.

## WP9 — Guard tests + end-to-end verification ⬜

pytest suite covering invariants G1–G5 (`tests/test_secrets.py`,
`test_store.py`, `test_scheduler.py`, `test_pipeline.py`, `test_e2e.py`)
using a fixture RSS feed served locally and a mocked LLM endpoint.
Explicit report of what was tested and what could not be tested (live
OpenRouter calls — T3; real SMTP delivery — T2).

**Done when:** all guard tests installed and green; verification statement
recorded in CHANGELOG.
