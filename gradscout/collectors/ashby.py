"""Ashby collector.

Official public posting API:
    https://api.ashbyhq.com/posting-api/job-board/{board}
Returns {"jobs": [...], "apiVersion": ...}. Each job: id, title, location,
employmentType, isRemote, publishedAt (ISO, reliable), jobUrl / applyUrl,
descriptionPlain / descriptionHtml. Confirmed stable and structured, so it is
implemented here rather than deferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gradscout.collectors.base import Collector, parse_iso
from gradscout.htmltext import html_to_text
from gradscout.models import RawJob, SourceType

API = "https://api.ashbyhq.com/posting-api/job-board/{board}"


@dataclass
class AshbyCollector(Collector):
    def __init__(self, company: str, board: str, company_priority: int = 3):
        super().__init__(
            source_type=SourceType.ashby,
            company=company,
            slug=board,
            company_priority=company_priority,
        )

    def url(self) -> str:
        return API.format(board=self.slug)

    def parse(self, payload: Any) -> tuple[list[RawJob], int]:
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError("ashby: payload missing 'jobs' list")
        rows: list[RawJob] = []
        errors = 0
        for row in payload["jobs"]:
            try:
                job_id = row["id"]
                title = row["title"]
                apply_url = row.get("jobUrl") or row.get("applyUrl")
                if not title or not apply_url:
                    raise KeyError("missing title/url")
                description = row.get("descriptionPlain") or html_to_text(
                    row.get("descriptionHtml")
                )
                rows.append(
                    RawJob(
                        source=SourceType.ashby,
                        source_company=self.slug,
                        company=self.company,
                        source_job_id=str(job_id),
                        title=title,
                        location=row.get("location"),
                        description_text=description,
                        apply_url=apply_url,
                        posted_at_raw=str(row.get("publishedAt"))
                        if row.get("publishedAt")
                        else None,
                        source_posted_at=parse_iso(row.get("publishedAt")),
                        raw_blob=row,
                    )
                )
            except Exception:
                errors += 1
        return rows, errors
