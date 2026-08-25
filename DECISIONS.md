# DECISIONS.md — newspipeline decision ledger

Append-only. Entries are written when a decision is made, tagged by
reversibility: R1 (reversible < 1h), R2 (bounded cost to reverse),
R3 (one-way door: persisted data formats, external contracts, security model,
core framework).

---

## 2026-08-08 — D1: Scheduling architecture — built-in scheduler in the web app process (R3, human-decided)

Context: the task requires local orchestration/scheduling of pipeline runs on
an Ubuntu 24.04 desktop. Branches presented with downstream consequences:
(a) built-in asyncio scheduler inside the web settings app process,
(b) systemd user timer / cron invoking a CLI run command,
(c) a separate scheduler daemon.
The human chose (a). Downstream: the app must run continuously (an optional
systemd *user* service is documented for autostart); startup catch-up of
missed runs is ported from the legacy scheduler; the sync Flask process hosts
the async scheduler on a dedicated thread with its own event loop.

## 2026-08-08 — D2: Web framework — Flask, sync (R3, human-decided)

Branches presented: FastAPI+uvicorn / Flask / stdlib http.server.
The human chose Flask. Downstream: the async pipeline is executed via a
dedicated event loop on the scheduler thread; manual CLI runs use
`asyncio.run()`; no pydantic validation — request validation is hand-rolled
in the web layer.

## 2026-08-08 — D3: Persistence — SQLite via stdlib sqlite3 for run data, TOML for configuration (R3, human-decided)

Branches presented: (a) sqlite3 + settings.toml, (b) SQLAlchemy with all
config in the DB, (c) JSON files only. The human chose (a).
External contract: `config/settings.toml` is the stable, human-editable
configuration format and must stay version-compatible from the first release.
Pipeline data (runs, articles, themes, deliverables, briefs) lives in
`data/news.db` (SQLite, WAL mode). No ORM; SQL is hand-written in `store.py`.

## 2026-08-08 — D4: tomli-w added as a dependency for writing settings.toml (R1)

stdlib `tomllib` is read-only; the web UI must write configuration back.
Rejected alternative: a hand-rolled TOML writer (error-prone string escaping).

## 2026-08-08 — D5: Runs reference interests by name, not by surrogate id (R1)

Interests live in TOML and have no stable integer id as in the legacy
Postgres schema. `runs.interest_name` is the join key. Consequence: renaming
an interest orphans its run history — accepted for a lean single-user app;
documented in README.

## 2026-08-08 — Task invariants (derived from the task specification and legacy behavior)

Guarded (each requires a named, green guard test before the task is reported
complete):

- G1: The API key is never written to settings.toml, never logged, and never
  returned by any web endpoint → `tests/test_secrets.py::test_api_key_not_persisted_or_exposed`
- G2: Article URL dedupe holds across runs → `tests/test_store.py::test_article_url_dedupe`
- G3: Next-run computation preserves legacy semantics (start_time + interval
  grid, 60s tick window) → `tests/test_scheduler.py::test_next_run_*`
- G4: Language-detection heuristic parity with legacy → `tests/test_pipeline.py::test_detect_language_*`
- G5: End-to-end pipeline run against a fixture RSS feed with a mocked LLM
  endpoint produces themes, deliverables, a brief, and file output →
  `tests/test_e2e.py::test_pipeline_end_to_end`

Triggered (not auto-testable; flag any diff touching these paths before
commit):

- T1: `.env` parsing / secret handling in `config.py` and `cli.py`
- T2: SMTP authentication paths in `emailer.py` (no live SMTP available in
  this environment)
- T3: Live OpenRouter request/response handling in `llm.py` (no API key in
  this environment)

## 2026-08-08 — WP4 implementation notes (R1)

- `feedparser.parse(feed_url)` is called directly with the feed URL, which
  fetches synchronously inside the async stage. Kept for parity with legacy;
  feed fetches are sequential and local-scale. Revisit only if feed counts
  grow substantially.
