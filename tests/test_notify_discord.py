"""Discord delivery boundary. All HTTP is mocked via httpx.MockTransport --
no test in this file makes a real network call."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from gradscout.models import (
    AlertPriority,
    EligibilityStatus,
    EmploymentType,
    JobRecord,
    ResumeConfidence,
    ResumeVariant,
    RoleFamily,
)
from gradscout.notify.discord import DiscordNotifier


def make_record(**overrides) -> JobRecord:
    base = dict(
        job_id=1,
        url_canonical="https://boards.greenhouse.io/acme/jobs/1",
        company="Acme",
        company_priority=1,
        title="Software Engineer, New Grad",
        location="New York, NY",
        remote=None,
        description_text="Backend APIs.",
        apply_url="https://boards.greenhouse.io/acme/jobs/1?utm_source=x",
        source_posted_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        first_seen_at=datetime(2027, 1, 2, tzinfo=timezone.utc),
        last_seen_at=datetime(2027, 1, 2, tzinfo=timezone.utc),
        eligibility_status=EligibilityStatus.eligible,
        eligibility_reasons=["New-grad / early-career language present"],
        role_family=RoleFamily.backend,
        role_priority=1,
        employment_type=EmploymentType.fulltime,
        is_new_grad=True,
        recommended_resume=ResumeVariant.backend,
        resume_confidence=ResumeConfidence.high,
        resume_reason="Matched backend keywords",
        alert_priority=AlertPriority.p1,
        llm_used=False,
        sources=[],
    )
    base.update(overrides)
    return JobRecord(**base)


def _client_with(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _capturing(status_code=204):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status_code)

    return handler, calls


# --------------------------------------------------------------------------- #
# Dry-run: never touches the network, never "delivers"
# --------------------------------------------------------------------------- #
def test_dry_run_makes_no_request_and_returns_false():
    handler, calls = _capturing()
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", dry_run=True)
    # dry_run notifiers must not even own a client.
    assert notifier.client is None
    delivered = notifier.send_job_alert(make_record())
    assert delivered is False
    assert calls == []  # unreachable anyway since no client was built


def test_unconfigured_webhook_makes_no_request():
    notifier = DiscordNotifier(webhook_url="", dry_run=False)
    assert notifier.enabled is False
    assert notifier.send_job_alert(make_record()) is False


# --------------------------------------------------------------------------- #
# Success / failure / transport-error boundary
# --------------------------------------------------------------------------- #
def test_2xx_response_delivers():
    handler, calls = _capturing(status_code=204)
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    assert notifier.send_job_alert(make_record()) is True
    assert len(calls) == 1


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 502, 503])
def test_non_2xx_response_leaves_alert_undelivered(status):
    handler, calls = _capturing(status_code=status)
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    assert notifier.send_job_alert(make_record()) is False
    assert len(calls) == 1


def test_transport_error_leaves_alert_undelivered():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connection timed out")

    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    assert notifier.send_job_alert(make_record()) is False


# --------------------------------------------------------------------------- #
# Message content: required fields, and never mislabeling first_seen_at
# --------------------------------------------------------------------------- #
def test_job_alert_embed_contains_required_fields():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    record = make_record()
    notifier.send_job_alert(record)

    body = json.loads(calls[0].content)
    embed = body["embeds"][0]
    assert embed["title"] == record.title
    assert embed["url"] == record.apply_url  # original application URL, exact

    field_names = {f["name"] for f in embed["fields"]}
    assert "Company" in field_names
    assert "Priority" in field_names
    assert "Eligibility" in field_names
    assert "Location" in field_names
    assert "Recommended resume" in field_names
    assert "Posted" in field_names
    assert "First discovered by GradScout" in field_names

    by_name = {f["name"]: f["value"] for f in embed["fields"]}
    assert by_name["Company"] == "Acme"
    assert by_name["Priority"] == "P1"
    assert by_name["Eligibility"] == "eligible"
    assert "high confidence" in by_name["Recommended resume"]
    # posted date and first-discovered time must never be conflated
    assert by_name["Posted"] == record.source_posted_at.isoformat()
    assert by_name["First discovered by GradScout"] == record.first_seen_at.isoformat()
    assert by_name["Posted"] != by_name["First discovered by GradScout"]


def test_job_alert_omits_posted_field_when_source_posted_at_missing():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    notifier.send_job_alert(make_record(source_posted_at=None))

    embed = json.loads(calls[0].content)["embeds"][0]
    field_names = {f["name"] for f in embed["fields"]}
    assert "Posted" not in field_names          # never fabricate a posting date
    assert "First discovered by GradScout" in field_names


# --------------------------------------------------------------------------- #
# Review digest: one message for many jobs, never one-per-job
# --------------------------------------------------------------------------- #
def test_review_digest_batches_all_jobs_into_one_message():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    jobs = [make_record(job_id=i, title=f"Job {i}") for i in range(1, 4)]
    assert notifier.send_review_digest(jobs) is True
    assert len(calls) == 1  # ONE message, not three

    embed = json.loads(calls[0].content)["embeds"][0]
    assert len(embed["fields"]) == 3


def test_empty_review_digest_sends_nothing():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    assert notifier.send_review_digest([]) is False
    assert calls == []


# --------------------------------------------------------------------------- #
# Source failure / recovery embeds
# --------------------------------------------------------------------------- #
def test_source_failure_and_recovery_messages():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)

    assert notifier.send_source_failure("greenhouse:stripe", "Stripe", "timeout") is True
    assert notifier.send_source_recovery("greenhouse:stripe", "Stripe") is True
    assert len(calls) == 2

    failure_embed = json.loads(calls[0].content)["embeds"][0]
    assert "DOWN" in failure_embed["title"]
    assert "Stripe" in failure_embed["title"]

    recovery_embed = json.loads(calls[1].content)["embeds"][0]
    assert "recovered" in recovery_embed["title"].lower()
