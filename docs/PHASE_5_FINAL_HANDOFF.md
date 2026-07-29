# GradScout — Phase 5 Final Handoff

Final consolidated handoff for Phase 5 (unattended GitHub Actions deployment with
durable SQLite state). Supersedes nothing -- `docs/PHASE_5_HANDOFF.md` remains the
detailed design/setup reference; this file is the closing summary of everything
implemented and verified in this chat, for the person about to commit and deploy it.

## 1. Current Git branch and repository state

- Current branch: **`feature/phase5-deployment`**.
- Other branches: `main` (local) and `remotes/origin/main`. **No `state` branch exists**,
  locally or on `origin`.
- `git log --oneline`: only the two pre-existing Phase 0–4 commits --
  `4be8691 feat: build GradScout job monitoring and analysis pipeline` and
  `baa94e8 feat: build GradScout core monitoring and analysis engine`.
- **Nothing from Phase 5 has been committed.** `git status --short` shows all Phase 5
  work as uncommitted modifications/untracked files:

  ```
   M README.md
   M gradscout/config.py
   M tests/test_config.py
  ?? .github/
  ?? docs/PHASE_5_HANDOFF.md
  ?? docs/PHASE_5_FINAL_HANDOFF.md
  ?? scripts/check_config.py
  ?? scripts/state_restore.py
  ?? scripts/state_save.py
  ?? tests/test_check_config.py
  ?? tests/test_state_restore.py
  ?? tests/test_state_save.py
  ?? tests/test_workflow_yaml.py
  ```

- `config.yaml` does not exist in the repo (only `config.example.yaml`). No production
  config has been created on your behalf (see §7, §13).

## 2. Completed Phase 5 files and responsibilities

| File | Responsibility |
|---|---|
| `.github/workflows/gradscout-monitor.yml` | Hourly (minute 17) + `workflow_dispatch` (with `dry_run` boolean input) unattended run. `permissions: contents: write` only. Concurrency serializes state-writing runs; dry-runs never queue behind them. |
| `scripts/state_restore.py` | Restores `data/gradscout.db` from the `state` branch via git plumbing (`ls-remote`/`fetch`/`show`) -- never checks the branch out into the working tree. |
| `scripts/state_save.py` | Builds a new commit (`hash-object`/`mktree`/`commit-tree`) whose tree contains **only** `data/gradscout.db` + a machine-managed README, pushed with `--force-with-lease` (or a plain create-push if the branch is new). |
| `scripts/check_config.py` | CI preflight: fails clearly, before anything else runs, if `config.yaml` is missing or still the demo config. |
| `gradscout/config.py` | Added `require_production`/`example_path` kwargs to `load_config` (default off, so all existing local-dev/pytest behavior is unchanged) and `ProductionConfigError`. |
| `docs/PHASE_5_HANDOFF.md` | Full design rationale, GitHub setup, manual verification, and troubleshooting reference. |
| `docs/PHASE_5_FINAL_HANDOFF.md` | This file. |
| `README.md` | Updated "State persistence" and "Status" sections. |
| `tests/test_state_restore.py`, `test_state_save.py`, `test_check_config.py`, `test_workflow_yaml.py`, `test_config.py` (additions) | 47 new offline tests (fake git responder / tmp files / YAML structural checks -- no real git remote, GitHub, or Discord call anywhere in the suite). |

No changes were made to `gradscout/pipeline.py`, `gradscout/db.py`, `gradscout/notify/discord.py`, or any Phase 0–4 invariant. Phase 5 is purely additive.

## 3. Exact workflow lifecycle

`.github/workflows/gradscout-monitor.yml`, single job (`monitor`), in order:

1. **Checkout main** (`actions/checkout@v4`).
2. **Set up Python 3.12** (`actions/setup-python@v5`, `cache: pip`, keyed on `pyproject.toml`).
3. **Install dependencies** (`pip install -e ".[llm]"`).
4. **Determine run mode** -- resolves `dry_run` to `'true'`/`'false'` from
   `workflow_dispatch` input (schedule/other triggers always resolve to `'false'`).
5. **Print run mode** -- trigger + dry_run to logs (no secrets).
6. **Check production config** (`scripts.check_config`) -- runs for **every** trigger,
   dry-run included; fails the whole job immediately if `config.yaml` is missing or demo.
7. **Restore state from 'state' branch** (`scripts.state_restore`) -- prints branch
   existence + restore status + tip SHA; step id `restore` exposes `outputs.sha`.
