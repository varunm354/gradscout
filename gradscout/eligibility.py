"""Deterministic eligibility evaluation.

Produces an EligibilityAssessment with an explicit status and human-readable
evidence for every decision. Hard rules (advanced-degree requirement, seniority,
excessive mandatory experience, incompatible return-to-school internship) mark
``hard_ineligible=True`` so the LLM resolver can never override them.

Never discards a job: the worst outcome is ``ineligible`` (still stored).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from gradscout.models import CandidateProfile, EligibilityStatus, EmploymentType, Job
from gradscout.textmatch import (
    PREFERRED_CUES,
    REQUIRED_CUES,
    _boundary_pattern,
    contains_any,
    context_is_preferred,
    find_first,
    normalize,
)

# --------------------------------------------------------------------------- #
# Keyword vocabularies
# --------------------------------------------------------------------------- #
SENIORITY_TOKENS = (
    "senior", "sr", "staff", "principal", "lead", "manager", "director",
    "head", "vp", "vice president", "architect", "distinguished", "fellow",
)
# Entry-level cues in a title override an accidental seniority token match.
ENTRY_TITLE_TOKENS = (
    "new grad", "new graduate", "entry level", "entry-level", "junior", "jr",
    "associate", "university graduate", "university grad", "campus", "apprentice",
    "intern", "graduate program", "early career", "early-career", "rotational",
    "class of",
)
NEW_GRAD_TOKENS = (
    "new grad", "new graduate", "recent graduate", "recent grad", "entry level",
    "entry-level", "early career", "early-career", "university graduate",
    "university grad", "campus", "class of 2027", "2027 grad", "grad program",
    "graduate program", "no experience required", "early in your career",
    "0-1 years", "0-2 years", "0 to 2 years", "1-2 years",
)
INTERNSHIP_TOKENS = ("intern", "internship", "co-op", "co op", "coop", "summer intern")

ADVANCED_DEGREE_TOKENS = (
    "phd", "ph.d", "ph. d", "doctorate", "doctoral", "master's", "masters",
    "master of", "m.s.", "msc", "m.sc", "graduate degree", "mba", "advanced degree",
)
BACHELOR_TOKENS = (
    "bachelor", "bachelor's", "bachelors", "b.s.", "bs degree", "ba/bs",
    "undergraduate degree", "undergraduate", "b.a.", "4-year degree",
)

INTERN_ACCEPTS_GRADS = (
    "recent graduate", "recent grad", "graduating senior", "graduating seniors",
    "final year", "final-year", "graduating student", "new graduate",
    "no requirement to return to school", "open to recent graduates",
    "recent or upcoming graduate",
)
INTERN_RETURN_TO_SCHOOL = (
    "return to school", "returning to school", "return to campus", "must be enrolled",
    "currently enrolled", "enrolled student", "pursuing a degree", "actively enrolled",
    "enrolled in a degree", "return to your studies", "continuing your studies",
)


@dataclass
class EligibilityAssessment:
    status: EligibilityStatus
    reasons: list[str] = field(default_factory=list)
    employment_type: EmploymentType = EmploymentType.unknown
    is_new_grad: bool | None = None
    hard_ineligible: bool = False
    hard_reason: str | None = None


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #
def detect_employment_type(job: Job, text_norm: str) -> EmploymentType:
    """Prefer structured hints from the raw payload, then fall back to text."""
    blob = job.raw_blob or {}
    structured = ""
    if isinstance(blob.get("employmentType"), str):        # Ashby
        structured = blob["employmentType"].lower()
    elif isinstance(blob.get("categories"), dict):          # Lever
        structured = str(blob["categories"].get("commitment", "")).lower()
    if "intern" in structured or "co-op" in structured or "coop" in structured:
        return EmploymentType.internship
    if "full" in structured:
        return EmploymentType.fulltime

    if contains_any(text_norm, INTERNSHIP_TOKENS):
        return EmploymentType.internship
    # ATS boards are overwhelmingly full-time roles when unspecified.
    return EmploymentType.fulltime


def detect_seniority(title_norm: str) -> str | None:
    """Return the offending seniority token if the TITLE is senior-level, else
    None. Entry-level title cues suppress accidental matches (e.g. 'leadership')."""
    token = find_first(title_norm, SENIORITY_TOKENS)
    if token is None:
        return None
    if contains_any(title_norm, ENTRY_TITLE_TOKENS):
        return None
    return token


@dataclass
class DegreeInfo:
    level: str  # advanced_required | advanced_preferred | bachelor_ok | unclear | none
    evidence: str | None = None


def detect_degree(text_norm: str) -> DegreeInfo:
    adv = find_first(text_norm, ADVANCED_DEGREE_TOKENS)
    bachelor_ok = contains_any(text_norm, BACHELOR_TOKENS)

    if adv is None:
        return DegreeInfo("bachelor_ok" if bachelor_ok else "none")

    # An advanced degree is mentioned. Is it required, preferred, or unclear?
    preferred = _mention_is_preferred(text_norm, ADVANCED_DEGREE_TOKENS)
    required = _mention_is_required(text_norm, ADVANCED_DEGREE_TOKENS)

    if bachelor_ok:
        # Bachelor's is explicitly accepted (e.g. "Bachelor's or Master's") -> eligible.
        return DegreeInfo("bachelor_ok", evidence=f"advanced degree optional (bachelor's accepted): '{adv}'")
    if required and not preferred:
        return DegreeInfo("advanced_required", evidence=f"requires advanced degree: '{adv}'")
    if preferred:
        return DegreeInfo("advanced_preferred", evidence=f"advanced degree only preferred: '{adv}'")
    return DegreeInfo("unclear", evidence=f"advanced degree mentioned, requirement unclear: '{adv}'")


def _mention_is_preferred(text: str, tokens: tuple[str, ...]) -> bool:
    return any(context_is_preferred(text, t) for t in tokens if find_first(text, [t]))


def _mention_is_required(text: str, tokens: tuple[str, ...]) -> bool:
    for t in tokens:
        m = _boundary_pattern(t).search(text)
        if not m:
            continue
        ctx = text[max(0, m.start() - 60): m.end() + 60]
        if contains_any(ctx, REQUIRED_CUES) and not contains_any(ctx, PREFERRED_CUES):
            return True
    return False


_YEARS_RE = re.compile(
    r"(\d+)\s*\+?\s*(?:-|–|to)?\s*(\d+)?\s*\+?\s*years?", re.IGNORECASE
)


@dataclass
class ExperienceInfo:
    min_years: int | None = None
    mandatory: bool = False
    evidence: str | None = None


def detect_experience(text_norm: str) -> ExperienceInfo:
    mandatory_mins: list[int] = []
    evidence: str | None = None
    for m in _YEARS_RE.finditer(text_norm):
        low = int(m.group(1))
        span = text_norm[max(0, m.start() - 50): m.end() + 50]
        is_pref = contains_any(span, PREFERRED_CUES)
        if not is_pref:
            mandatory_mins.append(low)
            if evidence is None:
                evidence = m.group(0).strip()
    if not mandatory_mins:
        return ExperienceInfo(None, False, None)
    return ExperienceInfo(min(mandatory_mins), True, evidence)


def detect_new_grad(text_norm: str) -> bool:
    return contains_any(text_norm, NEW_GRAD_TOKENS)


def _intern_accepts_grads(text_norm: str) -> str | None:
    return find_first(text_norm, INTERN_ACCEPTS_GRADS)


def _intern_return_to_school(text_norm: str, grad_year: int) -> str | None:
    token = find_first(text_norm, INTERN_RETURN_TO_SCHOOL)
    if token is None:
        # Also catch explicit graduation year at/after grad_year+1 in enrollment context.
        for m in re.finditer(r"(20\d\d)", text_norm):
            if int(m.group(1)) >= grad_year + 1:
                ctx = text_norm[max(0, m.start() - 40): m.end() + 20]
                if contains_any(ctx, ("graduat", "enrolled", "return")):
                    return f"enrollment/graduation {m.group(1)} incompatible with {grad_year} grad"
        return None
    return token


# --------------------------------------------------------------------------- #
# Main evaluation
# --------------------------------------------------------------------------- #
def evaluate_eligibility(
    job: Job, candidate: CandidateProfile, max_years: int = 5
) -> EligibilityAssessment:
    title_norm = normalize(job.title)
    text_norm = normalize(f"{job.title}\n{job.description_text or ''}")

    employment = detect_employment_type(job, text_norm)
    new_grad = detect_new_grad(text_norm)

    def hard(reason: str) -> EligibilityAssessment:
        return EligibilityAssessment(
            EligibilityStatus.ineligible, [reason], employment, new_grad,
            hard_ineligible=True, hard_reason=reason,
        )

    # 1) Seniority in title -> hard ineligible.
    senior = detect_seniority(title_norm)
    if senior is not None:
        return hard(f"Senior-level title token '{senior}'")

    # 2) Advanced degree explicitly required -> hard ineligible.
    degree = detect_degree(text_norm)
    if degree.level == "advanced_required":
        return hard(f"Explicitly {degree.evidence}")

    # 3) Excessive mandatory experience -> hard ineligible.
    exp = detect_experience(text_norm)
    if exp.min_years is not None and exp.mandatory and exp.min_years >= max_years:
        return hard(f"Excessive mandatory experience: '{exp.evidence}' (>= {max_years}y)")

    # 4) Internship-specific rules.
    if employment == EmploymentType.internship:
        accepts = _intern_accepts_grads(text_norm)
        if accepts is not None:
            return EligibilityAssessment(
                EligibilityStatus.eligible,
                [f"Internship explicitly accepts grads: '{accepts}'"],
                employment, new_grad,
            )
        rts = _intern_return_to_school(text_norm, candidate.graduation_year)
        if rts is not None:
            return hard(f"Internship requires return-to-school/enrollment: '{rts}'")
        return EligibilityAssessment(
            EligibilityStatus.review,
            ["Internship enrollment/eligibility unclear"],
            employment, new_grad,
        )

    # 5) Ambiguous (3-4y) mandatory experience -> review.
    if exp.min_years is not None and exp.mandatory and exp.min_years >= 3:
        return EligibilityAssessment(
            EligibilityStatus.review,
            [f"Experience requirement '{exp.evidence}' may exceed entry-level"],
            employment, new_grad,
        )

    # 6) Advanced degree mentioned but requirement unclear -> review.
    if degree.level == "unclear":
        return EligibilityAssessment(
            EligibilityStatus.review, [degree.evidence], employment, new_grad
        )

    # 7) Eligible, with the strongest supporting evidence.
    reasons: list[str] = []
    if new_grad:
        reasons.append("New-grad / early-career language present")
    if degree.level == "advanced_preferred":
        reasons.append("Graduate degree only preferred (weaker match)")
    if degree.level == "bachelor_ok":
        reasons.append(degree.evidence or "Bachelor's degree acceptable")
    if exp.min_years is not None and exp.min_years < 3:
        reasons.append(f"Entry-level experience range '{exp.evidence}'")
    if not reasons:
        reasons.append("No disqualifying requirements found")
    return EligibilityAssessment(
        EligibilityStatus.eligible, reasons, employment, new_grad
    )
