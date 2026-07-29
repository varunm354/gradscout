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


def _row(title: str, desc: str, *, job_id: str, posted_at=NOW, company="Acme") -> RawJob:
    return RawJob(
        source=SourceType.greenhouse,
        source_company="acme",
        company=company,
        source_job_id=job_id,
        title=title,
        description_text=desc,
        apply_url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        source_posted_at=posted_at,
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


if __name__ == "__main__":
    pytest.main([__file__])
