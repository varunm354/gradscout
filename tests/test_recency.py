"""Recency evaluation. No network, no DB."""

from datetime import datetime, timedelta, timezone

from gradscout.recency import is_recent

NOW = datetime(2027, 1, 10, 12, 0, tzinfo=timezone.utc)


def test_prefers_source_posted_at_when_present():
    posted = NOW - timedelta(hours=10)
    first_seen = NOW - timedelta(hours=100)  # would say "not recent" if used
    assert is_recent(posted, first_seen, recent_hours=48, now=NOW) is True


def test_falls_back_to_first_seen_at_when_no_source_posted_at():
    first_seen = NOW - timedelta(hours=10)
    assert is_recent(None, first_seen, recent_hours=48, now=NOW) is True
    stale_first_seen = NOW - timedelta(hours=100)
    assert is_recent(None, stale_first_seen, recent_hours=48, now=NOW) is False


def test_never_fabricates_when_both_missing():
    assert is_recent(None, None, recent_hours=48, now=NOW) is False


def test_boundary_is_inclusive():
    posted = NOW - timedelta(hours=48)
    assert is_recent(posted, None, recent_hours=48, now=NOW) is True
    posted_over = NOW - timedelta(hours=48, seconds=1)
    assert is_recent(posted_over, None, recent_hours=48, now=NOW) is False


def test_naive_datetime_is_treated_as_utc():
    posted_naive = (NOW - timedelta(hours=10)).replace(tzinfo=None)
    assert is_recent(posted_naive, None, recent_hours=48, now=NOW) is True
