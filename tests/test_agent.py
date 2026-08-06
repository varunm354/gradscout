"""Bounded LLM agent + resolver tests. No test makes a real LLM call."""

import json

from gradscout.analyze import analyze_deterministic, classify_job, classify_jobs, resolve
from gradscout.llm import JobAnalysisAgent, NullProvider
from gradscout.models import (
    AgentAnalysis,
    AlertPriority,
    Config,
    EligibilityStatus,
    RoleFamily,
)
from tests.conftest import make_job

CFG = Config()

REVIEW_JOB = make_job(
    "Software Engineer", "Backend APIs and services. 3+ years of experience required."
)
ELIGIBLE_JOB = make_job("Software Engineer, New Grad", "Backend APIs. Bachelor's degree.")
INELIGIBLE_JOB = make_job("Research Engineer", "PhD required in machine learning.")


def _agent_json(**overrides) -> str:
    payload = {
        "eligibility_status": "eligible",
        "evidence": ["reads entry-level"],
        "role_family": "backend",
        "recommended_resume": "backend",
        "resume_confidence": "medium",
        "priority_recommendation": "p2",
        "uncertainty_reasons": [],
        "requires_human_review": False,
        "deterministic_disagreement": False,
        "disagreement_reason": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


class FakeProvider:
    available = True

    def __init__(self, response: str):
        self._response = response

    def complete(self, prompt: str, *, timeout: float) -> str:
        return self._response


class TimeoutProvider:
    available = True

    def complete(self, prompt: str, *, timeout: float) -> str:
        raise TimeoutError("llm timed out")


# --------------------------------------------------------------------------- #
# Prefilter (cheap gate before any LLM work)
# --------------------------------------------------------------------------- #
def test_should_analyze_only_for_relevant_ambiguous_jobs():
    agent = JobAnalysisAgent(FakeProvider(_agent_json()))
    assert agent.should_analyze(REVIEW_JOB, analyze_deterministic(REVIEW_JOB, CFG)) is True
    # clearly eligible -> skip
    assert agent.should_analyze(ELIGIBLE_JOB, analyze_deterministic(ELIGIBLE_JOB, CFG)) is False
    # hard ineligible -> skip
    assert agent.should_analyze(INELIGIBLE_JOB, analyze_deterministic(INELIGIBLE_JOB, CFG)) is False


def test_irrelevant_ambiguous_job_is_not_sent_to_llm():
    # "Program Coordinator" has neither a credible target-role signal NOR a
    # hard nontechnical one (see gradscout.roles.NON_TARGET_TITLE_TOKENS) --
    # genuinely ambiguous, so it lands in review rather than a hard
    # ineligible/eligible verdict (unlike e.g. "Recruiter", a Phase 6
    # review-cleanup addition that is now hard-ineligible, not ambiguous).
    agent = JobAnalysisAgent(FakeProvider(_agent_json()))
    job = make_job("Program Coordinator", "3+ years of coordination experience required.")
    det = analyze_deterministic(job, CFG)
    assert det.status == EligibilityStatus.review
    assert det.relevant is False
    assert agent.should_analyze(job, det) is False


def test_hard_nontechnical_title_added_in_phase_6_is_ineligible_not_review():
    """Phase 6 review-digest cleanup: titles like Recruiter/Legal/Finance/... are
    a HARD override (never merely ambiguous->review), same treatment as the
    pre-existing sales/marketing tokens -- see gradscout.roles."""
    agent = JobAnalysisAgent(FakeProvider(_agent_json()))
    job = make_job("Recruiter", "3+ years of recruiting experience required.")
    det = analyze_deterministic(job, CFG)
    assert det.status == EligibilityStatus.ineligible
    assert det.hard_ineligible is True
    assert agent.should_analyze(job, det) is False


# --------------------------------------------------------------------------- #
# LLM disabled / timeout / invalid output -> deterministic retained
# --------------------------------------------------------------------------- #
def test_llm_disabled_without_key():
    agent = JobAnalysisAgent(NullProvider())
    assert agent.enabled is False
    assert agent.analyze(REVIEW_JOB, analyze_deterministic(REVIEW_JOB, CFG)) is None
    resolved = classify_job(REVIEW_JOB, CFG, agent)
    assert resolved.llm_used is False
    assert resolved.eligibility_status == EligibilityStatus.review


def test_llm_timeout_returns_none_and_keeps_review():
    agent = JobAnalysisAgent(TimeoutProvider())
    resolved = classify_job(REVIEW_JOB, CFG, agent)
    assert resolved.llm_used is False  # agent produced nothing
    assert resolved.eligibility_status == EligibilityStatus.review
    assert resolved.requires_human_review is True


def test_invalid_structured_response_is_ignored():
    agent = JobAnalysisAgent(FakeProvider('{"foo": "bar"}'))
    assert agent.analyze(REVIEW_JOB, analyze_deterministic(REVIEW_JOB, CFG)) is None
    agent2 = JobAnalysisAgent(FakeProvider("not json at all"))
    assert agent2.analyze(REVIEW_JOB, analyze_deterministic(REVIEW_JOB, CFG)) is None


# --------------------------------------------------------------------------- #
# Disagreement + hard-rule override
# --------------------------------------------------------------------------- #
def test_llm_resolves_review_and_records_disagreement():
    agent = JobAnalysisAgent(FakeProvider(_agent_json(eligibility_status="eligible")))
    resolved = classify_job(REVIEW_JOB, CFG, agent)
    assert resolved.llm_used is True
    assert resolved.eligibility_status == EligibilityStatus.eligible
    assert resolved.deterministic_disagreement is True   # det=review, llm=eligible
    assert resolved.decided_by == "llm-assisted"


def test_agent_cannot_override_hard_ineligibility():
    det = analyze_deterministic(INELIGIBLE_JOB, CFG)
    assert det.hard_ineligible is True
    # Even a confident "eligible" agent verdict cannot flip a hard rule.
    agent_out = AgentAnalysis(
        eligibility_status=EligibilityStatus.eligible,
        role_family=RoleFamily.ai,
        priority_recommendation=AlertPriority.p1,
        requires_human_review=False,
    )
    resolved = resolve(det, agent_out)
    assert resolved.eligibility_status == EligibilityStatus.ineligible
    assert resolved.alert_priority == AlertPriority.ineligible
    assert resolved.deterministic_disagreement is True
    assert "hard rule" in resolved.decided_by
    assert resolved.disagreement_reason


# --------------------------------------------------------------------------- #
# Batch: performance prefilter + counts
# --------------------------------------------------------------------------- #
def test_classify_jobs_stats_and_skipping():
    agent = JobAnalysisAgent(FakeProvider(_agent_json(eligibility_status="eligible")))
    skipped_job = make_job("Old Role", "Bachelor's degree.", apply_url="https://x/old")
    jobs = [ELIGIBLE_JOB, INELIGIBLE_JOB, REVIEW_JOB, skipped_job]

    def is_new(job):
        return job is not skipped_job

    stats = classify_jobs(jobs, CFG, agent, is_new_or_changed=is_new)
    assert stats.total == 4
    assert stats.skipped == 1
    assert stats.sent_to_llm == 1          # only the review+relevant job
    assert stats.llm_succeeded == 1
    assert stats.eligible >= 2             # ELIGIBLE_JOB + LLM-resolved REVIEW_JOB
    assert stats.ineligible == 1
    assert stats.disagreements == 1


def test_no_agent_runs_fully_deterministic():
    stats = classify_jobs([ELIGIBLE_JOB, REVIEW_JOB, INELIGIBLE_JOB], CFG, agent=None)
    assert stats.sent_to_llm == 0
    assert stats.eligible == 1
    assert stats.review == 1
    assert stats.ineligible == 1
