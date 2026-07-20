"""Greenhouse collector.

Official board API: https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true
Each job: id, title, absolute_url, location.name, first_published/updated_at (ISO),
content (HTML, often HTML-escaped). first_published is a reliable posting date.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gradscout.collectors.base import Collector, parse_iso
from gradscout.htmltext import html_to_text
from gradscout.models import RawJob, SourceType

API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"


@dataclass
class GreenhouseCollector(Collector):
    def __init__(self, company: str, board: str, company_priority: int = 3):
        super().__init__(
            source_type=SourceType.greenhouse,
            company=company,
            slug=board,
            company_priority=company_priority,
        )

    def url(self) -> str:
        return API.format(board=self.slug)

    def parse(self, payload: Any) -> tuple[list[RawJob], int]:
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError("greenhouse: payload missing 'jobs' list")
        rows: list[RawJob] = []
        errors = 0
        for row in payload["jobs"]:
            try:
                job_id = row["id"]
                title = row["title"]
                apply_url = row["absolute_url"]
                if not title or not apply_url:
                    raise KeyError("missing title/absolute_url")
                location = (row.get("location") or {}).get("name")
                posted_raw = row.get("first_published") or row.get("updated_at")
                rows.append(
                    RawJob(
                        source=SourceType.greenhouse,
                        source_company=self.slug,
                        company=self.company,
                        source_job_id=str(job_id),
                        title=title,
                        location=location,
                        description_text=html_to_text(row.get("content")),
                        apply_url=apply_url,
                        posted_at_raw=str(posted_raw) if posted_raw else None,
                        source_posted_at=parse_iso(row.get("first_published"))
                        or parse_iso(row.get("updated_at")),
                        raw_blob=row,
                    )
                )
            except Exception:
                errors += 1
        return rows, errors