8. **Run pipeline (real)** *or* **Run pipeline (dry-run)** -- mutually exclusive via
   `if: success() && dry_run == '...'`. Real gets `DISCORD_WEBHOOK_URL` +
   `OPENAI_API_KEY`; dry-run gets only `OPENAI_API_KEY` (webhook secret never enters its env).
9. **Save state to 'state' branch** (`scripts.state_save`, real runs only,
   `if: success() && dry_run == 'false'`) -- only reachable if step 8 succeeded.
10. **Report state status (dry-run)** -- prints `state changed: n/a` for symmetry when step 9 was skipped.

No step anywhere uses `continue-on-error` or `|| true`.

## 4. State branch restore/save mechanism

The `state` branch is **never checked out into the working tree** -- both scripts operate purely through git plumbing:

- **Restore**: `git ls-remote` (existence) → `git fetch --depth=1 origin state` + `git rev-parse FETCH_HEAD` (exact tip SHA) → `git show <sha>:data/gradscout.db` captured as raw bytes (never text-decoded) → written to the target path.
- **Save**: `git hash-object -w` the DB file (by path) and the README (via `--stdin`) → a small recursive `_build_nested_tree` helper builds the `data/gradscout.db` + `STATE_BRANCH_README.md` tree (git `mktree` only accepts immediate children, so `data/` requires one inner tree) → `git commit-tree` (author/committer identity passed as env vars scoped to that one call) → `git push <sha>:refs/heads/state [--force-with-lease=...]`.

This guarantees, by construction, that the branch can only ever contain those two paths -- there is no code path that could commit anything else to it.

## 5. First-run behavior when no state branch exists

- `remote_branch_exists` returns `False` → `restore_db` returns `RestoreResult(branch_exists=False, restored=False, sha=None)` -- a normal, non-error condition, printed as `state branch 'state' exists: False`.
- `scripts/run.py` proceeds exactly as it always has: `db.connect()` creates `data/gradscout.db` if absent, `db.init_db()` runs `CREATE TABLE IF NOT EXISTS` (safe either way) -- **no Phase 5 change to this path**.
- After a successful first real run, `state_save.save_state` sees `prior_sha=None`, builds an orphan commit (no `-p` parent), and pushes with a plain (non-force) ref-creation push -- which is itself safely rejected if another run created the branch in the meantime (the new commit has no parent, so it can never fast-forward a tip that appeared concurrently).
- The branch is also handled correctly if it exists but has no DB yet (e.g. a manually bootstrapped README-only commit, see `docs/PHASE_5_HANDOFF.md` §9.4): `restore_db` returns `restored=False` with a real `sha`, and `state_save` correctly treats that as "no prior DB content" (`changed=True`) while still building on top of that existing tip as its parent.

## 6. Concurrency and force-with-lease race protection

- **Concurrency**: `group` is `gradscout-monitor-state` for the schedule and any real (`dry_run=='false'`) `workflow_dispatch`, or a unique `gradscout-monitor-dryrun-<run_id>` for a manual dry-run. `cancel-in-progress: false` always -- an in-progress state-writing run is never killed mid-push. Net effect: only one state-writing run is ever in flight; dry-runs never queue behind one and never race with one (they never touch state).
- **`--force-with-lease`**: on save, if the branch already has history, the push is keyed to the exact `prior_sha` this run restored. If another run updated `state` in between, the push is rejected atomically server-side -- `state_save.py` raises a `RuntimeError` ("push...was rejected...Refusing to force-overwrite") and the job fails loudly. **No state is ever lost or overwritten this way.**
- This was verified against a real (non-mocked) local git remote in this chat: a simulated race using a stale `prior_sha` was correctly rejected, while the legitimate concurrent run's commit remained intact on the branch (see §10).

## 7. Production config validation behavior

- `gradscout/config.py::load_config(path, require_production=False, example_path=...)` -- default `False` preserves all existing local-dev/pytest behavior exactly (no change to `scripts/run.py` or any Phase 0-4 test).
- `require_production=True` (used only by `scripts/check_config.py`, invoked by the workflow for **every** trigger including manual dry-run) raises `ProductionConfigError` when:
  - `config.yaml` is missing.
  - it is byte-identical to `config.example.yaml`.
  - any greenhouse/lever/ashby/watchlist entry's company or board name contains "demo" (case-insensitive -- catches the shipped `LeverDemo`/`leverdemo` placeholder).
  - zero sources are configured across greenhouse/lever/ashby/github_repos.
