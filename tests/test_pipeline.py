"""End-to-end offline pipeline tests.

Collectors are fakes with a hardcoded ``fetch``/``parse`` (no network); Discord
delivery goes through an injected httpx.Client wired to httpx.MockTransport
(no network). Every invariant from the Phase 4 spec gets at least one direct
assertion here.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import yaml

from gradscout import db
from gradscout.collectors.base import Collector
from gradscout.llm import JobAnalysisAgent, NullProvider
from gradscout.models import (
    AlertChannel,
    AlertState,
    Config,
    NotificationConfig,
    RawJob,
    SourceType,
    WatchlistCompany,
)
from gradscout.notify.discord import DiscordNotifier
from gradscout.pipeline import run_once

NOW = datetime(2027, 1, 10, 10, 0, tzinfo=timezone.utc)  # hour != default daily_summary_hour_utc

ELIGIBLE_TITLE = "Software Engineer, New Grad"
ELIGIBLE_DESC = "Backend APIs and distributed systems. Bachelor's degree."
REVIEW_TITLE = "Software Engineer"
REVIEW_DESC = "Backend APIs and services. 3+ years of experience required."
INELIGIBLE_TITLE = "Research Engineer"
INELIGIBLE_DESC = "PhD required in machine learning."


class FakeCollector(Collector):
    """A Collector whose fetch()/parse() never touch the network -- the
    payload is just the pre-built RawJob rows, and parse() passes them
    through (or raises, to simulate a total source failure)."""

    def __init__(self, *args, rows: list[RawJob] | None = None, should_fail: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self._rows = rows or []
        self._should_fail = should_fail

    def url(self) -> str:
        return f"fake://{self.source_id}"

    def fetch(self, client: Any) -> Any:
        if self._should_fail:
            raise RuntimeError("simulated source failure")
        return self._rows

    def parse(self, payload: Any) -> tuple[list[RawJob], int]:
        return list(payload), 0


def _row(
    title: str,
    desc: str,
    *,
    job_id: str,
    posted_at=NOW,
    company="Acme",
    location: str | None = "San Francisco, CA",
) -> RawJob:
    # Defaults to a preferred (Bay Area) location (Phase 5.2) so every existing
    # dedupe/baseline/priority assertion in this file and tests/test_baseline.py
    # (which reuses this helper) keeps passing unaffected by location gating --
    # see tests/test_location_pipeline.py for location-specific end-to-end cases.
    return RawJob(
        source=SourceType.greenhouse,
        source_company="acme",
        company=company,
        source_job_id=job_id,
        title=title,
        description_text=desc,
        apply_url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        source_posted_at=posted_at,
        location=location,
    )


def _config(**notif_overrides) -> Config:
    notifications = NotificationConfig(**notif_overrides)
    return Config(
        watchlist=[WatchlistCompany(name="Acme", company_priority=1)],
        notifications=notifications,
    )


def _collector(rows=None, should_fail=False, company_priority=1) -> FakeCollector:
    return FakeCollector(
        source_type=SourceType.greenhouse,
        company="Acme",
        slug="acme",
        company_priority=company_priority,
        rows=rows,
        should_fail=should_fail,
    )


def _conn():
    c = db.connect(":memory:")
    db.init_db(c)
    return c


def _conn_past_baseline():
    """A connection for tests exercising ORDINARY (post-baseline) alert
    routing, not baseline-bootstrap behavior itself (see tests/test_baseline.py
    for that). Phase 5.2's baseline bootstrap only narrows alerting on a
    fresh DB's very first successful run; pre-marking the baseline complete
    here keeps these tests' original routing-only intent unaffected."""
    c = _conn()
    db.mark_baseline_complete(c, now=NOW - timedelta(days=1))
    return c


def _notifier(status_code=204, dry_run=False, calls: list | None = None) -> DiscordNotifier:
    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status_code)

    client = None if dry_run else httpx.Client(transport=httpx.MockTransport(handler))
    return DiscordNotifier(webhook_url="https://discord.test/hook", dry_run=dry_run, client=client)


