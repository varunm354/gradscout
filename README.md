# GradScout

A small, dependable utility that hourly monitors reliable structured job sources for
**2027 new-grad and eligible early-career technical roles**, prioritizes major tech and
highly competitive employers, matches each role to the best resume (Backend / AI / Data),
and alerts to Discord. Deterministic-first; the LLM is optional and bounded.

Not a SaaS product. No frontend, no server, no Docker, no auth, no auto-apply, no scraping.

## Pipeline

```
collect -> normalize -> store/dedupe -> eligibility -> prioritize
        -> resume-select -> notify -> health-report
```

The deterministic core is fully testable and never depends on the optional LLM.

## Requirements

- Python **3.12**
- SQLite (stdlib), httpx, Pydantic, PyYAML; pytest + ruff for dev

## Setup (local)

```bash
# 1. Python 3.12 is required. If missing on macOS:
brew install python@3.12

# 2. Create the venv and install
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 3. Configure
cp config.example.yaml config.yaml   # edit sources / watchlist
cp .env.example .env                  # set DISCORD_WEBHOOK_URL for real sends

# 4. Test / run
pytest
python -m scripts.run --dry-run      # full pipeline, no Discord sends
python -m scripts.run                # real run (needs DISCORD_WEBHOOK_URL)
```

## Configuration

Everything operator-editable lives in `config.yaml` (watchlist, Greenhouse/Lever/Ashby
boards, GitHub repos, keyword filters, notification thresholds). Secrets are env vars only.
`company_priority` is data, not code.

## Key design decisions (post-review)

1. **Cross-source dedupe.** A job is merged by stable source identity **OR** canonical
   application URL. `url_canonical` is uniqueness-enforced, and a `job_sources` mapping
   records every source that surfaced the same job — so a role seen via both a native ATS
   and a GitHub repo becomes one record.
2. **Internships.** Eligible only when the listing *explicitly* accepts graduating seniors,
   final-year students, or recent grads. Ineligible when it explicitly requires returning
   to school incompatibly with a 2027 grad. Review when enrollment eligibility is unstated
   or ambiguous. Nothing is discarded.
3. **Big-tech coverage status** per watchlist company: `direct` (native ATS configured),
   `indirect` (only via a GitHub repo), `not_configured`, or `failed`. GradScout never
   implies direct monitoring when coverage is only indirect.
4. **No lost alerts.** `max_alerts_per_run` caps sends per run; unsent qualifying jobs stay
   **pending** and are delivered on a later run / in the digest. An alert is recorded as
   sent **only after Discord accepts it**.
5. **Notifications are failure-first.** Immediate alerts are sent for failures (loudly for
   high-priority source failures). Healthy status is summarized once daily, not hourly.
6. **Bounded LLM agent (Phase 3-4).** The deterministic monitor works without it. The agent
   may call explicit job-analysis tools and return validated structured output; it may not
   control discovery, delete records, fabricate source fields, or bypass validation.
7. **Never fabricate.** Posting dates come only from the source; we distinguish
   `source_posted_at` from `first_seen_at` and never claim "posted recently" without evidence.
8. **Title-first relevance gating (Phase 5.1).** A job must show a credible target-role
   signal in its TITLE (e.g. software/backend/platform/site-reliability engineer, ML/AI
   engineer, applied scientist, data engineer, product engineer) before it can be
   classified as a relevant role family or receive a normal P1/P2/P3 alert. Description
   keywords may only refine which target family a credible title belongs to -- they can
   never independently promote a nontechnical title (compliance, policy, marketing,
   partnerships, sales, biological safety, fellowships, economics, ...) into one, no
   matter how many AI/ML terms appear in the body text. Titles with neither a credible nor
   a nontechnical signal are ambiguous and go to the review digest, never a normal alert.

## State persistence (GitHub Actions)

State is kept on a **dedicated orphan `state` branch** (not on `main`; the binary DB is
never committed to `main`). Each hourly run (`.github/workflows/gradscout-monitor.yml`):

1. checks out `main` for code, validates `config.yaml` is production-safe, then restores
   `data/gradscout.db` from the `state` branch (via git plumbing -- the branch is never
   checked out into the working tree);
2. runs the pipeline;
3. on success only, commits the updated DB back to the `state` branch, guarded by
   `--force-with-lease` so a concurrent run's newer state is never overwritten.

The orphan `state` branch shares no history with `main` and, by construction, can only
ever contain `data/gradscout.db.gz` and a tiny machine-managed README -- never application
code. `workflow_dispatch` supports a manual `dry_run` input; the hourly schedule fires at
minute 17 (not top-of-hour). See `docs/PHASE_5_HANDOFF.md` for full setup, secrets,
permissions, troubleshooting, and how to disable the schedule.

**Compressed state (Phase 5.1).** GitHub rejects any single pushed file over 100 MB; the
first production database (~135.7 MB raw) exceeded that, so `data/gradscout.db` is
deterministically gzip-compressed (`scripts/db_compression.py`) to `data/gradscout.db.gz`
before being committed to `state`, and decompressed back to `data/gradscout.db` on
restore. Compression is checkpointed/closed first (`PRAGMA wal_checkpoint`) and uses a
fixed `mtime=0` so re-compressing unchanged content always yields a byte-identical `.gz`
(needed for `state_save.py`'s no-op detection). If the compressed file would still exceed
100 MB, the save fails loudly with a clear error rather than attempting (and having
GitHub reject) the push. `state_restore.py` reads the compressed path first and falls
back to a legacy raw `data/gradscout.db` path for backward compatibility with state saved
before this change.

## Status

Phases 0–5 complete. `scripts/collect.py` remains available as a network-only collector
harness (no alerts, no classification). GradScout can run either locally
(`python -m scripts.run`, against a local `data/gradscout.db`) or unattended on GitHub
Actions with durable state on the `state` branch (see above and
`docs/PHASE_5_HANDOFF.md`). **A production `config.yaml` must still be created and
committed before the first real scheduled/manual run** -- see `docs/PHASE_5_HANDOFF.md`
§"First real run".