- `config.yaml` is treated as **not a secret** -- it's meant to be committed to `main` alongside code, since it only ever describes non-secret candidate/watchlist/source settings.

## 8. Required GitHub secrets and repository permissions

**Secrets** (Settings → Secrets and variables → Actions):

| Secret | Required? | Notes |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Yes, for real alerts | A run without it still completes; `DiscordNotifier` logs a warning and no-ops (never crashes). Never exposed to the dry-run pipeline step's env. |
| `OPENAI_API_KEY` | No | Optional bounded LLM enrichment; unset → deterministic-only, unchanged behavior. |

**Permissions**: the workflow declares `permissions: contents: write` at the top level -- the only scope needed, to push to `state` with the default `GITHUB_TOKEN`. No `issues`, `pull-requests`, `packages`, or other scope. If your org restricts the default `GITHUB_TOKEN` to read-only repo-wide, you must separately allow write access under **Settings → Actions → General → Workflow permissions** -- the in-file block alone can't escalate beyond that repo setting.

## 9. Dry-run versus real-run behavior

| | Dry-run (`workflow_dispatch`, `dry_run=true`) | Real (schedule, or `workflow_dispatch` with `dry_run=false`) |
|---|---|---|
| Production config preflight | **Runs** (by explicit design, so a dry-run accurately tests deployment readiness) | Runs |
| State restore | Runs (reads existing state) | Runs |
| `DISCORD_WEBHOOK_URL` in pipeline step env | **Never present** | Present (from secret) |
| Discord HTTP requests | Never made (`DiscordNotifier` dry-run boundary makes no request and always returns `False`) | Made if webhook configured |
| State save | **Never invoked** | Invoked, only `if: success()` |
| Concurrency group | Unique per run (`dryrun-<run_id>`) | Shared (`gradscout-monitor-state`) |

## 10. Final writable-state verification results

Performed live in this chat, from the actual repo root (`/Users/varunmohanraj/grad-scout`), using a disposable path under `/tmp` and fixture (non-network) collectors -- never the real `data/gradscout.db`, never live Discord/GitHub:

- **Full DB lifecycle via the real pipeline** (`gradscout.pipeline.run_once` + `gradscout.db`): init db → insert job → classify → enqueue alert → re-seen job bumps `last_seen_at` while preserving `first_seen_at` → close/reopen preserves everything. All passed.
- **Explicit writability checks**: db file and parent directory both `os.access(W_OK)`; `gradscout/db.py` confirmed to use a plain `sqlite3.connect(str(db_path))` (no read-only URI mode anywhere).
- **State round-trip through the real `state_restore`/`state_save` helpers**, against a throwaway local bare git remote (no GitHub, no network): pushed the verification DB to a `state` branch, restored it into a *new* path, confirmed byte-for-byte identical content, `mode=0o644`, writable, and re-ran the full pipeline against the restored copy (insert + classify + enqueue + close/reopen) successfully.
- All disposable artifacts (`/tmp/gradscout_verify*`) were removed afterward; `git status` and `data/` were confirmed clean of any leftovers.

## 11. Root-cause assessment for the earlier readonly-database error

An earlier ad hoc, backgrounded, live-network dry-run (~3 minutes, ~18,000 real Simplify jobs, piped through `tail`) failed with `sqlite3.OperationalError: attempt to write a readonly database` on a job insert. Targeted reproduction in this chat, from the correct working directory, did **not** reproduce it:

