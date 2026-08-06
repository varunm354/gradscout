# GradScout — Phase 6 Handoff: Personalized Job Intelligence

Consolidated handoff for Phase 6 (resume-aware matching, company/resume-category
diversity with explicit suppression, expanded startup ATS coverage, and review-digest
cleanup), including the Phase 6.1 follow-up UX fix (§13) that disables the review
digest by default in production. Builds on Phases 0–5 (`docs/PHASE_5_HANDOFF.md`,
`docs/PHASE_5_FINAL_HANDOFF.md`) without changing any of their invariants: durable
SQLite state, compressed state persistence, the GitHub Actions workflow, and
notification reliability guarantees are all unchanged and re-verified.

## 1. Current branch and repository state

- Branch: **`feature/personalized-job-intelligence`**.
- Nothing in this phase has been committed. `git status --short`:

  ```
   M config.example.yaml
   M config.yaml
   M gradscout/analyze.py
   M gradscout/collectors/factory.py
   M gradscout/db.py
   M gradscout/models.py
   M gradscout/notify/discord.py
   M gradscout/pipeline.py
   M gradscout/resume.py
   M gradscout/roles.py
   M tests/test_agent.py
   M tests/test_collectors.py
   M tests/test_db.py
   M tests/test_notify_discord.py
   M tests/test_pipeline.py
   M tests/test_roles_resume.py
   M tests/test_title_gate.py
  ?? gradscout/collectors/rippling.py
  ?? gradscout/diversify.py
  ?? tests/fixtures/rippling_missing_fields.json
  ?? tests/fixtures/rippling_ok.json
  ?? tests/test_diversify.py
  ?? tests/test_resume_matching.py
  ```

- `README.md` and this file are also new/updated but not yet committed (see §12).

## 2. What Phase 6 delivers

1. **Resume-aware matching.** Every eligible/review job is scored against all three
   resume profiles (AI, Backend, Data); exactly one is recommended with a match score
   and a skills-based explanation, shown in every Discord alert and review row.
2. **Company diversity.** `max_alerts_per_company_per_run` / `max_review_items_per_company_per_run`
   stop a single prolific poster (e.g. OpenAI) from dominating a run's alerts or the
   review digest, with round-robin preference across AI/Backend/Data categories.
3. **Explicit, non-spamming suppression.** Jobs that lose out to a company cap are
   marked `AlertState.suppressed` with reason `suppressed_company_cap` -- never resent
   every run, and only reconsidered if the job materially changes.
4. **Expanded startup discovery.** 20 additional established startups / growth-stage
   infra & dev-tool companies added to the watchlist, with live-verified direct ATS
   sources (Greenhouse/Lever/Ashby) wherever a real public board exists, plus a new
   `RipplingCollector` for companies on Rippling's own ATS.
5. **Review-digest cleanup.** Nontechnical titles (legal, procurement, HR, customer
   success, business affairs, workplace ops, finance/accounting, recruiting) are now
   classified `ineligible` instead of falling into the ambiguous `review` bucket.
6. **Review digest OFF by default (Phase 6.1 follow-up).** `send_review_digest` now
   defaults to `false` in the shipped production/example configs, since the digest
   tends to overwhelm the far more useful individual alerts. Purely a notification
   setting -- review classification/storage is completely unaffected. See §13.

## 3. Resume-aware matching — design

`gradscout/resume.py` is a full rewrite behind a `ResumeMatcher` `Protocol`:

- **`WeightedKeywordResumeMatcher`** (the only implementation today) scores a job's
  title+description against each resume's `list[WeightedTermSpec]` using the same
  word-boundary matcher as role classification (`gradscout.textmatch.find_first`), with
  a `+1` boost per term also mentioned in the title. This is a deterministic, fully
  offline, zero-cost design chosen explicitly so it can be swapped for an
  embeddings-based matcher later (e.g. cosine similarity over resume/job embeddings)
  **without changing `analyze.py`, `pipeline.py`, or the DB schema** -- both callers
  only ever see `recommend_resume(job, roles, matcher) -> ResumeRecommendation`.
