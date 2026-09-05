# Changelog

All notable changes to newspipeline are documented here, appended as work
happens. Format: version heading, date, grouped bullet points.

## 0.3.1 — 2026-09-05 (deployment)

### Added

- **Production deployment (R3):** deployed the server to the shared brainy
  VPS (37.60.240.152) as a native systemd service using
  `deploy/newspipeline.service` — no Docker, state under
  `/var/lib/newspipeline`, API loopback-only on 8421. Deployment record in
  `DEPLOYMENT.md` (safe to commit; secrets live only in the VPS-side
  `/var/lib/newspipeline/.env`).

## 0.3.0 — 2026-08-25 (client/server transformation)

### Added

- **Client/server split (D7):** newspipeline is now a headless server for a
  VPS plus a separate desktop client. The server's JSON API binds to
  127.0.0.1 only; the trust boundary is an SSH local forward — no TLS, no
  tokens, no public exposure. The 0.2.0 Host/Origin gates are retained as
  defense-in-depth.
- **Desktop client** (`client/`, distribution `newspipeline-client`,
  entry point `newspipeline-client`): PySide6 GUI + httpx. Owns the full
  tunnel lifecycle (runtime free-port pick, ssh spawn with
  `ExitOnForwardFailure=yes` and `ServerAliveInterval=30`, TCP readiness
  poll, clean teardown on exit or crash), shows connection state, and
  surfaces errors with the ssh stderr tail. Persists the connection
  profile (host, user, port — never secrets) under
  `~/.config/newspipeline-client/profile.json`. Feature parity with the
  old web UI: global settings; interests CRUD including feed add/remove,
  schedule, deliverable toggles, delivery modes and per-interest recipient
  override; discoverable paused banner; trigger-now; run-history view;
  rename with a trivial confirm.
- **First-class rename (D11, amends D5):** new
  `POST /api/interests/<name>/rename` preserves run history end-to-end:
  validate → repoint all `runs` rows in SQLite first (idempotent) →
  atomically rewrite settings.toml under the mutation lock, with loud
  before/after logging. Recovery contract: re-running a half-applied
  rename converges (a crash between the two stores is recoverable by
  retry). Accepted consequences documented in DECISIONS.md.
- **Startup sweep for orphaned runs (D10):** at `serve` startup only,
  before catch-up, every `runs` row stuck in `running` becomes
  `failed / 'interrupted by restart'` and the swept count is logged — the
  interest is schedulable again. One-shot `newspipeline run` invocations
  deliberately never sweep (a concurrent serve may legitimately own a
  running row).
- **Gmail relay delivery path (D9):** fully commented `smtp.gmail.com:587`
  example in `settings.example.toml` and a README "Gmail relay" section
  (dedicated sender account, 2FA + app password in `.env`, first-send
  security-review approval note, ~500/day cap irrelevant at this volume).
  No sender-code changes — the 0.2.0 certificate-verifying STARTTLS path
  is exactly the target; failure alerts follow existing recipient
  resolution.
- **VPS deployment:** systemd *system* unit replaces the user unit
  (`Restart=always` + `RestartSec=5`, paths via
  `NEWSPIPELINE_HOME=/var/lib/newspipeline`, hardening directives);
  README gains VPS deployment layout and sshd hardening notes
  (key-only auth; optional `restrict,permitopen="127.0.0.1:8421"` on the
  deploy key). A human-verification checklist lists what tests cannot
  cover (live Gmail inbox delivery, live deployment, first-send approval).
- Invariants G8 (rename preserves history;
  `tests/test_rename_history.py`) and G9 (serve-only startup sweep;
  `tests/test_startup_sweep.py`) added as guarded invariants.

### Changed

- **JSON API replaces the web UI (D8):** Flask kept as HTTP layer, but
  JSON-only — templates and static assets removed; routes moved under
  `/api/*` returning structured errors (`{"error": ...}`) with correct
  status codes. Minimum surface: GET/PUT `/api/settings`,
  GET/POST `/api/interests`, GET/PUT/DELETE `/api/interests/<name>`,
  nested feed add/remove, `/trigger`, `/rename`, and `/api/status`
  (recent runs per interest). Request validation stays hand-rolled (D2);
  `create_app(store, scheduler)` injectable pattern preserved; the config
  mutation lock now serializes every mutating route.
