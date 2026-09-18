# newspipeline-client

Desktop GUI for managing a [newspipeline](../README.md) server over an
SSH tunnel. PySide6 + httpx; no server code, no GUI dependencies on the
VPS.

```bash
pip install ./client
newspipeline-client
```

`pip install ./client` installs a **copied, not editable** package: edits to
`client/src/` have no effect until you reinstall
(`pip install --force-reinstall ./client`). For development, install it
editable instead: `pip install -e ./client`.

Enter host, SSH user, and the server's API port (default 8421), then
Connect. The app owns the full tunnel lifecycle: it picks a free local
port at runtime, spawns `ssh -N -L`, polls readiness until the API
answers, tears the forward down cleanly on exit or crash, and shows the
connection state prominently. Errors surface with the ssh stderr tail.

The connection profile (host, user, port) is stored under
`~/.config/newspipeline-client/profile.json` and contains **no secrets**
— authentication is your normal SSH key.

Tabs:

- **Global settings** — LLM endpoint/model(s), reasoning effort,
  timezone, output dir, SMTP relay. The SMTP password and LLM API key
  deliberately have no fields here: they live in `.env` on the VPS only.
- **Interests** — create/edit/delete, feeds (add/remove), schedule,
  deliverable toggles, delivery modes + recipient override + the per-theme
  email toggle ("Also email each theme separately"), a visible
  PAUSED banner when all deliverables are off, trigger-now, and rename
  (run history is preserved; trivial confirm).
- **Run history** — recent runs per interest with status, stage, and
  errors.

# version 0.5.0
