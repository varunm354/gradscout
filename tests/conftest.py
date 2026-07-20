import json
from pathlib import Path

import pytest

from gradscout.models import Job, SourceType
from gradscout.urls import canonicalize_url

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def load_fixture():
    def _load(name: str):
        return json.loads((FIXTURES / name).read_text())

    return _load


def make_job(
    title: str,
    description: str = "",
    *,
    company: str = "Acme",
    company_priority: int = 3,
    source: SourceType = SourceType.greenhouse,
    source_company: str = "acme",
    apply_url: str = "https://boards.greenhouse.io/acme/jobs/1",
    raw_blob: dict | None = None,
) -> Job:
    return Job(
        source=source,
        source_company=source_company,
        source_job_id="1",
        apply_url=apply_url,
        company=company,
        company_priority=company_priority,
        title=title,
        description_text=description,
        url_canonical=canonicalize_url(apply_url),
        raw_blob=raw_blob or {},
    )


@pytest.fixture()
def job_factory():
    return make_job
