# DEPLOYMENT.md — newspipeline on a fresh VPS

Deployment procedure for a **dedicated newspipeline install**: the project
always runs under its own user `newspipeline`. Never reuse another user's
account for this project — the service account, state
directory, checkout, and SSH access all belong to `newspipeline`. On a host
that already runs other stacks (the shared VPS), the two stay fully
separate: this project owns no host ports, other stacks keep 80/443.

**Dev-phase philosophy: low friction first.** During development the
`newspipeline` account is a normal, fully usable user — interactive SSH,
SFTP, a real shell. No OS/account hardening (nologin shells, key-only SSH,
restricted authorized_keys) at this stage; hardening is a deliberate later
phase, only when development stabilizes. The application's own security
model (loopback API + SSH tunnel, secrets in `.env` only) is unchanged by
this — it is part of the app, not account hardening.

## Host (reference: the current VPS)

| What | Value |
|---|---|
| Public IP | `37.60.240.152` |
| OS | Ubuntu 24.04.4 LTS, Python 3.12 |
| Sizing | 6 vCPU / 12 GB RAM / 96 GB SSD |
| User | `newspipeline` (dedicated to this project, full login access during dev) |
| Domain | none needed (loopback API + SSH tunnel) |

## 1. Create the dedicated user

```bash
sudo useradd -m -d /var/lib/newspipeline -s /bin/bash newspipeline
sudo passwd newspipeline
sudo mkdir -p /opt/newspipeline /srv/git
sudo chown newspipeline:newspipeline /opt/newspipeline /srv/git
```

`-m` = home at `/var/lib/newspipeline`, which is also `NEWSPIPELINE_HOME`
(all state lives there). `/bin/bash` gives a fully usable interactive
account — log in as `newspipeline` directly; no `sudo -u` indirection is
needed except where the doc says `sudo` (root-only actions like systemd and
package installs).

## 2. Git delivery path (private repo)

The repo is private, so the VPS cannot clone from GitHub. SSH in as
`newspipeline` and set up a bare repo with a `post-receive` hook that
force-checks out `main` to `/opt/newspipeline`:

```bash
ssh newspipeline@37.60.240.152          # interactive login works (dev phase)
git init --bare /srv/git/newspipeline.git
cat > /srv/git/newspipeline.git/hooks/post-receive <<'EOF'
#!/bin/sh
git --work-tree=/opt/newspipeline --git-dir=/srv/git/newspipeline.git checkout -f main
EOF
chmod +x /srv/git/newspipeline.git/hooks/post-receive

# silence git's ownership check for the hook's user (once):
git config --global --add safe.directory /srv/git/newspipeline.git
git config --global --add safe.directory /opt/newspipeline
```

From the dev machine (a bare push would prompt — sshpass is mandatory; the
password lives in the operator's private notes, never in this repo):

```bash
git remote add vps newspipeline@37.60.240.152:/srv/git/newspipeline.git  # first time
SSHPASS='<newspipeline ssh password>' GIT_SSH_COMMAND="sshpass -e ssh" git push vps main
```

## 3. Install code + venv

```bash
ssh newspipeline@37.60.240.152
sudo apt install -y python3.12-venv        # sudo-capable during dev
cd /opt/newspipeline
python3.12 -m venv .venv
/opt/newspipeline/.venv/bin/pip install /opt/newspipeline
```

## 4. State layout (NEWSPIPELINE_HOME)

Owned by user `newspipeline`:

- `/var/lib/newspipeline/config/settings.toml` — pipeline config (copy
  `config/settings.example.toml` from the checkout as the starting point)
- `/var/lib/newspipeline/.env` — **secrets: `NEWS_API_KEY`,
  `NEWS_SMTP_PASSWORD`** (created by the operator, 0600; nothing else
  stores them — there is no secret-setting API endpoint by design, G1)
- `/var/lib/newspipeline/data/news.db` — SQLite (runs + articles)
- `/var/lib/newspipeline/output/` — generated briefs

```bash
cp /opt/newspipeline/config/settings.example.toml \
   /var/lib/newspipeline/config/settings.toml
cp /opt/newspipeline/.env.example /var/lib/newspipeline/.env
chmod 600 /var/lib/newspipeline/.env
$EDITOR /var/lib/newspipeline/.env           # fill in real secrets
$EDITOR /var/lib/newspipeline/config/settings.toml   # real feeds/interests
```

## 5. systemd service

```bash
sudo cp /opt/newspipeline/deploy/newspipeline.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now newspipeline.service
journalctl -u newspipeline -f
```

The unit ships with `User=newspipeline`,
`Environment=NEWSPIPELINE_HOME=/var/lib/newspipeline`, sandbox options
(`ProtectSystem=strict`, `ReadWritePaths=/var/lib/newspipeline`, …) and
`Restart=always` — no edits needed when the standard layout is used.
`systemctl stop` sends SIGTERM; the CLI shuts the scheduler down cleanly.

Sanity check, on the VPS:

```bash
curl -fsS http://127.0.0.1:8421/api/interests
```

The API binds 127.0.0.1 only, no TLS, no tokens — reached from the desktop
via SSH tunnel (the security model; never expose it).

## Access from the desktop

```bash
ssh -N -L 8421:127.0.0.1:8421 newspipeline@37.60.240.152
# then point newspipeline-client at http://127.0.0.1:8421
```

The client does this itself; its connection profile is
host `37.60.240.152`, user `newspipeline`, port 8421. The same account
accepts interactive SSH/SFTP for dev work — that is intentional during the
dev phase.

## Day-to-day commands (on the VPS)

```bash
systemctl status newspipeline        # stack status (Type=simple, Restart=always)
journalctl -u newspipeline -f        # live logs (ISO 8601 timestamps)
sudo systemctl restart newspipeline  # needed after editing .env
curl -fsS http://127.0.0.1:8421/api/interests   # sanity check, on the VPS
```

`config/settings.toml` is re-read every scheduler tick — editing it needs no
restart. `.env` changes need a restart.

## Upgrades

```bash
# on the dev machine:
SSHPASS='<newspipeline ssh password>' GIT_SSH_COMMAND="sshpass -e ssh" git push vps main

# on the VPS — only needed when dependencies changed:
/opt/newspipeline/.venv/bin/pip install /opt/newspipeline
sudo systemctl restart newspipeline
```

## Later: hardening (deliberately NOT part of dev setup)

When development stabilizes, candidates in rough order of value: switch the
desktop client and git remote to SSH key auth, then `PasswordAuthentication no`
in `/etc/ssh/sshd_config`; keep the interactive shell (a `nologin` shell
would break this dev workflow, so only revisit if the account ever stops
needing interactive use); optional `restrict,permitopen="127.0.0.1:8421"`
on the client's deploy key (see README.md). None of this is needed for a
working dev install.

## Backup (suggested, not yet configured)

newspipeline state is self-contained in `/var/lib/newspipeline`. Add a
nightly cron entry on the VPS backing it up (news.db + settings.toml +
.env), e.g. an SQLite `VACUUM INTO` or a file copy after `systemctl stop`.
Keep the backup owned by root or `newspipeline`, never world-readable
(it contains `.env`).

## Notes

- State must stay under `NEWSPIPELINE_HOME`; the unit's sandbox enforces it.
- Renames go exclusively through `POST /api/interests/<name>/rename`; never
  edit interest names in settings.toml directly.
- Two pre-existing failed units on the shared host (`cloud-init.service`,
  `systemd-networkd-wait-online.service`) are unrelated noise.
- This file is safe to commit — no secrets here. Operator credentials live
  in private notes outside the repo.
