# DEPLOYMENT.md — newspipeline on the brainy VPS

Deployment record for the production install on the shared VPS, set up on
2026-09-05. Brainy (Docker compose stack) and newspipeline (native systemd
service) run side by side on the same host; brainy owns host ports 80/443
via Caddy, newspipeline is loopback-only and touches neither.

## Host

| What | Value |
|---|---|
| Public IP | `37.60.240.152` |
| Domain | `brainy.gronowski.cc` (brainy only — newspipeline has no domain) |
| OS | Ubuntu 24.04.4 LTS, Python 3.12 |
| Sizing | 6 vCPU / 12 GB RAM / 96 GB SSD |
| SSH user | `brainy` (password login enabled; password in brainy's `VPS.md`) |

At deployment time: ~880 MiB RAM and ~6.8 GB disk in use across both stacks.

## What is deployed (newspipeline)

- Code: repo `main` @ `2697a69`, checked out at `/opt/newspipeline`
  (owned by user `newspipeline`).
- Virtualenv: `/opt/newspipeline/.venv` (system Python 3.12;
  `python3.12-venv` was installed via apt to create it).
- Service: systemd unit `newspipeline.service`, copied verbatim from
  `deploy/newspipeline.service` to `/etc/systemd/system/`.
- State (`NEWSPIPELINE_HOME=/var/lib/newspipeline`, owned by user
  `newspipeline`, mode 750):
  - `/var/lib/newspipeline/config/settings.toml` — pipeline config
    (**currently the example copy — must be edited with real feeds/interests**)
  - `/var/lib/newspipeline/.env` — **secrets: `NEWS_API_KEY`,
    `NEWS_SMTP_PASSWORD` live here** (must be created by the operator,
    0600, owner `newspipeline`; nothing else stores them — there is no
    secret-setting API endpoint by design, G1)
  - `/var/lib/newspipeline/data/news.db` — SQLite (runs + articles)
  - `/var/lib/newspipeline/output/` — generated briefs
- API: `http://127.0.0.1:8421` — loopback bind only, no TLS, no tokens.
  Reached from the desktop via SSH tunnel (security model, do not expose).

## Access from the desktop

```bash
ssh -L 8421:127.0.0.1:8421 brainy@37.60.240.152
# then point newspipeline-client at http://127.0.0.1:8421
```

## Day-to-day commands (as `brainy`, on the VPS)

```bash
systemctl status newspipeline        # stack status (Type=simple, Restart=always)
journalctl -u newspipeline -f        # live logs (ISO 8601 timestamps)
sudo systemctl restart newspipeline  # needed after editing .env
curl -fsS http://127.0.0.1:8421/api/interests   # sanity check, on the VPS
```

`config/settings.toml` is re-read every scheduler tick — editing it needs no
restart. `.env` changes need a restart.

## Getting code onto the VPS (upgrades)

The GitHub repo is private, so the VPS cannot clone it. A bare repo lives on
the VPS with a post-receive hook that force-checks out `main` to
`/opt/newspipeline`. Local remote: `vps` → `brainy@37.60.240.152:/srv/git/newspipeline.git`.

```bash
# on the dev machine (sshpass mandatory — a bare git push would prompt):
SSHPASS='<brainy ssh password>' GIT_SSH_COMMAND="sshpass -e ssh" git push vps main

# on the VPS — venv tracks the repo, so a plain restart picks up new deps
# only after reinstalling; for dependency changes:
sudo /opt/newspipeline/.venv/bin/pip install -e /opt/newspipeline
sudo systemctl restart newspipeline
```

If a push reports `detected dubious ownership`, run once on the VPS:

```bash
git config --global --add safe.directory /srv/git/newspipeline.git
git config --global --add safe.directory /opt/newspipeline
```

## Operator checklist (one-time, after this doc's setup)

1. SSH in, create `/var/lib/newspipeline/.env` (owner `newspipeline`, 0600)
   with `NEWS_API_KEY` and `NEWS_SMTP_PASSWORD` — see `.env.example` in the
   repo for the full key list.
2. Edit `/var/lib/newspipeline/config/settings.toml` (currently the example
   copy) — replace the example feed/interests with real ones.
3. `sudo systemctl restart newspipeline`, then tunnel in and verify the API.

## Backup (suggested, not yet configured)

Nightly brainy backups run via `/etc/cron.d/brainy-backup` at 03:15 and only
cover Postgres. newspipeline state is separate — add a cron entry on the VPS
to back up `/var/lib/newspipeline` (news.db + settings.toml + .env) the same
way, e.g. an SQLite `VACUUM INTO` or file copy after `systemctl stop`.

## Notes

- The unit file hardens the service (`ProtectSystem=strict`,
  `ReadWritePaths=/var/lib/newspipeline`, etc.) — state must stay under
  `NEWSPIPELINE_HOME`.
- Renames go exclusively through `POST /api/interests/<name>/rename`; never
  edit interest names in settings.toml directly.
- Two pre-existing failed units on this host (`cloud-init.service`,
  `systemd-networkd-wait-online.service`) are unrelated noise.
- Brainy's credentials and server notes live in brainy's `VPS.md`
  (git-ignored, confidential). This file is safe to commit — no secrets here.
