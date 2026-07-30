"""Deterministic Bay Area / Northern California location classification (Phase 5.2).

No network, no DB -- pure unit tests of gradscout.location. Every scenario in the
Phase 5.2 spec is covered: accepted (preferred/remote_acceptable) and rejected
(out_of_region/unclear) examples, plus the priority-penalty and alert-gating helpers.
"""

from __future__ import annotations

from gradscout.analyze import analyze_deterministic, resolve
from gradscout.location import (
    classify_location,
    location_label,
    location_permits_alert,
)
from gradscout.models import (
    AgentAnalysis,
    AlertPriority,
    CandidateProfile,
    Config,
    EligibilityStatus,
    LocationClassification,
    RoleFamily,
    WatchlistCompany,
)
from gradscout.prioritize import apply_location_penalty
from tests.conftest import make_job

DEFAULT_PREFERRED = CandidateProfile().preferred_locations


def _classify(location, remote=None):
    return classify_location(location, remote, DEFAULT_PREFERRED)


# --------------------------------------------------------------------------- #
# Accepted: preferred (Bay Area / Northern California)
# --------------------------------------------------------------------------- #
def test_san_francisco_is_preferred():
    r = _classify("San Francisco, CA")
    assert r.classification == LocationClassification.preferred


def test_san_jose_is_preferred():
    r = _classify("San Jose, CA")
    assert r.classification == LocationClassification.preferred


def test_silicon_valley_is_preferred():
    r = _classify("Silicon Valley")
    assert r.classification == LocationClassification.preferred


def test_northern_california_is_preferred():
    r = _classify("Northern California")
    assert r.classification == LocationClassification.preferred


def test_norcal_is_preferred():
    assert _classify("NorCal (Remote OK)").classification == LocationClassification.preferred


def test_all_named_bay_area_cities_are_preferred():
    cities = [
        "San Francisco", "San Jose", "Santa Clara", "Sunnyvale", "Mountain View",
        "Palo Alto", "Redwood City", "Menlo Park", "Cupertino", "Oakland", "Berkeley",
        "Fremont", "Pleasanton", "San Mateo", "South San Francisco", "Bay Area",
    ]
    for city in cities:
        r = _classify(f"{city}, CA")
        assert r.classification == LocationClassification.preferred, city


def test_multi_location_bay_area_plus_new_york_is_preferred():
    """At least one valid Bay Area/NorCal location offered -> preferred, even
    though New York is also listed."""
    r = _classify("San Francisco, CA, New York, NY")
    assert r.classification == LocationClassification.preferred
    assert "san francisco" in r.reason.lower()


def test_multi_location_order_independent():
    r = _classify("New York, NY, Bay Area, CA")
    assert r.classification == LocationClassification.preferred


# --------------------------------------------------------------------------- #
# Accepted: remote_acceptable (clearly U.S.-remote)
# --------------------------------------------------------------------------- #
def test_us_remote_is_remote_acceptable():
    r = _classify("Remote - United States")
    assert r.classification == LocationClassification.remote_acceptable


def test_remote_us_parenthetical_is_remote_acceptable():
    r = _classify("Remote (US)")
    assert r.classification == LocationClassification.remote_acceptable


def test_remote_usa_is_remote_acceptable():
    r = _classify("Remote, USA")
    assert r.classification == LocationClassification.remote_acceptable


def test_structured_remote_flag_plus_us_text_is_remote_acceptable():
    """'Structured data proves U.S.-remote': the location text alone ('United
    States') doesn't say remote, but the collector's structured isRemote flag
    (surfaced as job.remote) combined with the U.S. country text does."""
    r = _classify("United States", remote=True)
    assert r.classification == LocationClassification.remote_acceptable


def test_us_remote_wins_even_if_other_countries_also_listed():
    """Explicit U.S.-remote eligibility wins even if other supported countries
    are also mentioned (per the clarified policy)."""
    r = _classify("Remote (US, Canada, UK)")
    assert r.classification == LocationClassification.remote_acceptable


# --------------------------------------------------------------------------- #
# Rejected: out_of_region (onsite/hybrid outside NorCal)
# --------------------------------------------------------------------------- #
def test_los_angeles_is_out_of_region():
    assert _classify("Los Angeles, CA").classification == LocationClassification.out_of_region


def test_irvine_is_out_of_region():
    assert _classify("Irvine, CA").classification == LocationClassification.out_of_region


