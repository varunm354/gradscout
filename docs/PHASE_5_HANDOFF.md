# GradScout — Phase 5 Handoff

Concise handoff for continuing work. **No commits have been made for Phase 5** (see
§11); everything below is implemented in the working tree only, on
`feature/phase5-deployment`.

## 1. Project goal and constraints

Unchanged from Phase 4 (see `docs/PHASE_4_HANDOFF.md` §1). GradScout hourly-monitors
structured job sources for 2027 new-grad / eligible early-career roles, prioritizes
major-tech employers, matches a resume, and alerts to Discord. Deterministic-first;
the LLM is optional and bounded. Not a SaaS product.

## 2. Completed phases 0–5

- **Phase 0–4**: scaffolding, persistence core, collectors + normalization,
  deterministic/LLM classification, full local pipeline orchestration, Discord
  delivery. See `docs/PHASE_3_HANDOFF.md` / `docs/PHASE_4_HANDOFF.md`.
- **Phase 5 (this phase)**: unattended hourly execution on GitHub Actions, with
  `data/gradscout.db` durably persisted on a dedicated orphan `state` branch (never on
  `main`), a production-config safety preflight, and a fully offline test suite for all
  of it. **No frontend, auto-apply, new ATS integrations, or additional agent features**
  were added -- explicitly out of scope for this phase.

## 3. What changed vs Phase 4 (contract changes to be aware of)

- **`gradscout/config.load_config` gained `require_production` and `example_path`
  kwargs.** Both default to the old behavior (`require_production=False`), so
  `scripts/run.py` and all existing local-dev usage (including `pytest`, and
  `cp config.example.yaml config.yaml` for local testing) are **completely unaffected**.
  `require_production=True` is used ONLY by the new `scripts/check_config.py` preflight
  that the GitHub Actions workflow runs before every trigger (schedule, manual dry-run,
  manual real run).
- **New `ProductionConfigError`** (in `gradscout/config.py`) is raised by
  `load_config(..., require_production=True)` when `config.yaml` is missing, is
  byte-identical to `config.example.yaml`, still references a source/watchlist entry
  whose company or board name contains "demo" (case-insensitive -- catches the shipped
  `LeverDemo`/`leverdemo` placeholder), or configures zero sources across
  greenhouse/lever/ashby/github_repos.
- **No changes to `gradscout/pipeline.py`, `gradscout/db.py`, or any Phase 0–4
  invariant.** Phase 5 is purely additive: a workflow, three new `scripts/*.py` helpers,
  and config-loader kwargs that default to a no-op.

## 4. Folder / module architecture (Phase 5 additions marked `*`)

```
grad-scout/
├── .github/
│   └── workflows/
│       └── gradscout-monitor.yml   * hourly (+ manual) unattended run
├── docs/PHASE_5_HANDOFF.md         * this file
├── gradscout/
│   └── config.py                     + require_production / ProductionConfigError
├── scripts/
│   ├── check_config.py             * CI preflight: fail clearly on missing/demo config
│   ├── state_restore.py            * restore data/gradscout.db from the `state` branch
│   ├── state_save.py               * save data/gradscout.db to the `state` branch
│   ├── run.py                        unchanged (Phase 4 operational entrypoint)
│   └── collect.py                    unchanged (network-only collector harness)
└── tests/
    ├── test_config.py                + require_production test cases
    ├── test_check_config.py        * offline preflight tests
    ├── test_state_restore.py       * offline state-restore tests (fake git responder)
    ├── test_state_save.py          * offline state-save tests (fake git responder)
    └── test_workflow_yaml.py       * structural validation of the workflow YAML
```

## 5. The state-branch mechanism (read this before touching `scripts/state_*.py`)

**The `state` branch is never checked out into the working tree.** `state_restore.py`
and `state_save.py` operate purely on git plumbing (`ls-remote`, `fetch`, `show`,
`hash-object`, `mktree`, `commit-tree`, `push`), so `main`'s working tree can never be
contaminated by, or leak into, `state`'s history.

