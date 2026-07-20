# GradScout — Phase 3 Handoff

Concise handoff for continuing work. **No commits have been made yet** (see §12).

## 1. Project goal and constraints

GradScout is a small, dependable utility that hourly monitors reliable structured
job sources for **2027 new-grad and eligible early-career technical roles**,
prioritizes major-tech / highly competitive employers, matches each role to the
best resume (Backend / AI / Data), and alerts to Discord. Deterministic-first;
the LLM is optional and bounded. It is explicitly **not** a SaaS product.

Constraints: Python 3.12, SQLite, httpx, Pydantic, pytest, GitHub Actions,
Discord webhook. No frontend, no FastAPI server, no Docker, no auth, no
auto-apply, no LinkedIn scraping, no browser automation, no unnecessary
abstractions. Secrets come from env vars only. Source failures must not crash the
run. One-developer maintainable.

## 2. Completed phases 0–3

- **Phase 0 — Scaffolding**: `pyproject.toml` (py3.12), `config.example.yaml`,
  `.env.example`, package skeleton, JSON logging, README. (venv recreated at
  Python 3.12.13.)
- **Phase 1 — Persistence core**: Pydantic models, `config.py`, SQLite schema
  (`jobs`, `job_sources`, `source_health`, `alerts`), cross-source dedupe, URL
  canonicalization, pending-alert lifecycle. DB tests.
- **Phase 2 — Collectors + normalization**: Greenhouse, Lever, Ashby, GitHub
  (Simplify `listings.json`) collectors; fail-soft wrapper; explicit per-phase
  timeouts + hard total-read budget + bounded retries; `--source`/`--no-github`
  live harness; offline fixture tests.
- **Phase 3 — Deterministic analysis + bounded LLM**: eligibility, role-family
  classification, resume recommendation, company/role/alert priority, and a
  bounded optional LLM agent with a resolver (hard rules win; disagreement
  recorded). Full rule/agent tests.

## 3. Folder / module architecture

```
grad-scout/
├── config.example.yaml        # operator-editable sources/watchlist/thresholds
├── .env.example               # documents secrets (env only)
├── pyproject.toml
├── data/                      # local SQLite (gitignored; CI uses state branch)
├── docs/PHASE_3_HANDOFF.md    # this file
├── gradscout/
│   ├── models.py              # Pydantic schema + analysis models
│   ├── config.py              # load/validate config.yaml
│   ├── db.py                  # SQLite: schema, dedupe, health, alerts
│   ├── urls.py                # canonicalize_url()
│   ├── htmltext.py            # HTML -> readable text
│   ├── textmatch.py           # phrase/context matching helpers
│   ├── normalize.py           # RawJob -> Job
│   ├── eligibility.py         # deterministic eligibility rules
│   ├── roles.py               # role-family classification
│   ├── resume.py              # resume recommendation
│   ├── prioritize.py          # company/role/alert priority
│   ├── analyze.py             # orchestrator + resolver + classify_jobs
│   ├── llm.py                 # bounded LLM provider + agent
│   ├── logging_setup.py       # JSON logging
│   └── collectors/            # base + greenhouse/lever/ashby/github_repo + factory
├── scripts/collect.py         # live collector harness (no alerts)
└── tests/                     # fixtures + offline tests (no network in pytest)
```

## 4. Important invariants (do not break)

- **Jobs are never discarded.** The worst classification is `ineligible`, which is
  still stored with its reason. Ambiguity → `review`, also stored.
- **Cross-source dedupe.** `jobs.url_canonical` is UNIQUE; merge key is *stable
  source identity OR canonical application URL*. Each surfacing source is kept as
  a `job_sources` row, so one posting from an ATS + a GitHub repo = one job with
  both source records. Original `apply_url` is preserved exactly per source;
  `url_canonical` is computed separately.
