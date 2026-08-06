# GradScout

A small, dependable utility that hourly monitors reliable structured job sources for
**2027 new-grad and eligible early-career technical roles**, prioritizes major tech and
highly competitive employers, matches each role to the best resume (Backend / AI / Data)
using a deterministic, config-driven skill matcher, diversifies alerts and the review
digest across companies, and alerts to Discord. Deterministic-first; the LLM is optional
and bounded.

Not a SaaS product. No frontend, no server, no Docker, no auth, no auto-apply, no scraping.

## Pipeline

```
collect -> normalize -> store/dedupe -> eligibility -> prioritize
        -> resume-select -> diversify -> notify -> health-report
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

Everything operator-editable lives in `config.yaml` (watchlist, Greenhouse/Lever/Ashby/
Rippling boards, GitHub repos, keyword filters, notification thresholds, resume-matching
profiles, per-company diversity caps). Secrets are env vars only. `company_priority` is
data, not code.

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
9. **Resume-aware matching is deterministic and swappable (Phase 6).** Every eligible/
   review job is scored against all three resume profiles (`gradscout/resume.py`,
   `WeightedKeywordResumeMatcher`) using sanitized, purely-technical weighted-term lists
   defined in `config.yaml` (`resume_profiles`) -- no PII, no LLM call, no network
   dependency. The recommendation, a saturating 0-100 match score, and a short
   explanation naming the actual top-weight matched skills (e.g. "Matched FastAPI,
   PostgreSQL, distributed systems") are shown in every Discord alert and digest row.
   The matcher is a `Protocol`, so a future embeddings-based engine can be swapped in
   without touching `analyze.py`, `pipeline.py`, or the DB schema.
10. **No single company can dominate alerts (Phase 6).** `max_alerts_per_company_per_run`
    / `max_review_items_per_company_per_run` cap how many items from one company one run
    selects (`gradscout/diversify.py`), round-robining across AI/Backend/Data
    recommendation categories first so a prolific poster (e.g. OpenAI) doesn't crowd out
    every other resume category, let alone every other company. Capped jobs are not lost
    or endlessly retried: they're marked `AlertState.suppressed` with an explicit
    `suppressed_company_cap` reason, never resent, and only become reconsiderable again
    if the underlying job materially changes (existing change-detection re-enqueues it as
    `pending`).
11. **The review digest is OFF by default in production (Phase 6.1).**
    `notifications.send_review_digest: false` is the shipped default because the digest
    tends to overwhelm the far more useful individual P1/P2/P3 alerts with a large batch
    of ambiguous roles. This is purely a notification setting: every job is still fully
    classified and stored with `eligibility_status='review'` in SQLite exactly as before
    -- nothing about eligibility classification, storage, or the underlying review DB
    logic/tests is disabled or removed. While off, a review job's alert is simply never
    enqueued in the first place, so it can never accumulate as pending notification
    spam; anything already pending from before the setting was flipped is explicitly
    transitioned to `AlertState.suppressed` (reason `suppressed_review_digest_disabled`)
    the same auditable way the company-diversity cap works. Set it back to `true` to
    restore the batched low-priority digest at any time.

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

Phases 0–6 complete. `scripts/collect.py` remains available as a network-only collector
harness (no alerts, no classification). GradScout can run either locally
(`python -m scripts.run`, against a local `data/gradscout.db`) or unattended on GitHub
Actions with durable state on the `state` branch (see above and
`docs/PHASE_5_HANDOFF.md`). **A production `config.yaml` must still be created and
committed before the first real scheduled/manual run** -- see `docs/PHASE_5_HANDOFF.md`
§"First real run". See `docs/PHASE_6_HANDOFF.md` for the personalized-job-intelligence
work (resume-aware matching, company diversity/suppression, expanded startup coverage,
review-digest cleanup, and the Phase 6.1 UX fix disabling the review digest by default).
