"""End-to-end location-preference (Phase 5.2) pipeline tests.

Reuses the offline fixtures/helpers from tests/test_pipeline.py (fake collectors,
MockTransport-backed Discord notifier, in-memory DB) -- same pattern as
tests/test_baseline.py. Every invariant from the Phase 5.2 spec gets at least one
direct end-to-end assertion here: jobs are always stored regardless of location,
only NORMAL individual alerting is ever gated/penalized, and the eligibility-review
digest is completely unaffected by location.
"""

from __future__ import annotations

from gradscout import db
from gradscout.models import (
    AlertChannel,
    AlertState,
    CandidateProfile,
    Config,
    LocationClassification,
    NotificationConfig,
    WatchlistCompany,
)
from gradscout.pipeline import run_once
from tests.test_pipeline import (
    ELIGIBLE_DESC,
    ELIGIBLE_TITLE,
    NOW,
    REVIEW_DESC,
    REVIEW_TITLE,
    _collector,
    _conn_past_baseline,
    _notifier,
    _row,
)


def _config(candidate: CandidateProfile | None = None, **notif_overrides) -> Config:
    return Config(
        watchlist=[WatchlistCompany(name="Acme", company_priority=1)],
        candidate=candidate or CandidateProfile(),
        notifications=NotificationConfig(**notif_overrides),
    )


# --------------------------------------------------------------------------- #
# out_of_region: stored, classified, but never enqueued (individual OR digest).
# --------------------------------------------------------------------------- #
def test_out_of_region_job_is_stored_but_never_normally_alerted():
    conn = _conn_past_baseline()
    config = _config()
    calls: list = []
    notifier = _notifier(calls=calls)
    row = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1", location="Los Angeles, CA")

    stats = run_once(conn, config, [_collector([row])], None, notifier, now=NOW)

    assert stats.jobs_created == 1
    assert stats.jobs_classified == 1
    assert stats.alerts_enqueued == 0    # never enqueued, individual or digest
    assert stats.alerts_sent == 0
    assert calls == []

    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert rec.eligibility_status.value == "eligible"          # eligibility is unaffected
    assert rec.location_classification == LocationClassification.out_of_region
    assert db.get_alert(conn, rec.job_id, AlertChannel.discord) is None


# --------------------------------------------------------------------------- #
# unclear (e.g. missing location): stored, classified, suppressed -- and, per
# the clarified policy, an otherwise-eligible job with unclear location is
# acceptable to suppress WITHOUT entering the review digest (the digest stays
# reserved for eligibility-review jobs; see the module docstring / final report).
# --------------------------------------------------------------------------- #
def test_unclear_location_job_is_stored_and_suppressed_without_digest():
    conn = _conn_past_baseline()
    config = _config()
    calls: list = []
    notifier = _notifier(calls=calls)
    row = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1", location=None)

    stats = run_once(conn, config, [_collector([row])], None, notifier, now=NOW)

    assert stats.alerts_enqueued == 0
    assert calls == []

    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert rec.eligibility_status.value == "eligible"
    assert rec.location_classification == LocationClassification.unclear
    assert db.get_alert(conn, rec.job_id, AlertChannel.discord) is None


# --------------------------------------------------------------------------- #
# A genuinely eligibility-review job still reaches the digest regardless of its
# location classification -- location only ever gates the "eligible" alert path.
# --------------------------------------------------------------------------- #
def test_review_status_job_with_out_of_region_location_still_reaches_digest():
    conn = _conn_past_baseline()
    config = _config()
    calls: list = []
    notifier = _notifier(calls=calls)
    row = _row(REVIEW_TITLE, REVIEW_DESC, job_id="1", location="New York, NY")

    stats = run_once(conn, config, [_collector([row])], None, notifier, now=NOW)

    assert stats.alerts_enqueued == 1
    assert stats.review_digest_sent is True
    assert len(calls) == 1  # the digest

    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert rec.eligibility_status.value == "review"
    assert rec.location_classification == LocationClassification.out_of_region
    assert db.get_alert(conn, rec.job_id, AlertChannel.discord)["state"] == AlertState.sent.value


