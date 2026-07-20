# GradScout — Phase 4 Handoff

Concise handoff for continuing work. **No commits have been made yet** (see §11).

## 1. Project goal and constraints

Unchanged from Phase 3 (see `docs/PHASE_3_HANDOFF.md` §1). GradScout hourly-monitors
structured job sources for 2027 new-grad / eligible early-career roles, prioritizes
major-tech employers, matches a resume, and alerts to Discord. Deterministic-first;
the LLM is optional and bounded. Not a SaaS product.

## 2. Completed phases 0–4

- **Phase 0–2**: scaffolding, persistence core, collectors + normalization (see
  Phase 3 handoff).
- **Phase 3**: deterministic eligibility/role/resume/priority classification +
  bounded optional LLM resolver.
- **Phase 4 (this phase)**: full local pipeline orchestration, atomic
  new/materially-changed detection, classification persistence, alert-queue
  generation, Discord delivery (individual alerts + review digest), transition-based
  source failure/recovery notifications, and a once-daily health summary. Everything
  is locally testable; no GitHub Actions / state-branch wiring yet (Phase 5).

## 3. What changed vs. Phase 3 (contract changes to be aware of)

- **`db.upsert_job` signature changed.** It now returns `UpsertResult(job_id, status)`
  (`status` is `ChangeStatus.created|changed|unchanged`), not a `(job_id, created)`
  tuple. It is also now **structural-only**: it never writes classification columns
  (`eligibility_status`, `role_family`, `alert_priority`, etc.) — those are written
  exclusively by the new `db.apply_classification(conn, job_id, resolved)`. This
  split means a re-seen, content-unchanged job gets its `last_seen_at` bumped
  without ever touching its prior classification or alert state.
- `jobs` gained a `content_hash` column and the DB gained a `meta(key, value)` table
  (see §5). Since nothing has been committed yet, this was a direct schema addition,
  not a migration.
- `gradscout/prioritize.py` gained `meets_min_priority(priority, threshold)`.

## 4. Folder / module architecture (additions marked with `*`)

```
grad-scout/
├── docs/PHASE_4_HANDOFF.md    * this file
├── gradscout/
│   ├── changes.py             * content_hash() for material-change detection
│   ├── recency.py             * is_recent() for P1 urgency
│   ├── pipeline.py            * run_once(): the full orchestrator
│   ├── notify/
│   │   └── discord.py         * DiscordNotifier: alerts, digest, health, failure/recovery
│   ├── db.py                    UpsertResult/apply_classification/meta additions
│   ├── prioritize.py             meets_min_priority() addition
│   └── ... (Phase 0-3 modules unchanged in spirit; see Phase 3 handoff)
├── scripts/
│   ├── collect.py               unchanged: network-only collector harness, no alerts
│   └── run.py                 * the real operational entrypoint
└── tests/
    ├── test_changes.py        * content-hash tests
    ├── test_recency.py        * recency tests
    ├── test_notify_discord.py * Discord boundary tests (httpx.MockTransport)
    └── test_pipeline.py       * end-to-end offline pipeline tests
```

## 5. Important invariants (do not break)

All Phase 3 invariants still hold (jobs never discarded, cross-source dedupe,
pending→sent alert lifecycle, hard rules override the LLM, LLM only sees
new/relevant/ambiguous jobs, collectors are fail-soft/time-bounded). Phase 4 adds:

- **`upsert_job` is structural-only; classification is separate.** Never make
  `upsert_job` write eligibility/role/resume/priority columns again — that reopened
  the bug this phase was built to avoid (reclassifying/re-alerting unchanged jobs).
- **Content hash = only analysis-relevant fields.** `gradscout/changes.compute_content_hash`
  hashes `title`, `description_text`, `location`, a structured employment hint
  (Ashby `employmentType` / Lever `categories.commitment`), and `raw_blob.sponsorship`
  / `raw_blob.degrees` when present (Simplify feed). It deliberately excludes
  `last_seen_at`, any collector/health timestamp, and is insensitive to `raw_blob`
  key ordering. Do not add volatile fields to it.
- **Classification only runs for created/changed jobs.** `pipeline.run_once` skips
  `classify_job`/`apply_classification`/alert-enqueue entirely for `unchanged` jobs —
  this is what keeps an hourly run from reclassifying the ~17k-row Simplify feed.
- **Alerts are enqueued only for created/changed jobs**, and only when: (a)
  `eligibility_status == review` (and `send_review_digest` is on) → digest, or (b)
  `eligibility_status == eligible` and `alert_priority` meets `discord_min_priority`
  → individual alert. `ineligible`/`unclassified` never enqueue.
- **Review jobs are batched into ONE digest message**, never one message per job
  (capped at `notify.discord.MAX_DIGEST_ITEMS` = 25 per run; excess stays pending
  for a later digest).
