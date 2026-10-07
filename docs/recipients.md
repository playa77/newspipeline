# Recipients, inheritance, and interest delivery

The legacy `[user]` and `[[interests]]` configuration continues to work. New
`[[recipients]]` entries have immutable ids and separate interests, feeds,
output preferences, histories, and deduplication scopes. An email address is
routing information, not identity: changing it does not disconnect history.
The same interest name can appear under several recipients.

## Configuration

Add these sections to `config/settings.toml`, alongside the existing global
and SMTP settings. The shared defaults are templates, not another recipient.
This example creates two independently processed subscriptions to Energy:

```toml
[recipient_defaults.interest_defaults]
enable_summary = true
enable_script = false
enable_script_de = false
enable_brief = true
target_summary_words = 300
summary_detail = "standard"
delivery = ["file", "email"]

[[recipient_defaults.interests]]
name = "Energy"
start_time = "04:00"                 # collection/generation
interval_hours = 24
# Each interest owns its delivery clock; collection and delivery are separate.
delivery_schedule = {start_time = "09:00", interval_hours = 24, timezone = "Europe/Berlin"}

[[recipient_defaults.interests.feeds]]
name = "Energy feed"
url = "https://example.com/energy.xml"
category = "energy"

[[recipients]]
id = "alice"
email = "alice@example.com"

[[recipients.interests]]
name = "Energy"
target_summary_words = 450
summary_detail = "detailed"

[[recipients]]
id = "bob"
parent = "alice"
email = "bob@example.com"

[[recipients.interests]]
name = "Energy"
target_summary_words = 150
summary_detail = "concise"
delivery_schedule = {start_time = "12:00"}
# Replace inherited subscriptions with an independent feed list.
feeds = [{name = "Alternative energy feed", url = "https://example.com/alternative.xml"}]
```

Inheritance is resolved on every scheduler tick:

1. Shared `recipient_defaults`.
2. The recipient's parent chain (other recipient ids).
3. The recipient's explicit overrides.

Mappings merge recursively; scalar values and lists replace inherited values.
Named `interests` are special: entries merge by name, and omitted entries remain
inherited. `interest_defaults` supplies settings for every interest; named
interest settings take precedence over those defaults. Sparse configuration is
retained on disk so subsequent parent edits propagate. Empty `interest_defaults`
means no overrides. `false`, empty strings, and empty feed lists are valid
explicit overrides. `enabled = false` excludes an inherited interest; setting it
back to true restores it. Missing parents, cycles, duplicate recipient ids, and
duplicate names within one configuration are rejected.

Deliverables use the existing independent toggles and word targets:
`enable_summary`/`target_summary_words`, `enable_script`/`target_script_words`,
`enable_script_de`/`target_script_de_words`, and
`enable_brief`/`target_brief_words`. Their respective `summary_detail`,
`script_detail`, `script_de_detail`, and `brief_detail` settings accept
`concise`, `standard`, or `detailed`. The detail setting is included in the LLM
prompt with the word target; model output length remains a target, not a hard
truncation limit. An interest with all four deliverables disabled is paused.

## Delivery timing and routing

`start_time` and `interval_hours` control collection/generation as before.
`delivery_schedule` independently controls delivery of completed content:

- `{}` or an omitted schedule delivers immediately after generation.
- A nonempty schedule uses `start_time` (`HH:MM`), `interval_hours` (1–24,
  default 24), and optional IANA `timezone` (server timezone by default).
- Delivery slots repeat within each local calendar day, starting at the given
  time and wrapping across midnight. Content is queued for the first slot at
  or after generation completes. A run finishing after its slot waits for the
  next one; an already queued overdue job is delivered on restart.
- The scheduler checks due jobs every 60 seconds. Delivery may therefore occur
  up to a polling interval after the clock time, plus send/write duration.
- DST gaps skip nonexistent wall times; repeated wall times run once at their
  first occurrence.

The interest's nonempty `email_recipient` overrides its owning recipient's
email. New recipient interests never fall back to the legacy user's email.
The server's SMTP transport and LLM connection remain shared. Secrets stay in
`.env`, outside recipient configuration and queued delivery payloads.

Queued jobs persist in SQLite and capture the recipient, deliverable choices,
and non-secret output/SMTP settings used for the completed run. Later settings
edits, pauses, and deletions do not redirect or remove already queued output.
A normal `newspipeline serve` process must run to drain the queue;
`serve --no-scheduler` and a one-shot `run` do not drain scheduled jobs. Queue
processing does not require an LLM key.

Successful jobs are not sent again. Failures remain visible as `failed`, with
an error and attempt count; retry them explicitly through Run history or the
API. An interrupted `sending` job becomes pending on serve startup. SMTP and
SQLite cannot commit atomically: a crash after SMTP accepts a message but before
the queue records success can cause a repeated email on recovery, as can
retrying a partially delivered batch. This is at-least-once recovery.

