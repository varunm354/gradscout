from gradscout.analyze import analyze_deterministic
from gradscout.models import AlertPriority, Config, WatchlistCompany
from gradscout.prioritize import resolve_company_priority
from tests.conftest import make_job


def _config(watchlist=None):
    return Config(watchlist=watchlist or [])


# --- company priority / watchlist aliases ---
def test_watchlist_exact_name_match():
    cfg = _config([WatchlistCompany(name="Stripe", company_priority=1)])
    prio, matched = resolve_company_priority(make_job("SWE", company="Stripe"), cfg)
    assert prio == 1
    assert matched == "Stripe"


def test_watchlist_alias_match():
    cfg = _config([WatchlistCompany(name="OpenAI", company_priority=1, aliases=["open ai"])])
    prio, matched = resolve_company_priority(
        make_job("SWE", company="Open AI Inc"), cfg
    )
    assert prio == 1
    assert matched == "OpenAI"


def test_no_watchlist_match_falls_back_to_source_priority():
    cfg = _config([WatchlistCompany(name="Stripe", company_priority=1)])
    prio, matched = resolve_company_priority(
        make_job("SWE", company="Nobody Inc", company_priority=3), cfg
    )
    assert prio == 3
    assert matched is None


# --- alert priority tiers ---
def test_p1_watchlist_fulltime_newgrad():
    cfg = _config([WatchlistCompany(name="Stripe", company_priority=1)])
    job = make_job(
        "Software Engineer, New Grad",
        "Backend APIs and distributed systems. Bachelor's degree.",
        company="Stripe",
    )
    det = analyze_deterministic(job, cfg, is_recent=True)
    assert det.alert_priority == AlertPriority.p1


def test_p2_eligible_newgrad_non_watchlist():
    cfg = _config([])
    job = make_job(
        "Software Engineer, New Grad",
        "Backend APIs and distributed systems. Bachelor's degree.",
        company="SmallCo",
        company_priority=3,
    )
    det = analyze_deterministic(job, cfg, is_recent=True)
    assert det.alert_priority == AlertPriority.p2


def test_p3_eligible_internship():
    cfg = _config([])
    job = make_job(
        "Software Engineering Intern",
        "Open to graduating seniors and recent graduates. Backend work.",
        company="SmallCo",
    )
    det = analyze_deterministic(job, cfg, is_recent=True)
    assert det.alert_priority == AlertPriority.p3


def test_review_priority_for_ambiguous_internship():
    cfg = _config([])
    job = make_job("Software Engineering Intern", "Summer platform work.", company="SmallCo")
    det = analyze_deterministic(job, cfg)
    assert det.alert_priority == AlertPriority.review


def test_ineligible_priority_for_masters_required():
    cfg = _config([])
    job = make_job("Software Engineer", "Master's degree required.", company="SmallCo")
    det = analyze_deterministic(job, cfg)
    assert det.alert_priority == AlertPriority.ineligible


def test_p1_requires_recent():
    cfg = _config([WatchlistCompany(name="Stripe", company_priority=1)])
    job = make_job(
        "Software Engineer, New Grad",
        "Backend APIs. Bachelor's degree.",
        company="Stripe",
    )
    det = analyze_deterministic(job, cfg, is_recent=False)
    assert det.alert_priority == AlertPriority.p2  # not recent -> falls to p2