- A missing RSS `pubDate` is passed to the store as `""`; `parse_rss_date`
  falls back to the current time (legacy passed now-ISO explicitly — same
  outcome, one less code path).
- Stage-level failures (analyze/generate/brief) are logged and swallowed so a
  run completes with partial results, matching legacy; only errors outside
  the stage bodies mark the run `failed`.

## 2026-08-08 — WP5 implementation notes (R1)

- Delivery dispatch lives in a new small module `delivery.py` (not planned in
  the scaffold) so the CLI (WP8) and scheduler (WP6) share one dispatch path
  instead of duplicating mode/recipient logic.
- Failure alerts are sent regardless of the interest's `delivery` modes: a
  failed run produces no output to deliver, so the mode list is treated as
  inapplicable to alerts. Rejected alternative: alerts only when "email" is
  in `delivery` — would silently hide failures from file-only interests.
- The legacy failure alert included a traceback; the scaffold's
  `send_failure_alert` signature (stage + error message) was kept instead.
  The full exception is already in the log via `logger.exception` in
  `Pipeline.run`.
- Email/file delivery functions are synchronous (smtplib and file writes are
  blocking). Callers on an event loop (scheduler, WP6) must run them in an
  executor; noted here so WP6 does not block the loop.

## 2026-08-08 — WP6 implementation notes (R1)

- `SchedulerThread.__init__` takes `store` plus injectable `get_settings` /
  `get_api_key` callables (defaults: `config.load_settings` /
  `config.get_api_key`) instead of the scaffold's fixed `timezone` parameter.
  Settings — including the timezone — are re-read on every tick, so edits
  made through the web UI (WP7) take effect without a restart.
- Concurrency guard is two-layered, as in legacy: in-memory task registry
  plus `store.is_interest_running`. Inherited limitation, accepted: a run row
  stuck in 'running' state (killed process) blocks that interest until the
  row is repaired. No automatic stale-run recovery was added.
- Catch-up semantics confirmed in the live smoke run: with a daily interest,
  starting the app fires the most recent missed slot even from a previous
  day (legacy `_find_most_recent_missed` behavior). This means every app
  start can trigger a run for daily interests — intended catch-up, not a bug.
- `stop()` cancels the check/catch-up tasks and closes the loop; in-flight
  pipeline runs are logged and cancelled with the loop rather than awaited.
  Signal handlers that call `stop()` are installed by the CLI `serve`
  command (WP8), not by the scheduler itself.

## 2026-08-08 — WP7 implementation notes (R1)

- The scaffold's planned `GET /status/<name>` route was dropped: the last 5
  runs per interest are rendered inline on the index page instead. One page
  less, same information; still no dashboard.
- `create_app(store=None, scheduler=None)` takes both as injectable
  dependencies (defaults: a real `Store()`, no scheduler). The CLI (WP8)
  wires the live `SchedulerThread`; tests inject fakes. With no scheduler,
  "run now" is disabled in the UI and the route flashes an error.
- Flask `secret_key` is random per process — it only signs flash-message
  cookies, and a local single-user app has no session persistence need.
- Feeds are add/remove only (no in-place edit); an edit is a remove plus an
  add. Keeps the form surface minimal.

## 2026-08-08 — WP8 implementation notes (R1)

- `newspipeline run --interest NAME` may target a paused interest (an
  explicit human choice overrides the pause), while `--all` skips paused
  interests like the scheduler does. `run` exits 1 on any failed run,
  unknown interest, or missing API key; 0 otherwise.
- SIGTERM handling in `serve`: the handler raises `SystemExit`, which
  unwinds through `app.run()` into the `finally` that stops the scheduler;
  `_cmd_serve` catches it and returns 0 normally. systemd's default
  `systemctl stop` (SIGTERM) therefore shuts the scheduler down cleanly.
