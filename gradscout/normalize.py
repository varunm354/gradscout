"""RawJob -> Job normalization.

Keeps the original application URL exactly as the source provided it, and computes
``url_canonical`` separately (the cross-source merge key). Never fabricates a
posting date: ``source_posted_at`` is carried through only if the collector set it.
Classification fields are left at their defaults for Phase 3 to populate.
"""

from __future__ import annotations

from gradscout.models import Job, RawJob
from gradscout.urls import canonicalize_url


def normalize(raw: RawJob, company_priority: int = 3) -> Job:
    return Job(
        source=raw.source,
        source_company=raw.source_company,
        source_job_id=raw.source_job_id,
        apply_url=raw.apply_url,               # preserved exactly
        company=raw.company,
        company_priority=company_priority,
        title=raw.title.strip(),
        location=(raw.location.strip() if raw.location else None),
        description_text=raw.description_text,
        url_canonical=canonicalize_url(raw.apply_url),
        source_posted_at=raw.source_posted_at,
        raw_blob=raw.raw_blob,
    )
