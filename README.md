# newspipeline

Standalone local news pipeline agent: monitors RSS feeds, extracts themes
with an LLM, generates summaries / video scripts / a daily brief, and
delivers them by email or as local markdown files. Runs on your Ubuntu
desktop with a built-in scheduler and a minimal local settings UI.

Python 3.12+, no Docker, no database server, no build step.

> Status: **0.1.0** — fully implemented; guard-test suite green
> (`pytest tests/`). See [WORKPACKAGES.md](WORKPACKAGES.md) for the plan and
> per-package state.

## Features

- Multiple **interests** (topics), each with its own feeds, schedule,
  deliverable toggles, and delivery modes
- Pipeline stages: **scrape** (RSS + full-article extraction) →
  **analyze** (LLM theme identification) → **generate** (summary,
  EN/DE YouTube scripts) → **brief** (daily synthesis)
- **Built-in scheduler**: per-interest start time + interval, catch-up of
  missed runs on startup, pause by disabling all deliverables
- **Delivery**: email via SMTP and/or local markdown files
- **Any OpenAI-compatible LLM endpoint** (default: OpenRouter), per-stage
  model overrides, optional reasoning effort
- Secrets in `.env` only — never in config files, logs, or the UI

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

newspipeline init          # creates config/settings.toml, data/, .env
$EDITOR .env               # set NEWS_API_KEY (OpenRouter key)
```

## Configuration

Two files, strictly separated:

| File | Contents |
|---|---|
| `.env` | **Secrets only**: `NEWS_API_KEY`, optional `NEWS_SMTP_PASSWORD` |
| `config/settings.toml` | Everything else: LLM endpoint/model, interests, feeds, schedules, delivery, SMTP host/port |

All paths (config, data, output) resolve relative to the checkout. Set
`NEWSPIPELINE_HOME=/some/dir` to relocate them — the variable must be set
for every process that touches state (`run`, `serve`, your systemd unit).

Edit `config/settings.toml` directly (see `config/settings.example.toml` for
a fully commented reference) or use the web UI:

```bash
newspipeline serve         # http://127.0.0.1:8421 — settings only
```

The UI binds to localhost and has **no authentication** — do not expose it
to a network.

## Running

```bash
# Manually, once (interests run sequentially, then delivery is dispatched):
newspipeline run --interest "Example"   # one interest, even if paused
newspipeline run --all                  # all non-paused interests

# Continuously (UI + scheduler):
newspipeline serve                      # http://127.0.0.1:8421
newspipeline serve --no-scheduler       # UI only, no scheduled runs
newspipeline serve --host 127.0.0.1 --port 8421
```

`run` exits 0 when every selected run completed, 1 otherwise (unknown
interest, missing API key, or a failed run — a failure alert email is sent
when a recipient is configured).

`serve` starts the scheduler on a background thread. Two behaviors to be
aware of:

- **Catch-up**: on startup, each interest whose most recent scheduled slot
  was missed runs immediately. With a daily interest this means most app
  starts trigger a run.
- **Paused interests** (all deliverables disabled) never run on a schedule;
  `run --interest NAME` can still force one manually.

If the process is killed mid-run, the run's row in `data/news.db` stays in
`running` state and blocks that interest from running again. Fix by setting
its status in the database:

```bash
sqlite3 data/news.db "UPDATE runs SET status='failed', error='killed' WHERE status='running';"
```

### Autostart (optional)

To keep the scheduler running across logins, install the shipped systemd
*user* unit and adjust the paths to your checkout:

```bash
cp deploy/newspipeline.service ~/.config/systemd/user/
$EDITOR ~/.config/systemd/user/newspipeline.service   # set WorkingDirectory/ExecStart
systemctl --user daemon-reload
systemctl --user enable --now newspipeline.service
loginctl enable-linger "$USER"   # keep it running when logged out
```

## Output

- Local files: `output/<interest>/<run-date>/` (brief + one markdown file
  per theme)
- Email: daily brief plus one email per theme with its enabled deliverables
- Run history and all scraped articles: `data/news.db` (SQLite)

Note: runs are linked to interests **by name** — renaming an interest starts
a fresh history (see DECISIONS.md D5).

## Development

```bash
pytest tests/ -v
```

The suite includes the guard tests for invariants G1–G5 (secrets, URL
dedupe, scheduler semantics, language detection, end-to-end run) defined in
DECISIONS.md; tests run entirely against local fixtures and never touch the
real `config/settings.toml`, `.env`, or `data/news.db`.

Project decisions and invariants: [DECISIONS.md](DECISIONS.md).
Changes: [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
