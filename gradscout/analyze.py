"""Deterministic analysis orchestrator + LLM resolver + batch classification.

analyze_deterministic() runs the independent deterministic classifier.
resolve() reconciles it with the optional LLM classifier into one validated
ResolvedAnalysis, where deterministic HARD rules always win and disagreement is
recorded. classify_jobs() adds the cheap prefilter, bounded LLM usage, and the
required skipped / resolved / review / sent-to-LLM logging.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from gradscout.eligibility import evaluate_eligibility
from gradscout.llm import JobAnalysisAgent
from gradscout.location import classify_location
from gradscout.models import (
    AgentAnalysis,
    AlertPriority,
    Config,
    EligibilityStatus,
    EmploymentType,
    Job,
    LocationClassification,
    ResolvedAnalysis,
    ResumeConfidence,
    ResumeVariant,
    RoleFamily,
)
from gradscout.prioritize import (
    apply_location_penalty,
    resolve_company_priority,
    score_alert_priority,
    score_role_priority,
)
from gradscout.resume import ResumeMatcher, build_matcher_from_config, recommend_resume
from gradscout.roles import classify_role

logger = logging.getLogger("gradscout.analyze")


@dataclass
class DeterministicAnalysis:
    status: EligibilityStatus
    reasons: list[str]
    employment_type: EmploymentType
    is_new_grad: bool | None
    hard_ineligible: bool
    hard_reason: str | None
    role_family: RoleFamily
    relevant: bool
    recommended_resume: ResumeVariant | None
    resume_confidence: ResumeConfidence | None
    resume_reason: str | None
    resume_match_score: int | None
    company_priority: int
    matched_watchlist: str | None
    role_priority: int
    alert_priority: AlertPriority
    is_recent: bool
    location_classification: LocationClassification
    location_reason: str | None
    remote_alert_penalty: int


def analyze_deterministic(
    job: Job,
    config: Config,
    is_recent: bool = True,
    resume_matcher: ResumeMatcher | None = None,
) -> DeterministicAnalysis:
    """``resume_matcher`` should be built ONCE per run via
    ``build_matcher_from_config`` (see ``gradscout.pipeline.run_once``) and
    passed down -- it is only built lazily here as a convenience for direct
    callers/tests that don't already have one."""
    elig = evaluate_eligibility(
        job, config.candidate, max_years=config.notifications.max_years_experience
    )
    roles = classify_role(job)
    matcher = resume_matcher or build_matcher_from_config(config)
    resume = None if elig.status == EligibilityStatus.ineligible else recommend_resume(job, roles, matcher)
    company_priority, matched = resolve_company_priority(job, config)
    role_priority = score_role_priority(elig, roles.relevant)
    alert_priority = score_alert_priority(
        elig.status,
        company_priority,
        role_priority,
        roles.relevant,
        resume.confidence if resume else None,
        is_recent,
    )
    # Location fit (Phase 5.2) is computed independently of eligibility -- it never
    # changes elig.status/reasons, only the alert_priority penalty applied here (and,
    # separately, whether a normal alert is enqueued at all -- see gradscout.pipeline).
    loc = classify_location(job.location, job.remote, config.candidate.preferred_locations)
    penalty = config.candidate.remote_alert_priority_penalty
    alert_priority = apply_location_penalty(alert_priority, loc.classification, penalty)
    return DeterministicAnalysis(
        status=elig.status,
        reasons=list(elig.reasons),
        employment_type=elig.employment_type,
        is_new_grad=elig.is_new_grad,
        hard_ineligible=elig.hard_ineligible,
        hard_reason=elig.hard_reason,
        role_family=roles.family,
        relevant=roles.relevant,
        recommended_resume=resume.variant if resume else None,
        resume_confidence=resume.confidence if resume else None,
        resume_reason=resume.reason if resume else None,
        resume_match_score=resume.match_score_pct if resume else None,
        company_priority=company_priority,
        matched_watchlist=matched,
        role_priority=role_priority,
        alert_priority=alert_priority,
        is_recent=is_recent,
        location_classification=loc.classification,
        location_reason=loc.reason,
        remote_alert_penalty=penalty,
    )