# --------------------------------------------------------------------------- #
# Full happy-path run: classification persisted, correct routing, one
# individual alert + one review digest (not one message per review job).
# --------------------------------------------------------------------------- #
def test_full_run_classifies_persists_and_routes_alerts():
    conn = _conn_past_baseline()
    config = _config()
    calls: list = []
    notifier = _notifier(calls=calls)
    rows = [
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1"),
        _row(REVIEW_TITLE, REVIEW_DESC, job_id="2"),
        _row(INELIGIBLE_TITLE, INELIGIBLE_DESC, job_id="3"),
    ]
    stats = run_once(conn, config, [_collector(rows)], None, notifier, now=NOW)

    assert stats.jobs_seen == 3
    assert stats.jobs_created == 3
    assert stats.jobs_classified == 3
    assert stats.alerts_enqueued == 2       # eligible (p1) + review (digest); ineligible never enqueued
    assert stats.alerts_sent == 1           # only the individual p1 alert
    assert stats.alerts_pending == 0
    assert stats.review_digest_sent is True
    assert stats.source_failures_notified == 0
    assert stats.source_recoveries_notified == 0

    # exactly 2 Discord messages: one job alert + one digest (never 3)
    assert len(calls) == 2

    eligible_rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert eligible_rec.alert_priority.value == "p1"
    assert eligible_rec.company_priority == 1
    assert db.get_alert(conn, eligible_rec.job_id, AlertChannel.discord)["state"] == AlertState.sent.value

    review_rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/2")
    assert review_rec.eligibility_status.value == "review"
    assert db.get_alert(conn, review_rec.job_id, AlertChannel.discord)["state"] == AlertState.sent.value

    ineligible_rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/3")
    assert ineligible_rec.eligibility_status.value == "ineligible"
    assert db.get_alert(conn, ineligible_rec.job_id, AlertChannel.discord) is None  # never enqueued


# --------------------------------------------------------------------------- #
# Unchanged jobs are never reclassified or re-alerted; edited content IS.
# --------------------------------------------------------------------------- #
def test_unchanged_job_is_not_reclassified_or_realerted():
    conn = _conn()
    config = _config()
    row = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1")

    stats1 = run_once(conn, config, [_collector([row])], None, _notifier(), now=NOW)
    assert stats1.jobs_created == 1
    assert stats1.jobs_classified == 1
    assert stats1.alerts_enqueued == 1

    # Re-seen with byte-identical content on a later run.
    later = NOW + timedelta(hours=1)
    stats2 = run_once(conn, config, [_collector([row])], None, _notifier(), now=later)
    assert stats2.jobs_created == 0
    assert stats2.jobs_changed == 0
    assert stats2.jobs_unchanged == 1
    assert stats2.jobs_classified == 0   # not reclassified
    assert stats2.alerts_enqueued == 0   # not re-alerted

    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert rec.last_seen_at == later     # last_seen_at still bumped


def test_materially_edited_job_is_reclassified():
    conn = _conn()
    config = _config()
    original = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1")
    run_once(conn, config, [_collector([original])], None, _notifier(), now=NOW)

    edited = _row(INELIGIBLE_TITLE, INELIGIBLE_DESC, job_id="1")  # same URL/id, new content
    later = NOW + timedelta(hours=1)
    stats2 = run_once(conn, config, [_collector([edited])], None, _notifier(), now=later)
    assert stats2.jobs_changed == 1
    assert stats2.jobs_classified == 1

    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert rec.eligibility_status.value == "ineligible"  # reclassified from the edited content


# --------------------------------------------------------------------------- #
# Per-run alert cap leaves the excess pending for a later run.
# --------------------------------------------------------------------------- #
def test_alert_cap_leaves_excess_pending():
    conn = _conn()
    config = _config(max_alerts_per_run=1)
    rows = [
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1"),
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="2", company="Acme"),
    ]
    # Give the two jobs distinct content so both are genuinely separate postings.
    rows[1] = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC + " Remote friendly.", job_id="2")

    stats = run_once(conn, config, [_collector(rows)], None, _notifier(), now=NOW)
    assert stats.alerts_enqueued == 2
    assert stats.alerts_sent == 1
    assert stats.alerts_pending == 1

    pending = db.get_pending_alerts(conn, AlertChannel.discord)
    assert len([p for p in pending if p["priority"] != "review"]) == 1


# --------------------------------------------------------------------------- #
# Discord failure/timeout/non-2xx leaves the alert pending, never marked sent.
# --------------------------------------------------------------------------- #
def test_discord_failure_leaves_alert_pending():
    conn = _conn()
    config = _config()
    row = _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1")
    notifier = _notifier(status_code=500)

    stats = run_once(conn, config, [_collector([row])], None, notifier, now=NOW)
    assert stats.alerts_enqueued == 1
    assert stats.alerts_sent == 0
    assert stats.alerts_pending == 1

    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert db.get_alert(conn, rec.job_id, AlertChannel.discord)["state"] == AlertState.pending.value


