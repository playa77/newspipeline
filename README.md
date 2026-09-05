# newspipeline

Headless news pipeline **server** for a small Linux box or VPS: monitors
RSS feeds, extracts themes with an LLM, generates summaries / video
scripts / a daily brief, and delivers them by email or as local markdown
files. Managed from your Ubuntu desktop by the separate **newspipeline
client** GUI over an SSH tunnel.

Python 3.12+, no Docker, no database server, no build step.

> Status: **0.3.0** — fully implemented; guard-test suite green
> (`pytest tests/`). See [WORKPACKAGES.md](WORKPACKAGES.md) for the plan
> and per-package state.

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
- Pipeline stages: **scrape** (RSS + full-article extraction) →
  **analyze** (LLM theme identification) → **generate** (summary,
  EN/DE YouTube scripts) → **brief** (daily synthesis)
- **Built-in scheduler**: per-interest start time + interval, catch-up of
  missed runs on startup, startup sweep of runs interrupted by a crash,
  pause by disabling all deliverables
- **Delivery**: email via SMTP (Gmail relay documented below) and/or local
  markdown files (a server-side archive)
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

## Configuration

Two files on the server, strictly separated:

| File | Contents |
|---|---|
| `.env` | **Secrets only**: `NEWS_API_KEY`, optional `NEWS_SMTP_PASSWORD` |
| `config/settings.toml` | Everything else: LLM endpoint/model, interests, feeds, schedules, delivery, SMTP host/port/user |

All paths resolve relative to the checkout. Set
`NEWSPIPELINE_HOME=/some/dir` to relocate them — the variable must be set
for every process that touches state (`run`, `serve`, the systemd unit).
See [VPS deployment](#vps-deployment) for the recommended layout.

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
- **Catch-up**: each interest whose most recent scheduled slot was missed
  runs immediately after startup.
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

**SSH key requirement.** The tunnel runs `ssh` with `BatchMode=yes`, so
only key-based auth works — password prompts are deliberately disabled to
fail fast instead of hanging. Before first connect, install your key on
the server account:

```bash
ssh-keygen -t ed25519             # skip if you already have a key
ssh-copy-id <user>@<server-host>
ssh -o BatchMode=yes <user>@<server-host> 'echo OK'   # must print OK
```

If connect fails with `Permission denied (publickey,password,...)`, the
key for that user is missing or not accepted — re-run the steps above.
(Errors like this are shown in the GUI without crashing: tunnel callbacks
are marshalled onto the GUI thread, so an auth failure surfaces as an
error dialog rather than a segfault.)

Tabs mirror the old web UI: global settings, interests (CRUD, feeds,
schedule, deliverable toggles, delivery modes, recipient override, paused
banner, trigger-now), and run history. Renaming an interest keeps its
history — renaming asks a trivial confirmation.

## Output (server-side archive)

- Local files: `<output_dir>/<interest>/<run-date>/` under
  `NEWSPIPELINE_HOME` — an archive on the VPS
- Email: daily brief plus one email per theme with its enabled deliverables
- Run history and all scraped articles: `data/news.db` (SQLite)

Runs are linked to interests **by name**. Deleting an interest still
orphans its history — but **renaming preserves it end-to-end** (runs rows
are repointed atomically-first; see DECISIONS.md D11).

## VPS deployment

Suggested layout:

```bash
sudo useradd -r -m -d /var/lib/newspipeline -s /usr/sbin/nologin newspipeline
sudo mkdir -p /var/lib/newspipeline/{config,data,output}
sudo cp settings.example.toml /var/lib/newspipeline/config/settings.toml
sudoedit /var/lib/newspipeline/.env        # NEWS_API_KEY (+ NEWS_SMTP_PASSWORD); chmod 600
```

Install the code (e.g. `/opt/newspipeline` checkout + its own venv), then:

```bash
sudo cp deploy/newspipeline.service /etc/systemd/system/
$EDITOR /etc/systemd/system/newspipeline.service   # adjust ExecStart/User paths
sudo systemctl daemon-reload
sudo systemctl enable --now newspipeline.service
journalctl -u newspipeline -f
```

The unit uses `NEWSPIPELINE_HOME=/var/lib/newspipeline`, restarts always
(`Restart=always`, `RestartSec=5` — a fresh start sweeps any interrupted
runs), and ships reasonable hardening (`ProtectSystem=strict`,
`ProtectHome=true`, `PrivateTmp=true`, …).

### sshd hardening for the management tunnel

Disable password authentication for the server account (key-only SSH):

```bash
# /etc/ssh/sshd_config:
PasswordAuthentication no
KbdInteractiveAuthentication no
```

Optionally restrict the client's deploy key to exactly this forwarding
use, in `/home/<user>/.ssh/authorized_keys` (or
`/var/lib/newspipeline/.ssh/authorized_keys`):

```
restrict,permitopen="127.0.0.1:8421" ssh-ed25519 AAAA... client@desktop
```

`restrict` disables pty/agent/X11/port forwarding beyond the single
permitted open; `permitopen="127.0.0.1:8421"` allows only the API's local
forward. Reload sshd afterwards: `sudo systemctl reload ssh`.

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
```

The suite includes the guard tests G1–G9 (secrets, URL dedupe, scheduler
semantics, language detection, end-to-end run, event-loop responsiveness,
SMTP TLS verification, rename-preserves-history, startup sweep) defined
in DECISIONS.md; tests run entirely against local fixtures and never
touch the real `config/settings.toml`, `.env`, or `data/news.db`.

## Human-verification checklist

These steps need live credentials, remote hosts, or human eyes and are
therefore mocked in tests but must be verified manually once:

- [ ] **Live Gmail inbox delivery**: configure the Gmail relay per above,
      trigger a run, confirm the brief/theme emails arrive in the personal
      (receiver) inbox.
- [ ] **First-send security approval**: on the first send from the VPS
      IP, approve Google's security review at myaccount.google.com/security
      and confirm subsequent sends succeed without intervention.
- [ ] **VPS deployment**: follow the VPS deployment section on a fresh
      VPS; confirm the unit starts, survives `kill -9` mid-run (row swept,
      interest reruns), and `systemctl stop` exits cleanly.
- [ ] **Tunnel end-to-end**: from the desktop, connect via the client and
      exercise every tab; verify direct access to port 8421 from outside
      is refused (loopback bind only).

Project decisions and invariants: [DECISIONS.md](DECISIONS.md).
Changes: [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
