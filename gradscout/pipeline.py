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
from gradscout.llm import JobAnalysisAgent
from gradscout.models import AlertChannel, ChangeStatus, Config, EligibilityStatus, SourceStatus
from gradscout.normalize import normalize
from gradscout.notify.discord import MAX_DIGEST_ITEMS, DiscordNotifier
from gradscout.prioritize import meets_min_priority
from gradscout.recency import is_recent

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
    alerts_pending: int = 0
    review_digest_sent: bool = False
    source_failures_notified: int = 0
    source_recoveries_notified: int = 0
    daily_summary_sent: bool = False


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

        transition = _notify_source_transition(notifier, prev_health, result)
        if transition == "failed":
            stats.source_failures_notified += 1
        elif transition == "recovered":
            stats.source_recoveries_notified += 1

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
            recent = is_recent(
                stored.source_posted_at if stored else None,
                stored.first_seen_at if stored else now,
                config.notifications.new_grad_recent_hours,
                now,
            )
            resolved = classify_job(job, config, agent, is_recent=recent)
            apply_to_job(job, resolved)
            db.apply_classification(conn, upsert_result.job_id, resolved)
            stats.jobs_classified += 1

            if _enqueue_if_warranted(conn, upsert_result.job_id, resolved, config, now):
                stats.alerts_enqueued += 1

    sent, pending = _send_job_alerts(conn, notifier, config)
    stats.alerts_sent += sent
    stats.alerts_pending += pending
    stats.review_digest_sent = _send_review_digest(conn, notifier, config)
    stats.daily_summary_sent = _maybe_send_daily_summary(conn, notifier, config, now)

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
                "review_digest_sent": stats.review_digest_sent,
                "daily_summary_sent": stats.daily_summary_sent,
            }
        },
    )
    return stats


def _notify_source_transition(notifier: DiscordNotifier, prev_health, result) -> str | None:
    """Fire an immediate failure/recovery notification only for a high
    priority (company_priority==1) source's healthy<->failed transition.
    No repeated alert while it stays failed; persistent failures instead
    surface in the daily summary via source_health. Returns "failed",
    "recovered", or None."""
    if result.company_priority != 1:
        return None
    prev_status = prev_health["last_status"] if prev_health is not None else None
    was_failed = prev_status == SourceStatus.error.value
    is_failed = result.status == SourceStatus.error
    if not was_failed and is_failed:
        notifier.send_source_failure(result.source_id, result.company, result.error)
        return "failed"
    if was_failed and not is_failed:
        notifier.send_source_recovery(result.source_id, result.company)
        return "recovered"
    return None


def _enqueue_if_warranted(conn, job_id: int, resolved, config: Config, now: datetime) -> bool:
    """Only enqueue for a job that's newly created or materially changed in a
    way relevant to eligibility/role/priority/resume/requirements -- which is
    guaranteed by the caller only reaching here for created/changed jobs."""
    if resolved.eligibility_status == EligibilityStatus.review:
        if not config.notifications.send_review_digest:
            return False
        return db.enqueue_alert(
            conn, job_id, AlertChannel.discord, resolved.alert_priority.value, now=now
        )
    if resolved.eligibility_status == EligibilityStatus.eligible and meets_min_priority(
        resolved.alert_priority, config.notifications.discord_min_priority
    ):
        return db.enqueue_alert(
            conn, job_id, AlertChannel.discord, resolved.alert_priority.value, now=now
        )
    return False


def _send_job_alerts(conn, notifier: DiscordNotifier, config: Config) -> tuple[int, int]:
    """Send individual (non-digest) urgent/strong-match alerts, ordered by
    company priority, up to max_alerts_per_run. Excess and any failed sends
    stay pending for a later run."""
    pending = db.get_pending_alerts(conn, AlertChannel.discord)
    individual = [p for p in pending if p["priority"] in ("p1", "p2", "p3")]
    cap = config.notifications.max_alerts_per_run
    sent = 0
    for row in individual[:cap]:
        record = db.get_job(conn, row["job_id"])
        if record is None:
            continue
        if notifier.send_job_alert(record):
            db.mark_alert_sent(conn, row["job_id"], AlertChannel.discord)
            sent += 1
    pending_remaining = len(individual) - sent
    return sent, pending_remaining


def _send_review_digest(conn, notifier: DiscordNotifier, config: Config) -> bool:
    """Batch all pending Eligibility Review jobs into ONE digest message
    (never one message per job). All-or-nothing: only marked sent if the
    single digest message is accepted."""
    if not config.notifications.send_review_digest:
        return False
    pending = db.get_pending_alerts(conn, AlertChannel.discord)
    review_rows = [p for p in pending if p["priority"] == "review"][:MAX_DIGEST_ITEMS]
    if not review_rows:
        return False
    records = [r for r in (db.get_job(conn, row["job_id"]) for row in review_rows) if r]
    if not records:
        return False
    if notifier.send_review_digest(records):
        for row in review_rows:
            db.mark_alert_sent(conn, row["job_id"], AlertChannel.discord)
        return True
    return False


def _maybe_send_daily_summary(conn, notifier: DiscordNotifier, config: Config, now: datetime) -> bool:
    """Send at most one health summary per UTC calendar day, at the
    configured hour, guarded by the meta table (never hourly)."""
    if now.hour != config.notifications.daily_summary_hour_utc:
        return False
    date_key = now.strftime("%Y-%m-%d")
    if db.get_meta(conn, "daily_summary_last_date") == date_key:
        return False
    rows = db.get_source_health(conn)
    if notifier.send_daily_summary(rows, date_key):
        db.set_meta(conn, "daily_summary_last_date", date_key)
        return True
    return False