- The systemd user unit is shipped at `deploy/newspipeline.service` with
  `%h`-relative paths to adjust on install, replacing the inline README
  sketch.

## 2026-08-08 — WP9 implementation notes (R1)

- G3/G4 parity is tested against the actual legacy modules, imported from
  `legacy/` via importlib (with throwaway `workbench.*` stubs for
  `legacy/news_pipeline.py`) — not against reimplementations of the legacy
  logic inside the tests.
- The G1 guard test caught a real defect fixed in WP9: `config.load_env()`
  only `setdefault`s into `os.environ`, so a new API key saved via the UI
  was ignored by the running process until restart. `_upsert_env` now also
  updates `os.environ`. (T1 path touched — flagged at delivery.)

## 2026-08-08 — D6: legacy/ tree removed; G3/G4 guards re-referenced to frozen snapshots (R2)

Human instruction: delete `legacy/` and all references to it.
The WP9 parity tests imported the legacy modules directly, so a plain
deletion would have deleted the G3/G4 guard tests — not permitted without
an R3. Decision: capture the legacy modules' outputs into
`tests/reference_snapshots.py` (45 next-run grid values, 4 malformed-time
values, 36 missed-run values, 8 language classifications) immediately
before removal, and point the same guards at the frozen data. Guard names
still match the invariant globs (`test_next_run_*`,
`test_detect_language_*`); 21/21 tests green before and after.
Rejected alternative: keep `legacy/` in the tree solely as test reference
material — contradicts the explicit removal instruction; the frozen
snapshots preserve the same semantics the parity tests enforced.
The snapshots are now the baseline: the pre-removal semantics are fixed by
fiat, and any future semantic change is a deliberate, visible diff against
them rather than silent drift.
Historical entries above and in CHANGELOG.md still mention `legacy/` —
both files are append-only and are not rewritten; this entry supersedes
the WP9 note about importlib-based parity testing.

## 2026-08-24 — R1: Refactor task opened — stop condition logged before first edit (R2)

Human instruction: refactor until no high-impact architectural objection
remains unresolved or explicitly accepted; live-test + review + commit after
each significant step; progress tracked in `.agent-reviews/refactor-newspipeline.md`.
Per contract §7 ("until satisfied"-style conditions are rejected), the stop
condition is expressed as metrics:

- M1 — guard suite green at every commit: `pytest tests/ -q`.
  Baseline measured now: **21 passed**. Target: all pass, none skipped/weakened.
- M2 — zero unresolved high-impact objections across two consecutive final
  review passes: `grep -c "\[OPEN\]" .agent-reviews/refactor-newspipeline.md`.
  Baseline measured now: **6** (initial review listed HI-1…HI-6). Target: 0,
  with every remaining HI either fixed or explicitly accepted by the human.
- Budget: **10 commits or 3 hours wall-clock**, whichever comes first.
  Budget exhausted with targets unmet → stop and report trajectory.

Cadence per significant step: edit → pytest → live boot test (`serve` smoke,
headless) → independent fresh-context review pass over the diff → commit.
No persisted-format, external-contract, framework, or security-model change
is planned; anything drifting toward R3 stops and escalates. Known items
already accepted in this ledger (D5 rename-orphaning; WP6 stale-running-row
limitation; WP4 sync feedparser note) are carried as accepted unless a
review pass upgrades them.

## 2026-08-24 — R2: Scrape-stage blocking work offloaded to threads (`asyncio.to_thread`)

Supersedes the WP4 note "feedparser.parse is called directly with the feed
URL … kept for parity". The scheduler hosts concurrent per-interest runs on
one event loop, so on-loop network fetches (feedparser) and CPU-bound HTML
parsing (trafilatura) stalled the tick loop and all other in-flight runs.
Fix: both calls wrapped in `asyncio.to_thread` inside `Pipeline._scrape`.
Rejected alternative: rewriting feed fetching on an async HTTP client —
would duplicate feedparser's fetch/parse behavior for no local-scale need.
Semantics unchanged: feeds still processed sequentially, article order and
dedupe untouched (G2/G5 guards cover).