- **Discord delivery is a pure best-effort boundary.** `DiscordNotifier._post`
  returns `True` only on a 2xx response and never raises; any transport
  error/timeout/rate-limit/non-2xx returns `False`. `mark_alert_sent()` is called
  **only** when a send returns `True`. Dry-run makes no HTTP request at all (the
  notifier doesn't even construct an `httpx.Client`) and always returns `False`.
- **`max_alerts_per_run` caps individual alerts only**; excess (and any that failed
  to send) stay `pending` and are retried/reconsidered on a later run.
- **Source failure/recovery notifications are transition-based**, for
  `company_priority == 1` sources only: fires once on healthy→failed and once on
  failed→healthy, by comparing `db.get_source_health_one()` (read BEFORE
  `record_source_result()` overwrites it) against the new result. No repeated
  hourly alert while a source stays down; persistent failures still show up in the
  daily summary via `source_health`. No 24h reminder in this phase.
- **The daily health summary fires at most once per UTC calendar day**, gated by
  `meta["daily_summary_last_date"]` (a `YYYY-MM-DD` string), only during the run
  whose `now.hour == daily_summary_hour_utc`. The marker is only set after a
  successful send, so a failed attempt is simply skipped for that day (accepted
  tradeoff — see §8).
- **Never label `first_seen_at` as a posting date.** Discord job-alert embeds show
  `Posted` (from `source_posted_at`, omitted entirely if absent) and `First
  discovered by GradScout` (from `first_seen_at`) as two distinct, clearly labeled
  fields.
- **No GitHub Actions / orphan `state` branch in this phase.** `scripts/run.py`
  always reads/writes a local DB file; scheduling and state persistence are Phase 5.

## 6. Commands (setup / tests / lint / local run)

```bash
# setup (unchanged)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml
cp .env.example .env   # DISCORD_WEBHOOK_URL for real sends

# tests + lint
pytest
ruff check .

# the real operational entrypoint (this phase)
python -m scripts.run --dry-run                 # full pipeline, no Discord sends
python -m scripts.run                            # real run (needs DISCORD_WEBHOOK_URL)
python -m scripts.run --db data/gradscout.db --config config.yaml

# still available: the network-only collector harness (no alerts/classification)
python -m scripts.collect --dry-run --no-github
```

## 7. Exact current test/lint results

- `pytest` → **130 passed** (up from 85; no network in tests — Discord and collector
  HTTP are both mocked/injected).
- `ruff check .` → **All checks passed!**
- No linter diagnostics in `gradscout/`, `scripts/`, or `tests/`.

## 8. Implementation warnings / tradeoffs discovered

- **`upsert_job`/`apply_classification` split is the core Phase 4 design decision.**
  Any future change that reunifies them (e.g. "just always upsert everything") will
  silently reintroduce reclassify-the-whole-feed-every-run and re-alert-on-every-run
  bugs. Keep them separate.
- **Recency is computed from the persisted (coalesced) record, not the in-flight
  `Job`.** `pipeline.run_once` re-reads the row via `db.get_job()` right after
  `upsert_job()` and feeds `stored.source_posted_at`/`stored.first_seen_at` into
  `is_recent()`, rather than the freshly normalized `Job`'s (possibly `None` on a
  later fetch) `source_posted_at`. This matches what `apply_url`/`source_posted_at`
  COALESCE actually persists.
- **Daily-summary retry tradeoff.** The `meta` marker is only set on a *successful*
  send. If Discord rejects the one daily-summary attempt for the day, GradScout does
  not retry until the same hour next day (explicitly acceptable per this phase's
  scope — "no 24-hour reminder required").
- **Source failure/recovery notifications are best-effort, not part of the
  `alerts` pending/sent lifecycle.** They're not job-scoped, so there's no natural
  `(job_id, channel)` row to track retry state; a missed one is superseded by the
  next hourly health check (`ok`/`error`) or the daily summary regardless.
- **Review digest and individual alerts share the same `alerts` table** and the same
  pending→sent lifecycle, distinguished only by `priority == "review"` vs
  `"p1"/"p2"/"p3"`. This reuses all existing idempotency/ordering guarantees instead
  of inventing a second queue.
- **`FakeCollector` pattern for pipeline tests.** `tests/test_pipeline.py` defines a
  `Collector` subclass whose `fetch()`/`parse()` never touch the network (mirrors
  the `c.fetch = lambda ...` monkeypatch already used in `tests/test_collectors.py`),
  and Discord is exercised via `httpx.Client(transport=httpx.MockTransport(...))`.
  Use the same patterns for any new pipeline-level tests.

## 9. Deferred features (Phase 5+)

GitHub Actions hourly workflow, orphan `state` branch bootstrap/restore/commit,
24-hour repeat reminder for persistent high-priority failures, richer Discord
formatting (e.g. mentions/threads), non-Simplify GitHub parsers, Workday/iCIMS and
other ATS, direct scraping without a public API, fuzzy company+title merge, richer
LLM enrichment, analytics/dashboards. Also still explicitly out of scope per the
project goal: frontend, auto-apply, browser automation, auth.

## 10. Major files and responsibilities (Phase 4 additions marked `*`)

| File | Responsibility |
|---|---|
| `gradscout/changes.py` * | `compute_content_hash(job)` — analysis-relevant fields only |
| `gradscout/recency.py` * | `is_recent(source_posted_at, first_seen_at, hours, now)` |
| `gradscout/pipeline.py` * | `run_once()`: the full orchestrator; source transitions, enqueue rules, digest/cap/daily-summary logic |
| `gradscout/notify/discord.py` * | `DiscordNotifier`: job-alert/digest/failure/recovery/summary embeds; 2xx-only delivery boundary |
| `gradscout/db.py` | + `UpsertResult`, `apply_classification`, `get_source_health_one`, `get_meta`/`set_meta` |
| `gradscout/prioritize.py` | + `meets_min_priority(priority, threshold)` |
| `scripts/run.py` * | Real operational entrypoint (`--config`, `--db`, `--dry-run`, `--now`) |
| `scripts/collect.py` | Unchanged: network-only collector harness, no alerts |
| `tests/test_changes.py`, `test_recency.py`, `test_notify_discord.py`, `test_pipeline.py` * | Offline coverage for all of the above |

## 11. Commit status

**No commits have been made.** The repository has no commits on `main`; all Phase
0–4 work is uncommitted in the working tree. No `state` branch has been created.
