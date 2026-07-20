"""Lever collector.

Official postings API: https://api.lever.co/v0/postings/{company}?mode=json
Returns a JSON array. Each posting: id, text (title), categories.{location,team,
commitment}, createdAt (epoch ms, reliable), descriptionPlain / description (HTML),
hostedUrl / applyUrl.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gradscout.collectors.base import Collector, parse_epoch
from gradscout.htmltext import html_to_text
from gradscout.models import RawJob, SourceType

API = "https://api.lever.co/v0/postings/{company}?mode=json"


@dataclass
class LeverCollector(Collector):
    def __init__(self, company: str, board: str, company_priority: int = 3):
        super().__init__(
            source_type=SourceType.lever,
            company=company,
            slug=board,
            company_priority=company_priority,
        )

    def url(self) -> str:
        return API.format(company=self.slug)

    def parse(self, payload: Any) -> tuple[list[RawJob], int]:
        if not isinstance(payload, list):
            raise ValueError("lever: payload is not a list")
        rows: list[RawJob] = []
        errors = 0
        for row in payload:
            try:
                job_id = row["id"]
                title = row["text"]
                apply_url = row.get("hostedUrl") or row.get("applyUrl")
                if not title or not apply_url:
                    raise KeyError("missing text/url")
                categories = row.get("categories") or {}
                description = row.get("descriptionPlain") or html_to_text(
                    row.get("description")
                )
                rows.append(
                    RawJob(
                        source=SourceType.lever,
                        source_company=self.slug,
                        company=self.company,
                        source_job_id=str(job_id),
                        title=title,
                        location=categories.get("location"),
                        description_text=description,
                        apply_url=apply_url,
                        posted_at_raw=str(row.get("createdAt"))
                        if row.get("createdAt")
                        else None,
                        source_posted_at=parse_epoch(row.get("createdAt"), unit="ms"),
                        raw_blob=row,
                    )
                )
            except Exception:
                errors += 1
        return rows, errors
