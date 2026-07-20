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

## State persistence (GitHub Actions)

State is kept on a **dedicated orphan `state` branch** (not on `main`, and the binary DB is
never committed to `main`). Each hourly run:

1. checks out `main` for code, then restores `data/gradscout.db` from the `state` branch;
2. runs the pipeline;
3. commits the updated DB back to the `state` branch only.

The orphan `state` branch shares no history with `main`, so DB churn never pollutes code
history. `workflow_dispatch` supports a manual `dry_run` input.

## Status

Phases 0–4 complete and locally testable (`python -m scripts.run`). `scripts/collect.py`
remains available as a network-only collector harness (no alerts, no classification).
GitHub Actions scheduling and orphan `state`-branch persistence are Phase 5 and not
yet implemented; runs today are local-only, against a local `data/gradscout.db`.
See `docs/PHASE_4_HANDOFF.md` for a detailed handoff.