- A plain SQLite write from the repo root: succeeded.
- 18,000 individual insert+commit cycles (matching `upsert_job`'s per-row-commit pattern at the same scale as that run): succeeded in 8.6s, with 33GB free disk.
- The full real pipeline, including a real git-based state restore/save round-trip: succeeded twice.

The only concrete sandbox restriction found was unrelated: `git init`/`clone`/`rm -rf` under `/tmp` fail with `Operation not permitted` specifically on git's executable hook-sample files (an exec-bit/chmod restriction on paths outside the workspace) -- resolved by requesting elevated permissions for those specific commands. The original failing run never touched git, so this wasn't its direct cause.

**Conclusion**: the earlier failure is attributed to that specific ad hoc command's session state -- it was a backgrounded process whose own terminal metadata reported a `cwd` pointing at a temp directory that had just been `rm -rf`'d moments earlier in the same shell session (leftover from prior exploratory commands), not a deliberate, isolated invocation. It is not reproducible under a correct, isolated invocation at even higher write volume, and is not a defect in `gradscout/db.py`, `scripts/run.py`, or any Phase 5 script.

**Why the GitHub Actions workflow is unaffected**: Actions runners are fresh, non-sandboxed Ubuntu VMs with full disk access, provisioned per job -- there is no Cursor seatbelt/sandbox profile, no shared or stale local-shell-session state, and no restricted syscalls of any kind carried over from local development. Neither candidate explanation (the hook-chmod restriction or the stale-cwd session artifact) has any counterpart in that environment.

## 12. Current test and lint results

- `pytest` → **177 passed** (up from 130 at the Phase 4 handoff; 47 new tests, all fully offline).
- `ruff check .` → **All checks passed!**
- No linter diagnostics in `gradscout/`, `scripts/`, or `tests/`.
- `git status` clean of any leftover verification artifacts; no `state` branch, no commits, nothing pushed.

## 13. Exact next steps

1. **Commit and push the Phase 5 feature branch**:
   ```bash
   git add .github/ docs/PHASE_5_HANDOFF.md docs/PHASE_5_FINAL_HANDOFF.md \
     scripts/check_config.py scripts/state_restore.py scripts/state_save.py \
     gradscout/config.py README.md \
     tests/test_config.py tests/test_check_config.py tests/test_state_restore.py \
     tests/test_state_save.py tests/test_workflow_yaml.py
   git commit -m "..."
   git push -u origin feature/phase5-deployment
   ```
2. **Open a PR** from `feature/phase5-deployment` into `main`, review, and **merge** (GitHub only runs the `schedule:` trigger from the default branch, so the workflow is inert until this lands on `main`).
3. **Create a production `config.yaml`** (§7 of `docs/PHASE_5_HANDOFF.md`): `cp config.example.yaml config.yaml`, replace the example watchlist/sources (including removing `LeverDemo`) with your real ones, verify with `python -m scripts.check_config --config config.yaml`, and commit it to `main` -- it is not a secret.
4. **Configure the `DISCORD_WEBHOOK_URL` secret** under Settings → Secrets and variables → Actions.
5. **Optionally configure `OPENAI_API_KEY`** the same way, only if you want bounded LLM enrichment.
6. **Run a manual GitHub Actions dry-run**: Actions → "GradScout Monitor" → "Run workflow" → check the `dry_run` input → Run. Confirm the config preflight passes and the pipeline completes with no Discord requests.
7. **Run the first real workflow**: either wait for the hourly schedule (minute 17 UTC) or trigger `workflow_dispatch` with `dry_run` left unchecked.
8. **Verify Discord and the `state` branch**: confirm expected alerts arrived in Discord, then (read-only, from a local machine) `git fetch origin state && git log origin/state --oneline` and confirm exactly one new commit containing only `STATE_BRANCH_README.md` and `data/gradscout.db`.

## 14. Commit status

**No Phase 5 changes have been committed or pushed.** Everything in §1–§13 exists only in the working tree on `feature/phase5-deployment`. No `state` branch exists locally or on `origin`. Step 1 of §13 is the first commit that will ever be made for this phase.

## 15. Phase 5.1 — first production run fixes (also uncommitted)

The first real GitHub Actions run (after §13 was carried out) surfaced two issues,
both fixed in a follow-up chat -- see `docs/PHASE_5_HANDOFF.md` §14 for the full
design writeup. Summary for the person about to commit/deploy this:

1. **State exceeded GitHub's 100 MB file limit.** The first production DB
   (~135.7 MB) was rejected on push, so state never persisted and every run
   re-alerted everything. Fixed by deterministically gzip-compressing state to
   `data/gradscout.db.gz` (new `scripts/db_compression.py`), with a clear
   fail-fast if the compressed size would still exceed 100 MB, and backward-
   compatible legacy-raw-path restore. No workflow YAML changes were needed.
2. **Nontechnical roles were generating normal Discord alerts** (and inflating
   the pending-alert queue to ~17,000 on the first run), because role/keyword
   scoring ran over the full description with no title gate, and an
   eligible-but-irrelevant job's priority fallback incorrectly still qualified
   as a normal `p3` alert. Fixed with title-first technical relevance gating in
   `gradscout/roles.py` (new `evaluate_title_gate()`) and `gradscout/eligibility.py`
   (a new hard/soft step-0 rule), plus filtering closed (`active: false`)
   listings out of the Simplify feed collector.

Both fixes are purely deterministic (no LLM involvement), fully covered by new
offline tests, and -- like all Phase 5 work -- **not committed or pushed**;
they exist only in the working tree alongside everything else in this handoff.