- **Restore** (`scripts/state_restore.py::restore_db`): `git ls-remote` checks
  existence; if present, `git fetch --depth=1 origin state` + `git rev-parse
  FETCH_HEAD` gets the exact tip SHA; `git show <sha>:data/gradscout.db` (captured as
  raw bytes, never decoded as text -- it's a binary SQLite file) is written to
  `data/gradscout.db`. Both "branch doesn't exist" and "branch exists but has no DB yet"
  (e.g. right after a bootstrap commit) are normal, non-error first-run conditions. A
  git failure for any OTHER reason (fetch/network error) is raised as a hard failure --
  proceeding as if there were no prior state when the branch actually has real history
  would risk a later save silently discarding it, which is worse than just failing loud.
- **Save** (`scripts/state_save.py::save_state`): builds a brand-new commit whose tree
  contains **only** `data/gradscout.db` and a tiny machine-managed
  `STATE_BRANCH_README.md`, via `hash-object` (writing blobs directly, including the
  README via `--stdin` so it's never written to the actual working tree) + a small
  recursive `_build_nested_tree` helper (needed because `git mktree` only accepts
  immediate children, so `data/gradscout.db` requires one inner tree for `data/` and one
  outer tree) + `commit-tree`. This is what *guarantees* the branch can only ever hold
  state files, by construction, on every single commit, forever.
  - **Race safety**: if the branch already has history, the push uses
    `git push ... --force-with-lease=refs/heads/state:<prior-sha>` -- atomically
    rejected server-side if another run has moved the branch tip since this run
    restored it. If the branch doesn't exist yet, a plain non-force ref-creation push is
    used, which is likewise rejected if another run created it first (the new commit has
    no parent, so it can never fast-forward a tip that appeared in the meantime). Either
    rejection raises a `RuntimeError` -- the run fails loudly rather than force-overwrite.
  - **No-op detection**: if the DB's content blob SHA is unchanged from what's already at
    `--prior-sha`, no commit/push happens at all (`changed=False, pushed=False`).
  - **Git identity**: `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_*` are passed as
    environment variables scoped to the single `commit-tree` subprocess call --
    never via `git config --global`, and this script only ever runs inside the CI job.

This was validated two ways: (1) offline `pytest` with an injected fake git responder
(no real git/network in the test suite), and (2) a real end-to-end smoke test against an
actual local bare git repo (first-run bootstrap, restore-after-save round-trip with exact
byte comparison, a no-op save, and a simulated race where a stale `--prior-sha` push was
correctly rejected by `--force-with-lease` without touching the real state). Not
committed anywhere; this was throwaway verification in `/tmp`.

## 6. Important invariants (do not break)

All Phase 0–4 invariants still hold (jobs never discarded, cross-source dedupe,
pending→sent alert lifecycle, hard rules override the LLM, first-seen time is never
posting time, etc. -- see `docs/PHASE_4_HANDOFF.md` §5). Phase 5 adds:

- **The SQLite database must never be committed to `main`.** `.gitignore` already
  excludes `*.db`; the workflow only ever writes it to the `state` branch, via plumbing
  that never touches `main`'s working tree.
- **A failed workflow must never destroy the last known-good state.** The pipeline step
  has no `continue-on-error` / `|| true` anywhere. The "Save state" step's `if:` requires
  `success()` explicitly (a custom `if:` overrides GitHub Actions' implicit
  success-only default, so this must stay explicit) -- a failed pipeline run never
  reaches the save step.
- **Dry-run never writes state and never sends Discord.** `scripts/run.py --dry-run`
  already guarantees no Discord HTTP call (Phase 4); the workflow additionally never
  invokes `scripts/state_save.py` when `dry_run == 'true'`, and the "Run pipeline
  (dry-run)" step doesn't even receive `DISCORD_WEBHOOK_URL` in its env.
- **Manual dry-run also runs the production-config preflight**, by design (per explicit
  instruction for this phase) -- so a dry-run accurately tests deployment readiness, not
  just pipeline logic. This means a dry-run will also fail clearly if `config.yaml` is
  missing/demo, same as a real run.
- **`config.yaml` is not a secret.** It's supplied by being committed to `main`,
  alongside code. Secrets are exclusively `DISCORD_WEBHOOK_URL` and (optional)
  `OPENAI_API_KEY`, flowing only as `${{ secrets.* }}` → the `scripts.run` step's process
  env -- never written to `config.yaml`, disk, logs, or the `state` branch.
- **Git identity is configured only within the workflow's `commit-tree` call** (see §5),
  never globally, and no Phase 5 script ever runs `git config --global` or mutates the
  local dev machine's git identity.
- **The agent never created or pushed the `state` branch, and never committed/pushed any
  Phase 5 change**, per explicit instruction for this phase. The branch will be created
  by the workflow itself, safely, the first time it completes a real (non-dry) run --
  see §9.

## 7. Commands (setup / tests / lint / local run -- unchanged)

```bash
# setup (unchanged)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml   # LOCAL TESTING ONLY -- see §9 for production
cp .env.example .env

# tests + lint
pytest
ruff check .

# local run (unchanged)
python -m scripts.run --dry-run
python -m scripts.run

# Phase 5 preflight / state scripts (normally only invoked by the workflow, but
# runnable locally for debugging -- see §10 for troubleshooting)
python -m scripts.check_config --config config.yaml
python -m scripts.state_restore --db data/gradscout.db --branch state
python -m scripts.state_save --db data/gradscout.db --branch state --prior-sha "<sha>"
```

## 8. Exact current test/lint results

- `pytest` → **177 passed** (up from 130 in Phase 4; 47 new tests, all offline -- no
  real git remote, no GitHub API, no Discord HTTP anywhere in the suite).
- `ruff check .` → **All checks passed!**
- No linter diagnostics in `gradscout/`, `scripts/`, or `tests/`.

## 9. GitHub repository setup (exact steps)

### 9.1 Secrets (Settings → Secrets and variables → Actions)

| Secret | Required | Used for |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Yes, for real alerts (a run without it completes fine, just sends nothing -- `DiscordNotifier` logs a warning and no-ops) | Real (non-dry) runs only; never exposed to the dry-run pipeline step |
| `OPENAI_API_KEY` | No -- optional | Bounded LLM enrichment; if unset, the deterministic monitor runs unchanged |

Never put either value in `config.yaml`, a commit, a log line, or the `state` branch.

### 9.2 Workflow permissions (least privilege)

The workflow declares `permissions: contents: write` at the top level -- that is the
**only** permission required (to push commits to the `state` branch with the default,
auto-revoked `GITHUB_TOKEN`). No `issues`, `pull-requests`, `packages`, or other scope is
requested. If your organization has restricted the default `GITHUB_TOKEN` permissions
repo-wide to read-only, you'll additionally need to allow write access for Actions under
**Settings → Actions → General → Workflow permissions** (or explicitly grant `contents:
write` there) -- the in-file `permissions:` block alone cannot escalate beyond the repo's
Actions setting.

### 9.3 Production `config.yaml` (must be created before the first real run)

`config.yaml` is intentionally **not** a secret -- commit it to `main`. It is not yet
created (only `config.example.yaml` exists). Steps:

1. `cp config.example.yaml config.yaml`
2. Edit `candidate`, `watchlist`, `greenhouse`/`lever`/`ashby`/`github_repos` with your
   real values. Remove/replace the `LeverDemo` placeholder entirely.
3. Verify locally: `python -m scripts.check_config --config config.yaml` must print
   `config check: OK (...)`.
4. Commit `config.yaml` to `main` (a normal code commit -- not the `state` branch).

Until this exists and passes the check, **every** workflow run (scheduled, manual
dry-run, or manual real run) will fail immediately at the "Check production config"
step, by design -- see §6.

### 9.4 (Optional) manually pre-creating the `state` branch

Not required -- the workflow's first successful real run creates `state` automatically
and safely (§5), because its tree is built purely from `hash-object`/`mktree`, which
guarantees it can only ever contain `data/gradscout.db` and the README. Pre-create it
yourself only if you have a specific reason (e.g. you want to configure branch
protection rules on `state` before the workflow ever runs). These commands are
**documented for reference only -- they are not run as part of this handoff**; run them
yourself, deliberately, if and when you decide to:

```bash
# From a scratch clone/worktree -- NOT your normal working tree, since --orphan
# switches the current branch and empties the working directory.
git checkout --orphan state
git rm -rf .
printf '# GradScout state branch\n\nMachine-managed by .github/workflows/gradscout-monitor.yml.\n' > STATE_BRANCH_README.md
git add STATE_BRANCH_README.md
git commit -m "gradscout state: bootstrap"
git push origin state
git checkout main   # return to your normal branch immediately after
```

### 9.5 Enabling the schedule

Once `config.yaml` is committed, the workflow at
`.github/workflows/gradscout-monitor.yml` runs automatically at minute 17 of every hour
(`17 * * * *`, UTC) as soon as it's merged to `main` (GitHub only runs `schedule:`
triggers from the default branch). No further setup is required beyond §9.1–9.3.

## 10. Manual verification steps

1. **Manual dry-run** (before ever enabling the schedule for real): GitHub → Actions →
   "GradScout Monitor" → "Run workflow" → the `dry_run` input defaults to unchecked
   (`false`), so explicitly check it to run a dry-run → Run. Confirm in the logs:
   - "Print run mode" shows `dry_run: true`.
   - "Check production config" passes (or fails clearly if `config.yaml` isn't ready
     yet -- fix per §9.3 and re-run).
   - "Restore state from 'state' branch" prints `state branch 'state' exists: False` on
     the very first run (expected -- nothing to restore yet), or `True` + `previous
     state restored: True` on any subsequent run.
   - "Run pipeline (dry-run)" completes with `jobs_seen`/`jobs_created` etc in the
     structured JSON log lines, and no Discord requests (dry-run never sends).
   - "Report state status (dry-run)" prints `state changed: n/a` -- confirms no state
     mutation happened.
2. **First real run**: same as above but leave `dry_run` unticked (`false`), or simply
   wait for the hourly schedule. Confirm additionally:
   - "Save state to 'state' branch" runs (only reachable on success) and prints `state
     changed: True`, `state pushed: True`, and a new commit SHA.
   - After the run, `git fetch origin state && git log origin/state --oneline` (from
     your local machine, read-only) shows exactly one new commit containing only
     `STATE_BRANCH_README.md` and `data/gradscout.db`.
3. **Second real run** (an hour later, or triggered manually): confirm "previous state
   restored: True" and that job counts (`jobs_seen`, `jobs_unchanged`) reflect the
   persisted DB (i.e. most jobs from the first run show up as `unchanged`, not
   `created`) -- this is the concrete evidence that state actually persisted.

## 11. Commit status

**No commits have been made for Phase 5.** All of §2–§10 above exists only in the
working tree on `feature/phase5-deployment`. No `state` branch exists locally or on
`origin` -- it will be created by the workflow itself on its first successful real run
(see §5 and §9.5), never by a human running branch-creation commands locally (§9.4's
manual commands are documented for optional reference only and were not executed).

## 12. Troubleshooting

- **"Check production config" fails with "not found"**: `config.yaml` isn't committed
  to `main` yet. See §9.3.
- **"Check production config" fails with "example/demo"**: `config.yaml` still matches
  `config.example.yaml` byte-for-byte, or still references a company/board containing
  "demo" (e.g. the shipped `LeverDemo` placeholder), or has zero sources configured.
  Edit `config.yaml` for real and re-commit.
- **"Restore state" fails with "fetching it failed"**: the `state` branch exists but
  couldn't be fetched (transient GitHub outage, or the workflow's `GITHUB_TOKEN` lost
  read access). Re-run the workflow; if it persists, check repo network/outage status
  and that `permissions: contents: write` is still in place (§9.2).
