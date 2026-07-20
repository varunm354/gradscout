from datetime import datetime, timezone

from gradscout import db
from gradscout.collectors.github_repo import GithubRepoCollector
from gradscout.collectors.greenhouse import GreenhouseCollector
from gradscout.models import RawJob, SourceType
from gradscout.normalize import normalize
from gradscout.urls import canonicalize_url


def test_normalize_preserves_apply_url_and_computes_canonical():
    raw = RawJob(
        source=SourceType.greenhouse,
        source_company="acme",
        company="Acme",
        source_job_id="1",
        title="  Software Engineer  ",
        location="  New York, NY ",
        apply_url="https://boards.greenhouse.io/acme/jobs/1?utm_source=x",
    )
    job = normalize(raw, company_priority=1)
    assert job.apply_url == "https://boards.greenhouse.io/acme/jobs/1?utm_source=x"  # exact
    assert job.url_canonical == "https://boards.greenhouse.io/acme/jobs/1"          # cleaned
    assert job.title == "Software Engineer"     # trimmed
    assert job.location == "New York, NY"       # trimmed
    assert job.company_priority == 1


def test_normalize_does_not_fabricate_posted_at():
    raw = RawJob(
        source=SourceType.lever,
        source_company="acme",
        company="Acme",
        title="Backend Engineer",
        apply_url="https://jobs.lever.co/acme/x",
    )
    assert normalize(raw).source_posted_at is None


def test_normalize_carries_reliable_posted_at():
    ts = datetime(2026, 6, 15, tzinfo=timezone.utc)
    raw = RawJob(
        source=SourceType.ashby,
        source_company="acme",
        company="Acme",
        title="ML Engineer",
        apply_url="https://jobs.ashbyhq.com/acme/a1",
        source_posted_at=ts,
    )
    assert normalize(raw).source_posted_at == ts


def test_cross_source_merge_ats_and_github_preserves_both_sources(load_fixture):
    """One Greenhouse (direct) fixture + one GitHub (indirect) fixture pointing at
    the same posting merge into a single job, with both source records preserved."""
    conn = db.connect(":memory:")
    db.init_db(conn)

    gh = GreenhouseCollector("Acme", "acme", company_priority=1)
    gh_rows, _ = gh.parse(load_fixture("greenhouse_ok.json"))
    ats_job = normalize(gh_rows[0], company_priority=1)  # jobs/555, direct

    repo = GithubRepoCollector("SimplifyJobs-NewGrad", "http://x")
    repo_rows, _ = repo.parse(load_fixture("github_simplify_ok.json"))
    repo_job = normalize(repo_rows[0], company_priority=3)  # same 555, indirect

    # sanity: different exact URLs, identical canonical URL
    assert ats_job.apply_url != repo_job.apply_url
    assert canonicalize_url(ats_job.apply_url) == canonicalize_url(repo_job.apply_url)

    r1 = db.upsert_job(conn, ats_job)
    r2 = db.upsert_job(conn, repo_job)

    assert r1.status.value == "created"
    assert r1.job_id == r2.job_id     # merged, not a second job
    assert db.count_jobs(conn) == 1

    rec = db.get_job(conn, r1.job_id)
    sources = {s.source for s in rec.sources}
    assert sources == {SourceType.greenhouse, SourceType.github_repo}
    # both original apply URLs preserved on their respective source records
    urls = {s.source: s.apply_url for s in rec.sources}
    assert urls[SourceType.greenhouse] == "https://boards.greenhouse.io/acme/jobs/555"
    assert urls[SourceType.github_repo].startswith(
        "https://www.boards.greenhouse.io/acme/jobs/555"
    )
    conn.close()