# --------------------------------------------------------------------------- #
# Dry-run: no Discord request at all, nothing ever marked sent.
# --------------------------------------------------------------------------- #
def test_dry_run_makes_no_discord_request_and_marks_nothing_sent():
    conn = _conn_past_baseline()
    config = _config()
    calls: list = []
    notifier = _notifier(dry_run=True, calls=calls)
    rows = [
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1"),
        _row(REVIEW_TITLE, REVIEW_DESC, job_id="2"),
    ]
    stats = run_once(conn, config, [_collector(rows)], None, notifier, now=NOW)

    assert calls == []
    assert stats.alerts_sent == 0
    assert stats.review_digest_sent is False
    assert stats.alerts_enqueued == 2  # enqueue still happens; only delivery is suppressed

    pending = db.get_pending_alerts(conn, AlertChannel.discord)
    assert len(pending) == 2  # both alerts still queryable as pending; nothing marked sent


# --------------------------------------------------------------------------- #
# High-priority source failure/recovery: transition-based, no hourly repeat.
# --------------------------------------------------------------------------- #
def test_source_failure_and_recovery_are_transition_based():
    conn = _conn()
    config = _config(daily_summary_hour_utc=23)  # avoid colliding with the +hours steps below
    calls: list = []
    notifier = _notifier(calls=calls)

    # Run 1: healthy (first-ever check) -> no transition notification.
    stats1 = run_once(conn, config, [_collector([])], None, notifier, now=NOW)
    assert stats1.source_failures_notified == 0
    assert len(calls) == 0

    # Run 2: healthy -> failed -> ONE immediate failure notification.
    t1 = NOW + timedelta(hours=1)
    stats2 = run_once(
        conn, config, [_collector([], should_fail=True)], None, notifier, now=t1
    )
    assert stats2.source_failures_notified == 1
    assert len(calls) == 1

    # Run 3: still failed -> NO repeated alert.
    t2 = NOW + timedelta(hours=2)
    stats3 = run_once(
        conn, config, [_collector([], should_fail=True)], None, notifier, now=t2
    )
    assert stats3.source_failures_notified == 0
    assert len(calls) == 1  # unchanged

    # Run 4: recovered -> ONE immediate recovery notification.
    t3 = NOW + timedelta(hours=3)
    stats4 = run_once(conn, config, [_collector([])], None, notifier, now=t3)
    assert stats4.source_recoveries_notified == 1
    assert len(calls) == 2

    recovery_embed = json.loads(calls[1].content)["embeds"][0]
    assert "recovered" in recovery_embed["title"].lower()


def test_low_priority_source_failure_never_notifies():
    conn = _conn()
    config = _config()
    calls: list = []
    notifier = _notifier(calls=calls)
    stats = run_once(
        conn, config, [_collector([], should_fail=True, company_priority=3)], None, notifier, now=NOW
    )
    assert stats.source_failures_notified == 0
    assert calls == []


# --------------------------------------------------------------------------- #
# Once-daily health summary: only at the configured hour, once per UTC day.
# --------------------------------------------------------------------------- #
def test_daily_summary_sent_once_at_configured_hour():
    conn = _conn()
    config = _config(daily_summary_hour_utc=13)
    calls: list = []
    notifier = _notifier(calls=calls)

    off_hour = NOW.replace(hour=10)
    stats_off = run_once(conn, config, [_collector([])], None, notifier, now=off_hour)
    assert stats_off.daily_summary_sent is False

    on_hour = NOW.replace(hour=13)
    stats_on = run_once(conn, config, [_collector([])], None, notifier, now=on_hour)
    assert stats_on.daily_summary_sent is True
    assert db.get_meta(conn, "daily_summary_last_date") == on_hour.strftime("%Y-%m-%d")

    # A second run within the SAME hour/day must not resend.
    stats_again = run_once(conn, config, [_collector([])], None, notifier, now=on_hour)
    assert stats_again.daily_summary_sent is False
    assert len([c for c in calls if b"daily health summary" in c.content]) == 1


# --------------------------------------------------------------------------- #
# Deterministic-only run (no LLM configured) still completes correctly.
# --------------------------------------------------------------------------- #
def test_runs_fully_deterministic_without_llm_agent():
    conn = _conn()
    config = _config()
    agent = JobAnalysisAgent(NullProvider())
    assert agent.enabled is False
    row = _row(REVIEW_TITLE, REVIEW_DESC, job_id="1")
    stats = run_once(conn, config, [_collector([row])], None, _notifier(), agent, now=NOW)
    assert stats.jobs_classified == 1
    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert rec.llm_used is False
    assert rec.eligibility_status.value == "review"