- **Score normalization** uses a saturating formula, `pct = round(100 * raw / (raw + K))`
  with `K = 6.0`: bounded in `[0, 100)` for any raw score, diminishing returns instead of
  a fragile hand-picked ceiling (`raw == K` → 50%, `raw == 3K` → 75%, `raw == 9K` → 90%).
- **Confidence** (`high`/`medium`/`low`) considers both the winning raw score and its
  margin over the runner-up, so a job that faintly matches two profiles equally doesn't
  get reported as a confident pick.
- **Explanation** (`_skills_reason`) always names the actual top-`_TOP_TERMS_SHOWN` (5)
  matched terms by weight, e.g. `"Matched FastAPI, PostgreSQL, distributed systems (backend
  resume)"` -- never just a bare percentage. A small `_DISPLAY_OVERRIDES` table renders
  known acronyms/product names correctly (`llm` → `LLM`, `fastapi` → `FastAPI`, etc.)
  instead of naive title-casing.
- **Tie-breaking and no-signal fallback**: if every profile scores 0, the job's
  deterministic role-family hint (from `gradscout.roles`) picks the variant, so a thin
  description never produces an arbitrary choice among equal zeros. Genuine ties among
  nonzero scores prefer the role-family hint's resume, else a fixed `ai > data > backend`
  order.
- **Config-driven, PII-free profiles**: `config.yaml`'s new `resume_profiles` section
  holds `ai` / `backend` / `data` profiles, each a list of `{term, weight, display}`
  entries. These were derived from three real resumes (AI, Backend, Data variants) by
  extracting only technical skills, tools, technologies, and project/domain areas --
  **no names, contact info, education, or dates** were encoded anywhere in the repo.
