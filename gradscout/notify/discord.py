"""Discord webhook delivery.

One webhook, three message shapes:
  * a concise embed per urgent/strong-match job alert (p1/p2/p3),
  * one batched embed for all pending Eligibility Review jobs (a digest,
    never one message per review job),
  * short operational embeds for a high-priority source failure/recovery
    transition and the once-daily health summary.

Delivery is a pure best-effort boundary: ``_post`` returns True only on a 2xx
response and never raises, so a Discord failure/timeout/rate-limit/non-2xx
response simply yields False and the caller (gradscout.pipeline) leaves the
underlying alert row pending for a later retry. Dry-run makes no HTTP request
at all and always returns False, so nothing is ever marked sent.

The HTTP client is injectable (tests pass an httpx.Client wired to a
httpx.MockTransport), keeping pytest fully offline.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

import httpx

from gradscout.models import AlertPriority, JobRecord

logger = logging.getLogger("gradscout.notify.discord")

DEFAULT_TIMEOUT = 10.0
# Discord embeds cap at 25 fields; also bounds how much of one run's pending
# review backlog is attempted per digest (the rest stays pending for later).
MAX_DIGEST_ITEMS = 25

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


def _job_embed(job: JobRecord) -> dict:
    fields = [
        {"name": "Company", "value": job.company, "inline": True},
        {"name": "Priority", "value": job.alert_priority.value.upper(), "inline": True},
        {"name": "Eligibility", "value": job.eligibility_status.value, "inline": True},
    ]
    if job.location:
        fields.append({"name": "Location", "value": job.location, "inline": True})

    resume_value = "-"
    if job.recommended_resume:
        conf = f" ({job.resume_confidence.value} confidence)" if job.resume_confidence else ""
        resume_value = f"{job.recommended_resume.value}{conf}"
    fields.append({"name": "Recommended resume", "value": resume_value, "inline": True})

    # Never describe first_seen_at as a posting date: label the two distinctly.
    if job.source_posted_at:
        fields.append(
            {"name": "Posted", "value": job.source_posted_at.isoformat(), "inline": True}
        )
    fields.append(
        {
            "name": "First discovered by GradScout",
            "value": job.first_seen_at.isoformat(),
            "inline": True,
        }
    )

    reason = job.resume_reason or (job.eligibility_reasons[0] if job.eligibility_reasons else "")
    return {
        "title": job.title[:256],
        "url": job.apply_url,
        "description": reason[:500] if reason else None,
        "color": _PRIORITY_COLOR.get(job.alert_priority, _DIGEST_COLOR),
        "fields": fields,
    }


def _digest_embed(jobs: list[JobRecord]) -> dict:
    shown = jobs[:MAX_DIGEST_ITEMS]
    fields = []
    for j in shown:
        reason = j.eligibility_reasons[0] if j.eligibility_reasons else "Needs manual review"
        fields.append(
            {
                "name": f"{j.company} — {j.title}"[:256],
                "value": f"[Apply]({j.apply_url}) · {reason}"[:1024],
                "inline": False,
            }
        )
    return {
        "title": f"Eligibility Review digest ({len(shown)} job{'s' if len(shown) != 1 else ''})",
        "color": _DIGEST_COLOR,
        "fields": fields,
    }


def _source_failure_embed(source_id: str, company: str | None, error: str | None) -> dict:
    return {
        "title": f"High-priority source DOWN: {company or source_id}",
        "description": f"`{source_id}` — {error or 'unknown error'}",
        "color": _FAILURE_COLOR,
    }


def _source_recovery_embed(source_id: str, company: str | None) -> dict:
    return {
        "title": f"Source recovered: {company or source_id}",
        "description": f"`{source_id}` is healthy again.",
        "color": _RECOVERY_COLOR,
    }


def _daily_summary_embed(rows: list[sqlite3.Row], date_key: str) -> dict:
    failing = [r for r in rows if r["last_status"] != "ok"]
    healthy_count = len(rows) - len(failing)
    fields = [
        {
            "name": f"{r['company'] or r['source_id']} ({r['source_id']})"[:256],
            "value": (r["last_error"] or r["last_status"])[:1024],
            "inline": False,
        }
        for r in failing
    ]
    return {
        "title": f"GradScout daily health summary — {date_key} UTC",
        "description": f"{healthy_count}/{len(rows)} sources healthy.",
        "color": _SUMMARY_DEGRADED_COLOR if failing else _SUMMARY_HEALTHY_COLOR,
        "fields": fields,
    }


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

    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()

    def _post(self, payload: dict) -> bool:
        """Send one webhook payload. Returns True ONLY on a 2xx response.
        Never raises: transport errors, timeouts, and non-2xx all yield
        False so the caller leaves the underlying alert pending."""
        if self.dry_run:
            logger.info("dry-run: discord send suppressed", extra={"fields": {"payload": payload}})
            return False
        if not self.enabled or self.client is None:
            logger.warning("discord webhook not configured; skipping send")
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

    def send_job_alert(self, job: JobRecord) -> bool:
        return self._post({"embeds": [_job_embed(job)]})

    def send_review_digest(self, jobs: list[JobRecord]) -> bool:
        if not jobs:
            return False
        return self._post({"embeds": [_digest_embed(jobs)]})

    def send_source_failure(self, source_id: str, company: str | None, error: str | None) -> bool:
        return self._post({"embeds": [_source_failure_embed(source_id, company, error)]})

    def send_source_recovery(self, source_id: str, company: str | None) -> bool:
        return self._post({"embeds": [_source_recovery_embed(source_id, company)]})

    def send_daily_summary(self, rows: list[sqlite3.Row], date_key: str) -> bool:
        return self._post({"embeds": [_daily_summary_embed(rows, date_key)]})
