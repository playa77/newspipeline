# newspipeline

Headless news pipeline **server** for a small Linux box or VPS: monitors
RSS feeds, extracts themes with an LLM, generates summaries / video
scripts / a daily brief, and delivers them by email or as local markdown
files. Managed from your Ubuntu desktop by the separate **newspipeline
client** GUI over an SSH tunnel.

Python 3.12+, no Docker, no database server, no build step.

> Status: **0.5.2 (in progress)** — fully implemented; guard-test suite green
> (222 tests, `pytest tests/`; additional offscreen client tests in
> `client/tests/`). Decisions and invariants:
> [DECISIONS.md](DECISIONS.md);
> changes: [CHANGELOG.md](CHANGELOG.md); this-host deployment:
> [DEPLOYMENT.md](DEPLOYMENT.md).

## Architecture

Two parts, one trust boundary:

| Piece | Runs on | What it is |
|---|---|---|
| `newspipeline` (this repo) | VPS | Headless daemon: JSON configuration API (`127.0.0.1:8421`) + built-in scheduler |
| `newspipeline-client` (`client/`) | Desktop | PySide6 GUI whose sole job is managing the server's configuration |

The client spawns an SSH local forward
(`ssh -N -L <port>:127.0.0.1:8421 <user>@<host>`) and speaks HTTP to its
own loopback. **The tunnel is the security model**: the API binds to
127.0.0.1 only, has no TLS and no tokens, and is never exposed publicly.
A localhost Host/Origin gate is kept as defense-in-depth against
browser-originated requests on the machines themselves.

- Multiple **interests** (topics), each with its own feeds, schedule,
  deliverable toggles, and delivery modes
- Pipeline stages: **scrape** (RSS + full-article extraction; outbounds are
  rate-limited by a politeness layer — 2 s minimum request gap, bounded
  transient retry with capped `Retry-After` handling, browser User-Agent with
  a single plain-identity fallback for browser-rejecting sources —
  D19 in DECISIONS.md) →
  **analyze** (LLM theme identification) → **generate** (summary,
  EN/DE YouTube scripts) → **brief** (daily synthesis)
- **Report documents**: every generate/brief answer is stored as a document
  (headline, deck, sections, per-topic sources — `document.py`, built by
  `compose.py`). The HTML email, the plain-text mail part and the markdown
  archive are three renderings of that one structure, so no output format is
  derived from another. A model that answers without the expected structure
  degrades to a prose document instead of failing the run.
- **Built-in scheduler**: per-interest start time + interval, catch-up of
  missed runs on startup, startup sweep of runs interrupted by a crash,
  pause by disabling all deliverables
