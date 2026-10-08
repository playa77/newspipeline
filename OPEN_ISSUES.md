# OPEN_ISSUES.md — evidenced open-issue ledger

Ledger mechanics (standing rules for every future session):

- **Append-only.** IDs are never renumbered; an item leaves only through a `Closed`
  section that carries a re-runnable evidence line. "Resolved" means evidenced, not asserted.
- **Inventory only.** This file was written as a sweep, not a fix session — nothing in it was
  repaired or resolved while writing it.
- **Standing constraints** (a fresh session must not violate these by accident):
  - The agent **never commits, stages, pushes, or tags**; merges and releases are the operator's.
  - **No model ids are written anywhere** by the agent (config, docs, ledger, code).
  - `.env` and `VPS-ACCESS.md` are off-limits — never read, echoed, or copied.
  - The security model (SSH local forward + loopback-only bind, no TLS, no tokens) is **settled
    policy** — never propose hardening it; Host/Origin gates are defense-in-depth only.
  - **Remote/SSH work is agent work.** Every `ssh`/`scp`/`git`-over-ssh invocation is run
    by the agent itself, non-interactively over `sshpass` (`sshpass -e` with the password
    from the documented access file; `GIT_SSH_COMMAND="sshpass -e ssh"` for the git remote;
    `StrictHostKeyChecking=accept-new`; `-tt` only when a real tty is needed). What is
    **forbidden** is any invocation that can fall back to an interactive prompt and pop a
    login/password window on the operator's dev box — that is what an un-`sshpass`'d
    `git ls-remote` did. The deployed-revision check (OI-12) is therefore an agent row, not
    an operator row.

## Snapshot (every value from the command shown, run 2026-09-16)

| Fact | Value | Command |
|---|---|---|
| Head SHA | `1241a6e8a7290456d07deb8df6e25f19dfc14a38` | `git rev-parse HEAD` |
| Head vs origin/main | equal — `1241a6e` on both (local remote-tracking ref, no network) | `git rev-parse HEAD origin/main` |
| Branch / working tree | `main`, clean — no dirty, no untracked files | `git status -b --porcelain` → `## main...origin/main` only |
| Ignored-but-present state | `.venv/`, `data/`, `prompts/`, `VPS-ACCESS.md`, `client/build/`, `client/.venv/`, `*.egg-info/`, `__pycache__/`, `.pytest_cache/` | `git status --porcelain --ignored=matching` |
| Tags | none (`0` tags); CHANGELOG top section is `0.5.0 — 2026-09-16 (feature, in progress)` | `git tag -l | wc -l` |
| `remotes/vps/main` tracking ref | `1241a6e 2026-09-16` — i.e. the last push *recorded locally* is head; **live** deployed revision not checked by the agent (OI-12) | `git log -1 remotes/vps/main` (local ref read only) |
| CI | none configured — no `.github/` directory exists | `ls .github` |
| Test suite | 173 passed in 58.59 s | `python -m pytest tests/ -q` (this session, local) |
| Deployed revision | operator-run check — command provided in OI-12 | — |

## Open issues

Rows whose ID also appears under **Closed** below have left the open list; they
are left in place here only as the ledger's own history.

### Class 1 — contract/correctness

