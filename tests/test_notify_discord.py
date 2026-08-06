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
    LocationClassification,
    ResumeConfidence,
    ResumeVariant,
    RoleFamily,
)
from gradscout.notify.discord import (
    MAX_EMBEDS_PER_MESSAGE,
    MAX_FIELD_VALUE_LEN,
    MAX_FIELDS_PER_EMBED,
    MAX_TITLE_LEN,
    MAX_TOTAL_CHARS_PER_MESSAGE,
    DiscordNotifier,
    _fits_message_limits,
    _payload_char_len,
)


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
        location_classification=LocationClassification.preferred,
        location_reason="test fixture default",
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


def test_job_alert_embed_shows_match_score_and_skills_explanation():
    """Phase 6: the Recommended resume field names the concrete match score
    alongside confidence, and the embed description is the skills-based
    explanation (never just a bare percentage)."""
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    record = make_record(
        recommended_resume=ResumeVariant.backend,
        resume_confidence=ResumeConfidence.high,
        resume_match_score=78,
        resume_reason="Matched FastAPI, PostgreSQL, distributed systems (backend resume)",
    )
    notifier.send_job_alert(record)

    body = json.loads(calls[0].content)
    embed = body["embeds"][0]
    by_name = {f["name"]: f["value"] for f in embed["fields"]}
    assert by_name["Recommended resume"] == "backend (78% match, high confidence)"
    assert embed["description"] == "Matched FastAPI, PostgreSQL, distributed systems (backend resume)"
    assert "%" not in embed["description"]  # explanation names skills, not just a score


def test_job_alert_embed_omits_score_gracefully_when_absent():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    record = make_record(resume_match_score=None)
    notifier.send_job_alert(record)

    body = json.loads(calls[0].content)
    by_name = {f["name"]: f["value"] for f in body["embeds"][0]["fields"]}
    assert by_name["Recommended resume"] == "backend (high confidence)"
    assert "%" not in by_name["Recommended resume"]


def test_job_alert_embed_shows_location_fit():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    notifier.send_job_alert(make_record(location_classification=LocationClassification.remote_acceptable))

    embed = json.loads(calls[0].content)["embeds"][0]
    by_name = {f["name"]: f["value"] for f in embed["fields"]}
    assert by_name["Location fit"] == "US Remote"


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
    result = notifier.send_review_digest(jobs)
    assert result.chunks_sent == 1
    assert result.chunks_failed == 0
    assert {j.job_id for j in result.delivered} == {1, 2, 3}
    assert len(calls) == 1  # ONE message, not three

    embed = json.loads(calls[0].content)["embeds"][0]
    assert len(embed["fields"]) == 3


def test_review_digest_notes_out_of_region_and_unclear_but_not_preferred():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    jobs = [
        make_record(job_id=1, title="Preferred Job", location_classification=LocationClassification.preferred),
        make_record(job_id=2, title="OOR Job", location_classification=LocationClassification.out_of_region),
        make_record(job_id=3, title="Unclear Job", location_classification=LocationClassification.unclear),
    ]
    notifier.send_review_digest(jobs)

    embed = json.loads(calls[0].content)["embeds"][0]
    values = {f["name"]: f["value"] for f in embed["fields"]}
    assert "Location:" not in values["Acme — Preferred Job"]
    assert "Location: Out of region" in values["Acme — OOR Job"]
    assert "Location: Location unclear" in values["Acme — Unclear Job"]


def test_review_digest_field_shows_compact_resume_match_suffix():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    jobs = [
        make_record(
            job_id=1, title="Job With Score",
            recommended_resume=ResumeVariant.ai, resume_match_score=78,
        ),
        make_record(job_id=2, title="Job Without Score", resume_match_score=None),
    ]
    notifier.send_review_digest(jobs)

    embed = json.loads(calls[0].content)["embeds"][0]
    values = {f["name"]: f["value"] for f in embed["fields"]}
    assert values["Acme — Job With Score"].endswith("· ai 78%")
    assert "%" not in values["Acme — Job Without Score"]


def test_empty_review_digest_sends_nothing():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    result = notifier.send_review_digest([])
    assert result.delivered == []
    assert result.chunks_sent == 0
    assert result.chunks_failed == 0
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


