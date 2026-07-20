from gradscout.eligibility import evaluate_eligibility
from gradscout.models import CandidateProfile, EligibilityStatus, EmploymentType
from tests.conftest import make_job

CAND = CandidateProfile()


def _status(title, desc="", **kw):
    return evaluate_eligibility(make_job(title, desc, **kw), CAND).status


# --- degree rules ---
def test_masters_required_is_ineligible():
    a = evaluate_eligibility(
        make_job("Software Engineer", "Master's degree required in computer science."), CAND
    )
    assert a.status == EligibilityStatus.ineligible
    assert a.hard_ineligible is True


def test_masters_preferred_is_not_disqualifying():
    a = evaluate_eligibility(
        make_job("Software Engineer, New Grad",
                 "Bachelor's degree. Master's degree preferred but not required."), CAND
    )
    assert a.status == EligibilityStatus.eligible


def test_phd_required_is_ineligible():
    assert _status("Research Engineer", "PhD required in machine learning.") == (
        EligibilityStatus.ineligible
    )


def test_bachelors_accepted_is_eligible():
    assert _status("Software Engineer", "Bachelor's degree required.") == (
        EligibilityStatus.eligible
    )


def test_bachelors_or_masters_is_eligible():
    assert _status("Software Engineer", "Bachelor's or Master's degree required.") == (
        EligibilityStatus.eligible
    )


# --- experience rules ---
def test_zero_to_two_years_is_eligible():
    assert _status("Software Engineer", "0-2 years of experience.") == (
        EligibilityStatus.eligible
    )


def test_three_plus_years_is_review():
    assert _status("Software Engineer", "3+ years of experience required.") == (
        EligibilityStatus.review
    )


def test_five_plus_years_is_ineligible():
    a = evaluate_eligibility(
        make_job("Software Engineer", "5+ years of experience required."), CAND
    )
    assert a.status == EligibilityStatus.ineligible
    assert a.hard_ineligible is True


def test_experience_preferred_not_disqualifying():
    assert _status("Software Engineer, New Grad", "5+ years experience preferred.") == (
        EligibilityStatus.eligible
    )


# --- new grad wording ---
def test_new_grad_wording_eligible():
    a = evaluate_eligibility(make_job("Software Engineer, New Grad", "Join us."), CAND)
    assert a.status == EligibilityStatus.eligible
    assert a.is_new_grad is True


def test_recent_graduate_wording_eligible():
    a = evaluate_eligibility(
        make_job("Software Engineer", "We welcome recent graduates and early career talent."),
        CAND,
    )
    assert a.status == EligibilityStatus.eligible
    assert a.is_new_grad is True


# --- internship rules ---
def test_internship_final_year_accepted_eligible():
    a = evaluate_eligibility(
        make_job("Software Engineering Intern",
                 "Open to graduating seniors and final-year students."), CAND
    )
    assert a.status == EligibilityStatus.eligible
    assert a.employment_type == EmploymentType.internship


def test_internship_return_to_school_rejected():
    a = evaluate_eligibility(
        make_job("Software Engineering Intern",
                 "You must be currently enrolled and returning to school after the internship."),
        CAND,
    )
    assert a.status == EligibilityStatus.ineligible
    assert a.hard_ineligible is True


def test_internship_ambiguous_is_review():
    assert _status("Software Engineering Intern", "Work on our platform this summer.") == (
        EligibilityStatus.review
    )


# --- seniority ---
def test_senior_title_true_positive_ineligible():
    a = evaluate_eligibility(make_job("Senior Software Engineer", "Backend."), CAND)
    assert a.status == EligibilityStatus.ineligible
    assert a.hard_ineligible is True


def test_lead_in_description_is_false_positive_not_senior():
    # "lead" appears only in the body as a verb; title is entry-level -> eligible.
    assert _status(
        "Software Engineer, New Grad", "You will lead small projects and show leadership."
    ) == EligibilityStatus.eligible


def test_staff_title_ineligible():
    assert _status("Staff Software Engineer", "Systems.") == EligibilityStatus.ineligible