def resolve(det: DeterministicAnalysis, agent_out: AgentAnalysis | None) -> ResolvedAnalysis:
    """Reconcile deterministic + optional LLM into one validated result."""
    common = dict(
        employment_type=det.employment_type,
        is_new_grad=det.is_new_grad,
        company_priority=det.company_priority,
        # Location is a hard boundary the LLM never sees or influences (same
        # deterministic-only treatment as hard_ineligible), carried through unchanged.
        location_classification=det.location_classification,
        location_reason=det.location_reason,
    )

    if agent_out is None:
        return ResolvedAnalysis(
            eligibility_status=det.status,
            eligibility_reasons=det.reasons,
            role_family=det.role_family,
            recommended_resume=det.recommended_resume,
            resume_confidence=det.resume_confidence,
            resume_reason=det.resume_reason,
            resume_match_score=det.resume_match_score,
            role_priority=det.role_priority,
            alert_priority=det.alert_priority,
            llm_used=False,
            decided_by="deterministic",
            deterministic_disagreement=False,
            disagreement_reason=None,
            requires_human_review=(det.status == EligibilityStatus.review),
            **common,
        )

    # Hard deterministic rules ALWAYS win; the LLM cannot override them.
    if det.hard_ineligible:
        disagreed = agent_out.eligibility_status != EligibilityStatus.ineligible
        reason = None
        if disagreed:
            reason = (
                f"LLM suggested '{agent_out.eligibility_status.value}' but deterministic "
                f"hard rule wins: {det.hard_reason}"
            )
        return ResolvedAnalysis(
            eligibility_status=EligibilityStatus.ineligible,
            eligibility_reasons=det.reasons,
            role_family=det.role_family,
            recommended_resume=det.recommended_resume,
            resume_confidence=det.resume_confidence,
            resume_reason=det.resume_reason,
            resume_match_score=det.resume_match_score,
            role_priority=det.role_priority,
            alert_priority=AlertPriority.ineligible,
            llm_used=True,
            decided_by="deterministic (hard rule)",
            deterministic_disagreement=disagreed,
            disagreement_reason=reason,
            requires_human_review=False,
            **common,
        )

    # Soft/ambiguous case: the LLM assists (only reached for det.status == review).
    final_status = agent_out.eligibility_status
    disagreement = final_status != det.status
    role_family = det.role_family if det.role_family != RoleFamily.other else agent_out.role_family
    resume = det.recommended_resume or agent_out.recommended_resume
    resume_conf = det.resume_confidence or agent_out.resume_confidence
    resume_reason = det.resume_reason or ("LLM-selected resume" if resume else None)
    # Only meaningful when the deterministic layer itself produced the resume
    # pick; an LLM-only pick has no comparable weighted score.
    resume_match_score = det.resume_match_score if det.recommended_resume else None
    reasons = list(det.reasons) + [f"LLM: {e}" for e in agent_out.evidence]
    alert_priority = score_alert_priority(
        final_status, det.company_priority, det.role_priority, det.relevant,
        resume_conf, det.is_recent,
    )
    alert_priority = apply_location_penalty(
        alert_priority, det.location_classification, det.remote_alert_penalty
    )
    disagreement_reason = agent_out.disagreement_reason or (
        f"deterministic='{det.status.value}', llm='{final_status.value}'"
        if disagreement
        else None
    )
    return ResolvedAnalysis(
        eligibility_status=final_status,
        eligibility_reasons=reasons,
        role_family=role_family,
        recommended_resume=resume,
        resume_confidence=resume_conf,
        resume_reason=resume_reason,
        resume_match_score=resume_match_score,
        role_priority=det.role_priority,
        alert_priority=alert_priority,
        llm_used=True,
        decided_by="llm-assisted",
        deterministic_disagreement=disagreement,
        disagreement_reason=disagreement_reason,
        requires_human_review=agent_out.requires_human_review
        or final_status == EligibilityStatus.review,
        **common,
    )