- **Delivery**: email via SMTP (Gmail relay documented below; sent as
  multipart/alternative — a designed HTML report plus a plain-text part
  rendered from the same document, so Gmail, Apple Mail and Outlook show the
  layout, and every topic's sources are linked under that topic) and/or local
  markdown files (a server-side archive of the same documents). The per-theme
  emails are a per-interest switch (`email_themes`, default on): with it off
  the interest receives the daily brief only, and its themes are still
  generated and archived
- **Any OpenAI-compatible LLM endpoint** (default: OpenRouter), per-stage
  model overrides, optional reasoning effort
- Secrets in `.env` on the server only — never in config files, never in
  logs, never in an API response

## Server setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

newspipeline init          # creates config/settings.toml, data/, .env
$EDITOR .env               # set NEWS_API_KEY (OpenRouter key)
```

## Recipients and independent delivery

Multiple recipients can inherit shared defaults or another recipient's
configuration while overriding their own interests, feeds, deliverable types,
word targets, detail levels, and delivery schedules. Collection and delivery
have separate per-interest clocks; scheduled output is persisted across restarts.
Deduplication and related-content groups are isolated by recipient and interest.
Existing single-recipient configurations remain supported. See
[Recipients, inheritance, and delivery](docs/recipients.md) for configuration,
API/client controls, CLI commands, and migration/recovery semantics.

## Configuration

Two files on the server, strictly separated:

| File | Contents |
|---|---|
| `.env` | **Secrets only**: `NEWS_API_KEY`, optional `NEWS_SMTP_PASSWORD` |
| `config/settings.toml` | Everything else: LLM endpoint/model, interests, feeds, schedules, delivery, SMTP host/port/user |

All paths resolve relative to the checkout. Set
`NEWSPIPELINE_HOME=/some/dir` to relocate them — the variable must be set
for every process that touches state (`run`, `serve`, the systemd unit).
See [Deployment on this host](#deployment-on-this-host) for the live layout.

Configure through the desktop client, or edit `config/settings.toml`
directly (see `config/settings.example.toml` for a fully commented
reference).

## Running (server)

```bash
# Manually, once (interests run sequentially, then delivery is dispatched):
newspipeline run --interest "Example"   # one interest, even if paused
newspipeline run --all                  # all non-paused interests

# Continuously (JSON API + scheduler):
newspipeline serve                      # binds 127.0.0.1:8421
newspipeline serve --no-scheduler       # API only, no scheduled runs
```

`run` exits 0 when every selected run completed, 1 otherwise (unknown
interest, missing API key, or a failed run — a failure alert email is sent
when a recipient is configured). `run` deliberately does NOT repair stale
rows: a concurrent `serve` process may legitimately own a running row.

`serve` starts the scheduler on a background thread. Behaviors to know:

- **Startup sweep**: if the previous process died mid-run, its orphaned
  `running` rows are marked `failed / 'interrupted by restart'` at serve
  startup and logged — the interests become schedulable again. No manual
  sqlite3 surgery needed.
- **Catch-up**: each interest whose most recent scheduled slot was missed runs
  immediately after startup — unless a completed (or still in-flight) run
  already covers that slot, so a restart never repeats a run that happened.
  A slot whose run failed or was interrupted is retried.
- **Paused interests** (all deliverables disabled) never run on a
  schedule; `run --interest NAME` can still force one manually.

## Client (desktop)

On your Ubuntu desktop:

```bash
pip install ./client       # pulls PySide6 + httpx; nothing server-side
newspipeline-client
```

Enter host, SSH user, and the server's API port (default 8421), then
Connect. The client owns the full tunnel lifecycle: it picks a free local
port at runtime, spawns the forward, polls readiness, tears down cleanly
on exit or crash, and shows connection state prominently. Your connection
profile (host, user, port — no secrets) persists under
`~/.config/newspipeline-client/profile.json`; authentication is your
normal SSH key.

The desktop administration client manages all recipients from one connection.
Use **Recipients and defaults** for recipient inheritance and shared templates;
the recipient selector scopes **Interests** and **Run history**. See the
[client guide](client/README.md) for configuration editing and the upgrade to
client 0.6.1.

The client's tunnel spawn runs `ssh` with `BatchMode=yes`, which cannot
prompt for a password even when the server allows one. For
`devops@37.60.240.152` password authentication stays enabled in any case
(operator requirement — the account is *not* to be switched to key-only);
two supported ways to use the client with that profile:

1. Manual tunnel: run `ssh -N -L 8421:127.0.0.1:8421 devops@37.60.240.152`
   yourself (password prompt works), then point the client at
   `http://127.0.0.1:8421` with its tunnel mode off.
2. Supervised key: additionally install a desktop SSH key
   (`ssh-copy-id devops@37.60.240.152`) so `BatchMode` succeeds. This adds
   a key alongside the password; it does **not** disable password
   authentication.

(Auth errors are shown in the GUI without crashing: tunnel callbacks are
marshalled onto the GUI thread, so a failure surfaces as an error dialog
rather than a segfault.)

Tabs mirror the old web UI: global settings, interests (CRUD, feeds,
schedule, deliverable toggles, delivery modes, recipient override, paused
banner, trigger-now), and run history. Renaming an interest keeps its
history — renaming asks a trivial confirmation.

## Output (server-side archive)

- Local files: `<output_dir>/<interest>/<run-date>/` under
  `NEWSPIPELINE_HOME` — an archive on the VPS
- Email: the daily brief, plus one email per theme with its enabled
  deliverables — unless the interest sets `email_themes = false`, in which
  case the brief is its only mail
- Run history and all scraped articles: `data/news.db` (SQLite)

Runs are linked to interests **by name**. Deleting an interest still
orphans its history — but **renaming preserves it end-to-end** (runs rows
are repointed atomically-first; see DECISIONS.md D11).

## Deployment on this host

The live install runs on **this machine** under the existing `devops`
account — no dedicated system user (operator decision, D21; supersedes the
0.5.x VPS deployment under a dedicated `newspipeline` user). Full
step-by-step procedure (checkout, venv, state, systemd, upgrades): see
[DEPLOYMENT.md](DEPLOYMENT.md). The layout in brief:

```bash
~/newspipeline            # checkout — also NEWSPIPELINE_HOME
├── .venv/                # CPython 3.12 via uv; pip install -e ".[dev]"
├── config/settings.toml  # non-secret configuration
├── .env                  # secrets (NEWS_API_KEY, NEWS_SMTP_PASSWORD), 0600
├── data/news.db          # SQLite state
└── output/               # server-side archive
```

Install the systemd unit (it ships matching `User=devops`,
`NEWSPIPELINE_HOME=/home/devops/newspipeline` and
`ReadWritePaths` for the checkout; `ProtectHome` stays off because the venv
lives in the home directory):

```bash
sudo cp deploy/newspipeline.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now newspipeline.service
journalctl -u newspipeline -f
```

The unit restarts always (`Restart=always`, `RestartSec=5` — a fresh start
sweeps any interrupted runs) and keeps the sandbox hardening
(`ProtectSystem=strict`, `PrivateTmp=true`, …). Code arrives via
`git pull` + the hermetic test gate; see DEPLOYMENT.md **Upgrades**.

### sshd configuration (informational — no change required)

Password authentication for `devops@37.60.240.152` **stays enabled**: the
account must not be switched to key-only operation (operator requirement).
The API's loopback-only bind remains the security boundary for the
management path; no `PasswordAuthentication no` or similar sshd
restrictions are part of this deployment.

## Gmail relay

For email delivery via Gmail, use a **dedicated sender Google account**
(the receiver should be a different, personal address):

1. In `config/settings.toml` `[smtp]`: `host = "smtp.gmail.com"`,
   `port = 587`, `user = "<the sending Gmail address>"`,
   `sender = "<same address>"` (fully commented example in
   `config/settings.example.toml`). The certificate-verifying STARTTLS
   path is used automatically.
2. Enable 2FA on the sender account, then create an **App password**
   (Google Account → Security → 2-Step Verification → App passwords).
   Put it in the server's `.env` as `NEWS_SMTP_PASSWORD`.
3. The **first send from a new IP may trip Google's security review** —
   approve it at <https://myaccount.google.com/security> ("was this you"
   / recent activity). Until approved, sends fail with SMTP auth errors;
   the pipeline retries twice before failing the delivery.