### Correction after review pass 1 (2026-08-24) — supersedes the paragraph above

The first-pass review correctly failed the implementation described above:
wrapping `feedparser.parse(url)` in a thread keeps feedparser's own UNTIMED
urllib fetch inside a non-daemon executor thread — a stalled feed server
would wedge the run task (interest lockout via the stale-'running' guard)
and hang interpreter shutdown at executor join. Delivered fix instead:
the feed is fetched through the existing timed httpx client
(`httpx.Timeout(article_fetch_timeout_seconds, default 30)`), and only XML
parsing runs in a thread (`asyncio.to_thread(feedparser.parse,
resp.content)`); trafilatura extraction likewise offloaded. The "rejected
alternative" line above is therefore wrong for feeds — the async-client
fetch is exactly what shipped; it remains rejected only as a *rewrite of
feedparser's parsing*. Behavioral deltas accepted: HTTP error pages now
parse as zero-entry feeds instead of surfacing status via bozo flags
(observability only, logged per feed); redirect chains re-arm the per-hop
timeout (worst case hop-count-bound wall clock). New guard test:
tests/test_scrape_loop.py.

## 2026-08-24 — Invariant G6 added (guarded): a stalled feed FETCH must not block the scheduler event loop

Derived during the scrape-offload refactor. Guarded via
tests/test_scrape_loop.py::test_scrape_keeps_event_loop_responsive
(fixture feed stalls 2s; probe coroutine must complete while the scrape is
in flight under a 1.0s deadline). Documented scope: detects on-loop
blocking that begins with the first feed request; executor-thread-only or
post-first-response blocking shapes are review-covered, not test-covered.
Companion non-invariant check in the same file: errored feeds are swallowed
without raising.

## 2026-08-24 — Invariant G7 added (guarded): authenticated SMTP upgrades with a certificate-verifying context, credentials never precede TLS

Derived during the STARTTLS fix (review pass flagged smtplib's default
STARTTLS context as non-verifying → credential exposure to active MITM).
Guarded via tests/test_emailer_tls.py: authenticated send must call
starttls with verify_mode=CERT_REQUIRED and check_hostname=True, and the
event order must be exactly starttls → login → send; the unauthenticated
relay path must never issue starttls. The live handshake itself remains T2
(untestable here). Behavioral delta logged in CHANGELOG.md: self-signed or
hostname-mismatched authenticated relays now fail loudly instead of
connecting. Rejected alternative: opt-in flag to disable verification —
rejected as a footgun; users needing it can pin the relay in an
OS-level trust store.

## 2026-08-24 — R2: Atomic config writes + mutation lock; G1 secret scan hardened (step 3 of the refactor)

- `save_settings` and `_upsert_env` now write via tmp file + `os.replace`
  (`_atomic_write_text`). Motivation: the scheduler re-reads settings.toml
  every tick and Flask threads serve concurrently; a plain in-place write
  exposed readers to torn files, and overlapping UI POSTs lost updates.
  `config.mutation_lock` (an RLock — internal `_upsert_env` acquisition plus
  route-level RMW cycles must not deadlock) serializes read-modify-write;
  readers stay lock-free because replace is atomic. Modes are explicit:
  settings.toml 0644 (previous umask-default), .env 0600. Directory-entry
  fsync after replace deliberately omitted: worst case on power loss is
  reverting to the previous file, acceptable for a desktop app.
- G1 hardening found during review: the old secret scan tested
  `secret in repr(settings)`, which repr/TOML escaping defeats for secrets
  containing quotes or backslashes. The scan now walks the settings' string
  leaf values directly. Guarded by
  tests/test_config_atomic.py::test_secret_scan_catches_escaping_secrets.
