"""Baseline bootstrap regression tests (Phase 5.2).

A fresh database's very first successful run must store every collected job
normally (classified, persisted, queryable) but must NOT flood Discord with
the entire historical backlog already open on the first crawl. Only a
genuinely-recent P1 job (source-provided ``source_posted_at`` within
``new_grad_recent_hours``) is ever alerted during this baseline run; review
items never enter the digest backlog during baseline either. Every later run
(once the baseline meta key is durably set) goes through the ordinary
created/changed alerting rules, unaffected.

Reuses the offline fixtures/helpers from tests/test_pipeline.py (fake
collectors, MockTransport-backed Discord notifier, in-memory DB) rather than
duplicating them.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from gradscout import db
from gradscout.models import AlertChannel, AlertState
from gradscout.pipeline import run_once
from tests.test_pipeline import (
    ELIGIBLE_DESC,
    ELIGIBLE_TITLE,
    NOW,
    REVIEW_DESC,
    REVIEW_TITLE,
    _collector,
    _config,
    _conn,
    _notifier,
    _row,
)

OLD = NOW - timedelta(days=30)  # a "historical" posting, well outside any recent window


# --------------------------------------------------------------------------- #
# 1) First run: stores jobs normally, no historical alert flood.
# --------------------------------------------------------------------------- #
def test_first_run_stores_jobs_without_historical_alert_flood():
    conn = _conn()
    config = _config()
    assert db.is_baseline_complete(conn) is False

    calls: list = []
    notifier = _notifier(calls=calls)
    historical_eligible = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="hist1", posted_at=OLD)
    historical_review = _row(REVIEW_TITLE, REVIEW_DESC, job_id="hist2", posted_at=OLD)
    genuinely_recent_p1 = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="hist3", posted_at=NOW)

    stats = run_once(
        conn, config,
        [_collector([historical_eligible, historical_review, genuinely_recent_p1])],
        None, notifier, now=NOW,
    )

    assert stats.baseline_run is True
    assert stats.baseline_completed is True
    assert stats.jobs_seen == 3
    assert stats.jobs_created == 3
    assert stats.jobs_classified == 3  # every job is still classified and stored normally

    # Only the genuinely-recent P1 job is ever alerted; the historical
    # p2-eligible job and the review job are stored but never enqueued.
    assert stats.alerts_enqueued == 1
    assert stats.alerts_sent == 1
    assert stats.alerts_pending == 0
    assert len(calls) == 1  # exactly one Discord message, never a digest or per-job flood

    hist_rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/hist1")
    assert hist_rec.eligibility_status.value == "eligible"  # stored/classified normally
    assert db.get_alert(conn, hist_rec.job_id, AlertChannel.discord) is None  # never enqueued

    review_rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/hist2")
    assert review_rec.eligibility_status.value == "review"
    assert db.get_alert(conn, review_rec.job_id, AlertChannel.discord) is None  # no digest backlog

    p1_rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/hist3")
    assert p1_rec.alert_priority.value == "p1"
    assert db.get_alert(conn, p1_rec.job_id, AlertChannel.discord)["state"] == AlertState.sent.value

    assert db.is_baseline_complete(conn) is True


# --------------------------------------------------------------------------- #
# 2) Second, unchanged run creates no alerts.
# --------------------------------------------------------------------------- #
def test_second_unchanged_run_creates_no_alerts():
    conn = _conn()
    config = _config()
    row = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1", posted_at=OLD)

    stats1 = run_once(conn, config, [_collector([row])], None, _notifier(), now=NOW)
    assert stats1.baseline_run is True
    assert stats1.alerts_enqueued == 0  # historical, not recent -> suppressed during baseline
    assert db.is_baseline_complete(conn) is True

    later = NOW + timedelta(hours=1)
    calls: list = []
    stats2 = run_once(conn, config, [_collector([row])], None, _notifier(calls=calls), now=later)

    assert stats2.baseline_run is False  # baseline already completed by run 1
    assert stats2.jobs_created == 0
    assert stats2.jobs_changed == 0
    assert stats2.jobs_unchanged == 1
    assert stats2.jobs_classified == 0  # unchanged: never reclassified
    assert stats2.alerts_enqueued == 0
    assert calls == []


# --------------------------------------------------------------------------- #
# 3) A genuinely new job on a later (third) run creates an alert.
# --------------------------------------------------------------------------- #
def test_third_run_genuinely_new_job_creates_an_alert():
    conn = _conn()
    config = _config()
    baseline_row = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1", posted_at=OLD)

    run_once(conn, config, [_collector([baseline_row])], None, _notifier(), now=NOW)  # run 1: baseline
    assert db.is_baseline_complete(conn) is True

    t1 = NOW + timedelta(hours=1)
    stats2 = run_once(conn, config, [_collector([baseline_row])], None, _notifier(), now=t1)  # run 2: unchanged
    assert stats2.alerts_enqueued == 0

    t2 = NOW + timedelta(hours=2)
    new_row = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="2", posted_at=t1)  # newly discovered + recent
    calls: list = []
    stats3 = run_once(
        conn, config, [_collector([baseline_row, new_row])], None, _notifier(calls=calls), now=t2
    )

    assert stats3.baseline_run is False
    assert stats3.jobs_created == 1       # only the new job
    assert stats3.jobs_unchanged == 1     # the pre-existing baseline job, untouched
    assert stats3.alerts_enqueued == 1
    assert stats3.alerts_sent == 1
    assert len(calls) == 1

    new_rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/2")
    assert db.get_alert(conn, new_rec.job_id, AlertChannel.discord)["state"] == AlertState.sent.value


# --------------------------------------------------------------------------- #
# 4) A materially changed existing job (after baseline) follows the ordinary
#    (non-baseline) alert rule, not the baseline-suppression rule.
# --------------------------------------------------------------------------- #
def test_materially_changed_job_after_baseline_follows_normal_alert_rule():
    conn = _conn()
    config = _config()
    original = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1", posted_at=OLD)

    run_once(conn, config, [_collector([original])], None, _notifier(), now=NOW)  # baseline: historical, suppressed
    assert db.is_baseline_complete(conn) is True
    hist_rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert db.get_alert(conn, hist_rec.job_id, AlertChannel.discord) is None

    edited = _row(
        ELIGIBLE_TITLE, ELIGIBLE_DESC + " Now hiring urgently across teams.",
        job_id="1", posted_at=OLD,
    )
    later = NOW + timedelta(hours=1)
    calls: list = []
    stats2 = run_once(conn, config, [_collector([edited])], None, _notifier(calls=calls), now=later)

    assert stats2.baseline_run is False
    assert stats2.jobs_changed == 1
    # Still not recent (posted_at unchanged) -> p2, but p2 meets the default
    # discord_min_priority (p2) under the ORDINARY (non-baseline) rule, so a
    # materially edited existing job is enqueued individually post-baseline.
    assert stats2.alerts_enqueued == 1
    assert stats2.alerts_sent == 1
    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert rec.alert_priority.value == "p2"
    assert db.get_alert(conn, rec.job_id, AlertChannel.discord)["state"] == AlertState.sent.value


# --------------------------------------------------------------------------- #
# 5) A failed first run must not mark the baseline complete.
# --------------------------------------------------------------------------- #
def test_failed_first_run_does_not_mark_baseline_complete(monkeypatch):
    conn = _conn()
    config = _config()
    row = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1", posted_at=NOW)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure mid-pipeline")

    monkeypatch.setattr(db, "apply_classification", boom)

    with pytest.raises(RuntimeError, match="simulated failure mid-pipeline"):
        run_once(conn, config, [_collector([row])], None, _notifier(), now=NOW)

    assert db.is_baseline_complete(conn) is False


# --------------------------------------------------------------------------- #
# 6) A dry-run must report what baseline mode would do but never mark the
#    baseline durably complete.
# --------------------------------------------------------------------------- #
def test_dry_run_first_run_does_not_mark_baseline_complete():
    conn = _conn()
    config = _config()
    row = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1", posted_at=NOW)
    calls: list = []
    notifier = _notifier(dry_run=True, calls=calls)

    stats = run_once(conn, config, [_collector([row])], None, notifier, now=NOW)

    assert stats.baseline_run is True
    assert stats.alerts_enqueued == 1     # still reports what baseline mode would alert on
    assert stats.alerts_sent == 0         # dry-run: no real delivery
    assert stats.baseline_completed is False
    assert db.is_baseline_complete(conn) is False
    assert calls == []

    # A subsequent real (non-dry) run must still be treated as baseline,
    # since the dry-run never durably completed it.
    later = NOW + timedelta(hours=1)
    stats2 = run_once(conn, config, [_collector([row])], None, _notifier(), now=later)
    assert stats2.baseline_run is True
    assert stats2.baseline_completed is True
    assert db.is_baseline_complete(conn) is True


# --------------------------------------------------------------------------- #
# 7) Reported first-run vs. second-run alert counts (offline integration).
# --------------------------------------------------------------------------- #
def test_report_first_and_second_run_alert_counts(capsys):
    conn = _conn()
    config = _config()
    rows = [
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id=f"hist{i}", posted_at=OLD)
        for i in range(20)
    ]
    rows.append(_row(REVIEW_TITLE, REVIEW_DESC, job_id="review1", posted_at=OLD))
    rows.append(_row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="recent1", posted_at=NOW))

    stats1 = run_once(conn, config, [_collector(rows)], None, _notifier(), now=NOW)
    print(
        f"baseline (first) run: jobs_seen={stats1.jobs_seen}, "
        f"alerts_enqueued={stats1.alerts_enqueued}, alerts_sent={stats1.alerts_sent}, "
        f"baseline_completed={stats1.baseline_completed}"
    )
    # 20 historical p2-eligible + 1 review + 1 genuinely-recent p1 = 22 stored,
    # but only the single recent P1 job is ever alerted.
    assert stats1.jobs_created == 22
    assert stats1.alerts_enqueued == 1
    assert stats1.alerts_sent == 1
    assert stats1.baseline_completed is True

    later = NOW + timedelta(hours=1)
    new_row = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="new1", posted_at=later)
    stats2 = run_once(conn, config, [_collector(rows + [new_row])], None, _notifier(), now=later)
    print(
        f"second run: jobs_seen={stats2.jobs_seen}, jobs_created={stats2.jobs_created}, "
        f"jobs_unchanged={stats2.jobs_unchanged}, alerts_enqueued={stats2.alerts_enqueued}, "
        f"alerts_sent={stats2.alerts_sent}"
    )
    # All 22 baseline jobs are unchanged (no re-alert); only the single newly
    # discovered job produces an alert.
    assert stats2.jobs_created == 1
    assert stats2.jobs_unchanged == 22
    assert stats2.alerts_enqueued == 1
    assert stats2.alerts_sent == 1

    captured = capsys.readouterr()
    assert "baseline (first) run" in captured.out
    assert "second run" in captured.out


if __name__ == "__main__":
    pytest.main([__file__])