- **Backward compatibility**: if a config has no `resume_profiles` at all (e.g.
  `config.example.yaml`'s minimal case, or any pre-Phase-6 config), `recommend_resume`
  detects that no profile has any terms and falls back byte-for-byte to the pre-Phase-6
  role-family-only recommendation logic (`_fallback_recommend`) -- zero behavior change
  for anyone who hasn't opted in.

**Persistence and threading**: `resume_match_score: int | None` was added to `Job`,
`JobRecord`, `ResolvedAnalysis`, and a new `resume_match_score INTEGER` column on `jobs`
(migrated in-place for legacy DBs via `_ensure_resume_score_column`). The matcher is
built **once per run** (`build_matcher_from_config(config)` in `pipeline.run_once`), not
once per job, and threaded through `classify_job(s)` → `analyze_deterministic` →
`recommend_resume`.

**Discord surface**: `_job_embed`'s "Recommended resume" field now reads e.g.
`"backend (78% match, high confidence)"` (score omitted gracefully if absent, e.g. under
the no-profile fallback), and the embed description carries the full skills explanation.
The review-digest field appends a compact suffix, e.g. `"· ai 78%"`.

## 4. Company/resume-category diversity — design

`gradscout/diversify.py` is a new, pure, side-effect-free module:

- `diversify_by_company(rows, *, per_company_cap, resume_key="recommended_resume") ->
  (selected, suppressed)` groups candidate rows by `company`, then for each company's
  group: ranks internally by urgency (`p1 > p2 > p3 > review`, then `created_at`), buckets
  into `ai` / `backend` / `data` / `other` category queues, and round-robins across those
  queues -- taking at most one per category per round -- until the cap is filled or
  candidates run out. This guarantees "one AI, one Backend, one Data" is preferred over
  e.g. three AI-recommended jobs from the same company, before ever repeating a category.
- It operates directly on the `sqlite3.Row`-like dict rows from
  `gradscout.db.get_pending_alerts` (extended to also `SELECT j.recommended_resume`) and
  never touches the DB -- the caller (`gradscout.pipeline`) decides what to do with each
  half of the split.
- **Wiring**: `pipeline._send_job_alerts` and `pipeline._send_review_digest` both call
  `diversify_by_company` with the new `max_alerts_per_company_per_run` /
  `max_review_items_per_company_per_run` config values (default `3` each) *before* the
  existing global `max_alerts_per_run` slice and Discord chunking logic -- diversity caps
  apply first, per company, then the existing global caps apply on top exactly as before.

**Suppression, not silent loss or endless pending**: rows in the `suppressed` half are
passed to the new `db.suppress_alert(conn, job_id, channel, reason, now)`, which
transitions a `pending` alert to a new `AlertState.suppressed` state with an explicit
`suppressed_reason` (`suppressed_company_cap`) and `suppressed_at` timestamp (both new
columns on `alerts`, migrated via `_ensure_alert_suppression_columns`). This directly
satisfies the explicit design constraint from this phase's kickoff:

> Company-cap suppressed jobs should NOT remain pending forever. Instead, suppress them
> with an explicit suppression reason... so they do not resend repeatedly. Only
> materially changed jobs should become eligible again through the existing
> change-detection pipeline.

Concretely:
- A `suppressed` alert is **never** re-selected by `get_pending_alerts` (which only
  returns `pending` rows), so it cannot resend or re-inflate the backlog run after run.
- `enqueue_alert` is the sole way a suppressed alert becomes reconsiderable: it now
  explicitly resets a `suppressed` row back to `pending` (clearing `suppressed_reason`/
  `suppressed_at`) when called again for that `(job_id, channel)` -- and it is only
  called again by the existing change-detection path when a job's content materially
  changes and needs to be re-enqueued. An unchanged job is never re-enqueued, so its
  suppressed alert simply stays suppressed, with no spam and no repeated DB churn.
- This is fully covered by dedicated pipeline-level tests (§7) simulating multi-run
  sequences: unchanged reruns keep a suppressed alert suppressed; a materially changed
  job's alert becomes pending again and is eligible for delivery on the next run.

## 5. Expanded startup discovery

All new watchlist entries were **live-verified** against real public ATS APIs
immediately before being written into `config.yaml` -- no slug was ever guessed. Two
batches were added, both marked with comments in `config.yaml` explaining verification:

**Established startups (10 – several thousand employees):** Tekion (Greenhouse),
Glean (Greenhouse, board `gleanwork`), Zoox (Lever), Rippling (new Rippling ATS
collector), Nuro (Greenhouse), Together AI (Greenhouse), Crusoe (Ashby), Decagon (Ashby),
Harvey (Ashby), Anysphere/Cursor (Ashby, board `cursor`), Runway (Ashby). Applied
Intuition is watchlist-only: its Greenhouse-hosted careers page returns 200 in the
browser but the API path 404s, so rather than guess or scrape, it's left on indirect
(Simplify) coverage only, with a comment explaining why.

**Bay Area growth-stage infra/dev-tool companies (from this phase's explicit follow-up
request):** Confluent (Greenhouse), Cockroach Labs (Greenhouse, board `cockroachlabs`),
Pinecone (Ashby), Baseten (Ashby), Modal (Ashby), Vercel (Greenhouse), Render (Ashby).
Weights & Biases and Fly.io were checked against every plausible slug on
Greenhouse/Lever/Ashby and had no resolvable public board -- both are left
watchlist-only with an explicit comment, applying the same "never guess a token"
standard used for Applied Intuition.

**New `RipplingCollector`** (`gradscout/collectors/rippling.py`): Rippling runs its own
ATS platform (`ats.rippling.com`), separate from Greenhouse/Lever/Ashby, with a
structured JSON API at `https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs`
(the `ats.rippling.com` UI host itself is Cloudflare-protected and unsuitable for
scraping; the API host is not). It returns a flat JSON array (not a `{"jobs": [...]}`
wrapper like Ashby), so the collector's `parse()` validates the top-level type
explicitly and treats a non-list response as a hard error rather than silently
returning zero jobs. `description_text` and `source_posted_at` are left `None` since
the API doesn't reliably expose them -- consistent with how other collectors handle
partial upstream data (job still ingested, just with fewer enrichable fields). Added
`SourceType.rippling`, a `RipplingSource` config model, a new top-level `rippling:` list
in `config.yaml`, and factory wiring in `collectors/factory.py`.

**Tekion note**: Tekion resolved on both Greenhouse and Ashby during verification; only
the Greenhouse source was kept to avoid any risk of duplicate ingestion (existing
canonical-URL dedupe would likely have merged them anyway, but avoiding the second
source entirely is simpler and removes the question).

## 6. Review-digest cleanup

`gradscout/roles.py`'s `NON_TARGET_TITLE_TOKENS` was expanded with terms covering
Legal, Procurement, HR, Customer Success, Business Affairs, Account Executive,
Marketing, Workplace Operations, Finance, Accounting, and Recruiting roles. Because
eligibility classification is title-first (Phase 5.1), a title matching one of these
tokens is now classified `ineligible` outright instead of falling into the ambiguous
`review` bucket purely because it lacked an explicit target-role signal. Combined with
the company diversity cap on the review digest itself, the digest now contains
meaningfully fewer, more genuinely ambiguous technical titles, spread across more
companies rather than dominated by one poster's high-volume nontechnical postings.

`tests/test_title_gate.py` and `tests/test_agent.py` were updated in lockstep: a
previously-"ambiguous-review" fixture (`"Recruiter"`) is now asserted `ineligible`
(new test `test_hard_nontechnical_title_added_in_phase_6_is_ineligible_not_review`),
and the LLM-boundary test that relied on "Recruiter" reaching `review` was switched to
a genuinely ambiguous title (`"Program Coordinator"`) to preserve its original intent.

## 7. Files changed and responsibilities

| File | Responsibility |
|---|---|
| `gradscout/models.py` | `SourceType.rippling`, `AlertState.suppressed`, `RipplingSource`, `WeightedTerm`, `ResumeProfileConfig`, `resume_profiles` on `Config`, diversity-cap fields on `NotificationConfig`, `resume_match_score` on `Job`/`JobRecord`/`ResolvedAnalysis`. |
| `gradscout/resume.py` | Full rewrite: `ResumeMatcher` protocol, `WeightedKeywordResumeMatcher`, score normalization, skills-based explanation, deterministic tie-breaking, pre-Phase-6 fallback. |
| `gradscout/diversify.py` | **New.** `diversify_by_company`: per-company cap + round-robin resume-category selection, pure/side-effect-free. |
| `gradscout/db.py` | `resume_match_score` column + migration; `suppressed_reason`/`suppressed_at` columns + migration; `suppress_alert()`; `enqueue_alert` suppressed→pending reconsideration; `get_pending_alerts` now selects `recommended_resume`. |
| `gradscout/analyze.py` | Threads an optional `ResumeMatcher` through `analyze_deterministic`/`classify_job(s)`; persists `resume_match_score`. |
| `gradscout/pipeline.py` | Builds the matcher once per run; wires `diversify_by_company` + `db.suppress_alert` into `_send_job_alerts` and `_send_review_digest`; new `RunStats.alerts_suppressed_company_cap`. Phase 6.1: `_send_review_digest` explicitly suppresses (reason `suppressed_review_digest_disabled`) any pending review alert when `send_review_digest` is off, instead of silently skipping; new `RunStats.alerts_suppressed_review_digest_disabled` (see §13). |
| `gradscout/notify/discord.py` | `_job_embed` shows match score + skills explanation; `_digest_field` appends a compact score suffix. |
| `gradscout/roles.py` | Expanded `NON_TARGET_TITLE_TOKENS` for review-digest cleanup. |
| `gradscout/collectors/rippling.py` | **New.** `RipplingCollector` for Rippling's ATS API. |
| `gradscout/collectors/factory.py` | Wires `RipplingSource` configs into `RipplingCollector` instances. |
| `config.yaml`, `config.example.yaml` | `resume_profiles` (sanitized, technical-only terms); expanded `watchlist`/`greenhouse`/`lever`/`ashby`/`rippling` sections for new startups; new `notifications` diversity-cap keys; Phase 6.1: `send_review_digest` flipped to `false` with an explanatory comment (see §13). |
| `tests/test_resume_matching.py` | **New.** Unit tests for `WeightedKeywordResumeMatcher`: scoring, explanations, normalization, title-mention boost, fallback. |
| `tests/test_diversify.py` | **New.** Unit tests for `diversify_by_company`: under/over cap, round-robin category preference, multi-round fill, urgency ranking, multi-company isolation, unrecognized-category handling, empty input, review-priority reuse. |
| `tests/test_collectors.py` | New `RipplingCollector` parse/error/factory tests + fixtures. |
| `tests/test_db.py` | New suppression tests (`suppress_alert` transitions/no-ops, `enqueue_alert` reconsideration) + legacy-schema migration tests for both new column groups. |
| `tests/test_pipeline.py` | New integration tests for company-cap suppression across runs (unchanged rerun stays suppressed; materially-changed job becomes reconsiderable) and review-digest diversity; existing chunking tests' `_config()` overridden to a high per-company cap where they intentionally exercise a single company at volume. Phase 6.1: new tests for the digest-disabled path (never enqueued, clean skip, pre-existing pending alert cleanup) and an explicit digest-enabled test (see §13). |
| `tests/test_notify_discord.py` | New tests asserting the match-score/skills-explanation embed format and the digest compact-suffix format, including graceful omission when no score is present. |
| `tests/test_agent.py`, `tests/test_title_gate.py`, `tests/test_roles_resume.py` | Updated for the `NON_TARGET_TITLE_TOKENS` expansion and the new `recommend_resume(job, roles, matcher)` signature. |

## 8. Verification results

- `pytest` → **412 passed**, fully offline (no real network/Discord/OpenAI calls
  anywhere in the suite; the new Rippling/resume/diversify tests all use fixtures or
  plain in-memory data).
- `ruff check .` → **All checks passed!**
- No linter diagnostics in any changed file.
- A local dry run (`python -m scripts.run --dry-run`) against a disposable DB, using
  the production `config.yaml`, was executed to confirm end-to-end wiring (collection →
  eligibility → resume scoring → diversification → Discord dry-run boundary) works with
  zero exceptions and zero real HTTP sends. See §9 for statistics.

## 9. Local production dry-run statistics

Run with `python -m scripts.run --config config.yaml --db /tmp/gradscout_phase6_dryrun.db
--dry-run` against the real, live-verified `config.yaml` sources (14 Greenhouse boards,
2 Lever boards, 15 Ashby boards, 1 Rippling board, plus the Simplify GitHub feed), with
`DISCORD_WEBHOOK_URL` unset (the dry-run boundary makes zero Discord requests
regardless). Used a disposable `/tmp` database path so the checked-in `data/` state was
never touched; the disposable DB was deleted afterward.

**Collection** -- all 38 configured sources returned `status: ok` (0 partial, 0 error):

- `jobs_seen: 10,688` raw postings across every source (Greenhouse 3,772 combined /
  Lever 528 / Ashby 2,117 / Rippling 767 / Simplify 2,440 + Google/Meta/Amazon/etc via
  Simplify).
- `jobs_created: 10,316`, `jobs_changed: 369`, `jobs_unchanged: 3` (a fresh DB, so
  almost everything is "created"; unchanged jobs are correctly skipped for
  reclassification per the existing incremental-sync guarantee).
- This was this DB's first-ever run, so the pipeline correctly ran in **baseline
  mode** (`baseline_run: true`): every job is still classified and scored normally, but
  ordinary alerting is narrowed to only genuinely-recent P1 jobs (pre-existing Phase 5.1
  behavior, unchanged by Phase 6) -- so `alerts_enqueued: 0` here reflects baseline
  bootstrap suppression, not a Phase 6 regression. Classification, resume scoring, and
  storage all ran at full volume regardless.

**Eligibility classification** (10,316 stored jobs):

| Status | Count |
|---|---|
| `review` | 5,968 |
| `ineligible` | 3,265 |
| `eligible` | 1,083 |

**Alert-priority split of the 1,083 eligible jobs**: `p3` 843, `p2` 240 (no `p1` in this
snapshot -- expected, since `p1` requires a genuinely-recent top-priority-company
posting).

**Resume-aware matching, applied to all 7,051 eligible+review jobs**:

| Recommended resume | Count |
|---|---|
| `backend` | 5,020 |
| `ai` | 1,116 |
| `data` | 915 |

Average match score `12.4%` (median-weighted by design toward realistic partial
matches; saturating formula bounds every score in `[0, 100)`), min `0%`, max `80%`.
Highest-confidence real examples observed (company, title → recommendation):

- Perplexity AI, "Internship - Search Machine Learning Engineer" → `ai` (71%, high)
- Elastic, "Agentic AI Engineer" → `ai` (67%, high)
- OpenAI, "Software Engineer, Database Systems" → `ai` (67%, high)
- Anthropic, "Research Engineer, Machine Learning (Reinforcement Learning)" → `ai` (62%, high)

**Startup coverage in action**: Rippling's new `RipplingCollector` returned `767`
`status: ok` raw postings from `https://api.rippling.com/platform/api/ats/v1/board/
rippling/jobs` (`400` distinct jobs persisted after cross-source dedupe) in `720ms`,
with `0` parse errors -- confirming the new ATS integration works end-to-end against
the live API, not just fixtures.

**Company diversity, demonstrated against this run's real classified data** (via a
standalone script feeding the 1,083 real eligible jobs into
`diversify_by_company(rows, per_company_cap=3)`, i.e. exactly what `_send_job_alerts`
would have done had this not been a baseline run):

