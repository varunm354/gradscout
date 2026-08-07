"""Discord webhook delivery.

One webhook, three message shapes:
  * a concise embed per urgent/strong-match job alert (p1/p2/p3),
  * one-or-more batched embeds for all pending Eligibility Review jobs (a
    digest, never one message per review job -- but see the Phase 5.3 note
    below: a large/long backlog is split across multiple digest MESSAGES,
    never truncated away),
  * short operational embeds for a high-priority source failure/recovery
    transition and the once-daily health summary.

Delivery is a pure best-effort boundary: ``_post`` returns True only on a 2xx
response and never raises, so a Discord failure/timeout/rate-limit/non-2xx
response simply yields False and the caller (gradscout.pipeline) leaves the
underlying alert row pending for a later retry. Dry-run makes no HTTP request
at all and always returns False, so nothing is ever marked sent.

The HTTP client is injectable (tests pass an httpx.Client wired to a
httpx.MockTransport), keeping pytest fully offline.

Phase 5.3 -- Discord payload-limit enforcement (postmortem fix):
    Production observed an HTTP 400 ``{"embeds": ["Embed size exceeds maximum
    size of 6000"]}`` from a review-digest message. Discord's real limits are
    enforced HERE, defensively, before every ``_post`` call, rather than ever
    relying on Discord's own rejection to discover an oversized payload:
      * <=10 embeds per message, <=25 fields per embed (Discord hard caps).
      * <=6000 aggregate characters (title + description + all field names/
        values, summed across every embed in the message) -- enforced against
        a conservative internal ceiling BELOW Discord's actual 6000, so any
        estimation slack never causes a real rejection.
      * title/description/field-name/field-value are each shortened (with a
        trailing "..." ellipsis, never silently dropped) to conservative
        internal limits below Discord's own per-field caps.
    The review digest -- the one message shape that batches an unbounded
    number of jobs into a single embed -- is additionally CHUNKED into
    multiple valid messages when the backlog doesn't fit in one; no job is
    ever dropped by chunking (only deferred to a later run by the existing
    per-run ``MAX_DIGEST_ITEMS`` cap, exactly as before). Each chunk is an
    independent HTTP request: ``send_review_digest`` reports exactly which
    jobs were included in a chunk that received a 2xx, so the caller
    (gradscout.pipeline) marks only those sent and leaves every job in a
    failed or unattempted chunk pending -- see BatchSendResult below.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from gradscout.freshness import format_posting_age
from gradscout.location import location_label
from gradscout.models import AlertPriority, JobRecord, LocationClassification

logger = logging.getLogger("gradscout.notify.discord")

DEFAULT_TIMEOUT = 10.0
# Discord embeds cap at 25 fields; also bounds how much of one run's pending
# review backlog is attempted per digest (the rest stays pending for later --
# spread across as many chunked MESSAGES as needed, see _chunk_digest_jobs).
MAX_DIGEST_ITEMS = 25

# --------------------------------------------------------------------------- #
# Discord's real hard limits, and the conservative internal ceilings we
# actually enforce (below the hard limits) so a rounding/estimation error on
# our side never produces an actual Discord rejection. See module docstring.
# --------------------------------------------------------------------------- #
DISCORD_MAX_EMBEDS_PER_MESSAGE = 10
DISCORD_MAX_FIELDS_PER_EMBED = 25
DISCORD_MAX_TOTAL_CHARS = 6000

MAX_EMBEDS_PER_MESSAGE = DISCORD_MAX_EMBEDS_PER_MESSAGE  # a count, not a size -- no margin needed
MAX_FIELDS_PER_EMBED = DISCORD_MAX_FIELDS_PER_EMBED  # a count, not a size -- no margin needed
MAX_TOTAL_CHARS_PER_MESSAGE = 5500  # conservative vs. Discord's hard 6000 aggregate cap
MAX_TITLE_LEN = 250  # conservative vs. Discord's 256
MAX_DESCRIPTION_LEN = 500
MAX_FIELD_NAME_LEN = 200  # conservative vs. Discord's 256
MAX_FIELD_VALUE_LEN = 800  # conservative vs. Discord's 1024


def _shorten(text: str | None, limit: int) -> str | None:
    """Safely shorten ``text`` to at most ``limit`` characters, replacing any
    cut content with a trailing ellipsis so a human reader can tell it was
    shortened. Never raises, never returns something longer than ``limit``.
    Whole items are never dropped this way -- only individual text fields are
    shortened (requirement: no truncating away whole jobs)."""
    if text is None or len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def _embed_char_len(embed: dict) -> int:
    """Discord's own character-count formula for one embed: title +
    description + every field's name + value (footer/author/timestamp are
    unused here, so omitted)."""
    total = len(embed.get("title") or "") + len(embed.get("description") or "")
    for f in embed.get("fields") or []:
        total += len(f.get("name") or "") + len(f.get("value") or "")
    return total


def _payload_char_len(embeds: list[dict]) -> int:
    """Aggregate character count across every embed in one message -- this is
    exactly what Discord's 6000 limit is measured against."""
    return sum(_embed_char_len(e) for e in embeds)