- Follow-up from the same review: `_upsert_env` silently corrupted secrets
  containing newlines (line-based parser truncates on read, tail parsed as
  junk keys). Multi-line values are now refused with ValueError; guarded by
  test_env_values_with_newlines_are_rejected.
- Torn-write guard: test_concurrent_writers_never_expose_torn_file runs
  reader threads against live concurrent saves; sensitivity verified by
  reverting to plain write_text locally (guard fails 3/3). Rejected
  alternative for lost updates between two browser tabs: optimistic
  versioning/ETags — overkill for a single-user local UI.

## 2026-08-24 — R2: Scheduler refuses to launch runs without an API key (step 4)

`SchedulerThread._launch` now checks the key before creating any run row or
task and returns the same guidance the CLI prints; previously a scheduled
slot with no key produced a scrape-then-fail run row plus a failure-alert
email for a known-missing precondition. The gated value is passed through
to `Pipeline` (single read — closes the gate-vs-consume TOCTOU flagged in
review). Noise bound: one WARNING per scheduled slot per interest, not per
tick. Guards: tests/test_scheduler.py launch-refusal (no rows) and
launch-starts (key passthrough + completion branch dispatch), plus a
failure-alert dispatch sibling. Rejected alternative: pre-check inside
Pipeline.run — too late, the doomed row would already exist.

## 2026-08-24 — R2: Run-completion dispatch deduplicated into delivery.dispatch_finished_run (step 5)

The run→status→deliver/alert routing existed twice (cli._run_selected and
scheduler._run_interest_safe) with drifting error handling; both now call
delivery.dispatch_finished_run, which returns (status, run) — the row read
for the dispatch decision — so callers never re-read a divergent snapshot.
Scheduler still runs it in the executor (blocking SMTP/file I/O); CLI runs
it synchronously. Dedup is itself guarded: a wrapping spy asserts both
entry points route through the shared function
(test_delivery_dispatch.py::test_scheduler_and_cli_route_through_shared_
dispatch); cli.py references the function via module attribute so the spy
cannot be bypassed by import-time binding. Rejected alternative: keeping
the duplication with a comment cross-reference — drift was the defect.

## 2026-08-24 — R2: Tolerant theme-array extraction replaces greedy regex in analyze (step 6)

The old `re.search(r"\[.*\]", response, re.DOTALL)` silently lost the whole
analyze stage whenever the model fenced, wrapped, or commented around the
JSON array. `_extract_json_array` now strips fences and scans every
top-level balanced `[...]` region (string/escape-aware; raw newlines end an
open "string" because JSON forbids them — unpaired prose quotes can no
longer poison the scan). Semantics: first array with ≥1 object wins;
literal `[]` = legitimate "no themes" (returned); non-empty dictless arrays
([1], ["x"]) raise loudly instead of masking as zero themes. Unparseable
output raises ValueError into the stage's existing log-and-continue path,
now logging a 500-char response excerpt (craft rule: diagnose from the
log). Trailing commas remain fatal (unchanged legacy behavior; accepted).
Guards: tests/test_analyze_parse.py (13 unit cases incl. both review-found
regressions) + integration pins for garbage-response and raising-LLM paths.
Rejected alternative: JSON-mode/tool-call API parameters — not portable
across arbitrary OpenAI-compatible endpoints (D2/D3 posture).

## 2026-08-25 — R3 (human-decided): ESC-1 path resolution + ESC-2 localhost origin enforcement

ESC-1: human chose fixing path resolution — `NEWSPIPELINE_HOME` env override,
else the current `__file__`-relative root. Human's stated reason: "we do not
want to break every actual install." Downstream: documented in README;
existing editable setups are unaffected because the fallback is exactly the
old behavior; a wrong override relocates state silently (failure scenario
presented and accepted).