- Selected: `564` / Suppressed: `519` (all would be marked `suppressed_company_cap`,
  never resent unless the underlying job materially changes).
- Before any cap, `OpenAI` alone had `109` eligible jobs this run (`72` backend / `23`
  ai / `14` data) and `Palantir` had `107` -- exactly the single-company-domination
  problem this phase's cap exists to prevent. After the cap, both are limited to their
  configured `3` per run, like every other company.
- Concretely for OpenAI: the 3 selected are `{backend: 1, ai: 1, data: 1}` -- one of
  each resume category, not a naive top-3-by-priority slice (which, given backend
  outnumbers ai/data roughly 3:1 for OpenAI, would have had a real chance of selecting
  all-backend and giving zero AI/Data visibility into OpenAI's postings that run).

**Review-digest cleanup, contextually**: of the `5,968` `review`-status jobs, the
largest single-company contributors before digest-level diversification were
Databricks (462), OpenAI (461), Stripe (323), Crusoe (273) -- exactly the kind of
single-poster volume `max_review_items_per_company_per_run` (default `3`) now caps in
the digest itself, on top of the title-gate cleanup already having moved obviously
nontechnical titles straight to `ineligible` (kept out of this bucket entirely).

**Correctness invariants held throughout**: `notification_delivery_failures: 0`,
`alerts_suppressed_company_cap: 0` in the raw pipeline log (expected -- suppression is
only computed inside `_send_job_alerts`/`_send_review_digest`, which only run against
*actually enqueued* pending alerts, and baseline mode enqueued none this run; the §9
diversity numbers above are a direct, faithful application of the same
`diversify_by_company` function against this run's real eligible-job output, not a
synthetic example).

## 10. Backward compatibility notes

- A config with no `resume_profiles` section (e.g. an older `config.yaml`, or a bare
  minimal config used only in a few pipeline tests) falls back byte-for-byte to
  pre-Phase-6 role-family-only resume recommendation -- verified by dedicated fallback
  tests in `tests/test_resume_matching.py` and `tests/test_roles_resume.py`.
- A config with no diversity-cap keys set uses the Pydantic model defaults; existing
  tests that pre-date this phase and construct a `NotificationConfig`/`Config` without
  the new fields continue to pass unmodified.
- Legacy SQLite databases (pre-Phase-6 schema, missing `resume_match_score` on `jobs`
  and `suppressed_reason`/`suppressed_at` on `alerts`) are migrated in place on
  `init_db()` via `_ensure_resume_score_column` / `_ensure_alert_suppression_columns`,
  exactly mirroring the established Phase 5 migration pattern (`ALTER TABLE ... ADD
  COLUMN` guarded by a `PRAGMA table_info` check) -- verified by dedicated legacy-schema
  migration tests in `tests/test_db.py`.
- No change to the GitHub Actions workflow, state compression/restore scripts, or the
  `state` branch mechanism from Phase 5 -- Phase 6 is purely additive to the pipeline's
  Python code and `config.yaml`'s schema.

## 11. Example: company diversity in action

Given 6 pending OpenAI-sourced alerts recommending `ai, ai, backend, backend, data, data`
(in priority order) and `max_alerts_per_company_per_run: 3`, `diversify_by_company`
selects the first `ai`, first `backend`, and first `data` job (one full round-robin
round) and suppresses the remaining 3 with `suppressed_company_cap` -- instead of the
naive "first 3 by priority" outcome, which could have selected `ai, ai, backend` and
starved the `data` category entirely. See `tests/test_diversify.py::
test_prefers_one_of_each_resume_category_before_repeating` for the exact assertion.

## 13. Phase 6.1 — review digest disabled by default (UX fix)

A follow-up, uncommitted change on top of the rest of Phase 6: the review digest
overwhelmed the far more useful individual P1/P2/P3 alerts in practice, so it is now
**disabled by default in production**.

**What changed, precisely:**

- `config.yaml` and `config.example.yaml`: `notifications.send_review_digest` flipped
  from `true` to `false`, with an inline comment explaining why and how to re-enable it.
  The underlying Pydantic field (`NotificationConfig.send_review_digest`, default
  `True`) was deliberately **left unchanged** -- it's a large surface of pre-existing
  Phase 6 pipeline tests that construct a bare `NotificationConfig()`/`_config()` and
  assert on digest-enabled behavior without passing this field explicitly; changing the
  Python-level default would have silently flipped dozens of unrelated tests. The real
  production/example configs are what actually govern deployed behavior, and both are
  now explicit and correct.
- `gradscout/pipeline.py`:
  - New constant `SUPPRESSED_REVIEW_DIGEST_DISABLED = "suppressed_review_digest_disabled"`,
    alongside the existing `SUPPRESSED_COMPANY_CAP`.
  - `_enqueue_if_warranted` already had an `if not config.notifications.send_review_digest:
    return False` guard for review-status jobs from the original Phase 6 diversity work --
    unchanged, and it's the reason this can never accumulate as *new* spam: a review job
    is never given a pending alert row in the first place while the digest is off. Its
    `eligibility_status='review'` classification is stored on the `jobs` row regardless
    -- durable, queryable, and the real audit trail (requirement 1/2: nothing about
    review classification or storage was touched or removed).
  - `_send_review_digest` now branches on `send_review_digest` *before* diversifying:
    when disabled, it fetches whatever review-priority alerts are currently `pending`
    (normally none, per the point above -- nonzero only as a one-time cleanup of alerts
    that were enqueued before this setting was flipped, e.g. an existing production DB
    upgrading into this change) and explicitly transitions every one of them to
    `AlertState.suppressed` with reason `suppressed_review_digest_disabled` via the
    existing `db.suppress_alert` -- the exact same auditable mechanism the
    company-diversity cap already uses (requirement 5). No Discord request is made
    either way (requirement 4: clean skip). Returns a 5-tuple now (added
    `digest_disabled_suppressed_count`); `run_once` threads it into a new
    `RunStats.alerts_suppressed_review_digest_disabled` field and the
    `"pipeline run complete"` log line, so the count is visible in ordinary run
    logs/metrics, not just the DB.
  - Individual P1/P2/P3 alerts (`_send_job_alerts`) were **not touched at all** --
    requirement 6.
- `README.md`: new "Key design decisions" item 11 explaining the default and its
  reasoning; "Status" section's `docs/PHASE_6_HANDOFF.md` pointer updated to mention it.
- `tests/test_pipeline.py`: new tests (see below) using `_config(send_review_digest=False)`
  for the disabled path, plus one explicit `_config(send_review_digest=True)` test so
  "enabled" behavior has coverage that doesn't quietly rely on the Pydantic default.

**Reconsideration semantics** (consistent with the existing company-cap design, by
deliberate choice): re-enabling `send_review_digest: true` later does **not**
retroactively resurrect alerts that were suppressed for reason
`suppressed_review_digest_disabled` unless the underlying job materially changes
(`db.enqueue_alert`'s existing suppressed→pending reset only fires on
created/changed jobs -- see `gradscout.pipeline._enqueue_if_warranted`'s upstream
created/changed gate). This mirrors the explicit constraint already established for
company-cap suppression in the rest of Phase 6 ("only materially changed jobs become
eligible again through the existing change-detection pipeline") rather than
introducing a second, different reconsideration rule.

**New tests added to `tests/test_pipeline.py`** (all offline, no network/Discord):

- `test_review_digest_disabled_by_default_config_value` -- a bare `_config()` (no
  override) still has `send_review_digest is True` at the Pydantic-default level (this
  documents the deliberate model-vs-config split above); the actual shipped
  `config.yaml`/`config.example.yaml` values are asserted `False` directly against the
  YAML files.
- `test_review_status_job_is_never_enqueued_while_digest_disabled` -- a review-status
  job, when `send_review_digest=False`, is classified `review` and stored, but gets
  **no** `alerts` row at all (`db.get_alert(...) is None`) -- confirming requirement 5's
  "never accumulates" guarantee at the source, not just the digest-send step.
- `test_review_digest_disabled_cleanly_skips_sending_with_no_http_calls` -- with
  `send_review_digest=False` and several review-status jobs, `run_once` makes zero
  calls to the injected Discord transport, `review_digest_sent is False`,
  `review_digest_chunks_sent == 0`.
- `test_preexisting_pending_review_alert_is_suppressed_when_digest_later_disabled` --
  enqueue a review alert while the digest is enabled (simulating an existing production
  DB), then rerun with it disabled: the alert transitions from `pending` to
  `suppressed` with `suppressed_reason == "suppressed_review_digest_disabled"`,
  `stats.alerts_suppressed_review_digest_disabled == 1`, and `stats.alerts_pending == 0`
  -- no backlog left dangling.
- `test_review_digest_still_works_when_explicitly_enabled` -- `_config(send_review_digest=True)`
  reproduces the pre-6.1 enabled behavior end-to-end (digest sent, alert marked sent),
  giving the "enabled" path direct coverage independent of the Pydantic default.
- `test_individual_alerts_unaffected_by_review_digest_being_disabled` -- a mixed run
  (one eligible P2 job + several review jobs) with the digest disabled still sends the
  individual alert normally (`alerts_sent == 1`), confirming requirement 6.

## 14. Exact next steps

1. Review this handoff and the diff on `feature/personalized-job-intelligence`.
2. Commit and push (not done by this phase, per explicit instruction):
   ```bash
   git add -A
   git commit -m "..."
   git push -u origin feature/personalized-job-intelligence
   ```
3. Open a PR into `main`, review, and merge.
4. No new secrets or GitHub Actions changes are required -- the existing
   `DISCORD_WEBHOOK_URL` / `OPENAI_API_KEY` secrets and workflow from Phase 5 are
   unchanged and sufficient.
5. After merging, the next scheduled run will pick up the expanded watchlist/sources
   and begin producing resume-scored, company-diversified alerts automatically, with the
   review digest off by default (flip `send_review_digest: true` in `config.yaml` to
   opt back in).