def _fits_message_limits(embeds: list[dict]) -> bool:
    """True only if ``embeds`` as one message payload complies with our
    conservative internal ceilings (themselves below Discord's hard limits)."""
    if len(embeds) > MAX_EMBEDS_PER_MESSAGE:
        return False
    for e in embeds:
        if len(e.get("fields") or []) > MAX_FIELDS_PER_EMBED:
            return False
    return _payload_char_len(embeds) <= MAX_TOTAL_CHARS_PER_MESSAGE


_PRIORITY_COLOR = {
    AlertPriority.p1: 0xE74C3C,  # red - urgent
    AlertPriority.p2: 0xF39C12,  # orange - strong match
    AlertPriority.p3: 0x3498DB,  # blue - lower priority
}
_DIGEST_COLOR = 0x95A5A6
_FAILURE_COLOR = 0xC0392B
_RECOVERY_COLOR = 0x27AE60
_SUMMARY_HEALTHY_COLOR = 0x2ECC71
_SUMMARY_DEGRADED_COLOR = 0xF1C40F


def _field(name: str, value: str, *, inline: bool = True) -> dict:
    """Build one Discord embed field, with both name and value shortened to
    our conservative internal caps (see MAX_FIELD_NAME_LEN/MAX_FIELD_VALUE_LEN)
    so a single field can never blow the message's aggregate character budget."""
    return {
        "name": _shorten(name, MAX_FIELD_NAME_LEN),
        "value": _shorten(value, MAX_FIELD_VALUE_LEN),
        "inline": inline,
    }


def _job_embed(job: JobRecord, now: datetime) -> dict:
    fields = [
        _field("Company", job.company),
        _field("Priority", job.alert_priority.value.upper()),
        _field("Eligibility", job.eligibility_status.value),
    ]
    if job.location:
        fields.append(_field("Location", job.location))
    fields.append(_field("Location fit", location_label(job.location_classification)))

    resume_value = "-"
    if job.recommended_resume:
        # Phase 6: name the concrete match score alongside confidence, e.g.
        # "ai (78% match, high confidence)" -- the *why* (top matched
        # skills/technologies) lives in the embed description via
        # job.resume_reason, never just this bare percentage.
        score = f"{job.resume_match_score}% match, " if job.resume_match_score is not None else ""
        conf = f"{job.resume_confidence.value} confidence" if job.resume_confidence else ""
        detail = f" ({score}{conf})" if (score or conf) else ""
        resume_value = f"{job.recommended_resume.value}{detail}"
    fields.append(_field("Recommended resume", resume_value))

    # Never describe first_seen_at as a posting date: label the two distinctly.
    # Phase 6.2: human-readable relative age (e.g. "45m ago", "Jul 28") only --
    # the exact source_posted_at timestamp is preserved internally (DB,
    # JobRecord) for sorting/tests/debugging, but is no longer shown raw here.
    posting_age = format_posting_age(job.source_posted_at, now)
    if posting_age:
        fields.append(_field("Posted", posting_age))
    fields.append(_field("First discovered by GradScout", job.first_seen_at.isoformat()))

    reason = job.resume_reason or (job.eligibility_reasons[0] if job.eligibility_reasons else "")
    return {
        "title": _shorten(job.title, MAX_TITLE_LEN),
        "url": job.apply_url,
        "description": _shorten(reason, MAX_DESCRIPTION_LEN) if reason else None,
        "color": _PRIORITY_COLOR.get(job.alert_priority, _DIGEST_COLOR),
        "fields": fields,
    }


_DIGEST_NOTEWORTHY_LOCATIONS = (LocationClassification.out_of_region, LocationClassification.unclear)


def _digest_field(j: JobRecord) -> dict:
    reason = j.eligibility_reasons[0] if j.eligibility_reasons else "Needs manual review"
    value = f"[Apply]({j.apply_url}) · {reason}"
    # Only call out location when it's the interesting case for a human reviewer
    # (preferred/remote_acceptable is unremarkable and would just add noise).
    if j.location_classification in _DIGEST_NOTEWORTHY_LOCATIONS:
        value += f" · Location: {location_label(j.location_classification)}"
    # Phase 6: compact resume-match suffix (e.g. " · ai 78%"), still within
    # the existing field-value character budget enforcement (_field shortens).
    if j.recommended_resume and j.resume_match_score is not None:
        value += f" · {j.recommended_resume.value} {j.resume_match_score}%"
    return _field(f"{j.company} — {j.title}", value, inline=False)