Files are separated below `output/recipients/<recipient-id>/`; legacy output
paths remain unchanged. Each recipient's pipeline produces its own content,
so types, lengths, and details can differ even for identical feed URLs.

## Management API and desktop client

The existing loopback Host/Origin gates cover the new API too. No authentication
or secret-setting endpoints have been added; manage through the SSH tunnel.

| Endpoint | Behavior |
|---|---|
| `GET /api/recipient-defaults` | Raw shared defaults |
| `PUT /api/recipient-defaults` | Replace raw shared defaults |
| `GET /api/recipients` | Raw recipients and resolved parent configurations |
| `POST /api/recipients` | Create a recipient |
| `GET /api/recipients/<id>` | Raw configuration plus effective interests |
| `PUT /api/recipients/<id>` | Replace raw configuration; id is immutable |
| `DELETE /api/recipients/<id>` | Remove a recipient; reparent children first |
| `GET /api/recipients/<id>/status` | Scoped history and delivery jobs |
| `/api/recipients/<id>/interests` | GET effective list; POST sparse new interest |
| `/api/recipients/<id>/interests/<name>` | GET effective settings and raw overrides; PUT sparse changes; DELETE disables interest |
| `POST .../<name>/rename` | Rename and preserve scoped history, including inherited descendants |
| `POST .../<name>/trigger` | Trigger only this recipient's interest |
| `POST .../<name>/feeds` | Add a feed, materializing a local list override |
| `DELETE .../<name>/feeds/<index>` | Remove an inherited/local feed from this recipient only |
| `GET /api/deliveries?recipient=<id>` | Job status without stored routing payloads |
| `POST /api/deliveries/<run-id>/retry?recipient=<id>` | Retry a failed job in this recipient scope |

For recipient and shared-default PUT requests, omitted fields revert to
inheritance: send the complete desired raw configuration. Interest PUT requests
are patches: only the supplied setting fields become overrides. To restore
individual inherited fields, send, for example,
`{"inherit": ["target_summary_words", "feeds", "delivery_schedule"]}`.
Interest creation keeps missing fields inherited rather than copying defaults.
Old `/api/interests` routes and `/api/status` continue to manage only legacy
interests; use recipient routes for the new scopes.

The desktop **Recipients and defaults** tab edits recipient addresses, parents,
and inherited deliverable/delivery defaults. Tristate toggles and unchecked word
overrides mean inherit. The recipient selector controls the **Interests** and
**Run history** tabs. Interest edits save only changed fields, keeping untouched
fields inherited; **Use inherited settings for this interest** clears local
setting/feed overrides. Collection and delivery schedules have separate
controls. The history tab shows scheduled jobs and provides explicit retries.
The **Restore inheritance for one setting…** action removes a selected local
override without discarding other preferences. The **Edit complete configuration
and interest templates…** editor in **Recipients and defaults** manages shared
named templates, feed lists, disabled subscriptions and every validated setting
through sparse JSON. Select Shared defaults or a recipient before opening it.
Its save replaces that selected raw configuration; standard form saves preserve
unexposed fields. Set `enabled = true` in a disabled subscription to restore it,
or recreate the same interest name from Interests. See the
[client administration guide](../client/README.md) for a worked example.

```bash
newspipeline run --recipient alice --interest Energy
newspipeline run --recipient bob --all
newspipeline run --all                 # all enabled legacy and recipient interests
newspipeline run --recipient '' --all  # legacy interests only
```

A manual interest name shared by several recipients requires `--recipient`.
Recipient deletion retains database history and existing delivery jobs. Reusing
an id/name reconnects that scope's retained history and deduplication state.
Renames refuse active runs/sends and retained destination scopes rather than
merging histories. Database updates precede atomic configuration writes; retry
a rename after an interrupted configuration write to converge. Renaming an
inherited interest materializes its effective settings under the new name;
inheriting descendants keep their own sparse overrides under that new name.

## Persistence upgrade and scope isolation

Store startup transactionally upgrades the old schema:

- `interest_scopes` assigns a stable id to `(recipient_id, interest_name)`.
  Legacy histories use the empty recipient id.
- Runs reference their scope. Articles carry `scope_id`, with
  `UNIQUE(scope_id, url)` and a composite FK enforcing the run's scope.
- Themes remain related-content groups within one interest's run. Their
  normalized `theme_articles` membership has composite foreign keys requiring
  both the theme and every source article to belong to the same run. The LLM
  sees only that run's articles; it cannot import sources from another interest.
- Existing run/article/theme/deliverable/brief ids and contents are preserved.
  Foreign keys are checked before committing. Corrupt legacy references abort
  and roll back the migration.

A repeated URL is suppressed across runs of the **same recipient/interest**.
The same URL is independently eligible in another interest or recipient. Renames
keep the stable scope id and its deduplication/group history. Articles suppressed
by the old global rule had no stored record; future scrapes can now collect them
for their other interests.
