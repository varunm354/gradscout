"""Resume recommendation: backend | ai | data.

Maps the role-family classification to one of the three resume variants and sets
a confidence based on how strong/decisive the signal was. Product and generic
SWE map to the backend resume (per the candidate's stated default).
"""

from __future__ import annotations

from dataclasses import dataclass

from gradscout.models import ResumeConfidence, ResumeVariant, RoleFamily
from gradscout.roles import RoleClassification

_FAMILY_TO_RESUME = {
    RoleFamily.ai: ResumeVariant.ai,
    RoleFamily.data: ResumeVariant.data,
    RoleFamily.backend: ResumeVariant.backend,
    RoleFamily.product: ResumeVariant.backend,
    RoleFamily.other: ResumeVariant.backend,
}


@dataclass
class ResumeRecommendation:
    variant: ResumeVariant
    confidence: ResumeConfidence
    reason: str


def recommend_resume(roles: RoleClassification) -> ResumeRecommendation:
    variant = _FAMILY_TO_RESUME[roles.family]

    if roles.family == RoleFamily.other:
        return ResumeRecommendation(
            variant,
            ResumeConfidence.low,
            "No strong role signal; defaulting to backend resume",
        )

    if roles.top_score >= 4 and roles.margin >= 2:
        confidence = ResumeConfidence.high
    elif roles.top_score >= 2:
        confidence = ResumeConfidence.medium
    else:
        confidence = ResumeConfidence.low

    evidence = roles.evidence[0] if roles.evidence else roles.family.value
    reason = f"Matched {evidence} -> {variant.value} resume"
    if roles.family == RoleFamily.product:
        reason = f"Product role ({evidence}); backend resume best fit"
    return ResumeRecommendation(variant, confidence, reason)
