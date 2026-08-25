# Refactor tracking — newspipeline

Task: refactor until no high-impact architectural objection remains
unresolved or explicitly accepted. Live-test + review pass + commit after
each significant step.

Stop condition (DECISIONS.md 2026-08-24):

- M1 `pytest tests/ -q` — baseline 2026-08-24: **21 passed**. Target: green every commit.
- M2 `grep -c "\[OPEN\]" .agent-reviews/refactor-newspipeline.md`
  — baseline: **6**. Target: 0 across two consecutive final passes.
- Budget: 10 commits or 3h wall-clock.

Commits land on `main` (repo convention: linear history, direct commits).
Review passes are run by a fresh-context reviewer agent over each step diff;
findings classified high/medium/low. High-impact ones become HI-n below.

## High-impact objections

Statuses: [OPEN] unresolved · [FIXED] resolved by commit · [ACCEPTED]
explicitly accepted by the human · [ESCALATED] awaiting human decision.

- HI-1 [FIXED — commit step 1] pipeline.py `_scrape` runs blocking network/CPU work directly
  on the scheduler's asyncio event loop: `feedparser.parse(url)` fetches the
  feed synchronously (pipeline.py:142) and `trafilatura.extract(...)` parses
  article HTML on-loop (pipeline.py:157). While one interest scrapes, the
  60s tick stalls and all other concurrent runs freeze mid-flight (e.g.
  between LLM calls). Async facade over blocking calls — the main
  architectural defect. Fix direction: `asyncio.to_thread` around both calls;
  sequential order preserved; no semantic change (G-guards untouched).
  Resolution: review pass 1 FAILED the naive to_thread wrap (feedparser's own
  fetch is untimed → executor wedge + shutdown hang); reworked to timed httpx
  fetch + parse-only offload. Review pass 2 PASS. New guarded invariant G6 +
  tests/test_scrape_loop.py; guard sensitivity verified against pre-fix code.
  Accepted LOWs from pass 2: redirect chains re-arm per-hop timeout;
  no raise_for_status on feeds (error pages = zero-entry feeds, logged);
  `resp` name reused for article responses.
- HI-2 [FIXED — commit step 2; T2 diff approved by human] emailer.py:57 `server.starttls()` without an SSL context —
  smtplib defaults to an *unverified* context, so STARTTLS credentials
  (NEWS_SMTP_PASSWORD) are exposed to active MITM. Fix direction: pass
  `ssl.create_default_context()`. Touches T2 (SMTP auth path) → flag to
  human before commit.
  Resolution: verifying context installed; guard tests pin context +
  starttls→login→send order (new invariant G7). Review PASS. Behavioral
  delta (self-signed authenticated relays now fail loudly) logged in
  CHANGELOG.md and accepted.
- HI-3 [FIXED — commit step 3; T1 diff approved by human] config.py:74 writes settings.toml in place (`write_text`) —
  a reader (scheduler tick re-reads settings every 60s; Flask threads serve
  concurrently) can observe a torn file; web routes do unlocked
  load→mutate→save cycles so two overlapping POSTs lose updates. Fix
  direction: atomic tmp-file + `os.replace`, plus a module-level mutation
  lock held across load→save cycles in the web layer.
  Resolution: `_atomic_write_text` for settings.toml (0644) and .env (0600);
  RLock `config.mutation_lock` around all six mutating routes + _upsert_env;
  G1 scan hardened to string-leaf walk (repr escaping defeated the old
  check); newline .env values now refused instead of silently corrupted.
  Torn-read guard test with live readers, sensitivity verified 3/3 against
  reverted write path. Review pass 2 PASS (residual LOWs accepted:
  dict keys unscanned by secret scan — schema-controlled; dir-fsync omitted
  on power-loss tradeoff; temp-hygiene test covers settings path only).
- HI-4 [FIXED — commit step 4] scheduler.py `_launch` starts runs without checking that an
  API key is configured (cli.py:108 checks). With no key, every scheduled
  slot produces a guaranteed-failed run row plus a failure-alert attempt —
  alert noise and DB rows for a known-missing precondition. Fix direction:
  pre-check in `_launch`, consistent with CLI semantics.
  Resolution: gate in _launch, key passed through to Pipeline (review pass 1
  FAIL caught duplicate import + stale version comment + TOCTOU re-read;
  all fixed; pass 2 PASS). Guards: refusal test (no rows), passthrough +
  completion-dispatch test, failure-alert dispatch sibling test.
  Accepted LOWs: get_api_key file I/O on loop thread is frequency-bounded;
  gate ordering may mislabel an exotic "no key while running" corner;
  duplicated guidance string between cli.py and scheduler.py can drift.
