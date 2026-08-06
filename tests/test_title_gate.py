"""Regression tests for Phase 5.1's title-first technical relevance gate.

Covers the two layers the gate feeds into:
- ``gradscout.roles.evaluate_title_gate`` / ``classify_role`` (family + relevance)
- ``gradscout.eligibility.evaluate_eligibility`` (hard-ineligible / review / eligible)

Every named false-positive example from the incident report is asserted
``ineligible`` (hard) at the eligibility layer and ``RoleFamily.other`` /
``relevant=False`` at the role layer -- even when paired with an AI/ML-heavy
description, since the title gate must block scoring the description at all.
Every legitimate credible title is asserted ``relevant=True`` and correctly
family-classified, and eligible (given an otherwise-clean description).
Ambiguous titles are asserted to land in ``review``, not ineligible and not a
silent normal alert.
"""

from __future__ import annotations

import pytest

from gradscout.eligibility import evaluate_eligibility
from gradscout.models import CandidateProfile, EligibilityStatus, RoleFamily
from gradscout.roles import classify_role, evaluate_title_gate
from tests.conftest import make_job

CAND = CandidateProfile()

AI_HEAVY_DESCRIPTION = (
    "Leverage AI and machine learning, including LLMs, generative AI, and deep "
    "learning models, to power our platform. Experience with PyTorch, MLOps, and "
    "computer vision is a plus."
)

# --------------------------------------------------------------------------- #
# Named false positives -- must never become a normal alert or a target role,
# even with an AI/ML-heavy description.
# --------------------------------------------------------------------------- #
FALSE_POSITIVE_TITLES = [
    "Biological Safety Research Scientist",
    "AI Compliance Officer",
    "Data Scientist, Marketing",
    "AI Policy Fellow",
    "Partnerships Associate, AI Solutions",
    # Phase 6 review-digest cleanup additions.
    "Legal Counsel, AI Policy",
    "Corporate Attorney",
    "Procurement Specialist, AI Vendors",
    "HR Business Partner, Engineering",
    "Human Resources Generalist",
    "Customer Success Manager, AI Platform",
    "Business Affairs Coordinator",
    "Workplace Experience Coordinator",
    "Finance Analyst, Machine Learning Products",
    "Accounting Manager",
    "Technical Recruiter, Engineering",
    "Recruiting Coordinator",
    "Talent Acquisition Partner, AI",
]


@pytest.mark.parametrize("title", FALSE_POSITIVE_TITLES)
def test_false_positive_titles_are_hard_ineligible_even_with_ai_description(title):
    assessment = evaluate_eligibility(make_job(title, AI_HEAVY_DESCRIPTION), CAND)
    assert assessment.status == EligibilityStatus.ineligible, (
        f"{title!r} should be hard-ineligible, got {assessment.status} "
        f"(reasons={assessment.reasons})"
    )
    assert assessment.hard_ineligible is True


@pytest.mark.parametrize("title", FALSE_POSITIVE_TITLES)
def test_false_positive_titles_are_role_family_other_even_with_ai_description(title):
    classification = classify_role(make_job(title, AI_HEAVY_DESCRIPTION))
    assert classification.family == RoleFamily.other, (
        f"{title!r} should classify as RoleFamily.other, got {classification.family}"
    )
    assert classification.relevant is False


@pytest.mark.parametrize("title", FALSE_POSITIVE_TITLES)
def test_false_positive_titles_are_non_target_at_gate_level(title):
    gate = evaluate_title_gate(title)
    assert gate.non_target is True
    assert gate.verdict == "non_target"


def test_data_scientist_marketing_non_target_overrides_credible_hit():
    """The domain signal ("marketing") must win over the credible-sounding
    "data scientist" phrase in the same title."""
    gate = evaluate_title_gate("Data Scientist, Marketing")
    assert gate.non_target is True
    assert gate.matched_credible == "data scientist"
    assert gate.matched_non_target == "marketing"
    assert gate.verdict == "non_target"


# --------------------------------------------------------------------------- #
# Legitimate credible titles -- must be relevant, correctly family-classified,
# and eligible given an otherwise clean description.
# --------------------------------------------------------------------------- #
LEGITIMATE_TITLES = [
    ("Backend Engineer, New Grad", RoleFamily.backend),
    ("Platform Engineer, New Grad", RoleFamily.backend),
    ("Infrastructure Engineer, New Grad", RoleFamily.backend),
    ("Site Reliability Engineer, New Grad", RoleFamily.backend),
    ("Full-Stack Engineer, New Grad", RoleFamily.backend),
    ("Software Engineer, New Grad", RoleFamily.backend),
    ("Machine Learning Engineer, New Grad", RoleFamily.ai),
    ("AI Engineer, New Grad", RoleFamily.ai),
    ("Applied Scientist, New Grad", RoleFamily.ai),
    ("Data Engineer, New Grad", RoleFamily.data),
    ("Product Engineer, New Grad", RoleFamily.product),
    ("Data Scientist, New Grad", RoleFamily.data),
]

CLEAN_DESCRIPTION = (
    "Join our engineering team to build and ship product features. Bachelor's "
    "degree required or equivalent experience. No prior professional experience "
    "necessary; new grads welcome."
)


@pytest.mark.parametrize("title,expected_family", LEGITIMATE_TITLES)
def test_legitimate_titles_are_relevant_and_correctly_classified(title, expected_family):
    classification = classify_role(make_job(title, CLEAN_DESCRIPTION))
    assert classification.relevant is True, f"{title!r} should be relevant"
    assert classification.family == expected_family, (
        f"{title!r} expected {expected_family}, got {classification.family}"
    )


@pytest.mark.parametrize("title,_family", LEGITIMATE_TITLES)
def test_legitimate_titles_are_eligible(title, _family):
    assessment = evaluate_eligibility(make_job(title, CLEAN_DESCRIPTION), CAND)
    assert assessment.status == EligibilityStatus.eligible, (
        f"{title!r} should be eligible, got {assessment.status} (reasons={assessment.reasons})"
    )


@pytest.mark.parametrize("title,_family", LEGITIMATE_TITLES)
def test_legitimate_titles_pass_the_gate_as_credible_and_not_non_target(title, _family):
    gate = evaluate_title_gate(title)
    assert gate.credible is True
    assert gate.non_target is False
    assert gate.verdict == "target"


# --------------------------------------------------------------------------- #
# Ambiguous titles -- neither credible nor non-target -> review, never a
# silent normal alert and never hard-ineligible.
# --------------------------------------------------------------------------- #
AMBIGUOUS_TITLES = [
    "Program Coordinator",
    "Technology Analyst",
    "Rotational Associate",
]


@pytest.mark.parametrize("title", AMBIGUOUS_TITLES)
def test_ambiguous_titles_go_to_review(title):
    assessment = evaluate_eligibility(make_job(title, "General responsibilities TBD."), CAND)
    assert assessment.status == EligibilityStatus.review, (
        f"{title!r} should be review, got {assessment.status}"
    )
    assert assessment.hard_ineligible is False


@pytest.mark.parametrize("title", AMBIGUOUS_TITLES)
def test_ambiguous_titles_are_role_family_other(title):
    classification = classify_role(make_job(title, "General responsibilities TBD."))
    assert classification.family == RoleFamily.other
    assert classification.relevant is False


@pytest.mark.parametrize("title", AMBIGUOUS_TITLES)
def test_ambiguous_titles_at_gate_level(title):
    gate = evaluate_title_gate(title)
    assert gate.credible is False
    assert gate.non_target is False
    assert gate.verdict == "ambiguous"