# --------------------------------------------------------------------------- #
# remote_acceptable: still alertable, but at a downgraded ("penalized") priority.
# --------------------------------------------------------------------------- #
def test_remote_acceptable_job_is_alerted_with_priority_penalty():
    conn = _conn_past_baseline()
    config = _config()  # default discord_min_priority=p2, remote_alert_priority_penalty=1
    calls: list = []
    notifier = _notifier(calls=calls)
    row = _row(
        ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1", location="Remote - United States"
    )

    stats = run_once(conn, config, [_collector([row])], None, notifier, now=NOW)

    assert stats.alerts_enqueued == 1
    assert stats.alerts_sent == 1
    assert len(calls) == 1

    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert rec.location_classification == LocationClassification.remote_acceptable
    # Would have been p1 (watchlist p1, recent, fulltime new-grad) without the
    # remote penalty; downgraded by remote_alert_priority_penalty=1 -> p2.
    assert rec.alert_priority.value == "p2"


# --------------------------------------------------------------------------- #
# candidate.allow_us_remote=False: remote_acceptable jobs are never alerted.
# --------------------------------------------------------------------------- #
def test_allow_us_remote_false_suppresses_remote_alert():
    conn = _conn_past_baseline()
    config = _config(candidate=CandidateProfile(allow_us_remote=False))
    calls: list = []
    notifier = _notifier(calls=calls)
    row = _row(
        ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1", location="Remote - United States"
    )

    stats = run_once(conn, config, [_collector([row])], None, notifier, now=NOW)

    assert stats.alerts_enqueued == 0
    assert calls == []
    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert rec.location_classification == LocationClassification.remote_acceptable
    assert db.get_alert(conn, rec.job_id, AlertChannel.discord) is None


# --------------------------------------------------------------------------- #
# candidate.location_required_for_alert=False: legacy (ungated) behavior.
# --------------------------------------------------------------------------- #
def test_location_required_for_alert_false_restores_legacy_behavior():
    conn = _conn_past_baseline()
    config = _config(candidate=CandidateProfile(location_required_for_alert=False))
    calls: list = []
    notifier = _notifier(calls=calls)
    row = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1", location="Los Angeles, CA")

    stats = run_once(conn, config, [_collector([row])], None, notifier, now=NOW)

    assert stats.alerts_enqueued == 1
    assert stats.alerts_sent == 1
    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert rec.location_classification == LocationClassification.out_of_region
    assert db.get_alert(conn, rec.job_id, AlertChannel.discord)["state"] == AlertState.sent.value


# --------------------------------------------------------------------------- #
# Report: alert counts by location classification (offline integration; see the
# final report for the same breakdown reproduced against the full config).
# --------------------------------------------------------------------------- #
def test_report_alert_counts_by_location_classification(capsys):
    conn = _conn_past_baseline()
    config = _config()
    calls: list = []
    notifier = _notifier(calls=calls)
    rows = [
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="pref1", location="San Francisco, CA"),
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="pref2", location="Silicon Valley"),
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="remote1", location="Remote - United States"),
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="ool1", location="Los Angeles, CA"),
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="ool2", location="New York, NY"),
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="unclear1", location=None),
    ]
    stats = run_once(conn, config, [_collector(rows)], None, notifier, now=NOW)

    counts: dict[str, int] = {}
    for job_id in ("pref1", "pref2", "remote1", "ool1", "ool2", "unclear1"):
        rec = db.get_job_by_canonical(conn, f"https://boards.greenhouse.io/acme/jobs/{job_id}")
        counts[rec.location_classification.value] = counts.get(rec.location_classification.value, 0) + 1

    print(
        f"jobs_seen={stats.jobs_seen} alerts_enqueued={stats.alerts_enqueued} "
        f"alerts_sent={stats.alerts_sent} location_counts={counts}"
    )

    assert counts == {"preferred": 2, "remote_acceptable": 1, "out_of_region": 2, "unclear": 1}
    # Only the 2 preferred + 1 remote_acceptable are ever alerted (3 individual sends).
    assert stats.alerts_enqueued == 3
    assert stats.alerts_sent == 3

    captured = capsys.readouterr()
    assert "location_counts=" in captured.out