- `PUT /api/interests/<name>` can never change the name — renaming
  happens exclusively through the rename endpoint (400 otherwise).
- The old UI's write-only API-key endpoint is not exposed over HTTP:
  secrets stay exclusively in `.env` on the VPS, edited over SSH (G1).
  No response body ever contains a secret value (extended G1 guard).

### Removed

- The HTML settings UI (`web/templates`, `web/static`) and its form-post
  routes — superseded by the desktop client over the tunnel.


## 0.2.0 — 2026-08-25 (architectural refactor)

### Changed

- **Path relocation:** set `NEWSPIPELINE_HOME=/some/dir` to resolve
  `config/`, `data/`, `.env`, and `output/` under that directory instead of
  the checkout (default behavior unchanged).
- **Localhost origin gate:** the settings UI now rejects any request whose
  Host header is not a loopback name (reads and writes — defeats DNS
  rebinding), and any mutating request carrying a non-loopback Origin
  (defeats drive-by CSRF from web pages). Local tooling without an Origin
  header is unaffected; non-loopback binds log a startup warning.
- Theme extraction in the analyze stage now tolerates markdown fences,
  prose wrapping, object-wrapped arrays, and citation-style brackets in the
  model response (the old greedy regex lost the themes on multi-region
  responses, object-wrapped payloads, and unpaired prose quotes).
  Junk output (arrays without theme objects) fails loudly into the existing
  log-and-continue path, which now includes a truncated response excerpt.
- Run-completion dispatch (deliver output vs failure alert) unified into
  `delivery.dispatch_finished_run`, shared by the CLI and the scheduler;
  previously duplicated with divergent error handling. Returns
  `(status, run)` so callers reuse one consistent row snapshot.
- The scheduler no longer starts pipeline runs when `NEWS_API_KEY` is
  unset: the launch is refused with the CLI's guidance (log warning;
  UI flash on manual trigger) instead of producing a doomed run row and a
  failure-alert email per scheduled slot.
- `config/settings.toml` and `.env` are now written atomically
  (tmp file + `os.replace`); readers can never observe a torn file and a
  crash mid-write preserves the previous version. Web UI save routes hold
  a mutation lock so overlapping POSTs cannot lose updates.
- G1 write-path guard hardened: secrets are now detected inside string
  values regardless of escaping (the old `repr()` substring scan missed
  secrets containing quotes or backslashes). Multi-line values are refused
  for `.env` keys instead of being silently corrupted.
- Authenticated SMTP now upgrades with a **certificate-verifying** TLS
  context (`ssl.create_default_context()`); previously `starttls()` used
  smtplib's non-verifying default. Behavioral delta: authenticated relays
  with self-signed or hostname-mismatched certificates will now fail loudly
  (after retries) instead of connecting — deliberate; unauthenticated local
  relays are unaffected.
- Scrape stage no longer blocks the scheduler event loop: feed fetches go
  through the timed httpx client and only XML parsing / article extraction
  run in worker threads (`asyncio.to_thread`). Fixes stalled-feed wedging
  of concurrent runs; also bounds feed fetches by
  `article_fetch_timeout_seconds` (previously unbounded via feedparser's
  internal fetcher). HTTP error pages now parse as zero-entry feeds
  (observability note in DECISIONS.md).
- `SchedulerThread.stop()` docstring corrected: in-flight runs are
  abandoned with the loop, not cancelled.

### Added

- Invariant G6 + guard test `tests/test_scrape_loop.py` (stalled feed must
  not block the event loop; errored feeds swallowed).
- Guard tests for STARTTLS verification and credential ordering on the
  authenticated SMTP path (`tests/test_emailer_tls.py`).

## 0.1.1 — 2026-08-08

### Removed

- `legacy/` reference tree (6 files) and every active reference to it
  (DECISIONS.md, below).

### Changed

- G3/G4 guard tests no longer import the retired reference modules; they
  assert against frozen reference outputs captured from those modules
  immediately before removal (`tests/reference_snapshots.py`). Guard test
  name globs (`test_next_run_*`, `test_detect_language_*`) unchanged;
  21/21 tests green.