def classify_job(
    job: Job,
    config: Config,
    agent: JobAnalysisAgent | None = None,
    is_recent: bool = True,
    resume_matcher: ResumeMatcher | None = None,
) -> ResolvedAnalysis:
    det = analyze_deterministic(job, config, is_recent, resume_matcher=resume_matcher)
    agent_out = agent.analyze(job, det) if agent else None
    return resolve(det, agent_out)


def apply_to_job(job: Job, resolved: ResolvedAnalysis) -> Job:
    """Copy a resolved classification onto a Job (for later persistence)."""
    job.eligibility_status = resolved.eligibility_status
    job.eligibility_reasons = resolved.eligibility_reasons
    job.employment_type = resolved.employment_type
    job.is_new_grad = resolved.is_new_grad
    job.role_family = resolved.role_family
    job.role_priority = resolved.role_priority
    job.recommended_resume = resolved.recommended_resume
    job.resume_confidence = resolved.resume_confidence
    job.resume_reason = resolved.resume_reason
    job.resume_match_score = resolved.resume_match_score
    job.company_priority = resolved.company_priority
    job.alert_priority = resolved.alert_priority
    job.llm_used = resolved.llm_used
    job.location_classification = resolved.location_classification
    job.location_reason = resolved.location_reason
    return job


@dataclass
class ClassificationStats:
    total: int = 0
    skipped: int = 0
    eligible: int = 0
    ineligible: int = 0
    review: int = 0
    sent_to_llm: int = 0
    llm_succeeded: int = 0
    disagreements: int = 0
    results: list[ResolvedAnalysis] = field(default_factory=list)


def classify_jobs(
    jobs: Iterable[Job],
    config: Config,
    agent: JobAnalysisAgent | None = None,
    *,
    is_new_or_changed: Callable[[Job], bool] | None = None,
    is_recent: Callable[[Job], bool] | None = None,
    resume_matcher: ResumeMatcher | None = None,
) -> ClassificationStats:
    """Classify many jobs, applying the LLM only to new/relevant/ambiguous cases.

    ``is_new_or_changed`` gates processing entirely (performance: never run over
    the whole 17k feed). ``is_recent`` feeds P1 urgency. Emits the required counts.
    ``resume_matcher`` should be built once per run (``build_matcher_from_config``)
    and passed in to avoid rebuilding it per job.
    """
    stats = ClassificationStats()
    matcher = resume_matcher or build_matcher_from_config(config)
    for job in jobs:
        stats.total += 1
        if is_new_or_changed is not None and not is_new_or_changed(job):
            stats.skipped += 1
            continue

        recent = is_recent(job) if is_recent else True
        det = analyze_deterministic(job, config, recent, resume_matcher=matcher)

        agent_out = None
        if agent is not None and agent.should_analyze(job, det):
            stats.sent_to_llm += 1
            agent_out = agent.analyze(job, det)
            if agent_out is not None:
                stats.llm_succeeded += 1

        resolved = resolve(det, agent_out)
        apply_to_job(job, resolved)
        stats.results.append(resolved)

        if resolved.deterministic_disagreement:
            stats.disagreements += 1
        if resolved.eligibility_status == EligibilityStatus.eligible:
            stats.eligible += 1
        elif resolved.eligibility_status == EligibilityStatus.ineligible:
            stats.ineligible += 1
        else:
            stats.review += 1

    logger.info(
        "classification complete",
        extra={"fields": {
            "total": stats.total, "skipped": stats.skipped, "eligible": stats.eligible,
            "ineligible": stats.ineligible, "review": stats.review,
            "sent_to_llm": stats.sent_to_llm, "llm_succeeded": stats.llm_succeeded,
            "disagreements": stats.disagreements,
        }},
    )
    return stats