# --------------------------------------------------------------------------- #
# Phase 5.1: title-first relevance gating end-to-end -- a mixed batch of
# false-positive nontechnical titles (even with AI/ML-heavy descriptions),
# legitimate credible titles, and one ambiguous title. Only the legitimate
# titles should produce an individual (non-digest) alert; the false positives
# must never be enqueued at all (hard-ineligible), and the ambiguous title
# must land in the review digest, not a normal alert.
# --------------------------------------------------------------------------- #
def test_nontechnical_titles_never_generate_normal_alerts_end_to_end():
    conn = _conn_past_baseline()
    config = _config()
    calls: list = []
    notifier = _notifier(calls=calls)

    ai_heavy_desc = (
        "Leverage AI and machine learning, including LLMs and generative AI, "
        "to power our platform. PyTorch and MLOps experience a plus."
    )
    rows = [
        _row("Biological Safety Research Scientist", ai_heavy_desc, job_id="fp1"),
        _row("AI Compliance Officer", ai_heavy_desc, job_id="fp2"),
        _row("Data Scientist, Marketing", ai_heavy_desc, job_id="fp3"),
        _row("Backend Engineer, New Grad", ELIGIBLE_DESC, job_id="legit1"),
        _row("Machine Learning Engineer, New Grad", ELIGIBLE_DESC, job_id="legit2"),
        _row("Program Coordinator", "General office support.", job_id="ambig1"),
    ]
    stats = run_once(conn, config, [_collector(rows)], None, notifier, now=NOW)

    assert stats.jobs_seen == 6
    # Only the 2 legitimate individual alerts + 1 review digest are enqueued;
    # the 3 nontechnical false positives are hard-ineligible and never enqueued.
    assert stats.alerts_enqueued == 3
    assert stats.alerts_sent == 2  # the 2 individual legitimate alerts
    assert stats.review_digest_sent is True

    # Exactly 3 Discord messages: 2 individual alerts + 1 digest (never one
    # per false-positive or per ambiguous job).
    assert len(calls) == 3

    for job_id in ("fp1", "fp2", "fp3"):
        rec = db.get_job_by_canonical(conn, f"https://boards.greenhouse.io/acme/jobs/{job_id}")
        assert rec.eligibility_status.value == "ineligible"
        assert db.get_alert(conn, rec.job_id, AlertChannel.discord) is None  # never enqueued

    for job_id in ("legit1", "legit2"):
        rec = db.get_job_by_canonical(conn, f"https://boards.greenhouse.io/acme/jobs/{job_id}")
        assert rec.eligibility_status.value == "eligible"
        assert db.get_alert(conn, rec.job_id, AlertChannel.discord)["state"] == AlertState.sent.value

    ambig_rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/ambig1")
    assert ambig_rec.eligibility_status.value == "review"
    assert db.get_alert(conn, ambig_rec.job_id, AlertChannel.discord)["state"] == AlertState.sent.value


# --------------------------------------------------------------------------- #
# Phase 5.3 postmortem fix: production reported alerts_enqueued=6,
# alerts_sent=0, alerts_pending=0 after a Discord 400 on a review digest --
# investigate whether that was a stats bug or real alert loss, and prove the
# no-lost-alerts invariant holds through the full pipeline end to end.
# --------------------------------------------------------------------------- #
def _review_job_id(i: int, *, long: bool) -> str:
    # A long, unique job id (reflected verbatim into apply_url) is what
    # actually inflates a review-digest FIELD's value -- title/description
    # stay exactly REVIEW_TITLE/REVIEW_DESC (unaffected) so classification
    # reliably still resolves to Eligibility Review either way.
    return f"{i}" + ("u" * 700) if long else str(i)


def _review_apply_url(i: int, *, long: bool = True) -> str:
    return f"https://boards.greenhouse.io/acme/jobs/{_review_job_id(i, long=long)}"


def _review_row(i: int, *, long: bool = True) -> RawJob:
    """A row that classifies as Eligibility Review (same title/experience-
    ambiguity signal as REVIEW_TITLE/REVIEW_DESC that existing tests in this
    file already rely on), with a long company name + long job id (reflected
    into apply_url) so 6 of these reproduce the exact production
    review-digest payload-size failure once each is stored as a JobRecord --
    each contributes exactly MAX_FIELD_NAME_LEN + MAX_FIELD_VALUE_LEN
    characters to the digest embed, verified empirically to require 2
    Discord messages for 6 of them and fit in 1 for 5."""
    job_id = _review_job_id(i, long=long)
    company = "Company " + ("C" * 250) if long else "Acme"
    return _row(REVIEW_TITLE, REVIEW_DESC, job_id=job_id, company=company)