def test_san_diego_is_out_of_region():
    assert _classify("San Diego, CA").classification == LocationClassification.out_of_region


def test_orange_county_is_out_of_region():
    assert _classify("Orange County, CA").classification == LocationClassification.out_of_region


def test_new_york_is_out_of_region():
    assert _classify("New York, NY").classification == LocationClassification.out_of_region


def test_dallas_is_out_of_region():
    assert _classify("Dallas, TX").classification == LocationClassification.out_of_region


# --------------------------------------------------------------------------- #
# Rejected: out_of_region (remote restricted outside the U.S.)
# --------------------------------------------------------------------------- #
def test_remote_india_is_out_of_region():
    r = _classify("Remote (India)")
    assert r.classification == LocationClassification.out_of_region


def test_remote_international_is_out_of_region():
    r = _classify("Remote - International")
    assert r.classification == LocationClassification.out_of_region


def test_remote_outside_us_negation_is_out_of_region():
    """'Outside the US' contains the literal word 'us' but negates it -- must
    not be misread as a U.S. indicator."""
    r = _classify("Remote (outside the US only)")
    assert r.classification == LocationClassification.out_of_region


# --------------------------------------------------------------------------- #
# Rejected / safe fallback: unclear
# --------------------------------------------------------------------------- #
def test_missing_location_is_unclear():
    r = _classify(None)
    assert r.classification == LocationClassification.unclear


def test_bare_remote_with_no_country_is_unclear():
    r = _classify("Remote")
    assert r.classification == LocationClassification.unclear


def test_structured_remote_with_no_text_at_all_is_unclear():
    r = _classify(None, remote=True)
    assert r.classification == LocationClassification.unclear


def test_bare_california_is_unclear_not_preferred():
    """Do not treat all California locations as preferred."""
    r = _classify("California")
    assert r.classification == LocationClassification.unclear


def test_unrecognized_city_is_unclear():
    r = _classify("Springfield")
    assert r.classification == LocationClassification.unclear


# --------------------------------------------------------------------------- #
# location_permits_alert policy
# --------------------------------------------------------------------------- #
def test_preferred_always_permits_alert():
    cand = CandidateProfile()
    assert location_permits_alert(LocationClassification.preferred, cand) is True


def test_remote_acceptable_permits_alert_when_allowed():
    cand = CandidateProfile(allow_us_remote=True)
    assert location_permits_alert(LocationClassification.remote_acceptable, cand) is True


def test_remote_acceptable_blocked_when_not_allowed():
    cand = CandidateProfile(allow_us_remote=False)
    assert location_permits_alert(LocationClassification.remote_acceptable, cand) is False


def test_out_of_region_never_permits_alert():
    cand = CandidateProfile()
    assert location_permits_alert(LocationClassification.out_of_region, cand) is False


def test_unclear_never_permits_alert():
    cand = CandidateProfile()
    assert location_permits_alert(LocationClassification.unclear, cand) is False


def test_location_not_required_permits_everything():
    cand = CandidateProfile(location_required_for_alert=False)
    for classification in LocationClassification:
        assert location_permits_alert(classification, cand) is True


# --------------------------------------------------------------------------- #
# apply_location_penalty
# --------------------------------------------------------------------------- #
def test_penalty_p1_to_p2_with_rank_one():
    assert (
        apply_location_penalty(AlertPriority.p1, LocationClassification.remote_acceptable, 1)
        == AlertPriority.p2
    )


def test_penalty_p2_to_p3_with_rank_one():
    assert (
        apply_location_penalty(AlertPriority.p2, LocationClassification.remote_acceptable, 1)
        == AlertPriority.p3
    )


def test_penalty_p3_stays_p3_with_rank_one():
    """p3 is already the lowest urgency tier -- a one-rank penalty is a no-op."""
    assert (
        apply_location_penalty(AlertPriority.p3, LocationClassification.remote_acceptable, 1)
        == AlertPriority.p3
    )


def test_penalty_caps_at_p3():
    assert (
        apply_location_penalty(AlertPriority.p2, LocationClassification.remote_acceptable, 5)
        == AlertPriority.p3
    )


def test_penalty_does_not_affect_preferred():
    assert (
        apply_location_penalty(AlertPriority.p1, LocationClassification.preferred, 1)
        == AlertPriority.p1
    )


