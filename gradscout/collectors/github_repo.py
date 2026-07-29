"""GitHub new-grad repository collector.

These aggregator repos are INDIRECT coverage: a row here means "someone listed a
posting", not that GradScout monitors that company's board directly. When a row's
``url`` points at a native ATS posting we also collect directly, the DB merges the
two by canonical URL while preserving both source records.

Primary supported format is the SimplifyJobs structured feed (listings.json):
    [{source, category, company_name, id, title, active, date_updated,
      date_posted (epoch s), url, locations[], degrees[], is_visible, ...}]
A structured JSON feed is far more reliable than scraping the HTML README, so we
use it instead of a brittle markdown table parser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gradscout.collectors.base import Collector, parse_epoch
from gradscout.models import RawJob, SourceType


@dataclass
class GithubRepoCollector(Collector):
    def __init__(self, name: str, url: str, parser: str = "simplify_json", company_priority: int = 3):
        super().__init__(
            source_type=SourceType.github_repo,
            company=name,          # repo identity; per-row employer set below
            slug=name,
            company_priority=company_priority,
        )
        self._url = url
        self.parser = parser

    def url(self) -> str:
        return self._url

    def parse(self, payload: Any) -> tuple[list[RawJob], int]:
        if self.parser != "simplify_json":
            raise NotImplementedError(f"unsupported github parser: {self.parser}")
        if not isinstance(payload, list):
            raise ValueError("github_repo: simplify_json payload is not a list")
        rows: list[RawJob] = []
        errors = 0
        for row in payload:
            try:
                if row.get("active") is False:
                    # Simplify keeps closed postings in the feed permanently
                    # for historical record; skipping them (rather than just
                    # relying on downstream dedup) stops every run from
                    # re-collecting the entire historical/global feed as
                    # "new" for any listing GradScout hasn't seen yet.
                    continue
                company = row["company_name"]
                title = row["title"]
                apply_url = row["url"]
                if not company or not title or not apply_url:
                    raise KeyError("missing company_name/title/url")
                locations = row.get("locations") or []
                location = ", ".join(locations) if locations else None
                rows.append(
                    RawJob(
                        source=SourceType.github_repo,
                        source_company=self.slug,       # repo identity
                        company=company,                # per-row employer
                        source_job_id=str(row["id"]) if row.get("id") else None,
                        title=title,
                        location=location,
                        description_text=None,          # feed has no description
                        apply_url=apply_url,
                        posted_at_raw=str(row.get("date_posted"))
                        if row.get("date_posted")
                        else None,
                        source_posted_at=parse_epoch(row.get("date_posted"), unit="s"),
                        raw_blob=row,
                    )
                )
            except Exception:
                errors += 1
        return rows, errors
