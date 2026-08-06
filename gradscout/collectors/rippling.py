"""Rippling ATS collector (Phase 6 startup discovery).

Companies using Rippling's own applicant-tracking product (e.g. Rippling
itself) publish a public, unauthenticated JSON API distinct from Greenhouse/
Lever/Ashby:

    GET https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs

Live-verified: returns a top-level JSON ARRAY (not paginated -- confirmed by
fetching Rippling's own board, which returned all 768 open jobs in a single
response), where each item looks like:

    {
      "uuid": "...", "name": "Account Executive ...",
      "department": {"id": "Sales", "label": "Sales"},
      "url": "https://ats.rippling.com/<board>/jobs/<uuid>",
      "workLocation": {"label": "Remote (Connecticut, US)", "id": "..."}
    }

The list endpoint carries no description text and no reliable posted-at
timestamp -- both are simply left absent (never fabricated) rather than
guessed from an unrelated field, consistent with the rest of the pipeline's
"never fabricate a timestamp" rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gradscout.collectors.base import Collector
from gradscout.models import RawJob, SourceType

API = "https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs"


@dataclass
class RipplingCollector(Collector):
    def __init__(self, company: str, board: str, company_priority: int = 3):
        super().__init__(
            source_type=SourceType.rippling,
            company=company,
            slug=board,
            company_priority=company_priority,
        )

    def url(self) -> str:
        return API.format(board=self.slug)

    def parse(self, payload: Any) -> tuple[list[RawJob], int]:
        if not isinstance(payload, list):
            raise ValueError("rippling: expected a top-level JSON array of jobs")
        rows: list[RawJob] = []
        errors = 0
        for row in payload:
            try:
                job_id = row["uuid"]
                title = row["name"]
                apply_url = row.get("url")
                if not title or not apply_url:
                    raise KeyError("missing title/url")
                location = None
                work_location = row.get("workLocation")
                if isinstance(work_location, dict):
                    location = work_location.get("label")
                elif isinstance(work_location, str):
                    location = work_location
                rows.append(
                    RawJob(
                        source=SourceType.rippling,
                        source_company=self.slug,
                        company=self.company,
                        source_job_id=str(job_id),
                        title=title,
                        location=location,
                        description_text=None,
                        apply_url=apply_url,
                        posted_at_raw=None,
                        source_posted_at=None,
                        raw_blob=row,
                    )
                )
            except Exception:
                errors += 1
        return rows, errors