def test_review_digest_failure_leaves_all_six_alerts_pending_not_lost():
    """Reproduces the production incident: 6 long review-eligible jobs whose
    digest would (pre-fix) have been rejected by Discord for exceeding the
    6000-char aggregate limit. With a notifier that rejects every send (worst
    case), confirms: no alert is ever marked sent, every alert is still
    genuinely pending in the DB, alerts_pending reflects that truthfully, and
    notification_delivery_failures makes the partial failure explicit in the
    run's own stats -- i.e. this is provably NOT alert loss."""
    conn = _conn_past_baseline()
    # All 6 rows share one (long) company name -- override the Phase 6
    # per-company review cap so this test keeps isolating Discord payload
    # chunking/pending-alert bookkeeping; company-diversity behavior itself
    # is covered separately in tests/test_diversify.py.
    config = _config(max_review_items_per_company_per_run=100)
    rows = [_review_row(i) for i in range(6)]
    notifier = _notifier(status_code=400)  # simulate the production Discord 400

    stats = run_once(conn, config, [_collector(rows)], None, notifier, now=NOW)

    assert stats.alerts_enqueued == 6
    assert stats.alerts_sent == 0
    # THE FIX: alerts_pending is a ground-truth DB query, so it correctly
    # shows all 6 review alerts are still pending -- never the misleading 0
    # that production reported.
    assert stats.alerts_pending == 6
    assert stats.review_digest_sent is False
    assert stats.review_digest_chunks_sent == 0
    assert stats.review_digest_chunks_failed >= 1
    assert stats.notification_delivery_failures >= 1

    pending = db.get_pending_alerts(conn, AlertChannel.discord)
    assert len(pending) == 6
    for i in range(6):
        rec = db.get_job_by_canonical(conn, _review_apply_url(i))
        alert = db.get_alert(conn, rec.job_id, AlertChannel.discord)
        assert alert["state"] == AlertState.pending.value  # never lost, never falsely marked sent
        assert alert["sent_at"] is None


def test_review_digest_partial_chunk_failure_marks_only_delivered_chunk_sent():
    """6 long review jobs split into 2 digest chunks; the first chunk's HTTP
    request succeeds and the second fails. Only the jobs in the successful
    chunk are marked sent; the rest remain pending, and alerts_pending/
    notification_delivery_failures reflect the partial failure accurately."""
    conn = _conn_past_baseline()
    config = _config(max_review_items_per_company_per_run=100)  # see prior test's note
    rows = [_review_row(i) for i in range(6)]

    statuses = iter([204, 400])
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(next(statuses))

    notifier = DiscordNotifier(
        webhook_url="https://discord.test/hook",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    stats = run_once(conn, config, [_collector(rows)], None, notifier, now=NOW)

    assert stats.alerts_enqueued == 6
    assert len(calls) == 2  # exactly 2 chunked digest messages, never one oversized message
    assert stats.review_digest_chunks_sent == 1
    assert stats.review_digest_chunks_failed == 1
    assert stats.review_digest_sent is False  # NOT fully sent -- one chunk failed
    assert stats.notification_delivery_failures == 1

    pending = db.get_pending_alerts(conn, AlertChannel.discord)
    sent_job_ids = set()
    for i in range(6):
        rec = db.get_job_by_canonical(conn, _review_apply_url(i))
        alert = db.get_alert(conn, rec.job_id, AlertChannel.discord)
        if alert["state"] == AlertState.sent.value:
            sent_job_ids.add(i)
    # Exactly the delivered chunk's jobs are sent; the rest are pending.
    assert len(sent_job_ids) == 5
    assert len(pending) == 1
    # Ground-truth pending count matches the DB exactly.
    assert stats.alerts_pending == len(pending)


def test_successful_review_digest_marks_alert_sent_exactly_once():
    conn = _conn_past_baseline()
    config = _config()
    row = _review_row(1, long=False)
    calls: list = []
    notifier = _notifier(calls=calls)

    stats = run_once(conn, config, [_collector([row])], None, notifier, now=NOW)
    assert stats.review_digest_sent is True
    assert stats.notification_delivery_failures == 0

    rec = db.get_job_by_canonical(conn, _review_apply_url(1, long=False))
    alert = db.get_alert(conn, rec.job_id, AlertChannel.discord)
    assert alert["state"] == AlertState.sent.value
    first_sent_at = alert["sent_at"]

    # A second run must never re-send or re-mark an already-sent alert.
    later = NOW + timedelta(hours=1)
    stats2 = run_once(conn, config, [_collector([])], None, notifier, now=later)
    assert stats2.alerts_sent == 0
    assert stats2.review_digest_sent is False  # nothing left pending to digest
    alert_again = db.get_alert(conn, rec.job_id, AlertChannel.discord)
    assert alert_again["sent_at"] == first_sent_at  # unchanged: marked sent exactly once
    assert len(calls) == 1  # still just the one original digest message


def test_dry_run_review_digest_performs_no_http_calls_and_changes_no_sent_state():
    conn = _conn_past_baseline()
    config = _config(max_review_items_per_company_per_run=100)  # see note above
    rows = [_review_row(i) for i in range(6)]
    calls: list = []
    notifier = _notifier(dry_run=True, calls=calls)

    stats = run_once(conn, config, [_collector(rows)], None, notifier, now=NOW)

    assert calls == []  # dry-run makes no HTTP calls at all
    assert stats.alerts_enqueued == 6
    assert stats.alerts_sent == 0
    assert stats.review_digest_sent is False
    assert stats.review_digest_chunks_sent == 0
    assert stats.review_digest_chunks_failed == 0  # dry-run is a deliberate skip, never a failure
    assert stats.notification_delivery_failures == 0
    assert stats.alerts_pending == 6

    pending = db.get_pending_alerts(conn, AlertChannel.discord)
    assert len(pending) == 6
    for row in pending:
        rec = db.get_job_by_canonical(conn, row["apply_url"])
        alert = db.get_alert(conn, rec.job_id, AlertChannel.discord)
        assert alert["state"] == AlertState.pending.value
        assert alert["sent_at"] is None


def test_notification_delivery_failures_visible_alongside_individual_alert_failure():
    """A mixed run (one individual alert succeeds to send, the review digest
    fails) must surface the partial failure explicitly via
    notification_delivery_failures, never silently as an all-zero, fully
    healthy-looking run."""
    conn = _conn_past_baseline()
    config = _config(max_review_items_per_company_per_run=100)  # see note above
    rows = [
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="ok1"),
        *[_review_row(i) for i in range(6)],
    ]
    notifier = _notifier(status_code=400)  # everything this run is rejected

    stats = run_once(conn, config, [_collector(rows)], None, notifier, now=NOW)

    assert stats.alerts_enqueued == 7
    assert stats.alerts_sent == 0
    assert stats.notification_delivery_failures >= 2  # the individual alert AND >=1 digest chunk
    assert stats.alerts_pending == len(db.get_pending_alerts(conn, AlertChannel.discord))
    assert stats.alerts_pending == 7