- **Pending→sent alert lifecycle.** `enqueue_alert()` creates a `pending` row
  (idempotent, never duplicates). An alert becomes `sent` ONLY via
  `mark_alert_sent()` after a successful delivery. So a per-run alert cap never
  loses alerts — unsent stay pending.
- **Hard deterministic rules override the LLM.** `resolve()` forces `ineligible`
  when `det.hard_ineligible` regardless of agent output, recording
  `deterministic_disagreement` + reason. The LLM cannot flip a hard rule.
- **LLM only receives relevant, ambiguous, new jobs.** `JobAnalysisAgent.should_analyze()`
  = enabled provider ∧ `status==review` ∧ `relevant` ∧ not `hard_ineligible`.
  Clearly eligible/ineligible jobs and the whole Simplify feed are never sent.
- **Collector failures are fail-soft and time-bounded.** `run_collector()` never
  raises; a failed source becomes an `error` result and the run continues.
  Fetches use explicit timeouts (connect 5 / read 15 / write 10 / pool 5), a hard
  `DEFAULT_MAX_TOTAL=45s` streaming budget, and bounded retries (`DEFAULT_RETRIES=1`).
  Empty-success is distinct from failure; partial-parse is its own state.

## 5. Commands (setup / tests / lint / live checks)

```bash
# setup
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml   # edit sources/watchlist
cp .env.example .env                  # DISCORD_WEBHOOK_URL for real sends (Phase 4)

# tests + lint
pytest
ruff check .

# live collector checks (network; no alerts, no DB writes)
python -m scripts.collect --config config.example.yaml --dry-run --no-github
python -m scripts.collect --config config.example.yaml --dry-run --source ashby:openai --limit 1
python -m scripts.collect --config config.example.yaml --dry-run --source github:simplify --limit 1
```

## 6. Exact current test/lint results

- `pytest` → **85 passed** (no network in tests).
- `ruff check .` → **All checks passed!**
- No linter diagnostics in `gradscout/` or `tests/`.

## 7. Config behavior and example sources

`config.yaml` is fully operator-editable without code changes. `company_priority`
is data (1 = highest). Coverage status is derived at runtime: `direct` (native ATS
configured), `indirect` (GitHub repo only), `not_configured`, `failed`.

Example sources verified live (elapsed): `greenhouse:stripe` (~0.85s, 520 jobs),
`ashby:openai` (~0.6s, 723), `lever:leverdemo` (variable, large/slow demo board),
`github_repo:SimplifyJobs-NewGrad` (~0.7s, ~17k rows). The example leaves
`Anthropic` on the watchlist with no direct source as an honest `not_configured`
illustration. Notification defaults: `discord_min_priority: p2`,
`send_healthy_reports: false`, `daily_summary_hour_utc: 13`,
`max_alerts_per_run: 25`, `new_grad_recent_hours: 48`, `max_years_experience: 5`.

## 8. Deferred features

Discord delivery, health/digest notifications, GitHub Actions workflow, orphan
`state` branch persistence, recency wiring, new/changed detection, and the full
pipeline (all Phase 4+). Also deferred: non-Simplify GitHub parsers, Workday/iCIMS
and other ATS, direct scraping of boards without a public API, fuzzy
company+title merge, richer LLM enrichment, analytics/dashboards.

## 9. Phase 4 implementation goals

1. **Full pipeline orchestration**: collect → normalize → upsert/dedupe →
   classify → enqueue alerts → notify → health report, wired in one entrypoint.
2. **New / materially-changed detection**: compare incoming `raw_blob` (or key
   fields) against stored job to gate reprocessing (feeds `is_new_or_changed`);
   never reclassify the whole 17k feed each run.
3. **Classification persistence**: apply `ResolvedAnalysis` to `Job` and upsert;
   consider storing disagreement info for review.
4. **Alert queue generation**: enqueue pending alerts for jobs meeting
   `discord_min_priority`, ordered by company priority; respect the cap while
   leaving unsent jobs pending.
