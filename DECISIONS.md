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

## 2026-09-05 — Invariant G10 added: LLM null/empty content is a retryable failure (R2)

- First real "completed but silent" run: glm-5.3-flash (mandatory
  reasoning, default effort max) returned HTTP 200 with
  message.content=null — max_tokens=2000 fully consumed by reasoning
  tokens. llm.py returned the null as a successful result,
  _extract_json_array(None) crashed ('NoneType' object has no attribute
  strip), the exception was swallowed as "Analysis failed", and the run
  completed with zero themes → zero deliverables → zero emails (and an
  empty output dir). A second trigger found no fresh articles (global
  URL dedupe) and again "completed" with nothing delivered. The mailer
  was correct; the failure was upstream and silent.
- G10 — an LLM response with null/empty message content must never be
  returned as a success: LLMClient.chat_completion raises LLMError for
  it and takes the normal retry path. Guarded via
  tests/test_llm_null_content.py (retry-then-raise, recover-on-retry,
  empty-string parity, glm-5.3-flash-shaped payload).
- Note: with reasoning models, "content is null" is usually a token
  budget problem, not a model outage. If G10 retries exhaust, raise
  max_tokens or set [global.reasoning] effort to low/high in
  settings.toml — do not weaken G10.
- Operational lesson recorded for future debugging: a run row saying
  status=completed is NOT evidence anything was produced or delivered.
  Check themes/deliverables/briefs counts and the "emails sent — N"
  log line (N=0 is a red flag) before suspecting the mailer.

## 2026-09-16 — D12: Per-topic summary target lowered 750 → 600 words (R1)

Operator ruling: "the articles generated per topic are too long. set them to
~600 words instead of ~750." Implemented as the default in all four places the
750 was hardcoded: `pipeline.py::_generate` (fallback when an interest omits
the key), `web/app.py::_INTEREST_DEFAULTS` (creation defaults), 
`config/settings.example.toml`, and the desktop client's word-spin default
(`widgets/interests_tab.py`); `tests/test_api.py` now asserts the 600 creation
default. No prompt change was needed: `SUMMARY_USER` already interpolates
`{target_words}` with a ±10% tolerance. Rejected alternative: leave the default
at 750 and set 600 only on today's interests — rejected because every
future interest would silently inherit the stale 750. Reversible in minutes.

Scope caveat for the live install: `target_summary_words` is persisted
explicitly per interest in `settings.toml` (verified through
`GET /api/interests` on 2026-09-16, which returns the interests verbatim), so
the five existing interests keep 750 until they are edited explicitly — the
code default only governs newly created interests.

## 2026-09-16 — Diagnosis, no decision yet: HTML emails have no structure because the 0.4.x prompt rewrite stopped asking for markdown (R2 — awaiting operator ruling)