# --------------------------------------------------------------------------- #
# Phase 6: company-diversity cap + explicit suppression (integration).
# --------------------------------------------------------------------------- #
def test_company_cap_suppresses_excess_alerts_from_one_company_explicitly():
    """5 eligible jobs from ONE prolific company (e.g. an OpenAI-style flood)
    with the default per-company cap of 3: only 3 are sent, and the other 2
    are explicitly transitioned to AlertState.suppressed (never left
    pending, never silently dropped) with reason 'suppressed_company_cap'."""
    conn = _conn_past_baseline()
    config = _config()  # default max_alerts_per_company_per_run == 3
    rows = [
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC + f" Variant {i}.", job_id=str(i), company="OpenAI")
        for i in range(5)
    ]
    stats = run_once(conn, config, [_collector(rows)], None, _notifier(), now=NOW)

    assert stats.alerts_enqueued == 5
    assert stats.alerts_sent == 3
    assert stats.alerts_suppressed_company_cap == 2
    assert stats.alerts_pending == 0  # nothing left ambiguously pending

    states = []
    for i in range(5):
        rec = db.get_job_by_canonical(conn, f"https://boards.greenhouse.io/acme/jobs/{i}")
        states.append(db.get_alert(conn, rec.job_id, AlertChannel.discord)["state"])
    assert states.count(AlertState.sent.value) == 3
    assert states.count(AlertState.suppressed.value) == 2

    # The suppression reason is explicit/auditable, not merely a status flag.
    suppressed_reasons = [
        db.get_alert(conn, db.get_job_by_canonical(conn, f"https://boards.greenhouse.io/acme/jobs/{i}").job_id, AlertChannel.discord)["suppressed_reason"]
        for i in range(5)
        if db.get_alert(conn, db.get_job_by_canonical(conn, f"https://boards.greenhouse.io/acme/jobs/{i}").job_id, AlertChannel.discord)["state"] == AlertState.suppressed.value
    ]
    assert suppressed_reasons == ["suppressed_company_cap", "suppressed_company_cap"]