4. Gmail caps relayed mail around 500 messages/day — irrelevant at this
   volume (a handful of emails per interest per day).

Failure alerts follow the existing recipient resolution (interest
recipient override, else the global fallback address).

## Development

```bash
pytest tests/ -v
# With the editable desktop client installed (pip install -e ./client):
QT_QPA_PLATFORM=offscreen pytest client/tests/ -v
```

The suite includes the guard tests G1–G10 (secrets, URL dedupe, scheduler
semantics, language detection, end-to-end run, event-loop responsiveness,
SMTP TLS verification, rename-preserves-history, startup sweep, LLM
null-content) defined
in DECISIONS.md; tests run entirely against local fixtures and never
touch the real `config/settings.toml`, `.env`, or `data/news.db`.

## Human-verification checklist

These steps need live credentials, remote hosts, or human eyes and are
therefore mocked in tests but must be verified manually once:

- [x] **Live Gmail inbox delivery** (2026-09-18, operator-witnessed on the
      live install): the brief and theme emails arrive in the personal
      (receiver) inbox, including the 0.5.0 HTML-first layout.
- [ ] **First-send security approval**: on the first send from this
      host's IP, approve Google's security review at
      myaccount.google.com/security and confirm subsequent sends succeed
      without intervention.
- [ ] **This-host deployment** (redo for the new layout, D21): the unit
      starts and serves under `devops`, runs complete, and `systemctl stop`
      exits cleanly. (The 2026-09-18 VPS verification covered the old
      dedicated-user layout, now superseded.)
- [ ] **Crash recovery on the live install**: `kill -9` mid-run leaves the
      run orphaned, the row is swept at the next serve startup on the real
      host, and the interest reruns (unit-level behaviour is covered by G9;
      this row is the on-host confirmation for the current install).
- [ ] **Tunnel end-to-end**: from the desktop, connect via the client and
      exercise every tab; verify direct access to port 8421 from outside
      is refused (loopback bind only).

The 2026-09-18 ticked rows were verified on the previous VPS install; with
the move to this host (D21) every checklist row needs its first manual pass
against the current install.

Project decisions and invariants: [DECISIONS.md](DECISIONS.md).
Deployment: [DEPLOYMENT.md](DEPLOYMENT.md). Changes:
[CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