- Docstrings/comments in `src/`, `README.md`, and `WORKPACKAGES.md`
  scrubbed of references to the removed tree.

## 0.1.0 — 2026-08-08

### Added

- Project scaffolding for the standalone news pipeline agent, extracted from
  the legacy Workbench monolith (`legacy/` is read-only reference material):
  - `pyproject.toml` (src layout, pinned deps: flask, httpx, feedparser,
    trafilatura, tomli-w; pytest as dev extra; `newspipeline` CLI entry point)
  - `.gitignore`, `.env.example`, `config/settings.example.toml`
  - Package skeleton under `src/newspipeline/`: `config.py`, `store.py`,
    `llm.py`, `pipeline.py`, `emailer.py`, `fileout.py`, `scheduler.py`,
    `cli.py`, `web/` (Flask app, template, stylesheet)
  - Pure helpers ported from legacy and functional at scaffold level:
    language detection, RSS date parsing, scheduler time math, SMTP sender
  - `tests/` skeleton with a smoke test
  - `README.md`, `WORKPACKAGES.md`, `DECISIONS.md`, this changelog
- WP4: pipeline stages implemented in `pipeline.py` — `Pipeline.run()` and
  the four stages (scrape → analyze → generate → brief), ported from
  `legacy/news_pipeline.py` with the planned adaptations: sync sqlite store,
  feeds from the interest dict, per-stage model overrides
  (`global.models.{analysis,generation,brief}` with fallback to
  `default_model`), reasoning effort forwarded per LLM call, and run
  status/stage transitions recorded in the store. Verified by a full run
  against a fixture RSS feed and a mock chat-completions endpoint (local
  HTTP server): 2 articles scraped with full-content extraction, 2 themes,
  summary+script deliverables per theme, a brief, run marked completed;
  second run confirmed URL dedupe (no re-scraped articles). `pytest` smoke
  suite: 3 passed. Formal G4/G5 guard tests land in WP9 as planned.
- WP5: delivery implemented —
  - `emailer.py`: `send_pipeline_results` (brief email + per-theme emails
    with the enabled deliverables) and `send_failure_alert`, ported from
    `legacy/news_emailer.py`; SMTP password from .env via
    `config.get_smtp_password()` (G1)
  - `fileout.py`: `write_run_output` writes `brief.md` and one markdown file
    per theme under `<output_dir>/<interest-slug>/<run-date>/`
  - `delivery.py` (new): `deliver_run` dispatch driven by
    `interest.delivery`, recipient resolution (interest `email_recipient`,
    else `[user] email`), and `deliver_failure_alert`
  - Verified end-to-end against the fixture feed + mock LLM plus a
    hand-rolled local SMTP server: file output (brief + theme files,
    correct headings, disabled script_de omitted), 2 result emails + 1
    failure alert captured with correct recipients/subjects/bodies,
    no-recipient paths skip with a warning. `pytest` smoke suite: 3 passed.
    Authenticated SMTP (STARTTLS+LOGIN) untested locally — T2.
- WP6: `SchedulerThread` implemented — daemon thread hosting its own asyncio
  loop; 60s check loop with the legacy 65s tick window; startup catch-up via
  `find_most_recent_missed`; two-layer per-interest concurrency guard
  (in-memory task registry + `store.is_interest_running`); thread-safe
  `trigger_now(name)` returning "" or an error message; settings (including
  timezone) re-read every tick so UI edits apply without restart; pipeline
  and delivery wired per run, with blocking delivery dispatched to an
  executor; idempotent `start()`/`stop()` (signal handlers are installed by
  the CLI in WP8). Verified live against the fixture feed + mock LLM:
  catch-up-only fire (1 run), tick-window fire (+1 catch-up = 2 runs),
  `trigger_now` success and all guard messages (duplicate trigger, unknown
  interest, paused interest, stale 'running' row), file delivery executed
  from the scheduler path, clean stop with joined threads. `pytest` smoke
  suite: 3 passed. G3 time-math guard tests land in WP9 as planned.