def _digest_embed(jobs: list[JobRecord], *, part: int = 1, total_parts: int = 1) -> dict:
    """Build ONE digest embed from a chunk of jobs already known to fit our
    field-count and aggregate character limits (see _chunk_digest_jobs, which
    is the only caller allowed to hand this more than one chunk's worth)."""
    fields = [_digest_field(j) for j in jobs]
    count_label = f"{len(jobs)} job{'s' if len(jobs) != 1 else ''}"
    title = f"Eligibility Review digest ({count_label})"
    if total_parts > 1:
        title = f"Eligibility Review digest (part {part}/{total_parts}, {count_label})"
    return {
        "title": _shorten(title, MAX_TITLE_LEN),
        "color": _DIGEST_COLOR,
        "fields": fields,
    }


def _chunk_digest_jobs(jobs: list[JobRecord]) -> list[list[JobRecord]]:
    """Split ``jobs`` into groups, each of which produces one digest embed
    that fits within MAX_FIELDS_PER_EMBED fields and MAX_TOTAL_CHARS_PER_MESSAGE
    aggregate characters -- so a long/large review backlog is delivered as
    multiple valid Discord MESSAGES rather than truncated away or rejected.

    Every job supplied is placed in exactly one chunk (never dropped here);
    the only place jobs are ever left out of a run's digest attempt entirely
    is the caller's own MAX_DIGEST_ITEMS per-run slice, applied before this
    function ever sees them.
    """
    chunks: list[list[JobRecord]] = []
    current: list[JobRecord] = []
    current_fields: list[dict] = []
    for job in jobs:
        field_ = _digest_field(job)
        candidate_fields = current_fields + [field_]
        candidate_embed = {"title": "", "color": _DIGEST_COLOR, "fields": candidate_fields}
        fits = (
            len(candidate_fields) <= MAX_FIELDS_PER_EMBED
            # Reserve headroom for the title itself (added once the chunk is
            # finalized) -- MAX_TITLE_LEN is a safe upper bound for that.
            and _embed_char_len(candidate_embed) + MAX_TITLE_LEN <= MAX_TOTAL_CHARS_PER_MESSAGE
        )
        if fits:
            current = current + [job]
            current_fields = candidate_fields
            continue
        if current:
            chunks.append(current)
        # A single already-shortened field should always fit alone given our
        # conservative per-field caps, but guard defensively rather than ever
        # dropping a job: start a new chunk with just this one job.
        current = [job]
        current_fields = [field_]
    if current:
        chunks.append(current)
    return chunks


def _source_failure_embed(source_id: str, company: str | None, error: str | None) -> dict:
    return {
        "title": _shorten(f"High-priority source DOWN: {company or source_id}", MAX_TITLE_LEN),
        "description": _shorten(
            f"`{source_id}` — {error or 'unknown error'}", MAX_DESCRIPTION_LEN
        ),
        "color": _FAILURE_COLOR,
    }


def _source_recovery_embed(source_id: str, company: str | None) -> dict:
    return {
        "title": _shorten(f"Source recovered: {company or source_id}", MAX_TITLE_LEN),
        "description": _shorten(f"`{source_id}` is healthy again.", MAX_DESCRIPTION_LEN),
        "color": _RECOVERY_COLOR,
    }


def _daily_summary_embed(rows: list[sqlite3.Row], date_key: str) -> dict:
    failing = [r for r in rows if r["last_status"] != "ok"]
    healthy_count = len(rows) - len(failing)
    fields = [
        _field(
            f"{r['company'] or r['source_id']} ({r['source_id']})",
            r["last_error"] or r["last_status"],
            inline=False,
        )
        for r in failing
    ]
    return {
        "title": _shorten(f"GradScout daily health summary — {date_key} UTC", MAX_TITLE_LEN),
        "description": _shorten(f"{healthy_count}/{len(rows)} sources healthy.", MAX_DESCRIPTION_LEN),
        "color": _SUMMARY_DEGRADED_COLOR if failing else _SUMMARY_HEALTHY_COLOR,
        "fields": fields,
    }


