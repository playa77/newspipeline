# WORKPACKAGES.md — newspipeline implementation plan

Each work package ends in a reviewable, testable state. Invariants G1–G9
and trigger paths T1–T3 are defined in [DECISIONS.md](DECISIONS.md); a
task is complete only when its guarded invariants have green guard tests.
Current version: **0.3.0**.

| WP | Title | Status |
|----|-------|--------|
| WP1 | Scaffolding, config & secrets handling | ✅ done (0.1.0) |
| WP2 | SQLite store | ✅ done (0.1.0, scaffold level — guard tests in WP9) |
| WP3 | LLM client | ✅ done (0.1.0, scaffold level — live calls untestable, T3) |
| WP4 | Pipeline stages | ✅ done (0.1.0 — guard tests G4/G5 landed in WP9) |
| WP5 | Delivery: email + local file | ✅ done (0.1.0 — live SMTP paths T2) |
| WP6 | Scheduler thread | ✅ done (0.1.0 — G3 guard tests landed in WP9) |
| WP7 | Settings UI (superseded by W2/WP12) | ✅ done (0.1.0; HTML surface removed in 0.3.0) |
| WP8 | CLI wiring, systemd unit, README finalization | ✅ done (0.1.0) |
| WP9 | Guard tests + end-to-end verification | ✅ done (0.1.0) |
| WP10 | Architectural refactor hardening (G6/G7, atomic writes) | ✅ done (0.2.0) |
| WP11 | JSON API replacing the web UI (D8) | ✅ done (0.3.0) |
| WP12 | First-class rename preserving history — G8 (D11) | ✅ done (0.3.0) |
| WP13 | Startup sweep for orphaned runs — G9 (D10) | ✅ done (0.3.0) |
| WP14 | Deployment: system unit + VPS docs (D7) | ✅ done (0.3.0) |
| WP15 | Gmail relay delivery path + docs (D9) | ✅ done (0.3.0 — live inbox check is a manual step) |
| WP16 | Desktop client `newspipeline-client` (D7) | ✅ done (0.3.0 — GUI verified by construction/offscreen; visual check manual) |

---

## WP11 — JSON API (0.3.0)

Flask retained as HTTP layer; templates/static deleted; all routes under
`/api/*` return structured JSON with correct status codes. Hand-rolled
validation per D2. `create_app(store, scheduler)` injectable pattern kept;
Host/Origin gates kept as defense-in-depth; mutation lock extended to every
mutating route. No secret-setting endpoint: `.env` is edited on the VPS.

**Done when:** API round-trips and validation errors covered by tests; G1
extended so no response body ever contains a secret value.

## WP12 — First-class rename (0.3.0)

Rename endpoint amends D5's orphaning consequence: validate → SQLite rows
repointed first (idempotent) → atomically rewritten settings.toml, loud
before/after logging, recovery by re-running the rename. PUT can never
rename.

**Done when:** G8 guard green (`tests/test_rename_history.py`), including
the simulated crash-between-stores convergence case.

## WP13 — Startup sweep (0.3.0)

Serve startup marks orphaned 'running' rows failed ('interrupted by
restart') before catch-up; one-shot `run` never sweeps (a concurrent serve
may own the row).

**Done when:** G9 guard green (`tests/test_startup_sweep.py`): seeded row
swept on scheduler start, interest schedulable again; CLI run path leaves
the row untouched.

## WP14 — Deployment (0.3.0)

Systemd *system* unit (`Restart=always`, `RestartSec=5`,
`NEWSPIPELINE_HOME=/var/lib/newspipeline`, hardening directives) replaces
the user unit; README gains VPS deployment layout and sshd hardening notes
(key-only auth, optional `restrict,permitopen="127.0.0.1:8421"`).

**Done when:** unit file shipped; walkthrough documented; live deployment
remains a documented manual step.

## WP15 — Gmail relay (0.3.0)

No sender changes: 0.2.0's certificate-verifying STARTTLS path (G7) is the
target. Fully commented smtp.gmail.com:587 example in settings.example.toml
(password via NEWS_SMTP_PASSWORD in .env only) plus README "Gmail relay"
section (dedicated sender account, 2FA/app password, first-send security
review, ~500/day cap irrelevant here). Failure alerts use existing
recipient resolution.

**Done when:** docs/example complete; live inbox delivery remains a
documented manual step (T2).

## WP16 — Desktop client (0.3.0)

New top-level `client/` distribution (`newspipeline-client`, PySide6 +
httpx, entry point `newspipeline-client`). Owns the full tunnel lifecycle:
runtime free-port pick, ssh spawn (`ExitOnForwardFailure=yes`,
`ServerAliveInterval=30`), TCP readiness poll, clean teardown on exit or
crash, visible connection state, structured error surface. Profile
(host/user/port, no secrets) under `~/.config/newspipeline-client/`.
Tabs: global settings, interests CRUD (feeds add/remove, schedule,
deliverables, delivery modes + recipient override, paused banner,
trigger-now, trivial-confirm rename), run history.

**Done when:** fresh `pip install ./client` works; GUI constructs
offscreen; feature parity list above present; visual/desktop-side check
remains a documented manual step.
