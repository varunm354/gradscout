"""Phase 6.2 fresh-first alert window: format_posting_age, compute_alert_window_start,
and evaluate_freshness. Pure functions, no DB/network -- see gradscout.freshness."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gradscout.freshness import (
    FreshnessOutcome,
    compute_alert_window_start,
    evaluate_freshness,
    format_posting_age,
)
from gradscout.models import CandidateProfile, Config

NOW = datetime(2027, 1, 10, 12, 0, tzinfo=timezone.utc)


def _config(**candidate_overrides) -> Config:
    return Config(candidate=CandidateProfile(**candidate_overrides))


# --------------------------------------------------------------------------- #
# format_posting_age
# --------------------------------------------------------------------------- #
def test_format_posting_age_none_returns_none():
    assert format_posting_age(None, NOW) is None


def test_format_posting_age_seconds_ago_is_just_now():
    assert format_posting_age(NOW - timedelta(seconds=30), NOW) == "just now"


def test_format_posting_age_minutes_ago():
    assert format_posting_age(NOW - timedelta(minutes=45), NOW) == "45m ago"


def test_format_posting_age_just_under_an_hour():
    assert format_posting_age(NOW - timedelta(minutes=59), NOW) == "59m ago"


def test_format_posting_age_hours_ago():
    assert format_posting_age(NOW - timedelta(hours=3), NOW) == "3h ago"


def test_format_posting_age_just_under_a_day():
    assert format_posting_age(NOW - timedelta(hours=23), NOW) == "23h ago"


def test_format_posting_age_days_ago():
    assert format_posting_age(NOW - timedelta(days=1), NOW) == "1d ago"
    assert format_posting_age(NOW - timedelta(days=6), NOW) == "6d ago"


def test_format_posting_age_older_than_a_week_is_absolute_date_same_year():
    posted = datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert format_posting_age(posted, NOW) == "Jan 01"


def test_format_posting_age_older_than_a_week_different_year_includes_year():
    posted = datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert format_posting_age(posted, NOW) == "Dec 01, 2026"


def test_format_posting_age_future_timestamp_clamped_to_just_now():
    """Cosmetic display never gates alerting: even a job "posted" slightly in
    the future (clock skew) reads as "just now", never a negative duration."""
    assert format_posting_age(NOW + timedelta(minutes=5), NOW) == "just now"


def test_format_posting_age_handles_naive_datetime_as_utc():
    naive = datetime(2027, 1, 10, 11, 30)  # no tzinfo
    assert format_posting_age(naive, NOW) == "30m ago"


# --------------------------------------------------------------------------- #
# compute_alert_window_start
# --------------------------------------------------------------------------- #
def test_compute_window_start_normal_overlap():
    last_run = NOW - timedelta(hours=1)
    config = _config(alert_overlap_minutes=60, recovery_max_age_hours=24)
    start = compute_alert_window_start(last_run, config, NOW)
    assert start == last_run - timedelta(minutes=60)


def test_compute_window_start_no_prior_run_uses_recovery():
    config = _config(recovery_max_age_hours=24)
    start = compute_alert_window_start(None, config, NOW)
    assert start == NOW - timedelta(hours=24)


def test_compute_window_start_stale_prior_run_uses_recovery():
    """A prior successful run more than recovery_max_age_hours ago must not
    leave a multi-day-old overlap window -- fall back to the fixed recovery
    window instead of the full historical gap."""
    last_run = NOW - timedelta(hours=48)
    config = _config(alert_overlap_minutes=60, recovery_max_age_hours=24)
    start = compute_alert_window_start(last_run, config, NOW)
    assert start == NOW - timedelta(hours=24)


def test_compute_window_start_exactly_at_recovery_boundary_uses_normal_overlap():
    last_run = NOW - timedelta(hours=24)  # exactly recovery_max_age_hours ago
    config = _config(alert_overlap_minutes=60, recovery_max_age_hours=24)
    start = compute_alert_window_start(last_run, config, NOW)
    assert start == last_run - timedelta(minutes=60)


# --------------------------------------------------------------------------- #
# evaluate_freshness
# --------------------------------------------------------------------------- #
def test_evaluate_freshness_disabled_when_window_start_is_none():
    """alert_window_start=None means freshness gating is off (baseline) --
    always fresh, regardless of the posted date."""
    assert evaluate_freshness(None, None, NOW, 15) == FreshnessOutcome.fresh
    assert (
        evaluate_freshness(NOW - timedelta(days=365), None, NOW, 15) == FreshnessOutcome.fresh
    )


def test_evaluate_freshness_missing_posted_at():
    window_start = NOW - timedelta(hours=1)
    assert (
        evaluate_freshness(None, window_start, NOW, 15)
        == FreshnessOutcome.suppressed_missing_posted_at
    )


def test_evaluate_freshness_invalid_posted_at_string():
    window_start = NOW - timedelta(hours=1)
    assert (
        evaluate_freshness("not-a-real-timestamp", window_start, NOW, 15)
        == FreshnessOutcome.suppressed_invalid_posted_at
    )


def test_evaluate_freshness_inside_window_is_fresh():
    window_start = NOW - timedelta(hours=1)
    posted = NOW - timedelta(minutes=30)
    assert evaluate_freshness(posted, window_start, NOW, 15) == FreshnessOutcome.fresh


def test_evaluate_freshness_accepts_iso_string_posted_at():
    window_start = NOW - timedelta(hours=1)
    posted_iso = (NOW - timedelta(minutes=30)).isoformat()
    assert evaluate_freshness(posted_iso, window_start, NOW, 15) == FreshnessOutcome.fresh


def test_evaluate_freshness_too_old_is_outside_window():
    window_start = NOW - timedelta(hours=1)
    posted = NOW - timedelta(hours=2)
    assert (
        evaluate_freshness(posted, window_start, NOW, 15)
        == FreshnessOutcome.suppressed_outside_freshness_window
    )


def test_evaluate_freshness_far_future_beyond_skew_is_outside_window():
    window_start = NOW - timedelta(hours=1)
    posted = NOW + timedelta(hours=6)  # far beyond a 15-minute skew tolerance
    assert (
        evaluate_freshness(posted, window_start, NOW, 15)
        == FreshnessOutcome.suppressed_outside_freshness_window
    )


def test_evaluate_freshness_slightly_future_within_skew_is_fresh():
    window_start = NOW - timedelta(hours=1)
    posted = NOW + timedelta(minutes=10)  # within a 15-minute skew tolerance
    assert evaluate_freshness(posted, window_start, NOW, 15) == FreshnessOutcome.fresh


def test_evaluate_freshness_exactly_at_skew_boundary_is_fresh():
    window_start = NOW - timedelta(hours=1)
    posted = NOW + timedelta(minutes=15)
    assert evaluate_freshness(posted, window_start, NOW, 15) == FreshnessOutcome.fresh


def test_evaluate_freshness_just_past_skew_boundary_is_outside_window():
    window_start = NOW - timedelta(hours=1)
    posted = NOW + timedelta(minutes=15, seconds=1)
    assert (
        evaluate_freshness(posted, window_start, NOW, 15)
        == FreshnessOutcome.suppressed_outside_freshness_window
    )


def test_evaluate_freshness_naive_datetimes_treated_as_utc():
    window_start = NOW - timedelta(hours=1)
    posted = (NOW - timedelta(minutes=30)).replace(tzinfo=None)
    assert evaluate_freshness(posted, window_start, NOW, 15) == FreshnessOutcome.fresh
