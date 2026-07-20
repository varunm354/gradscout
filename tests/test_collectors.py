"""Offline collector parser tests. No test requires network access."""

from datetime import datetime, timedelta, timezone

from gradscout.collectors.ashby import AshbyCollector
from gradscout.collectors.base import CollectorResult, run_collector
from gradscout.collectors.github_repo import GithubRepoCollector
from gradscout.collectors.greenhouse import GreenhouseCollector
from gradscout.collectors.lever import LeverCollector
from gradscout.models import SourceStatus, SourceType


# --------------------------------------------------------------------------- #
# source_id / distinguishability
# --------------------------------------------------------------------------- #
def test_source_ids_are_stable_and_distinguishable():
    assert GreenhouseCollector("Acme", "acme").source_id == "greenhouse:acme"
    assert LeverCollector("Acme", "acme").source_id == "lever:acme"
    assert AshbyCollector("Acme", "acme").source_id == "ashby:acme"
    gh = GithubRepoCollector("SimplifyJobs-NewGrad", "http://x")
    assert gh.source_id == "github_repo:SimplifyJobs-NewGrad"
    assert gh.source_type == SourceType.github_repo


# --------------------------------------------------------------------------- #
# Greenhouse
# --------------------------------------------------------------------------- #
def test_greenhouse_parse_ok(load_fixture):
    c = GreenhouseCollector("Acme", "acme", company_priority=1)
    rows, errors = c.parse(load_fixture("greenhouse_ok.json"))
    assert errors == 0
    assert len(rows) == 2
    j = rows[0]
    assert j.source == SourceType.greenhouse
    assert j.source_company == "acme"
    assert j.company == "Acme"
    assert j.source_job_id == "555"
    assert j.apply_url == "https://boards.greenhouse.io/acme/jobs/555"  # exact
    assert j.location == "New York, NY"
    # HTML content (escaped) becomes readable text with a bullet list
    assert "Build backend systems." in j.description_text
    assert "- Python" in j.description_text
    # reliable posting date parsed (with its original -04:00 offset)
    assert j.source_posted_at == datetime(2026, 7, 1, 9, 0, tzinfo=timezone(timedelta(hours=-4)))
    # second job has no timestamps -> no fabricated date
    assert rows[1].source_posted_at is None


def test_greenhouse_empty_is_success_not_failure(load_fixture):
    c = GreenhouseCollector("Acme", "acme")
    c.fetch = lambda client=None: load_fixture("greenhouse_empty.json")
    result = run_collector(c, client=None)
    assert result.status == SourceStatus.ok
    assert result.jobs_seen == 0


def test_greenhouse_malformed_is_total_failure(load_fixture):
    c = GreenhouseCollector("Acme", "acme")
    c.fetch = lambda client=None: load_fixture("greenhouse_malformed.json")
    result = run_collector(c, client=None)
    assert result.status == SourceStatus.error
    assert result.jobs_seen == 0
    assert result.error


def test_greenhouse_missing_fields_is_partial(load_fixture):
    c = GreenhouseCollector("Acme", "acme")
    c.fetch = lambda client=None: load_fixture("greenhouse_missing_fields.json")
    result = run_collector(c, client=None)
    assert result.status == SourceStatus.partial
    assert result.jobs_seen == 1
    assert result.parse_errors == 1


# --------------------------------------------------------------------------- #
# Lever
# --------------------------------------------------------------------------- #
def test_lever_parse_ok(load_fixture):
    c = LeverCollector("Acme", "acme")
    rows, errors = c.parse(load_fixture("lever_ok.json"))
    assert errors == 0
    assert len(rows) == 2
    j = rows[0]
    assert j.source_job_id == "abc-1"
    assert j.title == "Backend Engineer, University Grad"
    assert j.apply_url == "https://jobs.lever.co/acme/abc-1"  # hostedUrl preferred
    assert j.location == "San Francisco, CA"
    assert j.source_posted_at == datetime(2025, 7, 1, 12, 0, tzinfo=timezone.utc)
    # second posting has no createdAt -> no fabricated date
    assert rows[1].source_posted_at is None
    assert rows[1].apply_url == "https://jobs.lever.co/acme/def-2/apply"  # applyUrl fallback


def test_lever_not_a_list_is_error(load_fixture):
    c = LeverCollector("Acme", "acme")
    c.fetch = lambda client=None: {"not": "a list"}
    result = run_collector(c, client=None)
    assert result.status == SourceStatus.error


# --------------------------------------------------------------------------- #
# Ashby
# --------------------------------------------------------------------------- #
def test_ashby_parse_ok(load_fixture):
    c = AshbyCollector("Acme", "acme")
    rows, errors = c.parse(load_fixture("ashby_ok.json"))
    assert errors == 0
    assert len(rows) == 2
    j = rows[0]
    assert j.source == SourceType.ashby
    assert j.title == "Machine Learning Engineer, New Grad"
    assert j.apply_url == "https://jobs.ashbyhq.com/acme/a1"  # jobUrl preferred
    assert j.source_posted_at == datetime(2026, 6, 15, tzinfo=timezone.utc)
    # second uses descriptionHtml -> plain text
    assert "Backend platform work." in rows[1].description_text
    assert "- Go" in rows[1].description_text


# --------------------------------------------------------------------------- #
# GitHub repo (Simplify JSON)
# --------------------------------------------------------------------------- #
def test_github_parse_ok_indirect(load_fixture):
    c = GithubRepoCollector("SimplifyJobs-NewGrad", "http://x")
    rows, errors = c.parse(load_fixture("github_simplify_ok.json"))
    assert errors == 0
    assert len(rows) == 2
    j = rows[0]
    assert j.source == SourceType.github_repo          # indirect coverage
    assert j.source_company == "SimplifyJobs-NewGrad"  # repo identity
    assert j.company == "Acme"                          # per-row employer
    assert j.apply_url.startswith("https://www.boards.greenhouse.io/acme/jobs/555")
    assert j.location == "New York, NY"
    assert j.source_posted_at is not None
    assert rows[1].location == "Seattle, WA, Remote"


def test_github_missing_fields_is_partial(load_fixture):
    c = GithubRepoCollector("SimplifyJobs-NewGrad", "http://x")
    c.fetch = lambda client=None: load_fixture("github_simplify_missing_fields.json")
    result = run_collector(c, client=None)
    assert result.status == SourceStatus.partial
    assert result.jobs_seen == 1        # only the good row
    assert result.parse_errors == 2     # missing url + missing title


def test_github_unknown_parser_is_error():
    c = GithubRepoCollector("Repo", "http://x", parser="generic_md")
    c.fetch = lambda client=None: []
    result = run_collector(c, client=None)
    assert result.status == SourceStatus.error


# --------------------------------------------------------------------------- #
# Fail-soft wrapper: fetch failure = total failure
# --------------------------------------------------------------------------- #
def test_run_collector_fetch_exception_is_error():
    c = GreenhouseCollector("Acme", "acme")

    def boom(client=None):
        raise RuntimeError("connection reset")

    c.fetch = boom
    result = run_collector(c, client=None)
    assert isinstance(result, CollectorResult)
    assert result.status == SourceStatus.error
    assert "connection reset" in result.error
