"""Fresh-first alert window (Phase 6.2).

Two independent, pure, side-effect-free pieces feed the individual (non-digest)
p1/p2/p3 alert path:

1. ``compute_alert_window_start`` -- turns ``last_successful_run_at`` (durable
   meta, see gradscout.db) plus the operator's overlap/recovery config into a
   lower time bound: a job posted before this bound never gets an individual
   alert this run.
2. ``evaluate_freshness`` -- the single source of truth for whether one job's
   ``source_posted_at`` is "fresh enough" to alert on right now, given that
   lower bound and an upper (clock-skew) bound. Used both when a job is first
   considered for enqueueing (gradscout.pipeline._enqueue_if_warranted) and
   when re-validating already-pending alerts immediately before delivery
   (gradscout.pipeline._send_job_alerts) -- so a job that ages out of the
   window while stuck pending (repeated delivery failures, or simply having
   been enqueued before this feature existed) is never delivered stale.

``format_posting_age`` is a separate, purely cosmetic concern: how a job's
posted date is displayed in a Discord embed. It never gates anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

from gradscout.models import Config

# Below this, "Xm ago" would read as "0m ago", which looks broken -- and it
# covers ordinary negative clock skew (a job that appears to be posted a few
# seconds in the future due to clock drift) without a separate special case.
_JUST_NOW_SECONDS = 60
_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR
# Beyond this many days, switch from a relative marker to an absolute date --
# "45d ago" is much less scannable than "Jun 15".
_ABSOLUTE_DATE_AFTER_DAYS = 7


class FreshnessOutcome(str, Enum):
    """Every possible verdict for one job's freshness, and, when the verdict
    is not ``fresh``, the exact auditable reason recorded on the suppressed
    alert row (see gradscout.db.suppress_alert)."""

    fresh = "fresh"
    suppressed_missing_posted_at = "suppressed_missing_posted_at"
    suppressed_invalid_posted_at = "suppressed_invalid_posted_at"
    suppressed_outside_freshness_window = "suppressed_outside_freshness_window"


def _as_aware_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def format_posting_age(source_posted_at: datetime | None, now: datetime) -> str | None:
    """Human-readable relative age for a Discord embed, e.g. "45m ago",
    "3h ago", "1d ago", or "Jul 28" (with a year suffix if not this year) for
    anything older than a week. Returns None if ``source_posted_at`` is None,
    so callers can omit the field exactly as they do today.

    Never gates alerting -- purely cosmetic. A future timestamp (clock skew)
    is clamped to "just now" rather than shown as a negative duration."""
    if source_posted_at is None:
        return None
    basis = _as_aware_utc(source_posted_at)
    now = _as_aware_utc(now)
    delta_seconds = (now - basis).total_seconds()

    if delta_seconds < _JUST_NOW_SECONDS:
        return "just now"
    if delta_seconds < _HOUR:
        return f"{int(delta_seconds // _MINUTE)}m ago"
    if delta_seconds < _DAY:
        return f"{int(delta_seconds // _HOUR)}h ago"
    days = int(delta_seconds // _DAY)
    if days < _ABSOLUTE_DATE_AFTER_DAYS:
        return f"{days}d ago"
    if basis.year == now.year:
        return basis.strftime("%b %d")
    return basis.strftime("%b %d, %Y")


def compute_alert_window_start(
    last_successful_run_at: datetime | None, config: Config, now: datetime
) -> datetime:
    """Lower bound of this run's individual-alert freshness window.

    Normal case: the previous successful run's timestamp, minus a
    configurable overlap buffer (``candidate.alert_overlap_minutes``) so
    schedule drift or slightly-delayed ATS timestamps can never cause a job
    to be missed.

    Recovery case: if there is no recorded prior successful run, or it was
    longer ago than ``candidate.recovery_max_age_hours`` (e.g. the monitor
    was down, or this is a fresh, already-baselined DB), fall back to a
    fixed recovery window of the last ``recovery_max_age_hours`` instead of
    alerting on the full historical inventory."""
    now = _as_aware_utc(now)
    recovery = timedelta(hours=config.candidate.recovery_max_age_hours)
    if last_successful_run_at is None:
        return now - recovery
    last_successful_run_at = _as_aware_utc(last_successful_run_at)
    if now - last_successful_run_at > recovery:
        return now - recovery
    return last_successful_run_at - timedelta(minutes=config.candidate.alert_overlap_minutes)


def evaluate_freshness(
    raw_posted_at: str | datetime | None,
    alert_window_start: datetime | None,
    now: datetime,
    clock_skew_minutes: int,
) -> FreshnessOutcome:
    """Single source of truth for "is this job fresh enough to individually
    alert on right now".

    ``raw_posted_at`` accepts either an already-parsed ``datetime`` (the live
    enqueue path, where ``Job.source_posted_at`` is Pydantic-validated) or a
    raw ISO string (the send-time recheck path, which reads straight from a
    ``sqlite3.Row`` -- see gradscout.pipeline._send_job_alerts).

    ``alert_window_start is None`` means freshness gating is disabled for
    this call (the baseline bootstrap run -- see gradscout.pipeline.run_once):
    always ``fresh``.

    Fails safe: a missing or unparsable posted date is never fresh, and a
    "posted" date suspiciously far in the future (beyond ``clock_skew_minutes``
    -- a bad ATS timestamp or real clock skew) is treated as outside the
    window, not as trivially "freshest possible"."""
    if alert_window_start is None:
        return FreshnessOutcome.fresh
    if raw_posted_at is None:
        return FreshnessOutcome.suppressed_missing_posted_at

    if isinstance(raw_posted_at, datetime):
        basis = raw_posted_at
    else:
        try:
            basis = datetime.fromisoformat(raw_posted_at)
        except (TypeError, ValueError):
            return FreshnessOutcome.suppressed_invalid_posted_at

    basis = _as_aware_utc(basis)
    now = _as_aware_utc(now)
    alert_window_start = _as_aware_utc(alert_window_start)
    upper_bound = now + timedelta(minutes=clock_skew_minutes)
    if basis < alert_window_start or basis > upper_bound:
        return FreshnessOutcome.suppressed_outside_freshness_window
    return FreshnessOutcome.fresh
