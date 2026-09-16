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
  - **All remote/SSH access is operator-only.** The agent never opens an SSH connection
    (including via `sshpass`) and never contacts a git remote over SSH; `git` commands run
    against local refs only. The deployed-revision check is an operator row (OI-12).

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
- **OI-04** — The deployed revision on the VPS has not been verified against local head by the
  agent, by operator ruling. Evidence: this snapshot's "Deployed revision" row; the local
  tracking ref `remotes/vps/main` reads `1241a6e` but is not a live check.
  Repair: operator runs OI-12's command and compares to `git rev-parse HEAD`. Owner: **operator**

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
- **OI-12** — Deployed-revision check is operator-run (never the agent, per standing constraints).
  Evidence: this row; operator ruling recorded in the ledger header.
  Repair: operator runs, then compares with local `git rev-parse HEAD`:
  ```bash
  SSHPASS='<newspipeline ssh password>' sshpass -e ssh newspipeline@37.60.240.152 \
    'git --git-dir=/srv/git/newspipeline.git log --oneline -1'
  ```
  (Password from the operator's private notes — never stored in this repo.) Owner: **operator**

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
