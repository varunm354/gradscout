"""Full pipeline orchestration.

collect -> normalize -> upsert (atomic created/changed/unchanged) -> classify
(only created/changed) -> persist classification -> enqueue alerts -> Discord
delivery -> review digest -> source failure/recovery notifications -> once
daily health summary.

Kept as one well-tested function (``run_once``) so ``scripts/run.py`` stays a
thin CLI wrapper, and the whole pipeline is exercisable offline in tests with
injected collectors (fixture ``fetch``) and an injected Discord HTTP
transport (``httpx.MockTransport``).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from gradscout import db
from gradscout.analyze import apply_to_job, classify_job
from gradscout.collectors.base import Collector, run_collector
from gradscout.diversify import diversify_by_company
from gradscout.llm import JobAnalysisAgent
from gradscout.location import location_permits_alert
from gradscout.models import (
    AlertChannel,
    AlertPriority,
    ChangeStatus,
    Config,
    EligibilityStatus,
    SourceStatus,
)
from gradscout.normalize import normalize
from gradscout.notify.discord import MAX_DIGEST_ITEMS, DiscordNotifier
from gradscout.prioritize import meets_min_priority
from gradscout.recency import is_recent
from gradscout.resume import build_matcher_from_config

# Explicit suppression reason recorded on an alert that lost out to a
# per-company/resume-category diversity cap (see gradscout.diversify).
SUPPRESSED_COMPANY_CAP = "suppressed_company_cap"

# Explicit suppression reason recorded on a review-priority alert when the
# operator has disabled the review digest (notifications.send_review_digest:
# false -- the production default, since the digest tends to overwhelm the
# far more useful individual P1/P2/P3 alerts). See docs/PHASE_6_HANDOFF.md.
SUPPRESSED_REVIEW_DIGEST_DISABLED = "suppressed_review_digest_disabled"

logger = logging.getLogger("gradscout.pipeline")


@dataclass
class RunStats:
    sources_ok: int = 0
    sources_partial: int = 0
    sources_error: int = 0
    jobs_seen: int = 0
    jobs_created: int = 0
    jobs_changed: int = 0
    jobs_unchanged: int = 0
    jobs_classified: int = 0
    alerts_enqueued: int = 0
    alerts_sent: int = 0
    # Ground-truth count of every still-pending Discord alert (individual
    # p1/p2/p3 AND review-digest priority) queried from the DB after every
    # send attempt this run -- see the Phase 5.3 postmortem note on run_once
    # for why this must never be a derived/arithmetic estimate.
    alerts_pending: int = 0
    # Phase 6: alerts explicitly suppressed this run by the per-company/
    # resume-category diversity cap (AlertState.suppressed) -- see
    # gradscout.diversify. Distinct from alerts_pending: these will NOT
    # resurface next run unless the underlying job materially changes.
    alerts_suppressed_company_cap: int = 0
    # Review-digest-disabled UX fix: review-priority alerts explicitly
    # suppressed because notifications.send_review_digest is false. Normally
    # 0 every run (new review jobs are simply never enqueued while disabled
    # -- see _enqueue_if_warranted), nonzero only when cleaning up alerts
    # that were already pending from before the digest was disabled.
    alerts_suppressed_review_digest_disabled: int = 0
    review_digest_sent: bool = False
    # Phase 5.3: the review digest may be split across multiple Discord
    # messages (see gradscout.notify.discord._chunk_digest_jobs); these two
    # give per-chunk visibility that a single review_digest_sent bool cannot.
    review_digest_chunks_sent: int = 0
    review_digest_chunks_failed: int = 0
    source_failures_notified: int = 0
    source_recoveries_notified: int = 0
    daily_summary_sent: bool = False
    baseline_run: bool = False
    baseline_completed: bool = False
    # Phase 5.3 postmortem fix: an explicit count of every Discord request this
    # run that was actually attempted (webhook configured, not dry-run) and
    # did NOT receive a 2xx -- covers individual alerts, review-digest chunks,
    # source failure/recovery, and the daily summary. A GitHub Actions run
    # must never look fully healthy while this is nonzero; see run_once's
    # final logging below.
    notification_delivery_failures: int = 0


def run_once(
    conn: sqlite3.Connection,
    config: Config,
    collectors: list[Collector],
    http_client: httpx.Client,
    notifier: DiscordNotifier,
    agent: JobAnalysisAgent | None = None,
    *,
    now: datetime | None = None,
) -> RunStats:
    now = now or datetime.now(timezone.utc)
    stats = RunStats()

    # Baseline bootstrap (Phase 5.2): fixed for the whole run from the meta
    # table's state as of BEFORE this run touches anything, so a fresh DB's
    # very first successful run stores every job normally but never floods
    # Discord with the entire historical backlog already open on first
    # crawl. Only mark this run's is_baseline_run so subsequent runs (once
    # the meta key is set at the very end, on success) go through the
    # ordinary created/changed alerting rules untouched.
    is_baseline_run = not db.is_baseline_complete(conn)
    stats.baseline_run = is_baseline_run

    # Built once per run (not per job) -- see gradscout.resume.
    resume_matcher = build_matcher_from_config(config)

    for collector in collectors:
        # Inspect the PRIOR source-health row before record_source_result()
        # overwrites it, so a healthy<->failed transition can be detected.
        prev_health = db.get_source_health_one(conn, collector.source_id)
        result = run_collector(collector, http_client)

        if result.status == SourceStatus.ok:
            stats.sources_ok += 1
        elif result.status == SourceStatus.partial:
            stats.sources_partial += 1
        else:
            stats.sources_error += 1

        transition, transition_delivered = _notify_source_transition(notifier, prev_health, result)
        if transition == "failed":
            stats.source_failures_notified += 1
        elif transition == "recovered":
            stats.source_recoveries_notified += 1
        if transition is not None and not transition_delivered and _is_real_attempt(notifier):
            stats.notification_delivery_failures += 1

        db.record_source_result(
            conn,
            result.source_id,
            result.company,
            result.company_priority,
            result.status,
            error=result.error,
            jobs_seen=result.jobs_seen,
            now=now,
        )

        for raw in result.raw_jobs:
            stats.jobs_seen += 1
            job = normalize(raw, company_priority=result.company_priority)
            upsert_result = db.upsert_job(conn, job, now=now)

            if upsert_result.status == ChangeStatus.created:
                stats.jobs_created += 1
            elif upsert_result.status == ChangeStatus.changed:
                stats.jobs_changed += 1
            else:
                stats.jobs_unchanged += 1
                # Unchanged: last_seen_at was already bumped by upsert_job.
                # Never reclassify or re-alert.
                continue

            stored = db.get_job(conn, upsert_result.job_id)
            # During baseline, a freshly created row's first_seen_at is
            # always "now", which would make every historical listing look
            # artificially recent via the fallback below -- so baseline
            # recency is judged strictly by the source's own posted_at
            # timestamp (never fabricated; simply not recent if absent).
            first_seen_basis = None if is_baseline_run else (stored.first_seen_at if stored else now)
            recent = is_recent(
                stored.source_posted_at if stored else None,
                first_seen_basis,
                config.notifications.new_grad_recent_hours,
                now,
            )
            resolved = classify_job(
                job, config, agent, is_recent=recent, resume_matcher=resume_matcher
            )
            apply_to_job(job, resolved)
            db.apply_classification(conn, upsert_result.job_id, resolved)
            stats.jobs_classified += 1

            if _enqueue_if_warranted(
                conn, upsert_result.job_id, resolved, config, now, is_baseline_run=is_baseline_run
            ):
                stats.alerts_enqueued += 1

    sent, individual_failed, individual_suppressed = _send_job_alerts(conn, notifier, config, now=now)
    stats.alerts_sent += sent
    stats.notification_delivery_failures += individual_failed
    stats.alerts_suppressed_company_cap += individual_suppressed

    (
        digest_sent,
        digest_chunks_sent,
        digest_chunks_failed,
        digest_suppressed,
        digest_disabled_suppressed,
    ) = _send_review_digest(conn, notifier, config, now=now)
    stats.review_digest_sent = digest_sent
    stats.review_digest_chunks_sent = digest_chunks_sent
    stats.review_digest_chunks_failed = digest_chunks_failed
    stats.notification_delivery_failures += digest_chunks_failed
    stats.alerts_suppressed_company_cap += digest_suppressed
    stats.alerts_suppressed_review_digest_disabled += digest_disabled_suppressed

    daily_summary_sent, daily_summary_failed = _maybe_send_daily_summary(conn, notifier, config, now)
    stats.daily_summary_sent = daily_summary_sent
    stats.notification_delivery_failures += int(daily_summary_failed)

    # Phase 5.3 postmortem fix: query the DB directly for the ground truth
    # rather than deriving this arithmetically from only the individual-alert
    # counts above -- the original production bug was exactly this: a
    # derived alerts_pending that silently excluded review-priority alerts
    # left pending by a failed digest chunk, making a real partial-delivery
    # failure look like a fully clean, empty run.
    stats.alerts_pending = len(db.get_pending_alerts(conn, AlertChannel.discord))

    # Mark the baseline complete only now that every step above (collect,
    # normalize, classify, persist, enqueue, deliver) has run without
    # raising -- an exception anywhere earlier in this function propagates
    # out of run_once and this line is simply never reached, so a failed
    # first run correctly leaves the next run in baseline mode too. A
    # dry-run must still report what baseline mode *would* do (see
    # is_baseline_run/enqueue behavior above) but must never durably mark
    # the baseline complete, since no real delivery happened.
    if is_baseline_run and not notifier.dry_run:
        db.mark_baseline_complete(conn, now=now)
        stats.baseline_completed = True

    logger.info(
        "pipeline run complete",
        extra={
            "fields": {
                "sources_ok": stats.sources_ok,
                "sources_partial": stats.sources_partial,
                "sources_error": stats.sources_error,
                "jobs_seen": stats.jobs_seen,
                "jobs_created": stats.jobs_created,
                "jobs_changed": stats.jobs_changed,
                "jobs_unchanged": stats.jobs_unchanged,
                "alerts_enqueued": stats.alerts_enqueued,
                "alerts_sent": stats.alerts_sent,
                "alerts_pending": stats.alerts_pending,
                "alerts_suppressed_company_cap": stats.alerts_suppressed_company_cap,
                "alerts_suppressed_review_digest_disabled": stats.alerts_suppressed_review_digest_disabled,
                "review_digest_sent": stats.review_digest_sent,
                "review_digest_chunks_sent": stats.review_digest_chunks_sent,
                "review_digest_chunks_failed": stats.review_digest_chunks_failed,
                "daily_summary_sent": stats.daily_summary_sent,
                "baseline_run": stats.baseline_run,
                "baseline_completed": stats.baseline_completed,
                "notification_delivery_failures": stats.notification_delivery_failures,
            }
        },
    )
    if stats.notification_delivery_failures:
        # A GitHub Actions run must never look fully healthy from the
        # standard "pipeline run complete" INFO line alone while some
        # Discord delivery actually failed -- this WARNING line is the
        # explicit, hard-to-miss signal (fail-soft: run_once still returns
        # normally and scripts/run.py still exits 0).
        logger.warning(
            "pipeline completed with Discord delivery failures",
            extra={
                "fields": {
                    "notification_delivery_failures": stats.notification_delivery_failures,
                    "alerts_pending": stats.alerts_pending,
                    "review_digest_chunks_failed": stats.review_digest_chunks_failed,
                }
            },
        )
    return stats


def _is_real_attempt(notifier: DiscordNotifier) -> bool:
    """True only if a False return from the notifier represents an actual
    rejected/failed Discord delivery attempt -- i.e. not dry-run (which
    always returns False by design) and not simply unconfigured."""
    return not notifier.dry_run and notifier.enabled


def _notify_source_transition(
    notifier: DiscordNotifier, prev_health, result
) -> tuple[str | None, bool]:
    """Fire an immediate failure/recovery notification only for a high
    priority (company_priority==1) source's healthy<->failed transition.
    No repeated alert while it stays failed; persistent failures instead
    surface in the daily summary via source_health. Returns
    (transition, delivered) where transition is "failed", "recovered", or
    None (no transition -> no attempt made, delivered is always False)."""
    if result.company_priority != 1:
        return None, False
    prev_status = prev_health["last_status"] if prev_health is not None else None
    was_failed = prev_status == SourceStatus.error.value
    is_failed = result.status == SourceStatus.error
    if not was_failed and is_failed:
        delivered = notifier.send_source_failure(result.source_id, result.company, result.error)
        return "failed", delivered
    if was_failed and not is_failed:
        delivered = notifier.send_source_recovery(result.source_id, result.company)
        return "recovered", delivered
    return None, False


def _enqueue_if_warranted(
    conn, job_id: int, resolved, config: Config, now: datetime, *, is_baseline_run: bool = False
) -> bool:
    """Only enqueue for a job that's newly created or materially changed in a
    way relevant to eligibility/role/priority/resume/requirements -- which is
    guaranteed by the caller only reaching here for created/changed jobs.

    During baseline bootstrap (a fresh DB's very first successful run), every
    job is still classified and stored normally, but the ordinary alerting
    rules are replaced with a much narrower one: only a genuinely-recent P1
    job (source-provided posted_at within new_grad_recent_hours -- see the
    tightened recency computation in run_once) is ever enqueued. Review
    items never enter the digest backlog during baseline, and ordinary
    historical P2/P3-eligible jobs are stored but never alerted -- this is
    what stops a fresh first run from flooding Discord with thousands of
    already-open historical listings.

    Phase 5.2: a job's location classification (preferred / remote_acceptable /
    out_of_region / unclear -- see gradscout.location) additionally gates ONLY
    this normal/baseline "eligible" alert path, never the eligibility-review
    digest path just below -- a review-status job always reaches the digest
    regardless of location, and an eligible-but-out_of_region/unclear job is
    simply stored and never alerted at all (not even to the digest).
    """
    if is_baseline_run:
        if (
            resolved.eligibility_status == EligibilityStatus.eligible
            and resolved.alert_priority == AlertPriority.p1
            and meets_min_priority(resolved.alert_priority, config.notifications.discord_min_priority)
            and location_permits_alert(resolved.location_classification, config.candidate)
        ):
            return db.enqueue_alert(
                conn, job_id, AlertChannel.discord, resolved.alert_priority.value, now=now
            )
        return False

    if resolved.eligibility_status == EligibilityStatus.review:
        # UX fix: when the operator has disabled the review digest (the
        # production default -- it tends to overwhelm the far more useful
        # individual P1/P2/P3 alerts), a review-status job is still fully
        # classified and stored (eligibility_status='review' on the `jobs`
        # row is the durable, queryable audit trail), it simply never gets a
        # pending alert row at all. This means it can never accumulate as
        # notification spam by construction -- there is nothing to suppress
        # every run. See _send_review_digest for the one-time cleanup of any
        # review alert that was already pending from before this was set.
        if not config.notifications.send_review_digest:
            return False
        return db.enqueue_alert(
            conn, job_id, AlertChannel.discord, resolved.alert_priority.value, now=now
        )
    if (
        resolved.eligibility_status == EligibilityStatus.eligible
        and meets_min_priority(resolved.alert_priority, config.notifications.discord_min_priority)
        and location_permits_alert(resolved.location_classification, config.candidate)
    ):
        return db.enqueue_alert(
            conn, job_id, AlertChannel.discord, resolved.alert_priority.value, now=now
        )
    return False


def _send_job_alerts(
    conn, notifier: DiscordNotifier, config: Config, now: datetime | None = None
) -> tuple[int, int, int]:
    """Send individual (non-digest) urgent/strong-match alerts, ordered by
    company priority, up to max_alerts_per_run. Returns (sent, failed) --
    ``failed`` counts only genuine rejected/errored delivery attempts (never
    dry-run or unconfigured-webhook skips, which are not delivery failures).

    Phase 6: before the global cap, ``diversify_by_company`` applies a
    per-company/resume-category cap so a single prolific poster can never
    fill every alert slot in a run. Rows it excludes are explicitly
    transitioned to AlertState.suppressed (see gradscout.db.suppress_alert)
    rather than left pending -- so they never repeatedly resurface every run,
    and only become reconsiderable again if the underlying job materially
    changes (gradscout.db.enqueue_alert). Excess beyond the global cap, and
    any failed sends, DO simply stay pending for a later run (unchanged
    Phase 5 behavior)."""
    pending = db.get_pending_alerts(conn, AlertChannel.discord)
    individual = [p for p in pending if p["priority"] in ("p1", "p2", "p3")]
    selected, suppressed = diversify_by_company(
        individual, per_company_cap=config.notifications.max_alerts_per_company_per_run
    )
    for row in suppressed:
        db.suppress_alert(conn, row["job_id"], AlertChannel.discord, SUPPRESSED_COMPANY_CAP, now=now)

    cap = config.notifications.max_alerts_per_run
    real_attempt = _is_real_attempt(notifier)
    sent = 0
    failed = 0
    for row in selected[:cap]:
        record = db.get_job(conn, row["job_id"])
        if record is None:
            continue
        if notifier.send_job_alert(record):
            db.mark_alert_sent(conn, row["job_id"], AlertChannel.discord)
            sent += 1
        elif real_attempt:
            failed += 1
    return sent, failed, len(suppressed)


def _send_review_digest(
    conn, notifier: DiscordNotifier, config: Config, now: datetime | None = None
) -> tuple[bool, int, int, int, int]:
    """Batch all pending Eligibility Review jobs into one or more digest
    messages (never one message per job), chunked as needed to respect
    Discord's payload limits (see gradscout.notify.discord). Each chunk is
    marked sent independently: a job is only ever flipped to 'sent' if the
    specific chunk containing it received a 2xx; every job in a failed or
    unattempted chunk stays pending.

    Phase 6: ``diversify_by_company`` applies a per-company/resume-category
    cap (``max_review_items_per_company_per_run``) before the existing
    ``MAX_DIGEST_ITEMS`` slice, so the digest can't be dominated by one
    company's backlog of ambiguous roles. Excluded rows are explicitly
    suppressed (see ``_send_job_alerts``) rather than left pending.

    UX fix: when ``notifications.send_review_digest`` is false (the
    production default -- the digest tends to overwhelm the far more useful
    individual P1/P2/P3 alerts), the digest is skipped cleanly: no Discord
    request is made and nothing is marked sent. Eligibility review
    classification and DB storage are completely unaffected (see
    gradscout.roles / gradscout.eligibility / the `jobs` table). Any
    review-priority alert already ``pending`` (e.g. left over from a run
    before the digest was disabled) is explicitly transitioned to
    ``AlertState.suppressed`` with reason "suppressed_review_digest_disabled"
    -- the same auditable mechanism as the company-diversity cap -- so it
    can never sit as unbounded pending backlog or resurface as a delivery
    spike if the digest is re-enabled later. Since a NEW review job is never
    enqueued at all while disabled (see gradscout.pipeline._enqueue_if_warranted),
    this is normally a one-run cleanup, not an ongoing per-run cost.

    Returns (fully_sent, chunks_sent, chunks_failed, company_cap_suppressed,
    digest_disabled_suppressed). fully_sent is True only if at least one
    chunk was attempted and none of them failed."""
    pending = db.get_pending_alerts(conn, AlertChannel.discord)
    review_candidates = [p for p in pending if p["priority"] == "review"]

    if not config.notifications.send_review_digest:
        for row in review_candidates:
            db.suppress_alert(
                conn, row["job_id"], AlertChannel.discord, SUPPRESSED_REVIEW_DIGEST_DISABLED, now=now
            )
        return False, 0, 0, 0, len(review_candidates)

    selected, suppressed = diversify_by_company(
        review_candidates,
        per_company_cap=config.notifications.max_review_items_per_company_per_run,
    )
    for row in suppressed:
        db.suppress_alert(conn, row["job_id"], AlertChannel.discord, SUPPRESSED_COMPANY_CAP, now=now)
    suppressed_count = len(suppressed)

    review_rows = selected[:MAX_DIGEST_ITEMS]
    if not review_rows:
        return False, 0, 0, suppressed_count, 0
    records = [r for r in (db.get_job(conn, row["job_id"]) for row in review_rows) if r]
    if not records:
        return False, 0, 0, suppressed_count, 0
    result = notifier.send_review_digest(records)
    delivered_ids = {r.job_id for r in result.delivered}
    for row in review_rows:
        if row["job_id"] in delivered_ids:
            db.mark_alert_sent(conn, row["job_id"], AlertChannel.discord)
    fully_sent = result.chunks_sent > 0 and result.chunks_failed == 0
    return fully_sent, result.chunks_sent, result.chunks_failed, suppressed_count, 0


def _maybe_send_daily_summary(
    conn, notifier: DiscordNotifier, config: Config, now: datetime
) -> tuple[bool, bool]:
    """Send at most one health summary per UTC calendar day, at the
    configured hour, guarded by the meta table (never hourly). Returns
    (sent, failed) -- failed is only True for a genuine rejected/errored
    delivery attempt at the configured hour (never for "not due yet" or
    "already sent today", and never for dry-run/unconfigured skips)."""
    if now.hour != config.notifications.daily_summary_hour_utc:
        return False, False
    date_key = now.strftime("%Y-%m-%d")
    if db.get_meta(conn, "daily_summary_last_date") == date_key:
        return False, False
    rows = db.get_source_health(conn)
    if notifier.send_daily_summary(rows, date_key):
        db.set_meta(conn, "daily_summary_last_date", date_key)
        return True, False
    return False, _is_real_attempt(notifier)
