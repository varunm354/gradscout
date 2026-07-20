from datetime import datetime, timedelta, timezone

import pytest

from gradscout import db
from gradscout.models import (
    AlertChannel,
    AlertState,
    EligibilityStatus,
    Job,
    SourceStatus,
    SourceType,
)
from gradscout.urls import canonicalize_url


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def make_job(
    *,
    source: SourceType,
    source_company: str,
    apply_url: str,
    source_job_id: str | None = None,
    company: str = "Acme",
    title: str = "Software Engineer, New Grad",
    company_priority: int = 1,
) -> Job:
    return Job(
        source=source,
        source_company=source_company,
        source_job_id=source_job_id,
        apply_url=apply_url,
        company=company,
        company_priority=company_priority,
        title=title,
        url_canonical=canonicalize_url(apply_url),
    )


# --------------------------------------------------------------------------- #
# Cross-source dedupe
# --------------------------------------------------------------------------- #
def test_same_posting_from_two_sources_merges_by_canonical_url(conn):
    ats = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="555",
        apply_url="https://boards.greenhouse.io/acme/jobs/555",
    )
    repo = make_job(
        source=SourceType.github_repo,
        source_company="SimplifyJobs",
        apply_url="https://www.boards.greenhouse.io/acme/jobs/555/?utm_source=github",
    )

    job_id_1, created_1 = db.upsert_job(conn, ats)
    job_id_2, created_2 = db.upsert_job(conn, repo)

    assert created_1 is True
    assert created_2 is False           # merged, not a new job
    assert job_id_1 == job_id_2
    assert db.count_jobs(conn) == 1

    rec = db.get_job(conn, job_id_1)
    # both source records preserved
    assert len(rec.sources) == 2
    kinds = {s.source for s in rec.sources}
    assert kinds == {SourceType.greenhouse, SourceType.github_repo}


def test_stable_source_identity_dedupes_across_url_change(conn):
    first = make_job(
        source=SourceType.lever,
        source_company="acme",
        source_job_id="abc-123",
        apply_url="https://jobs.lever.co/acme/abc-123",
    )
    # same stable id, different (canonicalized) URL -> still one job, one source row
    again = make_job(
        source=SourceType.lever,
        source_company="acme",
        source_job_id="abc-123",
        apply_url="https://jobs.lever.co/acme/abc-123/apply",
    )
    jid1, _ = db.upsert_job(conn, first)
    jid2, created = db.upsert_job(conn, again)
    assert jid1 == jid2
    assert created is False
    assert db.count_jobs(conn) == 1
    rec = db.get_job(conn, jid1)
    assert len(rec.sources) == 1


def test_reupsert_preserves_first_seen_updates_last_seen(conn):
    t0 = datetime(2027, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=3)
    job = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    jid, _ = db.upsert_job(conn, job, now=t0)
    db.upsert_job(conn, job, now=t1)
    rec = db.get_job(conn, jid)
    assert rec.first_seen_at == t0
    assert rec.last_seen_at == t1


def test_distinct_postings_are_separate_jobs(conn):
    a = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    b = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="2",
        apply_url="https://boards.greenhouse.io/acme/jobs/2",
    )
    db.upsert_job(conn, a)
    db.upsert_job(conn, b)
    assert db.count_jobs(conn) == 2


def test_classification_update_on_merge(conn):
    job = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    jid, _ = db.upsert_job(conn, job)
    assert db.get_job(conn, jid).eligibility_status == EligibilityStatus.unclassified

    job.eligibility_status = EligibilityStatus.eligible
    job.eligibility_reasons = ["explicit new grad language"]
    db.upsert_job(conn, job)
    rec = db.get_job(conn, jid)
    assert rec.eligibility_status == EligibilityStatus.eligible
    assert rec.eligibility_reasons == ["explicit new grad language"]


# --------------------------------------------------------------------------- #
# Source health
# --------------------------------------------------------------------------- #
def test_source_health_success_then_failure_preserves_last_success(conn):
    t0 = datetime(2027, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)
    db.record_source_result(
        conn, "greenhouse:stripe", "Stripe", 1, SourceStatus.ok, jobs_seen=4, now=t0
    )
    db.record_source_result(
        conn, "greenhouse:stripe", "Stripe", 1, SourceStatus.error,
        error="timeout", now=t1,
    )
    rows = {r["source_id"]: r for r in db.get_source_health(conn)}
    h = rows["greenhouse:stripe"]
    assert h["last_status"] == "error"
    assert h["last_error"] == "timeout"
    assert h["last_check_at"] == t1.isoformat()
    assert h["last_success_at"] == t0.isoformat()   # preserved across failure


# --------------------------------------------------------------------------- #
# Alerts: pending -> sent
# --------------------------------------------------------------------------- #
def test_alert_starts_pending_and_becomes_sent_only_on_mark(conn):
    job = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    jid, _ = db.upsert_job(conn, job)

    assert db.enqueue_alert(conn, jid, AlertChannel.discord, "p1") is True
    assert db.get_alert(conn, jid, AlertChannel.discord)["state"] == AlertState.pending.value

    pending = db.get_pending_alerts(conn, AlertChannel.discord)
    assert [p["job_id"] for p in pending] == [jid]

    assert db.mark_alert_sent(conn, jid, AlertChannel.discord) is True
    assert db.get_alert(conn, jid, AlertChannel.discord)["state"] == AlertState.sent.value
    assert db.get_pending_alerts(conn, AlertChannel.discord) == []


def test_enqueue_is_idempotent_never_duplicates(conn):
    job = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    jid, _ = db.upsert_job(conn, job)
    assert db.enqueue_alert(conn, jid, AlertChannel.discord, "p1") is True
    assert db.enqueue_alert(conn, jid, AlertChannel.discord, "p1") is False  # no dup
    db.mark_alert_sent(conn, jid, AlertChannel.discord)
    # already sent -> still no re-enqueue
    assert db.enqueue_alert(conn, jid, AlertChannel.discord, "p1") is False


def test_mark_sent_is_noop_when_not_pending(conn):
    job = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    jid, _ = db.upsert_job(conn, job)
    # nothing enqueued yet
    assert db.mark_alert_sent(conn, jid, AlertChannel.discord) is False


def test_pending_alerts_ordered_by_company_priority(conn):
    low = make_job(
        source=SourceType.greenhouse, source_company="c", source_job_id="9",
        apply_url="https://boards.greenhouse.io/c/jobs/9", company_priority=3,
        company="LowCo",
    )
    high = make_job(
        source=SourceType.greenhouse, source_company="s", source_job_id="1",
        apply_url="https://boards.greenhouse.io/s/jobs/1", company_priority=1,
        company="HighCo",
    )
    jid_low, _ = db.upsert_job(conn, low)
    jid_high, _ = db.upsert_job(conn, high)
    db.enqueue_alert(conn, jid_low, AlertChannel.discord, "p2")
    db.enqueue_alert(conn, jid_high, AlertChannel.discord, "p1")
    order = [p["job_id"] for p in db.get_pending_alerts(conn, AlertChannel.discord)]
    assert order == [jid_high, jid_low]   # highest priority company first
