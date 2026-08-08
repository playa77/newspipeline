# newspipeline

Standalone local news pipeline agent: monitors RSS feeds, extracts themes
with an LLM, generates summaries / video scripts / a daily brief, and
delivers them by email or as local markdown files. Runs on your Ubuntu
desktop with a built-in scheduler and a minimal local settings UI.

Extracted from a larger monolithic project (see `legacy/`, read-only
reference). Python 3.12+, no Docker, no database server, no build step.

> Status: **0.1.0 scaffold** — see [WORKPACKAGES.md](WORKPACKAGES.md) for the
> implementation plan and current state.

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

Edit `config/settings.toml` directly (see `config/settings.example.toml` for
a fully commented reference) or use the web UI:

```bash
newspipeline serve         # http://127.0.0.1:8421 — settings only
```

The UI binds to localhost and has **no authentication** — do not expose it
to a network.

## Running

```bash
# Manually, once:
newspipeline run --interest "Example"
newspipeline run --all

# Continuously (UI + scheduler):
newspipeline serve
```

### Autostart (optional)

To keep the scheduler running across logins, install a systemd *user*
service (replace paths):

```ini
# ~/.config/systemd/user/newspipeline.service
[Unit]
Description=newspipeline settings UI + scheduler

[Service]
WorkingDirectory=%h/projects/newspipeline
ExecStart=%h/projects/newspipeline/.venv/bin/newspipeline serve
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
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

Project decisions and invariants: [DECISIONS.md](DECISIONS.md).
Changes: [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
