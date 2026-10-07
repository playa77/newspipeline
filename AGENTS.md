# AGENTS.md — newspipeline

Headless news-pipeline **server** (`src/newspipeline/`, dist `newspipeline`)
plus desktop **client** GUI (`client/src/newspipeline_client/`, dist
`newspipeline-client`) — two separate setuptools distributions in one repo,
each with its own `pyproject.toml` (add dependencies to the right one).
Python 3.12+, src layout. No linter/formatter/typechecker configured;
`pytest` is the only automated gate.

## Commands

```bash
source .venv/bin/activate
pip install -e ".[dev]"        # server + dev deps (client: pip install ./client)
pytest tests/ -v               # full suite — green and fully hermetic (~60 s, 209 tests)
pytest tests/test_rename_history.py -v   # single guard file
QT_QPA_PLATFORM=offscreen pytest client/tests/ -v  # desktop/API checks (client and server installed)
```

App entry point (`newspipeline.cli`):
- `newspipeline run --interest NAME` / `run --all` — one-shot; exit 1 on any failure
- `newspipeline serve` — JSON API on 127.0.0.1:8421 + scheduler thread (`--no-scheduler` for API only)

## Architecture notes

- Pipeline stages in `pipeline.py`: scrape → analyze → generate → brief.
  Run rows + articles live in SQLite (`data/news.db`); configuration lives
  in `config/settings.toml` (reference: `config/settings.example.toml`).
- `serve` runs the scheduler on a background asyncio thread; settings are
  re-read every tick, so config edits apply without restart.
- Runs link to stable `interest_scopes` identified by `(recipient_id, interest_name)`;
  legacy interests use the empty recipient id. URLs dedupe within that scope.
  `theme_articles` composite FKs require theme/source membership within one run.
  Recipients inherit sparse shared/default/parent settings (recipients.py).
  Renaming goes exclusively through
  `POST /api/interests/<name>/rename` or its `/api/recipients/<id>/interests/`
  equivalent: rename the stable scope in SQLite first
  (idempotent), then atomically rewrite settings.toml; re-running a
  half-applied rename converges. `PUT /api/interests/<name>` can never
  change the name.
- Per-interest `delivery_schedule` is separate from collection `start_time`/
  `interval_hours`. Completed scheduled output is persisted in
  `pending_deliveries`; serve drains due jobs without needing an LLM key.
  Failed jobs require explicit retries; interrupted sends are at-least-once.
  See docs/recipients.md for API contracts and inheritance precedence.
- Orphaned `running` rows are swept to failed at **serve startup only**;
  one-shot `run` deliberately never sweeps (a concurrent serve may own the row).
- `NEWSPIPELINE_HOME=/dir` relocates `config/`, `data/`, `.env`,
  `output/`; it must be set for every process that touches state.

## Testing

- Fully hermetic via `tests/conftest.py`: fixture RSS feed + article pages +
  mock OpenAI-compatible `/v1/chat/completions` endpoint (`fixture_server`),
  fake SMTP server (`smtp_server`), and `isolated_config` redirecting
  settings/.env paths to tmp. Real `config/settings.toml`, `.env`, and
  `data/news.db` are never touched — keep it that way; use these fixtures.
- Guard tests G1–G10 are defined in DECISIONS.md and map 1:1 to named files
  (`test_secrets`, `test_store`, `test_scheduler`, `test_pipeline`,
  `test_e2e`, `test_scrape_loop`, `test_emailer_tls`,
  `test_rename_history`, `test_startup_sweep`, `test_llm_null_content`).
  Never weaken or skip them.
- The client has HTTP-routing/offscreen regression tests in `client/tests/`,
  including complete-configuration editor integration with the server API.
  Install both distributions to run these checks. Visual desktop and remote
  SSH checks remain manual (client/README.md checklist).

## Hard boundaries

- Secrets (`NEWS_API_KEY`, `NEWS_SMTP_PASSWORD`) live only in `.env` — never
  in settings.toml, logs, or any API response (G1). There is deliberately NO
  secret-setting API endpoint; `.env` is edited over SSH.
- Security model = SSH local forward + loopback-only bind, no TLS, no
  tokens. Do not "harden" it by exposing the API; the Host/Origin gates are
  defense-in-depth only.
- Authenticated SMTP must upgrade with a certificate-verifying TLS context
  before credentials (G7); self-signed/mismatched relays fail loudly by design.
- Config/state writes go through tmp-file + `os.replace` under the mutation
  lock; never write settings.toml directly.

## Conventions

- Source files carry a trailing `# version:` comment; bump it when editing a file.
- Process docs are part of the workflow: append decisions to `DECISIONS.md`
  as they are made (R1/R2/R3 tagged), append changes to `CHANGELOG.md`,
  update `README.md` when behavior changes, and keep `DEPLOYMENT.md` current
  (deployments always use the dedicated `newspipeline` user, never another
  user's account).
