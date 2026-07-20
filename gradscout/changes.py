"""Deterministic content hashing for material-change detection.

The hash covers only fields that could change an eligibility, role, resume, or
priority decision: title, description, location, and structured employment /
degree / sponsorship hints carried in the collector's raw payload. It
deliberately EXCLUDES volatile fields (``last_seen_at``, any collector
timestamp, source-health state) and is insensitive to raw-payload key
ordering, since ``dict`` insertion order in ``raw_blob`` carries no meaning.

``db.upsert_job`` compares this hash against the stored value to decide
whether a re-seen posting is genuinely unchanged, so unchanged jobs are never
needlessly reclassified or re-alerted.
"""

from __future__ import annotations

import hashlib
import json

from gradscout.models import Job


def _employment_hint(raw_blob: dict) -> str:
    """Best-effort structured employment-type hint (Ashby / Lever), mirroring
    the same fields ``eligibility.detect_employment_type`` inspects."""
    employment_type = raw_blob.get("employmentType")
    if isinstance(employment_type, str):
        return employment_type.strip().lower()
    categories = raw_blob.get("categories")
    if isinstance(categories, dict):
        return str(categories.get("commitment", "")).strip().lower()
    return ""


def compute_content_hash(job: Job) -> str:
    """Hash of the job-analysis-relevant fields only.

    Two normalizations of the same underlying posting (re-fetched with a
    different raw_blob key order, or with a bumped last_seen_at) hash equal;
    an edited title/description/location/employment/degree/sponsorship signal
    hashes differently.
    """
    blob = job.raw_blob or {}
    sponsorship = blob.get("sponsorship")
    degrees = blob.get("degrees")
    payload = {
        "title": (job.title or "").strip(),
        "description_text": (job.description_text or "").strip(),
        "location": (job.location or "").strip(),
        "employment_hint": _employment_hint(blob),
        "sponsorship": sponsorship if isinstance(sponsorship, str) else "",
        "degrees": sorted(degrees) if isinstance(degrees, list) else [],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