def test_suppressed_alert_never_resurfaces_on_an_unchanged_rerun():
    """A suppressed job must NOT come back as pending/spam just because
    another run happens -- only a material content change can reconsider it
    (the existing created/changed change-detection gate)."""
    conn = _conn_past_baseline()
    config = _config()
    rows = [
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC + f" Variant {i}.", job_id=str(i), company="OpenAI")
        for i in range(5)
    ]
    run_once(conn, config, [_collector(rows)], None, _notifier(), now=NOW)

    later = NOW + timedelta(hours=1)
    stats2 = run_once(conn, config, [_collector(rows)], None, _notifier(), now=later)
    assert stats2.jobs_unchanged == 5
    assert stats2.alerts_enqueued == 0
    assert stats2.alerts_sent == 0
    assert stats2.alerts_suppressed_company_cap == 0  # nothing new to (re-)suppress
    assert stats2.alerts_pending == 0


def test_materially_changed_suppressed_job_becomes_reconsiderable():
    """A suppressed job whose content later materially changes goes back
    through eligibility/enqueue normally and can be delivered on a later run
    -- 'only materially changed jobs become eligible again'."""
    conn = _conn_past_baseline()
    config = _config()
    rows = [
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC + f" Variant {i}.", job_id=str(i), company="OpenAI")
        for i in range(5)
    ]
    run_once(conn, config, [_collector(rows)], None, _notifier(), now=NOW)
    suppressed_before = [
        i for i in range(5)
        if db.get_alert(
            conn, db.get_job_by_canonical(conn, f"https://boards.greenhouse.io/acme/jobs/{i}").job_id,
            AlertChannel.discord,
        )["state"] == AlertState.suppressed.value
    ]
    assert len(suppressed_before) == 2
    changed_id = suppressed_before[0]

    edited_rows = [
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC + f" Variant {changed_id}. Now with Kubernetes.",
             job_id=str(changed_id), company="OpenAI")
    ]
    later = NOW + timedelta(hours=1)
    stats2 = run_once(conn, config, [_collector(edited_rows)], None, _notifier(), now=later)

    assert stats2.jobs_changed == 1
    assert stats2.alerts_enqueued == 1  # reconsidered and re-enqueued
    assert stats2.alerts_sent == 1      # well under cap now (only 1 pending for OpenAI this run)

    rec = db.get_job_by_canonical(conn, f"https://boards.greenhouse.io/acme/jobs/{changed_id}")
    alert = db.get_alert(conn, rec.job_id, AlertChannel.discord)
    assert alert["state"] == AlertState.sent.value
    assert alert["suppressed_reason"] is None


def test_review_digest_also_diversifies_and_suppresses_across_companies():
    """The same per-company cap + explicit suppression applies to the review
    digest, diversified across companies (not just individual alerts)."""
    conn = _conn_past_baseline()
    config = _config()  # default max_review_items_per_company_per_run == 3
    rows = [
        _row(REVIEW_TITLE, REVIEW_DESC, job_id=f"r{i}", company="OpenAI") for i in range(5)
    ] + [
        _row(REVIEW_TITLE, REVIEW_DESC, job_id="other1", company="Anthropic"),
    ]
    stats = run_once(conn, config, [_collector(rows)], None, _notifier(), now=NOW)

    assert stats.alerts_enqueued == 6
    assert stats.alerts_suppressed_company_cap == 2  # 5 - 3 from OpenAI; Anthropic's 1 fits
    assert stats.review_digest_sent is True

    openai_states = [
        db.get_alert(conn, db.get_job_by_canonical(conn, f"https://boards.greenhouse.io/acme/jobs/r{i}").job_id, AlertChannel.discord)["state"]
        for i in range(5)
    ]
    assert openai_states.count(AlertState.sent.value) == 3
    assert openai_states.count(AlertState.suppressed.value) == 2
    anthropic_rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/other1")
    assert db.get_alert(conn, anthropic_rec.job_id, AlertChannel.discord)["state"] == AlertState.sent.value


# --------------------------------------------------------------------------- #
# Phase 6.1: review digest disabled by default -- clean skip, no accumulation
# as pending spam, explicit auditable suppression of anything pre-existing.
# --------------------------------------------------------------------------- #
def test_review_digest_disabled_by_default_config_value():
    """The Pydantic model default is deliberately left True (a large body of
    pre-existing tests above construct a bare _config()/NotificationConfig()
    and rely on digest-enabled behavior); it's the shipped config.yaml /
    config.example.yaml values that actually disable it in production -- see
    docs/PHASE_6_HANDOFF.md §13."""
    assert _config().notifications.send_review_digest is True

    for path in ("config.yaml", "config.example.yaml"):
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data["notifications"]["send_review_digest"] is False, path