def test_penalty_does_not_affect_review_or_ineligible():
    assert (
        apply_location_penalty(AlertPriority.review, LocationClassification.remote_acceptable, 1)
        == AlertPriority.review
    )
    assert (
        apply_location_penalty(AlertPriority.ineligible, LocationClassification.remote_acceptable, 1)
        == AlertPriority.ineligible
    )


def test_zero_penalty_is_a_noop():
    assert (
        apply_location_penalty(AlertPriority.p1, LocationClassification.remote_acceptable, 0)
        == AlertPriority.p1
    )


# --------------------------------------------------------------------------- #
# location_label
# --------------------------------------------------------------------------- #
def test_location_label_is_human_readable_for_every_classification():
    for classification in LocationClassification:
        label = location_label(classification)
        assert isinstance(label, str) and label


# --------------------------------------------------------------------------- #
# resolve() applies the location penalty exactly once (regression coverage
# for the "Stripe p1 -> p3" report inconsistency -- see investigation below).
# --------------------------------------------------------------------------- #
_PRIORITY_ONE_CONFIG = Config(
    watchlist=[WatchlistCompany(name="Acme", company_priority=1)],
)


def test_resolve_no_llm_applies_penalty_exactly_once():
    """A p1-tier, remote-acceptable job must land on p2 (one rank down), and
    resolve()'s deterministic-only path must simply pass det.alert_priority
    through unchanged -- it must not re-derive or re-penalize it."""
    job = make_job(
        "Software Engineer, New Grad",
        "Backend APIs. Bachelor's degree.",
        company="Acme",
        company_priority=1,
        location="Remote - United States",
    )
    det = analyze_deterministic(job, _PRIORITY_ONE_CONFIG, is_recent=True)
    assert det.status == EligibilityStatus.eligible
    assert det.company_priority == 1
    assert det.location_classification == LocationClassification.remote_acceptable
    assert det.alert_priority == AlertPriority.p2  # p1 pre-penalty, penalized once

    resolved = resolve(det, None)
    assert resolved.alert_priority == AlertPriority.p2
    assert resolved.alert_priority == det.alert_priority  # passthrough, not re-applied


def test_resolve_with_llm_applies_penalty_exactly_once():
    """Same scenario, but resolved through the LLM-assisted branch: the agent
    confirms 'eligible', priority is recomputed from scratch (deterministic
    inputs, unaffected by the LLM), and the penalty is applied exactly once."""
    job = make_job(
        "Software Engineer, New Grad",
        "Backend APIs. Bachelor's degree.",
        company="Acme",
        company_priority=1,
        location="Remote - United States",
    )
    det = analyze_deterministic(job, _PRIORITY_ONE_CONFIG, is_recent=True)
    assert det.alert_priority == AlertPriority.p2  # already-penalized deterministic value

    agent_out = AgentAnalysis(
        eligibility_status=EligibilityStatus.eligible,
        role_family=RoleFamily.backend,
        priority_recommendation=AlertPriority.p1,
        requires_human_review=False,
    )
    resolved = resolve(det, agent_out)
    # Recomputed from det's (unpenalized) inputs -> p1, then penalized once -> p2.
    # If the penalty were mistakenly applied twice (e.g. by reusing det.alert_priority,
    # which is already-penalized, as an input) this would incorrectly come out as p3.
    assert resolved.alert_priority == AlertPriority.p2


def test_hard_ineligible_never_alerts_regardless_of_location():
    """A hard-ineligible job must stay AlertPriority.ineligible no matter its
    location classification, both with and without an LLM opinion."""
    job = make_job(
        "Research Engineer",
        "PhD required in machine learning.",
        company="Acme",
        company_priority=1,
        location="Remote - United States",  # remote_acceptable location
    )
    det = analyze_deterministic(job, _PRIORITY_ONE_CONFIG, is_recent=True)
    assert det.hard_ineligible is True
    assert det.location_classification == LocationClassification.remote_acceptable
    assert det.alert_priority == AlertPriority.ineligible

    resolved_no_llm = resolve(det, None)
    assert resolved_no_llm.alert_priority == AlertPriority.ineligible

    agent_out = AgentAnalysis(
        eligibility_status=EligibilityStatus.eligible,  # LLM disagrees; must not matter
        role_family=RoleFamily.ai,
        priority_recommendation=AlertPriority.p1,
        requires_human_review=False,
    )
    resolved_with_llm = resolve(det, agent_out)
    assert resolved_with_llm.alert_priority == AlertPriority.ineligible
    assert resolved_with_llm.eligibility_status == EligibilityStatus.ineligible
