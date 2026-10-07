# newspipeline-client

Desktop administration GUI for managing multiple recipients on a [newspipeline](../README.md) server over an
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

The desktop is an **administration client**. One SSH connection manages all
recipients on the server. Recipient ids are stable identities; email addresses
are delivery destinations. Recipients can independently subscribe to interests
with the same name, with their own feeds, output settings, history and delivery.

Tabs:

- **Global settings** — shared LLM endpoint/models, reasoning effort,
  server timezone, output directory and SMTP relay, plus the legacy user.
  API keys and SMTP passwords remain in `.env` on the server.
- **Recipients and defaults** — create/delete recipients; edit email, parent
  recipient, deliverable types, word targets, detail levels, delivery modes
  and delivery timing defaults. Partial checkbox state, unchecked word overrides
  and `Inherit` selections preserve inheritance. Reparent children before
  deleting their parent.
- **Interests** — the selected recipient's effective interests, feeds,
  collection schedule, deliverables, analysis input, email overrides and
  separate delivery schedule; create, edit, exclude, rename and trigger.
  Saves override only changed settings. Restore one override (including feeds
  or the delivery schedule) with **Restore inheritance for one setting…**,
  or clear all local settings with **Use inherited settings for this interest**.
- **Run history** — recent runs and queued delivery jobs for the selected
  recipient, including errors, due times and attempts. Select a failed job
  to retry delivery. Successful and pending jobs cannot be retried.

The recipient selector above the tabs scopes **Interests** and **Run history**.
**Global settings** and **Recipients and defaults** administer the whole server.
`Legacy configuration` selects the original single-account interests; shared
defaults are templates and do not run as a recipient.

Manage a recipient:

1. Connect, then open **Recipients and defaults** and create an immutable id.
2. Select that recipient in the left list. Set its email or inherit an address,
   choose an optional parent id, and save its defaults.
3. Select its id in the selector above the tabs. Add an interest or edit an
   inherited one; add feeds and choose collection and delivery times separately.
4. Trigger a run and inspect its history and delivery status in **Run history**.

Shared named interest templates and all additional settings are available via
**Edit complete configuration and interest templates…** in **Recipients and
defaults**. This advanced editor uses sparse JSON and saves through the validated
server API. It edits the selected recipient or **Shared defaults**, including
named interests, feeds, collection schedules, analysis settings, `max_themes`,
and per-theme email preferences. It replaces that selected raw configuration;
keep any existing fields you want to retain. The standard form preserves fields
that it does not expose.

For example, select **Shared defaults** and save:

```json
{
  "interest_defaults": {
    "enable_summary": true,
    "target_summary_words": 300,
    "summary_detail": "standard"
  },
  "interests": [
    {
      "name": "Energy",
      "start_time": "04:00",
      "delivery_schedule": {"start_time": "09:00", "timezone": "Europe/Berlin"},
      "feeds": [{"name": "Energy feed", "url": "https://example.com/energy.xml"}]
    }
  ]
}
```

Recipients inherit this subscription. Parent and recipient overrides take
precedence; named interests merge by name, mappings merge recursively, and
feed lists replace inherited lists. Omit a field to inherit it; `false`, empty
strings and empty feed lists are explicit overrides. An empty delivery schedule
means immediate delivery. Removing a local interest entry restores inheritance
rather than excluding an inherited subscription: use `"enabled": false` to
exclude it and `"enabled": true` to restore it. You can also restore an excluded
interest with **New interest…** using its existing name.

Exclusion and recipient deletion retain stored history and queued delivery
jobs. Already queued jobs retain their original routing and settings. A normal
server scheduler must run to drain deliveries; retries can repeat an email if
an earlier attempt partially succeeded. Recipient files live under
`output/recipients/<id>/` on the server. See [recipient contracts](../docs/recipients.md)
for inheritance, rename propagation and recovery details.

Install client **0.6.1** alongside a server with the recipient API. After updating
this repository, reinstall a copied client (`pip install --upgrade ./client`);
restarting an old installation does not pick up source changes. For development:

```bash
pip install -e ./client
QT_QPA_PLATFORM=offscreen pytest client/tests/ -v
```

Manual desktop checks: connect over SSH; create two recipients sharing an
interest name; change one recipient's feeds/settings and check the other;
change a shared template and verify unoverridden values propagate; restore one
override; exclude/restore a subscription; switch recipients while requests are
in flight; inspect and retry a failed delivery; disconnect/reconnect cleanly.
Offscreen tests exercise API routing, editor saves and recipient switching;
visual desktop rendering and a live remote SSH session remain manual checks.

# version: 0.6.1