- **"Save state" fails with "rejected -- another run may have updated state
  concurrently"**: another workflow run pushed to `state` after this run restored from
  it. This should be rare given the `concurrency:` group serializes state-writing runs,
  but if it happens, it is by design a safe, loud failure -- **no state was lost**; the
  other run's newer commit is still on `state`. Simply re-run the workflow (or wait for
  the next scheduled run) to pick up the latest state and try again.
- **A run failed mid-pipeline (e.g. a collector or Discord error)**: the "Save state"
  step never ran, so `state` still has the previous successful run's DB. Fix the
  underlying error (see the structured JSON logs in that step's output) and re-run --
  no manual recovery of the `state` branch is needed.
- **You suspect `state` branch content is wrong / want to inspect it**: read-only,
  from your local machine: `git fetch origin state && git show origin/state:data/gradscout.db > /tmp/gradscout.db`
  then open it with any SQLite browser. Never push to `state` from a local machine.

## 13. Disabling the hourly schedule safely

- **Temporarily** (keep the ability to trigger manually): GitHub → Actions →
  "GradScout Monitor" → "..." menu → "Disable workflow". Manual `workflow_dispatch` runs
  (including dry-run) still work while disabled; only the `schedule:` trigger is
  suppressed. Re-enable from the same menu.
- **Durably** (e.g. long-term pause without relying on the UI toggle): comment out or
  remove the `schedule:` block in `.github/workflows/gradscout-monitor.yml` and commit
  that change to `main`. `workflow_dispatch` continues to work either way.
- Disabling the schedule never touches the `state` branch -- the last known-good
  `data/gradscout.db` remains exactly as it was.

## 14. Phase 5.1 — first production run fixes

The first real GitHub Actions run (schedule + one manual real run) surfaced two
production issues, both fixed in this chat. **No commits/pushes were made for
this phase either** -- see the top-level chat summary for exact verification
results.

### 14.1 Durable state exceeded GitHub's 100 MB file limit

The first production `data/gradscout.db` was ~135.7 MB. GitHub hard-rejects any
single pushed file over 100 MB, so `scripts/state_save.py`'s push to `state`
was rejected every run; since state never actually persisted, every run
restarted from empty and re-alerted everything it had already alerted.

Fix -- compressed state:
- New `scripts/db_compression.py`: `checkpoint_and_close()` (best-effort
  `PRAGMA wal_checkpoint(TRUNCATE)` + commit + close before touching the file
  at all, so compression always reads a consistent, non-open file), and
  deterministic `compress_db()`/`decompress_db()` (`gzip` with a fixed
  `mtime=0` and fixed compression level, so re-compressing byte-identical
  input always produces a byte-identical `.gz` -- required for
  `state_save.py`'s existing blob-SHA no-op detection to keep working on the
  compressed artifact).
- `scripts/state_save.py` now compresses `data/gradscout.db` to a sibling
  `data/gradscout.db.gz` before hashing, and that `.gz` path is the **only**
  DB path ever written into the tree (`STATE_BRANCH_README.md` +
  `data/gradscout.db.gz`, nothing else) -- an older commit's legacy raw
  `data/gradscout.db` entry is therefore dropped automatically on the very
  next save. If the compressed size would still exceed 100 MB,
  `ensure_within_github_limit()` raises a clear `StateTooLargeError` *before*
  ever attempting the push.
- `scripts/state_restore.py` tries `data/gradscout.db.gz` first (decompressing
  it back to `data/gradscout.db`), and falls back to reading a legacy raw
  `data/gradscout.db` path verbatim if the compressed path doesn't exist at
  the fetched tip -- so any branch history written before this change still
  restores correctly.
- No workflow YAML changes were needed: both scripts still take
  `--db data/gradscout.db` and handle the `.gz` sibling internally.
- A realistic generated-SQLite test (`tests/test_db_compression.py`) builds a
  real ~8 MB gradscout DB (5,000 synthetic job rows) and confirms it
  compresses to well under 1 MB (a ~92% reduction) -- consistent with a
  135.7 MB production DB compressing to comfortably under the 100 MB limit.

### 14.2 Nontechnical roles generating normal Discord alerts

The very first run also produced normal alerts for confidently nontechnical
roles (Biological Safety Research Scientist, AI Compliance Officer, Data
Scientist/Marketing, policy/fellowship programs, partnerships/business-facing
roles) because `gradscout/roles.py`'s keyword scoring ran over
`title + description` with no title gate -- any AI/ML mention in the body text
could make a nontechnical job score as a target role family. Worse,
`gradscout/prioritize.py`'s `score_role_priority`/`score_alert_priority` fell
back to `role_priority=3` / `alert_priority=p3` for ANY otherwise-eligible job
regardless of role relevance, and `p3` met `config.yaml`'s
`discord_min_priority: p3` -- so these got enqueued as individual normal
alerts, not just the review digest. This combined with the Simplify feed's
full historical/global listing (every company, not just the watchlist, and
including closed listings that the collector never filtered by `active`) to
produce the ~17,000-pending-alert backlog on the first run.

Fix -- title-first technical relevance gating (see `README.md` key design
decision #8 for the user-facing summary):
- `gradscout/roles.py` gained `evaluate_title_gate()`, checked against the
  TITLE only: a small `CREDIBLE_TITLE_FAMILY` phrase list (software/backend/
  platform/infrastructure/site-reliability/full-stack engineer, ML/AI
  engineer, applied/research scientist, data engineer, analytics engineer,
  data scientist, product engineer) and a `NON_TARGET_TITLE_TOKENS` list
  (compliance, policy, biological safety, marketing, partnerships, sales,
  account executive, fellowship(s), economics). A non-target hit always
  overrides a credible hit in the same title (e.g. "Data Scientist,
  Marketing"). `classify_role()` now returns `RoleFamily.other` immediately
  -- never scoring the description at all -- unless the title clears this
  gate, which is what stops description AI/ML mentions from resurrecting a
  nontechnical title.
- `gradscout/eligibility.py` gained a step-0 check using the same gate: a
  non-target title is now a **hard** ineligible rule (same class as the
  existing seniority/degree/experience hard rules -- never overridable by the
  optional LLM); a title with neither a credible nor non-target signal
  (ambiguous) becomes a **soft** `review` (consistent with how other
  ambiguous cases already resolve elsewhere in that function). Because both
  `classify_role` and `evaluate_eligibility` gate on the identical title
  check, `eligible => relevant=True` is now an invariant, so the old
  `p3`-fallback bug for `eligible`-but-irrelevant jobs is structurally
  unreachable -- `gradscout/prioritize.py` needed no change.
- `gradscout/collectors/github_repo.py` now skips Simplify feed rows marked
  `"active": false` (closed listings kept in the feed for historical record),
  directly reducing how much of the historical/global feed gets re-collected
  as "new" every run.
- Full regression coverage added in `tests/test_title_gate.py` (every named
  false-positive example plus every legitimate credible-family title) and
  extensions to `tests/test_roles_resume.py`, `tests/test_collectors.py`, and
  `tests/test_pipeline.py`.

## 15. Deferred features (Phase 6+)

Unchanged from Phase 4's deferred list (24-hour repeat reminder, richer Discord
formatting, non-Simplify GitHub parsers, Workday/iCIMS/other ATS, direct scraping,
fuzzy company+title merge, richer LLM enrichment, analytics/dashboards), plus,
specific to deployment: a CI job that runs `pytest`/`ruff` on every push (not added in
this phase -- out of scope; only the operational monitor workflow was requested), and a
24h/periodic reminder if the state branch itself hasn't been updated in an unexpectedly
long time (currently a stuck schedule would only be visible via GitHub's own Actions UI,
not a Discord alert). Also still explicitly out of scope: frontend, auto-apply, browser
automation, auth.