def test_review_status_job_is_never_enqueued_while_digest_disabled():
    """With the digest disabled, a review-status job is still fully
    classified and stored, but never gets a pending alert row at all -- so it
    cannot accumulate as notification spam by construction (requirement 5)."""
    conn = _conn_past_baseline()
    config = _config(send_review_digest=False)
    row = _row(REVIEW_TITLE, REVIEW_DESC, job_id="1")

    stats = run_once(conn, config, [_collector([row])], None, _notifier(), now=NOW)

    assert stats.alerts_enqueued == 0
    assert stats.alerts_pending == 0
    assert stats.alerts_suppressed_review_digest_disabled == 0  # nothing to clean up

    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert rec.eligibility_status.value == "review"  # classification preserved
    assert db.get_alert(conn, rec.job_id, AlertChannel.discord) is None  # no alert row at all


def test_review_digest_disabled_cleanly_skips_sending_with_no_http_calls():
    """Individual alerts still flow normally; the digest step makes zero
    Discord requests and reports itself as not sent, never as a failure."""
    conn = _conn_past_baseline()
    config = _config(send_review_digest=False)
    calls: list = []
    notifier = _notifier(calls=calls)
    rows = [
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1"),
        *[_review_row(i) for i in range(3)],
    ]
    stats = run_once(conn, config, [_collector(rows)], None, notifier, now=NOW)

    assert stats.review_digest_sent is False
    assert stats.review_digest_chunks_sent == 0
    assert stats.review_digest_chunks_failed == 0  # a deliberate skip, never a delivery failure
    assert stats.notification_delivery_failures == 0
    assert stats.alerts_sent == 1  # only the individual eligible alert
    assert len(calls) == 1  # exactly one HTTP request: the individual alert, never a digest


def test_preexisting_pending_review_alert_is_suppressed_when_digest_later_disabled():
    """Simulates an existing production DB that already has a review alert
    pending from before the digest was disabled: it must be explicitly
    suppressed with a clear, auditable reason -- never left dangling as
    unbounded pending backlog, and never silently dropped."""
    conn = _conn_past_baseline()
    row = _row(REVIEW_TITLE, REVIEW_DESC, job_id="1")

    # First run: digest enabled (default), but Discord delivery fails, so the
    # review alert is enqueued and stays genuinely `pending` (not `sent`).
    run_once(
        conn, _config(), [_collector([row])], None, _notifier(status_code=500), now=NOW
    )
    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert db.get_alert(conn, rec.job_id, AlertChannel.discord)["state"] == AlertState.pending.value

    # Operator disables the digest; next run cleans up the pre-existing
    # pending review alert instead of leaving it stuck forever.
    later = NOW + timedelta(hours=1)
    stats2 = run_once(
        conn, _config(send_review_digest=False), [], None, _notifier(), now=later
    )

    assert stats2.alerts_suppressed_review_digest_disabled == 1
    assert stats2.alerts_pending == 0
    alert = db.get_alert(conn, rec.job_id, AlertChannel.discord)
    assert alert["state"] == AlertState.suppressed.value
    assert alert["suppressed_reason"] == "suppressed_review_digest_disabled"


def test_review_digest_still_works_when_explicitly_enabled():
    """Explicit send_review_digest=True (independent of the Pydantic default)
    reproduces full pre-6.1 enabled behavior end-to-end."""
    conn = _conn_past_baseline()
    config = _config(send_review_digest=True)
    row = _row(REVIEW_TITLE, REVIEW_DESC, job_id="1")

    stats = run_once(conn, config, [_collector([row])], None, _notifier(), now=NOW)

    assert stats.review_digest_sent is True
    assert stats.alerts_suppressed_review_digest_disabled == 0
    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert db.get_alert(conn, rec.job_id, AlertChannel.discord)["state"] == AlertState.sent.value


def test_individual_alerts_unaffected_by_review_digest_being_disabled():
    """Requirement 6: individual P1/P2/P3 alerts are completely unchanged by
    this setting, even in a mixed run alongside several review jobs."""
    conn = _conn_past_baseline()
    config = _config(send_review_digest=False)
    rows = [
        _row(ELIGIBLE_TITLE, ELIGIBLE_DESC, job_id="1"),
        *[_review_row(i) for i in range(5)],
    ]
    stats = run_once(conn, config, [_collector(rows)], None, _notifier(), now=NOW)

    assert stats.alerts_sent == 1
    assert stats.alerts_suppressed_company_cap == 0
    rec = db.get_job_by_canonical(conn, "https://boards.greenhouse.io/acme/jobs/1")
    assert db.get_alert(conn, rec.job_id, AlertChannel.discord)["state"] == AlertState.sent.value


if __name__ == "__main__":
    pytest.main([__file__])