ESC-2: human chose hardening — POST requests to the settings UI are
rejected unless they originate from the local UI itself (Origin, when
present, must be same-origin with a loopback Host; Host header must be a
loopback name, which also defeats DNS rebinding). Chosen on re-ask without
a distinct stated reason → recorded as delegated-with-warning per contract.
Downstream: local CLI/tooling POSTs without an Origin header remain allowed;
the documented no-auth posture is unchanged; only cross-site browser posts
are blocked.

### ESC-2 correction after review (2026-08-25)

The first gate implementation checked the Host header only on POST/PUT/
DELETE, leaving DNS-rebinding READ access to the settings page — the
ledger claim "defeats DNS rebinding" was not delivered. Delivered now:
the loopback-Host check applies to every request (reads included); the
Origin check applies to all non-safe methods, default-deny for anything
added later. Pinned tradeoff: a loopback Origin hostname with ANY port is
accepted — browsers forge neither Host nor Origin, so hostname membership
is the trust boundary; port mismatches are tolerated by design
(test_web_origin.py pins this). Non-loopback `serve --host` values log a
warning at startup (browsers would get 403s).

### Step-6 characterization correction (2026-08-25)

The step-6 and changelog text said the old greedy regex "silently produced
zero themes" for fenced/prose-wrapped arrays; precisely, it handled single
fenced/wrapped arrays but lost themes on multi-region responses (citation
brackets), object-wrapped-only payloads, and unpaired prose quotes. The
delivered `_extract_json_array` is a strict superset of the old acceptance;
only the historical wording overstated the defect.

## 2026-08-25 — ID note for the 0.3.0 client/server task (R1)

Labeling only. The 0.3.0 task brief labels its five decisions D6–D10 and two
new guard invariants G7/G8. This ledger already contains a D6 (legacy-tree
removal, 2026-08-08) and a G7 (SMTP STARTTLS verification, 2026-08-24); the
ledger is append-only, so IDs continue from the next free numbers. Mapping,
used consistently in CHANGELOG.md and code comments:
task-D6→**D7**, task-D7→**D8**, task-D8→**D9**, task-D9→**D10**,
task-D10→**D11**; task-G7→**G8**, task-G8→**G9**.

## 2026-08-25 — D7 (task brief's "D6"): Client/server split — headless VPS server + desktop GUI client over an SSH tunnel (R3, human-directed)

Human instruction: transform newspipeline into a server (this repo: headless
daemon = JSON config API + scheduler, bound to 127.0.0.1, default port 8421)
plus a separate desktop client distribution (`client/`,
`newspipeline-client`) whose sole job is managing the server's
configuration over `ssh -L`. One-way-door aspects, accepted downstream:
- Security model is now loopback bind + SSH tunnel as THE trust boundary —
  no TLS termination, no tokens, no public exposure; the 0.2.0 Host/Origin
  gates are retained as defense-in-depth only.
