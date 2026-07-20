"""Company / role / alert priority scoring.

company_priority is resolved from the configurable watchlist (name + aliases), so
big-tech behavior is data, not hardcoded. Lower number = higher priority.
"""

from __future__ import annotations

from gradscout.eligibility import EligibilityAssessment
from gradscout.models import AlertPriority, Config, EligibilityStatus, EmploymentType, Job, ResumeConfidence
from gradscout.textmatch import normalize


def resolve_company_priority(job: Job, config: Config) -> tuple[int, str | None]:
    """Return (company_priority, matched_watchlist_name).

    Falls back to the job's source-provided priority when no watchlist entry
    matches by name or alias.
    """
    company_norm = normalize(job.company)
    source_norm = normalize(job.source_company)
    for entry in config.watchlist:
        candidates = [entry.name, *entry.aliases]
        for cand in candidates:
            cnorm = normalize(cand)
            if not cnorm:
                continue
            if (
                cnorm == company_norm
                or cnorm in company_norm
                or company_norm in cnorm
                or cnorm == source_norm
                or cnorm in source_norm
            ):
                return entry.company_priority, entry.name
    return job.company_priority, None


def score_role_priority(elig: EligibilityAssessment, relevant: bool) -> int:
    if elig.status != EligibilityStatus.eligible:
        return 4
    if elig.employment_type == EmploymentType.internship:
        return 3
    if relevant and elig.employment_type == EmploymentType.fulltime and elig.is_new_grad:
        return 1
    if relevant and elig.employment_type in (EmploymentType.fulltime, EmploymentType.unknown):
        return 2
    if relevant:
        return 2
    return 3


# Lower rank = more urgent. Used only to compare against the configured
# discord_min_priority threshold; review/ineligible/unclassified never
# qualify as an immediate per-job alert (review goes to the digest instead).
_ALERT_PRIORITY_RANK = {
    AlertPriority.p1: 1,
    AlertPriority.p2: 2,
    AlertPriority.p3: 3,
    AlertPriority.review: 4,
    AlertPriority.ineligible: 5,
    AlertPriority.unclassified: 6,
}


def meets_min_priority(priority: AlertPriority, threshold: AlertPriority) -> bool:
    """True if ``priority`` is at least as urgent as ``threshold`` (e.g. p1/p2
    both meet a p2 threshold; p3 does not)."""
    return _ALERT_PRIORITY_RANK[priority] <= _ALERT_PRIORITY_RANK[threshold]


def score_alert_priority(
    status: EligibilityStatus,
    company_priority: int,
    role_priority: int,
    relevant: bool,
    resume_confidence: ResumeConfidence | None,
    is_recent: bool,
) -> AlertPriority:
    if status == EligibilityStatus.ineligible:
        return AlertPriority.ineligible
    if status == EligibilityStatus.review:
        return AlertPriority.review

    # eligible
    good_resume = resume_confidence in (ResumeConfidence.high, ResumeConfidence.medium)
    if company_priority == 1 and role_priority <= 2 and relevant and is_recent:
        return AlertPriority.p1
    if role_priority <= 2 and good_resume:
        return AlertPriority.p2
    if role_priority == 3:
        return AlertPriority.p3
    return AlertPriority.p3
