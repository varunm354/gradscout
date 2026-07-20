from datetime import datetime, timedelta, timezone

import pytest

from gradscout import db
from gradscout.models import (
    AlertChannel,
    AlertPriority,
    AlertState,
    ChangeStatus,
    EligibilityStatus,
    EmploymentType,
    Job,
    ResolvedAnalysis,
    RoleFamily,
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

    r1 = db.upsert_job(conn, ats)
    r2 = db.upsert_job(conn, repo)

    assert r1.status == ChangeStatus.created
    assert r2.status == ChangeStatus.unchanged  # merged, not new, no content change
    assert r1.job_id == r2.job_id
    assert db.count_jobs(conn) == 1

    rec = db.get_job(conn, r1.job_id)
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
    r1 = db.upsert_job(conn, first)
    r2 = db.upsert_job(conn, again)
    assert r1.job_id == r2.job_id
    assert r2.status == ChangeStatus.unchanged
    assert db.count_jobs(conn) == 1
    rec = db.get_job(conn, r1.job_id)
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
    r1 = db.upsert_job(conn, job, now=t0)
    r2 = db.upsert_job(conn, job, now=t1)
    assert r2.status == ChangeStatus.unchanged
    rec = db.get_job(conn, r1.job_id)
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


# --------------------------------------------------------------------------- #
# Atomic created / changed / unchanged + classification persistence
# --------------------------------------------------------------------------- #
def _resolved(**overrides) -> ResolvedAnalysis:
    base = dict(
        eligibility_status=EligibilityStatus.eligible,
        eligibility_reasons=["explicit new grad language"],
        employment_type=EmploymentType.fulltime,
        is_new_grad=True,
        role_family=RoleFamily.backend,
        recommended_resume=None,
        resume_confidence=None,
        resume_reason=None,
        company_priority=1,
        role_priority=1,
        alert_priority=AlertPriority.p1,
        llm_used=False,
        decided_by="deterministic",
    )
    base.update(overrides)
    return ResolvedAnalysis(**base)


def test_upsert_alone_never_writes_classification(conn):
    """upsert_job is structural-only; a freshly created row stays
    'unclassified' until apply_classification runs."""
    job = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    r = db.upsert_job(conn, job)
    assert r.status == ChangeStatus.created
    assert db.get_job(conn, r.job_id).eligibility_status == EligibilityStatus.unclassified


def test_apply_classification_writes_resolved_fields(conn):
    job = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    r = db.upsert_job(conn, job)
    db.apply_classification(conn, r.job_id, _resolved())
    rec = db.get_job(conn, r.job_id)
    assert rec.eligibility_status == EligibilityStatus.eligible
    assert rec.eligibility_reasons == ["explicit new grad language"]
    assert rec.company_priority == 1
    assert rec.alert_priority == AlertPriority.p1


def test_reupsert_identical_content_is_unchanged_and_preserves_classification(conn):
    job = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        title="Software Engineer, New Grad",
    )
    r1 = db.upsert_job(conn, job)
    db.apply_classification(conn, r1.job_id, _resolved())

    # Re-seen with byte-identical analysis-relevant content -> unchanged, and
    # the previously applied classification must be left completely alone.
    same_job = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        title="Software Engineer, New Grad",
    )
    r2 = db.upsert_job(conn, same_job)
    assert r2.status == ChangeStatus.unchanged
    rec = db.get_job(conn, r1.job_id)
    assert rec.eligibility_status == EligibilityStatus.eligible
    assert rec.alert_priority == AlertPriority.p1


def test_reupsert_with_edited_title_is_changed(conn):
    job = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        title="Software Engineer, New Grad",
    )
    r1 = db.upsert_job(conn, job)
    db.apply_classification(conn, r1.job_id, _resolved())

    edited = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        title="Senior Software Engineer",  # materially different
    )
    r2 = db.upsert_job(conn, edited)
    assert r2.status == ChangeStatus.changed
    rec = db.get_job(conn, r1.job_id)
    assert rec.title == "Senior Software Engineer"
    # structural update does not itself reclassify -- prior classification
    # is still in place until the caller reclassifies and calls
    # apply_classification again.
    assert rec.eligibility_status == EligibilityStatus.eligible


def test_reupsert_with_different_last_seen_only_is_unchanged(conn):
    """Volatile fields (here: only the upsert timestamp changes) must never
    flip a job to 'changed'."""
    job = make_job(
        source=SourceType.greenhouse,
        source_company="acme",
        source_job_id="1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
    )
    t0 = datetime(2027, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)
    db.upsert_job(conn, job, now=t0)
    r2 = db.upsert_job(conn, job, now=t1)
    assert r2.status == ChangeStatus.unchanged


# --------------------------------------------------------------------------- #
# Meta key/value store
# --------------------------------------------------------------------------- #
def test_meta_roundtrip_and_missing_key(conn):
    assert db.get_meta(conn, "daily_summary_last_date") is None
    db.set_meta(conn, "daily_summary_last_date", "2026-07-20")
    assert db.get_meta(conn, "daily_summary_last_date") == "2026-07-20"
    db.set_meta(conn, "daily_summary_last_date", "2026-07-21")
    assert db.get_meta(conn, "daily_summary_last_date") == "2026-07-21"


# --------------------------------------------------------------------------- #
# Source health: prior-row inspection for transition detection
# --------------------------------------------------------------------------- #
def test_get_source_health_one_returns_prior_row_before_overwrite(conn):
    assert db.get_source_health_one(conn, "greenhouse:stripe") is None
    db.record_source_result(conn, "greenhouse:stripe", "Stripe", 1, SourceStatus.ok, jobs_seen=3)
    prior = db.get_source_health_one(conn, "greenhouse:stripe")
    assert prior["last_status"] == "ok"
    db.record_source_result(conn, "greenhouse:stripe", "Stripe", 1, SourceStatus.error, error="down")
    # the row we already fetched is untouched (it was a snapshot copy)
    assert prior["last_status"] == "ok"
    assert db.get_source_health_one(conn, "greenhouse:stripe")["last_status"] == "error"


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
    jid = db.upsert_job(conn, job).job_id

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
    jid = db.upsert_job(conn, job).job_id
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
    jid = db.upsert_job(conn, job).job_id
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
    jid_low = db.upsert_job(conn, low).job_id
    jid_high = db.upsert_job(conn, high).job_id
    db.enqueue_alert(conn, jid_low, AlertChannel.discord, "p2")
    db.enqueue_alert(conn, jid_high, AlertChannel.discord, "p1")
    order = [p["job_id"] for p in db.get_pending_alerts(conn, AlertChannel.discord)]
    assert order == [jid_high, jid_low]   # highest priority company first