@dataclass
class BatchSendResult:
    """Outcome of a possibly-multi-message batched send (currently only the
    review digest). ``delivered`` is the exact list of jobs that were part of
    a chunk whose HTTP request received a 2xx -- the caller (gradscout.
    pipeline) marks ONLY these sent; every other job (in a failed chunk, or
    never attempted at all) stays pending, by construction."""

    delivered: list[JobRecord] = field(default_factory=list)
    chunks_sent: int = 0
    chunks_failed: int = 0


@dataclass
class DiscordNotifier:
    webhook_url: str
    dry_run: bool = False
    client: httpx.Client | None = None
    timeout: float = DEFAULT_TIMEOUT
    _owns_client: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.client is None and not self.dry_run and self.enabled:
            self.client = httpx.Client(timeout=self.timeout)
            self._owns_client = True

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    @property
    def _would_attempt(self) -> bool:
        """True only if a False return from ``_post`` represents a genuine
        rejected/errored delivery, never a dry-run or unconfigured-webhook
        skip (both of which intentionally return False without trying)."""
        return not self.dry_run and self.enabled

    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()

    def _post(self, payload: dict) -> bool:
        """Send one webhook payload. Returns True ONLY on a 2xx response.
        Never raises: transport errors, timeouts, and non-2xx all yield
        False so the caller leaves the underlying alert pending.

        Defensively re-checks our own conservative Discord payload-limit
        ceilings immediately before sending (requirement: enforce limits
        before every network request, never rely on Discord's own rejection
        to discover an oversized payload). Every embed builder above already
        shortens its own fields, so this should be unreachable in practice --
        it exists purely as a last-resort guard against a future embed
        builder that forgets to size its own content."""
        if self.dry_run:
            logger.info("dry-run: discord send suppressed", extra={"fields": {"payload": payload}})
            return False
        if not self.enabled or self.client is None:
            logger.warning("discord webhook not configured; skipping send")
            return False
        embeds = payload.get("embeds") or []
        if not _fits_message_limits(embeds):
            logger.error(
                "discord payload exceeds internal size limits; refusing to send",
                extra={
                    "fields": {
                        "embed_count": len(embeds),
                        "char_count": _payload_char_len(embeds),
                    }
                },
            )
            return False
        try:
            resp = self.client.post(self.webhook_url, json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            logger.warning("discord send error", extra={"fields": {"error": repr(exc)}})
            return False
        if 200 <= resp.status_code < 300:
            return True
        logger.warning(
            "discord send rejected",
            extra={"fields": {"status": resp.status_code, "body": resp.text[:300]}},
        )
        return False

    def send_job_alert(self, job: JobRecord, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self._post({"embeds": [_job_embed(job, now)]})

    def send_review_digest(self, jobs: list[JobRecord]) -> BatchSendResult:
        """Send all ``jobs`` as one or more digest messages, chunked to
        respect Discord's field-count and aggregate character limits (see
        _chunk_digest_jobs). Each chunk is its own independent HTTP request:
        a failure on one chunk never affects any other chunk's delivery, and
        every job in a failed chunk is simply absent from
        ``BatchSendResult.delivered`` so the caller leaves it pending."""
        result = BatchSendResult()
        if not jobs:
            return result
        chunks = _chunk_digest_jobs(jobs)
        total = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            embed = _digest_embed(chunk, part=idx, total_parts=total)
            if self._post({"embeds": [embed]}):
                result.delivered.extend(chunk)
                result.chunks_sent += 1
            elif self._would_attempt:
                result.chunks_failed += 1
        return result

    def send_source_failure(self, source_id: str, company: str | None, error: str | None) -> bool:
        return self._post({"embeds": [_source_failure_embed(source_id, company, error)]})

    def send_source_recovery(self, source_id: str, company: str | None) -> bool:
        return self._post({"embeds": [_source_recovery_embed(source_id, company)]})

    def send_daily_summary(self, rows: list[sqlite3.Row], date_key: str) -> bool:
        embed = _daily_summary_embed(rows, date_key)
        if len(embed["fields"]) > MAX_FIELDS_PER_EMBED:
            # The health summary is a point-in-time snapshot (refreshed daily,
            # not an alert that must never be lost) -- if the failing-source
            # count itself somehow exceeds Discord's field cap, keep the
            # message valid by keeping only the first N and saying so, rather
            # than failing to send any health status at all.
            omitted = len(embed["fields"]) - MAX_FIELDS_PER_EMBED
            embed["fields"] = embed["fields"][:MAX_FIELDS_PER_EMBED]
            embed["description"] = _shorten(
                f"{embed.get('description', '')} ({omitted} more failing source"
                f"{'s' if omitted != 1 else ''} not shown.)",
                MAX_DESCRIPTION_LEN,
            )
        return self._post({"embeds": [embed]})