5. **Discord delivery**: `notify/discord.py` webhook sender (dry-run aware) that
   only calls `mark_alert_sent()` after Discord accepts; every alert includes the
   original application URL.
6. **Needs-review digest**: separate low-priority section for `review` jobs
   (config `send_review_digest`).
7. **High-priority source failure alerts**: immediate, loud, naming the exact
   company + source when a `company_priority==1` source fails.
8. **Daily health summary**: one success summary at `daily_summary_hour_utc`; do
   NOT send hourly healthy reports.
9. **Recency wiring**: compute `is_recent` from `first_seen_at` /
   `source_posted_at` and `new_grad_recent_hours` to drive P1 urgency; keep the
   posted-vs-discovered distinction (never fabricate posting dates).

## 10. Implementation warnings / tradeoffs discovered

- **httpx read timeout ≠ total time.** A slow-but-steady stream (`lever:leverdemo`
  took ~31s) never trips a per-read timeout; the hard `DEFAULT_MAX_TOTAL`
  streaming budget is what actually bounds a source. Keep it.
- **Normalization removes apostrophes** (`Master's`→`masters`) so degree tokens
  match; other punctuation becomes whitespace. Changing this will break degree
  detection.
- **Seniority is title-only** with entry-level suppression to avoid false
  positives from body text ("lead a team", "leadership"). Don't scan the body.
- **Experience gating is intentionally permissive** (uses the smallest mandatory
  minimum) to avoid wrongly discarding; jobs are stored regardless.
- **Full-time is assumed** when a source doesn't state employment type (ATS boards
  are overwhelmingly FT). Internship detection uses structured hints first.
- **Simplify feed is ~17k global rows.** Filtering/relevance is deterministic and
  must run before any LLM work; never call the LLM across the feed.
- **State persistence must use the orphan `state` branch, not `main`** (no remote
  configured yet; the branch is not created locally).
- **The DB commits per upsert** (fine at this scale); revisit if volume grows.

## 11. Major files and responsibilities

| File | Responsibility |
|---|---|
| `gradscout/models.py` | Enums, `RawJob`, `Job`, `JobRecord`, config models, `AgentAnalysis`, `ResolvedAnalysis` |
| `gradscout/config.py` | Load + validate `config.yaml` |
| `gradscout/db.py` | SQLite schema, cross-source upsert/dedupe, source health, pending→sent alerts |
| `gradscout/urls.py` | Canonical URL for dedupe |
| `gradscout/htmltext.py` | HTML → readable plain text |
| `gradscout/textmatch.py` | Phrase/context matching + `normalize()` |
| `gradscout/normalize.py` | `RawJob` → `Job` (exact apply_url, computed canonical, no fabricated dates) |
| `gradscout/eligibility.py` | Deterministic eligibility + evidence + hard-rule flags |
| `gradscout/roles.py` | Role-family classification (+ evidence, margin) |
| `gradscout/resume.py` | Resume variant + confidence + reason |
| `gradscout/prioritize.py` | Watchlist/alias company priority; role + alert priority |
| `gradscout/analyze.py` | Deterministic orchestrator, LLM resolver, `classify_jobs` + stats/logging |
| `gradscout/llm.py` | Provider interface, Null/OpenAI, bounded agent + prefilter |
| `gradscout/logging_setup.py` | JSON structured logging |
| `gradscout/collectors/base.py` | Collector base, timeouts/retries/budget, `run_collector`, `CollectorResult` |
| `gradscout/collectors/{greenhouse,lever,ashby,github_repo}.py` | Per-source parsers |
| `gradscout/collectors/factory.py` | `build_collectors(config)` |
| `scripts/collect.py` | Live collector harness (no alerts) |
| `tests/` | Offline fixtures + unit tests (no network) |

## 12. Commit status

**No commits have been made.** The repository has no commits on `main`; all Phase
0–3 work is uncommitted in the working tree. No `state` branch has been created.