Checks run: last-touched audit of every tracked `.md` against the change range (produced OI-09);
prompt set-equality compare of all six `prompts/*.txt` against `src/newspipeline/prompts.py`
(5/6 byte-identical modulo the documented format-escape convention — the `analyze_themes` USER
example's `{{…}}` vs `{…}` is the export convention, not drift); no OpenAPI schema or
error-code tables exist in the repo to diff routes against; exception/status mapping not
re-audited this sweep (no reported drift).

- **OI-01** — `client/README.md` promises "authentication is your normal SSH key" while the
  deployed dev-phase install is password-auth, so the client's tunnel spawn against the live VPS
  is on an unverified path. Evidence: `client/README.md:20` vs `DEPLOYMENT.md` ("Dev-phase
  philosophy: low friction first" — interactive password SSH; "Access from the desktop": "The
  client does this itself"). Repair: one verified client connect against the VPS as deployed, or
  correct the README claim. Owner: **verify**

- **OI-23** — **fixed in 0.5.1 (D16)** — catch-up re-ran every interest on every serve restart:
  `find_most_recent_missed` reads only the schedule and `_run_catch_up` launched without consulting
  the run history, so a deploy or crash-restart added a second full round per interest. Evidence
  (live, 2026-09-18): two runs per interest per day on the 16th, 17th and 18th with
  `interval_hours = 24`, and 24 `Catch-up:` journal lines in 72 hours.
  Repair: `SchedulerThread._covering_run` + three guard tests. Owner: **agent** (closed — see Closed)

### Class 2 — never-verified behaviour

- **OI-02** — All four human-verification checklist items in README.md remain unchecked, and the
  0.5.0 rewrite of `emailer`/`email_layout`/`pipeline`/`store` landed after the last live email
  evidence (0.3.3, 2026-09-05), so live Gmail delivery of the HTML-first layout is unverified.
  Evidence: `README.md:249`–257 (four unchecked boxes); CHANGELOG 0.5.0 delivery rewrite.
  Repair: run the checklist once on the live install and check the boxes with dates. Owner: **verify** (needs live credentials — operator-assisted)
- **OI-03** — The five live interests still carry `target_summary_words = 750`; the 600-word
  default (D12) governs only newly created interests. Evidence: DECISIONS.md D12 "Scope caveat
  for the live install" (verified via `GET /api/interests` on 2026-09-16).
  Repair: operator edits the five interests to 600 (client or settings.toml). Owner: **operator**
- **OI-04** — The deployed revision on the VPS has not been verified against local head
  (agent row since 2026-09-18, when the false operator-only SSH rule was purged). Evidence:
  this snapshot's "Deployed revision" row; the local tracking ref `remotes/vps/main` reads
  `1241a6e` but is not a live check.
  Repair: agent runs OI-12's command and compares to `git rev-parse HEAD`. Owner: **agent**

### Class 3 — docs/ledger drift

- **OI-05** — The CHANGELOG 0.5.0 section contradicts itself: its first bullet says the HTML
  layout is "not yet wired into delivery" while a later bullet in the same section says delivery
  now runs on documents. Evidence: `CHANGELOG.md:10` vs `CHANGELOG.md:33`; `src/newspipeline/emailer.py:31,64-65` renders `email_layout`.
  Repair: drop the stale parenthetical. Owner: **agent**
- **OI-06** — All six `prompts/*.txt` header comments claim to be exports of prompts "hardcoded
  in `src/newspipeline/pipeline.py`", false since 0.4.1 moved prompts into `prompts.py`.
  Evidence: `prompts/analyze_themes.txt:3` and the same header in the five sibling files.
  Repair: regenerate headers to name `src/newspipeline/prompts.py`. Owner: **agent**
- **OI-07** — AGENTS.md's guard inventory lists G1–G9, but G10 (LLM null content) exists with
  its own guard file. Evidence: AGENTS.md "Guard tests G1–G9" vs DECISIONS.md G10 entry and
  `tests/test_llm_null_content.py`. Repair: update AGENTS.md to G1–G10. Owner: **agent**
- **OI-08** — AGENTS.md claims the full suite runs in "~10s"; measured 58.59 s this session.
  Evidence: AGENTS.md Commands section vs the snapshot table. Repair: update or drop the number.
  Owner: **agent**
- **OI-09** — `client/README.md` was last touched 2026-08-25, predating every client change since
  (0.3.2/0.3.3 segfault fixes, 0.4.4 word-default) and still carries version 0.3.0 — the doc
  nobody re-checked. Evidence: `git log -1 -- client/README.md` → `336bbe4 2026-08-25`.
  Repair: re-check it against current client behaviour in the docs sweep. Owner: **agent**

### Class 4 — ops/deploy

- **OI-10** — No release tag exists for any shipped version, so nothing anchors what the VPS
  checkout or a rollback corresponds to. Evidence: `git tag -l` → empty; CHANGELOG records
  releases through 0.5.0-in-progress. Repair: operator tags at least the deploy baseline and
  adopts a tag-per-release rule. Owner: **operator**
- **OI-11** — Backup policy is "suggested, not yet configured": no nightly VPS backup of
  `/var/lib/newspipeline` exists. Evidence: `DEPLOYMENT.md:171` ("Backup (suggested, not yet
  configured)"). Repair: operator installs the cron entry per that section. Owner: **operator**
- **OI-12** — Deployed-revision check: agent row (see the standing constraints above; the
  earlier "operator-only" designation came from a rule that was wrong and has been purged).
  Evidence: this row, plus the ledger entry of 2026-09-18 recording the correction.
  Repair: the agent runs and compares against local `git rev-parse HEAD`:
  ```bash
  SSHPASS='<newspipeline ssh password>' sshpass -e ssh newspipeline@37.60.240.152 \
    'git --git-dir=/srv/git/newspipeline.git log --oneline -1'
  ```
  (Password from the documented access file — never stored in this repo.) Owner: **agent**

### Class 5 — parked minors (copied out of the tracked review trail so they stay visible)

Source: `.agent-reviews/refactor-newspipeline.md`, "Accepted-by-ledger (carried, not
re-litigated)" block. Listed here for visibility; each stays closed unless re-opened by ruling.

- **OI-13** — Trailing-comma JSON in theme extraction remains fatal (accepted legacy seam,
  documented only in a docstring). Evidence: `.agent-reviews/refactor-newspipeline.md`
  Accepted-by-ledger block. Repair: decide — fix the parser, or keep as a documented seam.
  Owner: **operator**
- **OI-14** — Executor delivery can outlive the scheduler join on SIGTERM (accepted WP6-family
  seam): a stop mid-delivery may cut delivery short. Evidence: same block.
  Repair: decide — document as accepted, or add a delivery-drain step at shutdown. Owner: **operator**
- **OI-15** — Cross-process settings writes are unlocked (accepted single-user posture): two
  concurrent `newspipeline run` processes could race `settings.toml`. Evidence: same block.
  Repair: decide — accept permanently (note in DECISIONS.md) or widen the mutation lock.
  Owner: **operator**
- **OI-24** — **(opened 2026-10-08, D20/D19/D21) Netiquette across the FULL run is
  partially implemented: politeness spacing/retry/UA exists per scrape run (D19), but
  interests run CONCURRENTLY at the scheduler level, so at 04:00 (or a post-restart
  catch-up burst) up to 6 parallel request streams hit servers simultaneously, with
  combined outbound rate possibly far above the polite per-run 2 s gap.
  Evidence: live smoke run of 2026-10-08 — 6 "Pipeline run starting" lines all at
  13:47:30, 6 distinct hosts being fetched in the same second (journalctl).
  Scope to decide and implement (operator): serialize interest RUNS (one at a
  time), or share ONE PolitenessLayer across all concurrent runs (per-host or
  global gap), or stagger interest start times; plus verify politeness covers
  every outbound request class (Reddit 403s still happen per D19 wanted UA
  fallback?), confirm the 2 s gap applies to retry paths too, and confirm
  politeness applies to article fetches identically to feed fetches.
  Repair options documented (not chosen yet). Owner: **agent + operator**

### Class 6 — tooling/hygiene

- **OI-16** — The client is installed as a **copied, non-editable** package (`pip install
  ./client`), so client-source edits have no effect until the install is re-run — and no doc
  says so. Evidence: `AGENTS.md:14` and `client/README.md:8` (install commands; no reinstall
  note anywhere). Repair: add the reinstall step to AGENTS.md + `client/README.md`, or switch
  dev installs to `pip install -e ./client`. Owner: **agent**
- **OI-17** — No linter or typechecker is configured; pytest is the only automated gate.
  Evidence: AGENTS.md states it; neither `pyproject.toml` nor `client/pyproject.toml` has lint
  config. Repair: add ruff config to both distributions, wired into the CI row (OI-20).
  Owner: **agent**
- **OI-18** — `prompts/` — the human-editing surface for the six prompts (the 0.4.0/0.4.1
  export → edit → import-back flow) — is git-ignored, so human prompt edits are unversioned and
  a fresh clone has none. Evidence: `.gitignore` ("#misc / prompts/"); CHANGELOG 0.4.0/0.4.1.
  Repair: un-ignore `prompts/` or document the lifecycle in README and cover it with OI-21's
  guard. Owner: **agent**
- **OI-19** — `deploy/newspipeline.service` still self-identifies as "(0.3.0)" while the server
  is at 0.5.0. Evidence: `deploy/newspipeline.service:1`. Repair: bump the header comment in the
  next server change. Owner: **agent**

### Class 7 — missing guards

- **OI-20** — No CI exists at all: pytest runs only when someone invokes it locally; nothing
  judges a push. Evidence: `ls .github` → missing (snapshot). Repair: add a GitHub Actions
  workflow running `pytest tests/` on push/PR; operator enables Actions on the private repo.
  Owner: **agent** (write the workflow) + **operator** (enable)
- **OI-21** — No guard asserts the `prompts/*.txt` exports match `src/newspipeline/prompts.py` —
  today's drift surfaced only via a manual compare. Evidence: the per-file compares behind
  OI-06 (manual, unrepeatable); no test references the exports. Repair: add
  `tests/test_prompts_export.py` asserting all six SYSTEM/USER pairs match (headers excluded,
  the `{…}`/`{{…}}` export convention asserted explicitly). Owner: **agent**
- **OI-22** — The standing constraint "no model ids written anywhere by the agent" has no
  mechanical check, and DECISIONS.md's G10 entry already cites a specific model id in a tracked
  file. Evidence: the G10 entry in DECISIONS.md (2026-09-05). Repair: operator rules whether
  historical ledger mentions are exempt; then add a grep guard over *newly changed* tracked
  lines in CI (historical text untouched). Owner: **operator** (ruling) + **agent** (guard)

## Closed

Each row left the open list through the evidence line shown; re-run the
command to check it.

- **OI-03** — live interests' `target_summary_words`: closed by the
  2026-09-16 live-config entry in DECISIONS.md ("Live-install config:
  topic-article target 600 → 500 words"), which superseded this row's 600
  target AND its "repair" scope: the five interests were edited through the
  API and read back on the host.
  Evidence: `grep -n "topic-article target 600 → 500" DECISIONS.md`;
  re-check on the host with `curl -fsS http://127.0.0.1:8421/api/interests`.
- **OI-05** — CHANGELOG self-contradiction: the "not yet wired into delivery"
  parenthetical is gone; the 0.5.0 section now states the document-based
  delivery once.
  Evidence: `grep -c "not yet wired into delivery" CHANGELOG.md` → `0`.
- **OI-06** — all six `prompts/*.txt` headers named
  `src/newspipeline/pipeline.py`; they now name
  `src/newspipeline/prompts.py` and the constant pair each file exports.
  Evidence: `grep -l "pipeline.py" prompts/*.txt` → no output.
- **OI-07** — AGENTS.md guard inventory now reads G1–G10 and lists
  `test_llm_null_content`.
  Evidence: `grep -n "G1–G10" AGENTS.md`.
- **OI-08** — AGENTS.md's suite-time claim replaced with the measured value
  ("~60 s, 176 tests").
  Evidence: `grep -n "176 tests" AGENTS.md`.
- **OI-16** — the client's non-editable install is now documented where an
  installer looks (`client/README.md`), with the editable alternative.
  Evidence: `grep -n "copied, not editable\|pip install -e ./client" client/README.md`.
- **OI-19** — `deploy/newspipeline.service` header self-identity bumped
  0.3.0 → 0.5.0.
  Evidence: `head -1 deploy/newspipeline.service`.

- **OI-10** — no release tag existed for any shipped version, so nothing anchored
  what the VPS checkout or a rollback corresponded to. Closed 2026-09-18: the
  0.5.0 release carries the first annotated tag (D15, tag-per-release rule).
  Evidence: `git tag -l` → `v0.5.0`; `git ls-remote --tags origin v0.5.0` → the
  tag object plus its peeled `^{}` commit; release page
  `https://github.com/playa77/newspipeline/releases/tag/v0.5.0`.
- **OI-23** — catch-up no longer repeats a slot that already ran: `_covering_run` skips a slot
  covered by a completed or in-flight run, while a failed/interrupted one is still retried (crash
  recovery, D10, intact).
  Evidence: `pytest tests/test_scheduler.py -k catch_up -v` → three named tests green; the
  regression test fails against the pre-fix code (short-circuit `_covering_run` to `return None`
  and re-run it).
  Live re-check after deploying 0.5.1: restart the service and confirm the journal shows
  `Catch-up skipped — interest … already served the … slot (run N)` instead of `Catch-up: …`.

Still open and worth naming here: **OI-12** and **OI-04** (deployed-revision
checks) are agent rows per the corrected standing constraints.

## Closing sequence (dependency order)

1. **Decide the operator batch** in one round: OI-03, OI-10, OI-11, OI-13, OI-14, OI-15,
   OI-22's ruling, OI-20's enablement.
2. **Behaviour fixes** that follow from those rulings (each with its tests).
3. **Verify rows with real runs**: OI-01 and OI-02 on the live install (operator-assisted).
4. **Documentation sweep** only after the above: OI-05, OI-06, OI-07, OI-08, OI-09, OI-16,
   OI-18, OI-19.
5. **Install the guards and prove they fail on a deliberate drift**: OI-20, OI-21, OI-22's
   check — each demonstrated once against an injected violation.
6. **Release mechanics last**: OI-10 (tags), OI-12 (deployed-revision re-check after the next
   operator push), OI-11 (backup).

**Definition of done:** every row closed with a re-runnable evidence line; the guard suite green
on `main` in CI (not just locally); every operator-gated item *decided*, not merely agreed with.
