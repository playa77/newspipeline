# DEPLOYMENT.md — newspipeline on THIS host (vmd192484 / 37.60.240.152)

Deployment for the **single-host install**: the project runs on this box
under the existing `devops` account — there is **no dedicated
`newspipeline` user** (D21). The checkout, the venv and all state live under
`/home/devops/newspipeline`; the JSON API binds loopback only and is reached
from the desktop via SSH tunnel.

> Historical note: earlier revisions (0.5.x) deployed on a VPS under a
> dedicated `newspipeline` user with state in `/var/lib/newspipeline` and a
> bare-repo push path. That model was superseded on 2026-10-08 (D21,
> operator decision): one machine, existing account, GitHub checkout. The
> host rows below now describe **this** machine.

## Host (verified live, 2026-10-08)

| What | Value |
|---|---|
| Hostname / public IP | `vmd192484` / `37.60.240.152` |
| OS | Ubuntu 26.04.1 LTS |
| Accounts | `devops` (sudo-capable owner of this install; system Python 3.14) |
| Python for the app | CPython **3.12** venv via uv (`~/newspipeline/.venv`) — the repo requires >=3.12 and nothing in it runs on a bare system Python |
| Domain | none needed (loopback API + SSH tunnel) |

## 1. Checkout + venv (as `devops`)

```bash
cd ~
git clone https://github.com/playa77/newspipeline   # or: git pull in an existing checkout
cd newspipeline
uv venv --python 3.12 .venv
uv pip install -e ".[dev]" --python .venv/bin/python
.venv/bin/newspipeline --version                    # sanity: CLI on PATH of the venv
.venv/bin/python -m pytest tests/ -q                # gate: hermetic suite must be green
```

Code changes are pushed to GitHub and pulled here; there is no bare-repo
push path on this host.

## 2. State layout (NEWSPIPELINE_HOME = the checkout)

All state lives under `/home/devops/newspipeline`:

- `config/settings.toml` — pipeline config (reference: `config/settings.example.toml`)
- `.env` — **secrets: `NEWS_API_KEY`, `NEWS_SMTP_PASSWORD`** (`0600`,
  git-ignored; nothing else stores them — there is no secret-setting API
  endpoint by design, G1)
- `data/news.db` — SQLite (runs + articles)
- `output/` — generated briefs

```bash
newspipeline init          # first time only: creates settings.toml, data/, .env
chmod 600 .env             # init already creates it 0600; verify if recreated
$EDITOR .env               # secrets — never in settings.toml, never committed
$EDITOR config/settings.toml
```

## 3. systemd service

```bash
sudo cp deploy/newspipeline.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now newspipeline.service
journalctl -u newspipeline -f
```

The unit (`deploy/newspipeline.service`, D21) runs as `User=devops` with
`NEWSPIPELINE_HOME=/home/devops/newspipeline`, `WorkingDirectory` at the
checkout, `Restart=always` (a fresh start sweeps interrupted runs, D10) and
sandbox options (`ProtectSystem=strict`, `ReadWritePaths` for the checkout,
`PrivateTmp`, …). `ProtectHome` stays off because the home directory holds
the venv being executed.

Sanity check, on the host:

```bash
curl -fsS http://127.0.0.1:8421/api/interests
```

The API binds 127.0.0.1 only, no TLS, no tokens — reached from the desktop
via SSH tunnel (the security model; never expose it).

## Access from the desktop

```bash
ssh -N -L 8421:127.0.0.1:8421 devops@37.60.240.152
# then point newspipeline-client at http://127.0.0.1:8421
```

The client does this itself; its connection profile is
host `37.60.240.152`, user `devops`, port 8421. Password authentication
stays enabled for this account (operator requirement — no key-only
switch); either run the tunnel manually (`ssh -N -L
8421:127.0.0.1:8421 devops@37.60.240.152`, password prompt works) or give
the client a supplementary desktop key via `ssh-copy-id devops@37.60.240.152`
(adding a key does not disable password auth; the client's `BatchMode`
spawn cannot prompt for a password itself).

## Day-to-day commands (on the host)

```bash
systemctl status newspipeline        # stack status (Type=simple, Restart=always)
journalctl -u newspipeline -f        # live logs (ISO 8601 timestamps)
sudo systemctl restart newspipeline  # needed after editing .env
curl -fsS http://127.0.0.1:8421/api/interests   # sanity check, on the host
```

`config/settings.toml` is re-read every scheduler tick — editing it needs no
restart. `.env` changes need a restart.

## Upgrades

```bash
cd ~/newspipeline
git pull                            # code changes arrive via GitHub
.venv/bin/python -m pytest tests/ -q    # gate before restarting
sudo systemctl restart newspipeline    # only when the code (not just config) changed
# venv deps need attention only when pyproject.toml changed:
uv pip install -e ".[dev]" --python .venv/bin/python
```

## Backup (suggested, not yet configured)

All state is self-contained under `/home/devops/newspipeline` (`data/news.db`,
`config/settings.toml`, `.env`, `output/`). A nightly copy of those paths is
sufficient; the backup must not be world-readable (it contains `.env`).

## Notes

- State must stay under `NEWSPIPELINE_HOME`; the unit's
  `ReadWritePaths` enforces the write side of that.
- Renames go exclusively through `POST /api/interests/<name>/rename`; never
  edit interest names in settings.toml directly.
- This file is safe to commit — no secrets here. Operator credentials live
  in private notes outside the repo.

## Recipient configuration and scheduled-delivery upgrade

The recipient/interest scope migration runs automatically when Store opens.
Stop the service and make a consistent SQLite backup before upgrading the
code, then start the service again (as `devops`). Migration preserves ids
and content and rolls back invalid foreign keys; do not bypass a migration
failure. The new schema is not compatible with an old server binary:
rollback requires restoring the pre-upgrade database backup. No extra
service or system account is needed.

Configure recipients and inherited defaults in settings.toml or through the
SSH-tunneled API/client. Existing legacy configuration is supported. Keep a
normal `newspipeline serve` running: `--no-scheduler` does not drain scheduled
output. Inspect delivery status in the client's Run history or
`GET /api/deliveries?recipient=<id>`; failed jobs require explicit retries.
Interrupted sends recover on restart and can repeat emails in the SMTP/SQLite
crash window. Recipient archives are under `output/recipients/<id>/` (relative
to NEWSPIPELINE_HOME unless output_dir is absolute); ensure the service user
(`devops`) retains write access. Details and API contracts:
[Recipients and delivery](docs/recipients.md).

Reinstall the desktop distribution separately after this upgrade:
`pip install --upgrade ./client` on each administration desktop, then restart
`newspipeline-client`. Client 0.6.1 requires the recipient-capable server API;
updating only the server does not update a copied desktop installation. Verify
two recipient scopes, shared-template inheritance and delivery retries using
the [client checklist](client/README.md).