# --------------------------------------------------------------------------- #
# Phase 5.3 postmortem fix: Discord payload-limit enforcement, chunking, and
# no-lost-alerts regression coverage.
#
# Production observed: HTTP 400 {"embeds": ["Embed size exceeds maximum size
# of 6000"]} from a review-digest message carrying 6 long alerts, alongside
# alerts_enqueued=6 / alerts_sent=0 / alerts_pending=0. Root cause:
# `_digest_embed` packed every pending review job into ONE embed with only
# per-field (not aggregate) truncation, and the 6 review-priority alerts were
# invisible to the individual-alert pending count. Fixed by: conservative
# per-field shortening everywhere, defensive pre-send aggregate/field/embed-
# count enforcement, and splitting an oversized review digest into multiple
# independently-delivered chunks.
# --------------------------------------------------------------------------- #
def _review_job(job_id: int, *, long: bool = True) -> JobRecord:
    """A review-priority job whose digest FIELD is deliberately oversized (a
    ~150-char company/title pair and a >1000-char eligibility reason) so that,
    after `_field`'s per-field shortening, it reliably contributes EXACTLY
    MAX_FIELD_NAME_LEN + MAX_FIELD_VALUE_LEN characters to a digest embed --
    or a short realistic one when ``long`` is False."""
    if long:
        company = "Company " + ("C" * 150)
        title = "T" * 150
        apply_url = "https://boards.greenhouse.io/acme/jobs/" + ("u" * 100) + str(job_id)
        reason = "R" * 1200
    else:
        company = "Acme"
        title = f"Reviewable Job {job_id}"
        apply_url = f"https://boards.greenhouse.io/acme/jobs/{job_id}"
        reason = "Needs manual review"
    return make_record(
        job_id=job_id,
        url_canonical=apply_url,
        company=company,
        title=title,
        apply_url=apply_url,
        eligibility_status=EligibilityStatus.review,
        eligibility_reasons=[reason],
        alert_priority=AlertPriority.review,
        recommended_resume=None,
        resume_confidence=None,
        resume_reason=None,
        location_classification=LocationClassification.unclear,
    )


def test_job_embed_shortens_oversized_field_values_with_ellipsis():
    """An oversized field value (e.g. a company/location string from
    untrusted source data) is shortened, never dropped, and always ends with
    an ellipsis so a human reader knows it was cut."""
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    huge_location = "L" * 5000
    huge_title = "T" * 5000
    notifier.send_job_alert(make_record(location=huge_location, title=huge_title))

    embed = json.loads(calls[0].content)["embeds"][0]
    assert len(embed["title"]) <= MAX_TITLE_LEN
    assert embed["title"].endswith("…")
    by_name = {f["name"]: f["value"] for f in embed["fields"]}
    assert len(by_name["Location"]) <= MAX_FIELD_VALUE_LEN
    assert by_name["Location"].endswith("…")
    # The whole job is still sent -- oversized text is shortened, not dropped.
    assert len(calls) == 1


def test_job_alert_payload_always_complies_with_discord_limits():
    """Defense in depth: whatever a single job alert's raw content looks
    like, the actual payload sent must comply with our conservative internal
    ceilings (which are themselves below Discord's hard limits)."""
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    notifier.send_job_alert(
        make_record(company="C" * 5000, location="L" * 5000, title="T" * 5000)
    )
    embed = json.loads(calls[0].content)["embeds"][0]
    assert _fits_message_limits([embed])


# --- Requirement 7: payload just below / above the character limit -------- #
def test_digest_payload_just_below_char_limit_sent_as_one_message():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    jobs = [_review_job(i) for i in range(5)]  # verified below MAX_TOTAL_CHARS_PER_MESSAGE
    result = notifier.send_review_digest(jobs)

    assert len(calls) == 1
    embed = json.loads(calls[0].content)["embeds"][0]
    assert _payload_char_len([embed]) <= MAX_TOTAL_CHARS_PER_MESSAGE
    assert result.chunks_sent == 1
    assert result.chunks_failed == 0
    assert {j.job_id for j in result.delivered} == {j.job_id for j in jobs}


def test_digest_payload_above_char_limit_is_split_into_multiple_messages():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    jobs = [_review_job(i) for i in range(6)]  # verified above MAX_TOTAL_CHARS_PER_MESSAGE as 1 embed
    result = notifier.send_review_digest(jobs)

    assert len(calls) > 1  # split into multiple valid Discord messages
    for call in calls:
        embed = json.loads(call.content)["embeds"][0]
        assert _fits_message_limits([embed])
    # No job silently dropped by the split.
    assert {j.job_id for j in result.delivered} == {j.job_id for j in jobs}
    assert result.chunks_sent == len(calls)
    assert result.chunks_failed == 0


# --- Requirement 7: more than 10 embeds / more than 25 fields ------------- #
def test_more_than_10_embeds_in_one_message_is_refused_before_sending():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    embeds = [{"title": f"E{i}", "fields": []} for i in range(MAX_EMBEDS_PER_MESSAGE + 1)]
    assert _fits_message_limits(embeds) is False
    assert notifier._post({"embeds": embeds}) is False
    assert calls == []  # refused before ever touching the network