- HI-5 [FIXED — commit step 5] Run-completion dispatch duplicated: cli.py `_run_selected`
  and scheduler.py `_run_interest_safe` both implement
  run→status→deliver_run/deliver_failure_alert with slightly divergent
  error handling. Drift risk between manual and scheduled delivery. Fix
  direction: one shared dispatch function (delivery.py), both callers use it.
  Resolution: `delivery.dispatch_finished_run` returns (status, run);
  scheduler via executor, CLI synchronous; dedup itself guarded by wrapping
  spy on both entry points. Review PASS (2 MEDIUMs + 3 LOWs applied
  pre-commit). Accepted LOWs: none outstanding.
- HI-6 [FIXED — commit step 6] pipeline.py:236 extracts themes with greedy regex
  `re.search(r"\[.*\]", response, re.DOTALL)` — markdown fences, leading
  prose, or trailing commentary make parsing fail silently and the analyze
  stage yields zero themes while the run still reports completed. Fix
  direction: tolerant extraction (strip code fences, balanced-bracket scan),
  unit-tested.
  Resolution: `_extract_json_array` (fence strip + all balanced regions,
  string/escape/newline-aware; object-wrapped + citation-bracket cases
  found by review pass 1 FAIL and fixed; junk arrays raise loudly; literal
  [] legit). Integration pins for garbage/raising LLM. Pass 2 PASS.
  Accepted LOWs: trailing commas remain fatal (legacy); empty-vs-junk-array
  precedence documented in docstring.

## Pending human decision (escalated / acceptance candidates)

- ESC-1 [ESCALATED] config.py PROJECT_ROOT derives from `__file__`.parents[2],
  which resolves correctly only for editable installs (the documented setup).
  Under a regular install, config/data paths would resolve inside site-packages.
  Fixing means changing path resolution — touches where state persists
  (deployment contract). Candidate outcome: accept as-is (documented
  editable-install posture) or fix. Human decides.
- ESC-2 [ESCALATED] Web UI has no Origin/Host validation: a malicious page
  open in the user's browser can cross-site POST to 127.0.0.1:8421 (CSRF:
  rewrite settings, inject feeds, trigger runs, delete interests) and DNS
  rebinding defeats the localhost binding. Hardening = security-model change
  → R3 escalation required; cannot implement without explicit instruction.
  Branches: (a) add Origin/Host allowlist middleware on POSTs, (b) accept
  current documented posture (bind 127.0.0.1, single-user, no auth).
- Accepted-by-ledger (carried, not re-litigated): D5 rename orphans run
  history; WP6 stale 'running' row blocks interest until manual repair;
  WP4 sequential sync-style scraping kept for legacy parity (HI-1 revisits
  the loop-blocking aspect only).

## Step log

| # | Scope | Tests | Review verdict | Commit |
|---|-------|-------|----------------|--------|
| 0 | Stop condition logged, tracking file created | n/a (docs) | n/a | b774bb4 |
| 1 | HI-1 scrape off-loop + G6 guard test | 23 passed; serve smoke 200 | pass 1 FAIL → rework → pass 2 PASS | 0ccf799 |
| 2 | HI-2 STARTTLS verification (T2 flagged, approved) | 25 passed; serve smoke 200 | PASS (5 findings applied pre-commit) | 1d53097+532649c |
| 3 | HI-3 atomic config writes + lock + G1 hardening (T1 flagged, approved) | 32 passed; serve smoke incl. POST save/api-key | pass 1 PASS w/ MEDIUMs → all applied → pass 2 PASS | 222d12e |
| 4 | HI-4 scheduler API-key launch gate | 35 passed; serve smoke 200 | pass 1 FAIL → all findings applied → pass 2 PASS | 7618dcf |
| 5 | HI-5 shared dispatch_finished_run | 39 passed; CLI run --all + serve smoke | PASS (2 MEDIUM + 3 LOW applied) | b7e326b |
| 6 | HI-6 tolerant theme-array extraction | 59 passed; serve smoke 200 | pass 1 FAIL (2 HIGH verified) → rework → pass 2 PASS | (below) |

M2 after step 6: `grep -c "\[OPEN\]"` = 0 (HI all resolved; ESC-1/ESC-2 pending human decision).