- WP7: web settings UI implemented — `web/app.py` routes and a full
  `index.html` form surface: global settings (endpoint, default model,
  per-stage models, reasoning effort, timezone, output dir), SMTP, fallback
  user email, write-only API key entry (shows configured/not-set only — G1),
  interest CRUD with schedule editing and deliverable/delivery toggles, feed
  add/remove, "run now" via the scheduler thread, and the last 5 runs per
  interest rendered inline from the store (no separate status page — see
  DECISIONS.md). Validation is hand-rolled (D2) with flash errors that leave
  settings.toml untouched. Verified with the Flask test client against
  redirected config paths (real settings.toml/.env untouched): round-trip of
  every settings.toml field, API key written to .env and present in no
  rendered page nor in settings.toml, duplicate/invalid interest rejection,
  rename with D5 history warning, feed CRUD, run-now success and all error
  paths, run status rendering. `pytest` smoke suite: 3 passed. Not covered:
  visual/browser rendering — the agent cannot see pages; a human browser
  check is pending at task approval.
- WP8: CLI wiring completed —
  - `run`: sequential pipeline runs over the selected interests
    (`--all` skips paused; `--interest NAME` may force a paused one),
    delivery dispatch on completion, failure-alert dispatch on failure,
    exit code 1 on any failure / unknown interest / missing API key
  - `serve`: builds the Flask app with the store and a live
    `SchedulerThread`, binds 127.0.0.1:8421 by default, SIGTERM unwinds
    into a clean scheduler stop and exit 0 (systemd-compatible)
  - `deploy/newspipeline.service`: systemd user unit (replaces the inline
    README sketch)
  - README finalized against actual behavior: status line, run exit codes,
    catch-up/pause semantics, stale-'running'-row repair via sqlite3,
    autostart via the shipped unit
  - Verified end-to-end with redirected config paths (real
    settings.toml/.env/data untouched): `run --all` (completed run + file
    delivery, paused interest untouched), explicit paused-interest run,
    unknown-interest and missing-key exits, and `serve` with a real browser
    round-trip (GET / 200, POST /run → real scheduled run completed,
    SIGTERM → scheduler stopped, exit 0). `pytest` smoke suite: 3 passed.
- WP9: guard-test suite installed (`pytest tests/`: **21 passed**) —
  - `tests/conftest.py`: fixture RSS feed + article pages + mock
    chat-completions endpoint (capturing all LLM requests), fake SMTP
    server, config-path isolation (real settings.toml/.env never touched),
    legacy module loader
  - `test_secrets.py::test_api_key_not_persisted_or_exposed` (G1) +
    `test_api_key_only_in_authorization_header`: write-path guard, .env
    permissions, key absent from settings.toml / every rendered page /
    request URLs / request bodies / logs
  - `test_store.py::test_article_url_dedupe` (G2) + RSS date variants
  - `test_scheduler.py::test_next_run_*` (G3): grid semantics, tick window,
    malformed-time fallback, missed-run grace period — including parity
    against the actual legacy module over a start_time × interval grid
  - `test_pipeline.py::test_detect_language_*` (G4): German/English/empty,
    1.5x threshold boundary, parity against the actual legacy module
  - `test_e2e.py::test_pipeline_end_to_end` (G5): full run against the
    fixture feed + mock LLM → 2 full-content articles, 2 themes, all three
    deliverable types per theme, brief with word count, file output
    (brief.md + 2 theme files), cross-run dedupe; plus
    `test_email_delivery_end_to_end` against the fake SMTP server
  - Bug fixed (found by the G1 guard): `config._upsert_env` now updates
    `os.environ` — previously a newly saved API key was ignored by the
    running process until restart (T1 path touched, flagged)

### Verification statement (WP9)

Tested: all guarded invariants G1–G5 with the named guard tests green;
pipeline, delivery (file + unauthenticated SMTP), scheduler, web UI, and
CLI behavior end-to-end against local fixtures, per the WP4–WP8
verification records above. Not tested (no live credentials/servers in this
environment): live OpenRouter request/response behavior (T3), authenticated
SMTP delivery — STARTTLS+LOGIN (T2), and visual/browser rendering of the
settings UI (agent cannot see pages; human browser check pending).