- A second installable distribution exists (`newspipeline-client`); third
  parties (the human's own VPS + desktop installs) come to depend on both
  the wire format of `/api/*` and the profile file layout under
  `~/.config/newspipeline-client/`.
- Scheduling semantics (G3), pipeline stages, dedupe (G2), language
  detection (G4) untouched; settings.toml remains the source of truth
  (D3); secrets remain exclusively in .env (G1).
Rejected alternatives: token auth or HTTPS on the API — redundant inside a
tunnel and more surface to manage; keeping the HTML UI server-side — the
VPS would carry GUI-serving code for no benefit.

## 2026-08-25 — D8 (task brief's "D7"): JSON-only Flask API replaces the web UI (R2)

Flask retained as HTTP layer (D2 posture unchanged); templates/static
deleted; routes become `/api/*` returning structured JSON errors with
correct status codes. Request validation stays hand-rolled (D2 — no
pydantic). The `create_app(store, scheduler)` injectable-dependency pattern
is preserved; the 0.2.0 Host/Origin gates are kept verbatim as
defense-in-depth behind the tunnel; `config.mutation_lock` now serializes
EVERY mutating route (previously only the form-save routes). G1's guard is
extended: no API response body may ever contain a secret value.
Scoping decision: the old UI's write-only "set API key" endpoint is NOT
re-exposed over HTTP. Rejected alternative: a write-only key endpoint for
parity with the old UI — it would put a secret-setting route on the wire
that the tunnel model does not need; `.env` is edited directly on the VPS
over SSH. SMTP host/port/username are configurable via the API (non-secret);
NEWS_SMTP_PASSWORD never appears in any request or response body.

## 2026-08-25 — D9 (task brief's "D8"): Gmail SMTP relay as the primary delivery path (R2)

No sender-code changes: the 0.2.0 certificate-verifying STARTTLS path
(G7 invariant, tests/test_emailer_tls.py) is exactly what gmail-smtp needs.
Delivered as documentation + example config: fully commented
smtp.gmail.com:587 block in settings.example.toml (username = sending Gmail
address; password via NEWS_SMTP_PASSWORD in .env) and a README "Gmail relay"
section (dedicated sender account, 2FA + app password, first-send security
review at myaccount.google.com/security, ~500/day cap irrelevant at this
volume). Failure alerts follow the existing recipient-resolution logic
(delivery.py, unchanged). Reversible by editing settings.toml back to any
other relay.

## 2026-08-25 — D10 (task brief's "D9"): Startup sweep for orphaned runs — serve-only scope (R1)

At `serve` startup ONLY, before catch-up: every runs row with
status='running' becomes status='failed',
error='interrupted by restart'; the swept count is logged. Rationale: a
fresh serve start implies the previous process died, so a 'running' row can
only be orphaned. One-shot `newspipeline run` invocations must NOT sweep: a
concurrent serve process may legitimately own a running row, and a second
process sweeping would clobber it. Implemented inside SchedulerThread startup
(the CLI `run` path never constructs one).

## 2026-08-25 — D11 (task brief's "D10"): First-class rename operation amends D5's orphaning consequence (R2)

Renaming an interest is now an explicit operation that PRESERVES run
history, in this exact order: (1) validate new name non-empty + unique;
(2) SQLite first — UPDATE runs SET interest_name=<new> WHERE
interest_name=<old> (idempotent: zero rows matched is fine); (3) then
atomically rewrite settings.toml with the renamed interest under the
mutation lock. Loud before/after logging around each store write. Recovery
contract: a crash between (2) and (3) leaves a half-applied rename; the
documented remedy is to RE-RUN the rename — step (2) is idempotent, so the
retry converges. Accepted consequences: future runs write output files under
the new slug while pre-rename archives remain under the old slug;
failure-alert emails reference the current name; DELETE still orphans
history (only rename preserves). The scheduler self-corrects on its next
tick (settings re-read per tick — WP6 note). PUT /api/interests/<name> can
never change the name; renaming happens exclusively through the rename
endpoint. This amends D5: rename no longer orphans history; D5's join-key
observation itself stands.

## 2026-08-25 — Invariants G8 and G9 added (guarded), derived from the 0.3.0 task specification

- G8 (task brief's "G7") — rename preserves history end-to-end: renaming
  through the API repoints all runs rows to the new name AND updates
  settings.toml; a simulated crash between the two stores leaves a
  half-applied state from which RE-RUNNING the rename converges. Guarded
  via tests/test_rename_history.py::test_rename_preserves_run_history and
  ::test_crash_between_stores_converges_on_rerun.
- G9 (task brief's "G8") — serve startup sweeps orphaned runs; one-shot
  runs never do: a seeded status='running' row becomes
  failed/'interrupted by restart' when a SchedulerThread starts (interest
  schedulable again — is_interest_running false), and the CLI `run` path
  leaves such rows untouched. Guarded via tests/test_startup_sweep.py
  (test_serve_startup_sweeps_orphaned_rows,
  test_cli_run_does_not_sweep).