def test_more_than_25_fields_is_split_across_multiple_embeds():
    handler, calls = _capturing()
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    jobs = [_review_job(i, long=False) for i in range(30)]  # short content -> field-count-driven split
    result = notifier.send_review_digest(jobs)

    assert len(calls) > 1
    for call in calls:
        embed = json.loads(call.content)["embeds"][0]
        assert len(embed["fields"]) <= MAX_FIELDS_PER_EMBED
    assert {j.job_id for j in result.delivered} == {j.job_id for j in jobs}


# --- Requirement 7: first-chunk-succeeds/second-fails, and first-fails ---- #
def test_digest_second_chunk_fails_first_chunk_still_delivered():
    jobs = [_review_job(i) for i in range(6)]  # 2 chunks, per prior test
    statuses = iter([204, 400])
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(next(statuses))

    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    result = notifier.send_review_digest(jobs)

    assert len(calls) == 2
    assert result.chunks_sent == 1
    assert result.chunks_failed == 1
    # First chunk's jobs are delivered; second chunk's jobs are NOT -- they
    # must remain pending, not lost and not marked sent.
    first_chunk_ids = {j.job_id for j in jobs[:5]}
    second_chunk_ids = {j.job_id for j in jobs[5:]}
    delivered_ids = {j.job_id for j in result.delivered}
    assert delivered_ids == first_chunk_ids
    assert delivered_ids.isdisjoint(second_chunk_ids)


def test_digest_first_chunk_fails_second_chunk_still_attempted_and_delivered():
    jobs = [_review_job(i) for i in range(6)]  # 2 chunks
    statuses = iter([400, 204])
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(next(statuses))

    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    result = notifier.send_review_digest(jobs)

    assert len(calls) == 2
    assert result.chunks_sent == 1
    assert result.chunks_failed == 1
    first_chunk_ids = {j.job_id for j in jobs[:5]}
    second_chunk_ids = {j.job_id for j in jobs[5:]}
    delivered_ids = {j.job_id for j in result.delivered}
    # The failed first chunk's jobs are never marked delivered...
    assert delivered_ids.isdisjoint(first_chunk_ids)
    # ...but the independent second chunk still got through.
    assert delivered_ids == second_chunk_ids


def test_digest_dry_run_makes_no_calls_and_reports_no_failures():
    """Dry-run must never look like a delivery FAILURE -- it's an
    intentional no-op, distinct from a rejected/errored send."""
    jobs = [_review_job(i) for i in range(6)]  # would be 2 chunks if actually sent
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", dry_run=True)
    result = notifier.send_review_digest(jobs)
    assert result.delivered == []
    assert result.chunks_sent == 0
    assert result.chunks_failed == 0  # NOT a failure -- dry-run is a deliberate skip


# --- Requirement 8: production reproduction -------------------------------- #
def test_reproduces_production_incident_six_long_alerts_all_requests_comply():
    """Exact production reproduction: 6 long Eligibility Review alerts that,
    prior to this fix, were packed into a single oversized embed and
    rejected by Discord with HTTP 400 ``Embed size exceeds maximum size of
    6000``. Proves every resulting HTTP request now complies with Discord's
    real limits, and that all 6 jobs are still delivered (across however
    many messages it takes), none lost."""
    handler, calls = _capturing(status_code=204)
    client = _client_with(handler)
    notifier = DiscordNotifier(webhook_url="https://discord.test/hook", client=client)
    jobs = [_review_job(i) for i in range(6)]

    # Sanity check: the OLD (pre-fix) unbounded single embed of this content
    # -- which only truncated each field to Discord's own per-field caps
    # (256 name / 1024 value), with no aggregate check -- really would have
    # exceeded Discord's real 6000-char hard limit. This is the exact
    # production failure mode being fixed.
    from gradscout.notify.discord import DISCORD_MAX_TOTAL_CHARS

    naive_chars = 0
    for j in jobs:
        reason = j.eligibility_reasons[0]
        name = f"{j.company} — {j.title}"[:256]
        value = f"[Apply]({j.apply_url}) · {reason}"[:1024]
        naive_chars += len(name) + len(value)
    assert naive_chars > DISCORD_MAX_TOTAL_CHARS

    result = notifier.send_review_digest(jobs)

    assert len(calls) >= 2  # split into multiple compliant messages
    for call in calls:
        body = json.loads(call.content)
        assert _fits_message_limits(body["embeds"])
        for embed in body["embeds"]:
            assert len(embed["fields"]) <= MAX_FIELDS_PER_EMBED
        assert len(body["embeds"]) <= MAX_EMBEDS_PER_MESSAGE
    # All 6 production alerts are accounted for -- none silently dropped.
    assert {j.job_id for j in result.delivered} == {j.job_id for j in jobs}
    assert result.chunks_failed == 0