- Verified locally: `emailer.markdown_to_html()` renders markdown input into
  `<h1>`/`<h2>`/`<ul>`/`<strong>`/`<a>`, while prose-only input yields exactly
  one heading (the emailer's own `## Summary`) plus `<p>` blocks. The HTML
  part's richness is therefore 100% a property of the model's text.
- Verified: commit 9cda53b introduced the HTML part and replaced all six
  prompts in the same change. `SUMMARY_USER` now prescribes prose only (a
  headline line, a lede, one to three paragraphs) and no prompt in the set asks
  for markdown. The pre-sprint one-liner ("Write a well-structured,
  journalistic summary with a headline") left formatting to the model, whose
  markdown travelled verbatim in the old plain-text email — that is the "more
  sophisticated formatting with the markdown" the operator remembers.
- Test gap that let it through: `tests/conftest.py`'s mock LLM answers
  "Generated fixture content for model X." for summaries/scripts/brief, and
  `tests/test_e2e.py` asserts only the section headings the emailer adds itself
  (`<h2>Summary</h2>`, `<h2>Script (EN)</h2>`). Nothing expresses "markdown
  from the model must render into structure".
- Not verifiable from the dev box: the live summary/brief text and the HTML of
  the received emails — the DB under the service's home is root-only, so a
  sample has to come from the operator.
- Companion open item: daily-brief source links are one deduplicated union
  dumped at the end of the brief (`emailer.send_pipeline_results`), with no
  attribution to a theme; the operator rejected that shape as worthless and
  asked for per-topic attribution (design not yet ruled).

## 2026-09-16 — HTML is first-class output: markdown-derived composition dropped (R2, operator-directed)

Operator ruling: "the html is now, of course, first-class output, just as the
markdown is. the html can therefore not be derived from the markdown, but the
design must be made for html in the first place" — followed by "drop your
current design completely, it cannot be trusted to be optimal since it was
derived from markdown, not first-class."

- The intermediate per-topic-source design built earlier today (a `SOURCES: n`
  marker contract substituted into the brief markdown, with a `render.py` and
  its test file) was **reverted in full** on that instruction: nothing of it
  remains in the tree. The 600-word change from earlier stands; the brief's
  pooled source list is back to its pre-session state.
- New architecture, now in the tree: `document.py` holds the block model
  (`Document`/`Section`/`Paragraph`/`Bullets`/`Source`) and the **markdown
  archive renderer** (`Document.to_markdown`, a sibling output);
  `email_layout.py` holds the **HTML renderer** (the design: table layout,
  inline styles, preheader, dark-mode-aware palette, per-topic source blocks)
  and the **plain-text renderer** built from the same document. 33 guard tests
  in `tests/test_email_layout.py` assert the HTML itself, not its source text.
- The support rules and their sources are recorded in the user-local
  `html-email-reports` skill (with `references/client-support.md`: caniemail
  data, RFC 5322/2046, W3C contrast) so the next change to the layout starts
  from the client-support facts instead of re-deriving them.
- Not yet wired: `emailer.send_email` still renders the old markdown-derived
  HTML. Wiring needs the decision below.

## 2026-09-16 — OPEN (R3, awaiting operator): how structured deliverables are persisted

The HTML-first architecture only pays off if the structure exists at
generation time (headline, deck, paragraphs, bullets, and — for the brief —
sections that name the theme they report on). Today `deliverables.content` and
`briefs.content` hold free prose/markdown, which is exactly the markdown
derivation that was just rejected. Options, recommendation first:

- **(a) Additive nullable columns** (`content_json` on `deliverables` and
  `briefs`), keeping `content` as the markdown archive rendering. Old rows keep
  rendering as they do today, no reader breaks, migration is additive.
  *Recommended.*
- **(b) JSON in the existing `content` column**, format-detected on read.
  No schema change, but every consumer must branch and old rows mean the
  column has two formats.
- **(c) Persist prose only** and let delivery re-derive structure — rejected:
  that is the markdown-derived design the operator just dropped.

Batched with it (R2, same round): whether the generation prompts return
**structured content** (JSON with headline/deck/paragraphs/bullets, and brief
sections naming their theme) with a graceful fallback to the current prose
shape when a model answers with non-JSON, so a run never fails on format.

## 2026-09-16 — R3 resolved: documents are stored as JSON in `content`; structured generation approved (operator)

Operator ruling on the options above: "this is a fresh install and nothing
outside of tests has ever run, backward compatibility is not of any concern
here", and on structured generation: "your recommendation is approved".

- **Stored format:** the existing `deliverables.content` and `briefs.content`
  columns hold the document JSON (`Document.to_dict()`, marker
  `DOCUMENT_FORMAT = 1`). No parallel column, no dual format, no NULL
  handling: with no legacy rows to keep alive, one canonical format is
  cleaner than a migration-shaped one. The markdown archive is rendered at
  file-output time (`Document.to_markdown`) instead of being stored.
- **Generation:** summary, both scripts and both briefs answer with a JSON
  object (prompts Revision 3, in code and in the `prompts/` exports). The
  brief's sections carry `theme_number`, which is what binds each topic's
  source links to that topic — attribution is structural, not string surgery.
- **Fallback (never fail a run on format):** a model answering with prose or
  partial JSON yields a prose document plus `FALLBACK_NOTE`; for the brief the
  per-theme source blocks are still emitted, so a formatting failure costs the
  reader structure but never a link.
- **Removed the `Markdown` dependency** — no output is converted from markdown
  any more; the HTML is hand-built (email_layout.py) and the markdown archive
  is rendered from the document.
- Delivery sinks (email, file), the pipeline and `store.insert_brief` were
  rewired to documents in the same change; guard coverage is
  `tests/test_compose.py` (21 tests), `tests/test_email_layout.py` (33) plus
  the updated e2e assertions, which now prove per-topic attribution
  end-to-end.

## 2026-09-16 — Live-install config: topic-article target 600 → 500 words (R1, operator-directed)

Operator set the default per-topic article length to ~500 words on the VPS
install only; script (1250) and brief (600) targets unchanged, code defaults
untouched. Applied via `PUT /api/interests/<name>` (`{"target_summary_words":
500}`) for all five interests; verified in the API read-back and on-disk
`/var/lib/newspipeline/config/settings.toml`. Supersedes D12's 600 for the
deployed instance only.

## 2026-09-18 — D13: daily brief target 600 → 690 words (+15%), product-wide (R1, operator-directed)

Operator: "also make the daily brief 15% longer for me (the user/customer)".
600 + 15 % = 690 words. Unlike the live-only 500-word topic-article change
above, this one lands in every place the default lives —
`config/settings.example.toml`, the API's creation defaults
(`web/app.py::_INTEREST_DEFAULTS`), the pipeline fallback
(`pipeline.py::_brief`) and the client's word-target spin default
(`widgets/interests_tab.py::_WORD_DEFAULTS`) — because the operator IS the
customer: a fresh install must behave like the live one. The prompt already
interpolates `{target_words}` with a ±10 % tolerance
(`prompts.py::BRIEF_EN_USER` / `BRIEF_DE_USER`), so no prompt change was
needed. Covered by `tests/test_api.py` (creation default 690, boolean/
word-key defaults) and by the new
`test_example_config_documents_every_creation_default`, which fails when the
example config and the creation defaults drift apart — proved against a
deliberate 690 → 600 edit before being accepted.

## 2026-09-18 — D14: per-theme result emails become a per-interest toggle (`email_themes`); the live install goes brief-only (R2, operator-directed)

Operator: "limit the current jobs on the vps to the daily briefs for user
interests only. no email per theme anymore, as that is too much and the
daily brief is enough." Rejected alternative (config-only, no code):
`enable_summary = false` stops the per-topic articles existing at all —
rejected because the complaint was mail volume, not article volume, and the
articles feed the client's theme views, the file archive and the DB rows.

- New per-interest boolean `email_themes` (default `true`), validated like
  the other deliverable toggles, honoured in
  `emailer.send_pipeline_results`: false suppresses the per-theme emails and
  leaves the daily brief as the interest's only mail. The brief is
  synthesized from each theme's title and description
  (`pipeline.py::_brief`), never from the per-theme articles, so brief
  content is identical either way — that is what makes the toggle safe.
- A per-deliverable mail mode inside the `delivery` list was rejected: a
  wider persisted-config contract change (API validation, client checkbox
  semantics, docs) for the same result.
- Client: "Also email each theme separately" in the Delivery box,
  round-tripped through the save payload.
- Live install (operator row, pending the release handover at the time of
  writing): all five interests to `email_themes = false` (plus
  `target_brief_words = 690`, D13), applied over the loopback API and read
  back afterwards.

## 2026-09-18 — D15: release 0.5.0 — first annotated tag, one label for both distributions, artifacts published (R3, operator-decided)

Operator: "let's do a release at this point", then approval of the batched
ruling round (label, client version, publishing shape) with "proceed".
Rulings, in the operator's own terms:

- **Label 0.5.0** — the version already declared in
  `pyproject.toml` / `__init__.py` and described by the CHANGELOG's
  "in progress" section; the label names the content it carries and that
  section is promoted in the same change. First tag in the repo (closes
  OI-10), which adopts the tag-per-release rule: every released version gets
  an annotated tag, so a rollback target always exists.
- **One label for both distributions** — the client
  (`client/pyproject.toml`, `newspipeline_client.__version__`) moves
  0.3.0 → 0.5.0; one product version is what the operator and a future
  installer can reason about.
- **Publishing shape** — annotated tag + GitHub release page carrying the
  built sdist + wheel of both distributions and a `SHA256SUMS` manifest,
  with the published bytes verified by re-download and compared against
  GitHub's own asset digests. The repo stays a path install
  (`pip install -e .`, `pip install ./client`); `dist/` is git-ignored, so
  the artifacts exist on the release page only.
- **Release mechanics are operator rows** (standing constraint in
  OPEN_ISSUES.md: the agent never commits, tags or pushes); the agent
  prepares the tree, the artifacts, the notes and the command block.

## 2026-09-18 — Correction: remote/SSH work is the agent's job (R3, operator-corrected)

The operator corrected a standing rule that had been recorded backwards: an
earlier session encoded the 2026-09-16 ssh-prompt incident as "the agent never
opens an SSH connection to the VPS; remote/deployed-revision checks are
operator rows". Operator ruling: "this memory/understanding of our workflow is
absolutely, diametrically wrong … what i need for you is to do all ssh stuff
over sshpass and never spawn a ui login prompt window on the developer
system."

- **Ruling:** the agent runs every remote operation itself — `ssh`, `scp` and
  `git`-over-ssh — non-interactively over `sshpass` (`sshpass -e` with the
  password from the documented access file; `GIT_SSH_COMMAND="sshpass -e ssh"`
  for the git remote; `StrictHostKeyChecking=accept-new`; `-tt` only when a
  real tty is required). Where key-based auth is configured, it is used with
  the same prompt-proof flags (`BatchMode=yes`, `NumberOfPasswordPrompts=0`).
- **What remains forbidden:** any invocation that can fall back to an
  interactive prompt and pop a login/password window on the operator's dev box
  — which is what the un-`sshpass`'d `git ls-remote vps` did. The failure mode
  was the prompt, not the SSH.
- **Unchanged:** the agent never commits, stages, pushes or tags; merges and
  releases stay the operator's, as do the git pushes that carry them.
- Applied in the same change: OPEN_ISSUES.md's standing-constraints block and
  rows OI-04/OI-12 (the deployed-revision check is an agent row), and the local
  memory note that carried the false rule.

## 2026-09-18 — D16: catch-up skips a slot that already ran (R2 — fix for a live defect)

Found while deploying 0.5.0 to the live install: the restart fired catch-up for
all five interests although that day's 04:00 runs had completed. Root cause:
`find_most_recent_missed` (`scheduler.py`) answers only "which slot does the
schedule say was missed" and `SchedulerThread._run_catch_up` launched it without
consulting the run history, so **every** serve restart added a second full round
per interest. Live evidence: two runs per interest per day on 2026-09-16, 17 and
18 with `interval_hours = 24`, and 24 `Catch-up:` launches in 72 hours of journal
— double the LLM spend and double the delivered mail.

- **Ruling (R2, fixed in the same change):** catch-up skips a slot that a
  **completed or in-flight** run already covers —
  `SchedulerThread._covering_run(name, slot)` walks `Store.list_runs` (newest
  first) and accepts a run whose `started_at` is within `_SLOT_TOLERANCE`
  (5 minutes; the tick window can fire up to 65 s early) before the slot. The
  skip is logged by name and run id, so a suppressed catch-up is visible in the
  journal rather than silent.
- **Deliberately unchanged:** a **failed** run does not cover a slot. A run
  interrupted by a crash is swept to `failed` at serve startup (D10), and that
  slot must still be retried — the crash-recovery path is what the sweep exists
  for. The alternative (any run row covers its slot) was rejected for exactly
  that reason; retrying a genuinely failing interest on every restart is the
  pre-existing behaviour and was left alone.
- **Scope:** scheduler semantics — G3's frozen next-run/missed references are
  untouched (`tests/test_scheduler.py::test_find_most_recent_missed_*` still
  assert the same values); the new behaviour is guarded by three tests, one of
  which was proved to fail against the pre-fix code.
- **Version:** 0.5.1 (a fix). The client distribution is unchanged.



## 2026-10-07 — D17: recipients, independent delivery, and interest scopes (R1)

Requested multi-recipient configuration and per-interest isolation supersede
D5's single-user/global-deduplication assumptions. Configuration stays in TOML
(D3), secrets stay outside the API (G1), and the SSH trust boundary (D7) stays.

- Recipients have immutable ids. Shared defaults, parent chains and sparse
  overrides are resolved without freezing inherited values. Named interests
  merge by name; feed lists replace. Invalid parent graphs are rejected.
- Collection and delivery clocks are separate. Scheduled completed output has
  a durable job capturing its intended recipient and non-secret delivery
  settings. Due jobs are processed without requiring an LLM key. Failed sends
  require explicit retries; interrupted sends recover with at-least-once
  semantics because SMTP and SQLite cannot commit together.
- G2 now means URL deduplication **within a recipient/interest across runs**.
  The existing guard retains its same-scope repeat assertion and adds a
  different-interest acceptance assertion; it is not disabled or skipped.
  Explicit scope FKs and normalized theme/article memberships prevent
  cross-interest grouping. Old ids/content are retained by a transactional
  migration with foreign-key validation and rollback on corrupt references.
- Scoped renames retain the D11 database-first/config-second recovery contract;
  inherited descendant histories update atomically and destination histories
  cannot be implicitly merged. Active runs/sends block renaming.
- New recipient CLI/API/client paths coexist with legacy scopes. No deployment,
  staging, commit, push, or publication is performed by this implementation.
  Operational details: docs/recipients.md.
