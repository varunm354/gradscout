"""Resume recommendation: backend | ai | data (Phase 6: weighted profile matching).

Every eligible/review job is scored against ALL THREE resume profiles using a
deterministic, weighted keyword matcher built from sanitized, purely-technical
profile data in ``Config.resume_profiles`` (skills/tools/technologies only --
never any personal information such as name, contact details, or education).
The highest-scoring variant is recommended, with a human-readable explanation
naming the concrete highest-weight matched skills (never just a bare score).

The scoring engine sits behind the ``ResumeMatcher`` protocol specifically so
it can be swapped for an embedding-based matcher later (e.g. cosine similarity
over a resume/job embedding) WITHOUT changing ``gradscout.analyze`` or
``gradscout.pipeline`` -- both only ever call ``recommend_resume(job, roles,
matcher)`` and read the returned ``ResumeRecommendation``.

Falls back to the pre-Phase-6 role-family mapping when a resume variant has no
configured profile terms (e.g. an unedited/minimal config), so behavior never
regresses for a config that hasn't opted into resume_profiles yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from gradscout.models import Config, Job, ResumeConfidence, ResumeVariant, RoleFamily
from gradscout.roles import RoleClassification
from gradscout.textmatch import find_first, normalize

# Saturating normalization constant: pct = 100 * raw / (raw + K). Bounded in
# [0, 100) for any raw score, with diminishing returns rather than a fragile
# hand-calibrated ceiling. raw=K -> 50%, raw=3K -> 75%, raw=9K -> 90%.
_SATURATION_K = 6.0

# How many top-weighted matched terms to name in the human-readable reason.
_TOP_TERMS_SHOWN = 5

# Pre-Phase-6 fallback when a variant has no configured resume_profiles terms.
_FAMILY_TO_RESUME_FALLBACK = {
    RoleFamily.ai: ResumeVariant.ai,
    RoleFamily.data: ResumeVariant.data,
    RoleFamily.backend: ResumeVariant.backend,
    RoleFamily.product: ResumeVariant.backend,
    RoleFamily.other: ResumeVariant.backend,
}

# A small set of acronyms/product names that should never be plain title-cased
# in a human-readable explanation (e.g. "llm" -> "LLM", not "Llm").
_DISPLAY_OVERRIDES = {
    "llm": "LLM",
    "llms": "LLMs",
    "rag": "RAG",
    "etl": "ETL",
    "elt": "ELT",
    "sql": "SQL",
    "api": "API",
    "apis": "APIs",
    "aws": "AWS",
    "ml": "ML",
    "ai": "AI",
    "ai/ml": "AI/ML",
    "nlp": "NLP",
    "bi": "BI",
    "dbt": "dbt",
    "etl/elt": "ETL/ELT",
    "fastapi": "FastAPI",
    "postgresql": "PostgreSQL",
    "postgres": "Postgres",
    "mysql": "MySQL",
    "numpy": "NumPy",
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "pgvector": "pgvector",
    "graphql": "GraphQL",
    "grpc": "gRPC",
    "ci/cd": "CI/CD",
}


def _pretty(term: str) -> str:
    """Human-readable display form for a matched term (cosmetic only -- never
    affects matching). Uses an explicit override for known acronyms/product
    names, else title-cases each word."""
    lowered = term.lower()
    if lowered in _DISPLAY_OVERRIDES:
        return _DISPLAY_OVERRIDES[lowered]
    return " ".join(w if w in _DISPLAY_OVERRIDES else w.capitalize() for w in term.split())


@dataclass
class WeightedTermSpec:
    """A resolved (term, weight, display) tuple -- the runtime counterpart of
    ``gradscout.models.WeightedTerm``."""

    term: str
    weight: int
    display: str


@dataclass
class ResumeProfile:
    variant: ResumeVariant
    terms: list[WeightedTermSpec] = field(default_factory=list)

    @property
    def has_terms(self) -> bool:
        return bool(self.terms)


@dataclass
class ResumeScore:
    variant: ResumeVariant
    raw_score: int
    matched: list[WeightedTermSpec] = field(default_factory=list)

    @property
    def top_terms(self) -> list[WeightedTermSpec]:
        return sorted(self.matched, key=lambda t: t.weight, reverse=True)


class ResumeMatcher(Protocol):
    """Pluggable resume-scoring engine. ``WeightedKeywordResumeMatcher`` is the
    deterministic implementation today; a future embedding-based matcher can
    implement this same protocol (e.g. cosine similarity against a resume
    embedding) without any caller change."""

    def score_all(self, title: str, description: str) -> dict[ResumeVariant, ResumeScore]: ...

    def has_profile(self, variant: ResumeVariant) -> bool: ...


@dataclass
class WeightedKeywordResumeMatcher:
    """Deterministic weighted-keyword matcher. Reuses the same word-boundary
    text matching as ``gradscout.roles`` (``gradscout.textmatch.find_first``),
    with a title-mention boost identical in spirit to role classification."""

    profiles: dict[ResumeVariant, ResumeProfile]

    def has_profile(self, variant: ResumeVariant) -> bool:
        p = self.profiles.get(variant)
        return p is not None and p.has_terms

    def score_all(self, title: str, description: str) -> dict[ResumeVariant, ResumeScore]:
        title_norm = normalize(title)
        body_norm = normalize(f"{title}\n{description or ''}")
        scores: dict[ResumeVariant, ResumeScore] = {}
        for variant, profile in self.profiles.items():
            total = 0
            matched: list[WeightedTermSpec] = []
            for spec in profile.terms:
                if find_first(body_norm, [spec.term]):
                    boost = 1 if find_first(title_norm, [spec.term]) else 0
                    total += spec.weight + boost
                    matched.append(spec)
            scores[variant] = ResumeScore(variant=variant, raw_score=total, matched=matched)
        return scores


def build_matcher_from_config(config: Config) -> WeightedKeywordResumeMatcher:
    """Build the matcher ONCE per run (not per job) from ``config.resume_profiles``."""
    profiles: dict[ResumeVariant, ResumeProfile] = {}
    for variant in ResumeVariant:
        cfg_profile = config.resume_profiles.get(variant)
        terms = [
            WeightedTermSpec(term=t.term, weight=t.weight, display=t.display or _pretty(t.term))
            for t in (cfg_profile.terms if cfg_profile else [])
        ]
        profiles[variant] = ResumeProfile(variant=variant, terms=terms)
    return WeightedKeywordResumeMatcher(profiles=profiles)


def _score_to_pct(raw_score: int) -> int:
    if raw_score <= 0:
        return 0
    return round(100 * raw_score / (raw_score + _SATURATION_K))


def _confidence_for(raw_score: int, margin: int) -> ResumeConfidence:
    if raw_score >= 8 and margin >= 3:
        return ResumeConfidence.high
    if raw_score >= 3:
        return ResumeConfidence.medium
    return ResumeConfidence.low


def _skills_reason(variant: ResumeVariant, score: ResumeScore) -> str:
    top = [t.display for t in score.top_terms[:_TOP_TERMS_SHOWN]]
    if top:
        return f"Matched {', '.join(top)} ({variant.value} resume)"
    return f"No strong skill match; defaulting to {variant.value} resume"


@dataclass
class ResumeRecommendation:
    variant: ResumeVariant
    confidence: ResumeConfidence
    reason: str
    match_score_pct: int
    scores: dict[ResumeVariant, ResumeScore] = field(default_factory=dict)


def _fallback_recommend(roles: RoleClassification) -> ResumeRecommendation:
    """Pre-Phase-6 role-family-only behavior, used when NO resume variant has
    any configured profile terms (e.g. a bare-bones config)."""
    variant = _FAMILY_TO_RESUME_FALLBACK[roles.family]
    if roles.family == RoleFamily.other:
        return ResumeRecommendation(
            variant, ResumeConfidence.low, "No strong role signal; defaulting to backend resume", 0
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
    return ResumeRecommendation(variant, confidence, reason, 0)


def recommend_resume(
    job: Job, roles: RoleClassification, matcher: ResumeMatcher
) -> ResumeRecommendation:
    """Score ``job`` against ALL THREE resume profiles and recommend exactly
    one. Ties/absence of any keyword signal fall back to the deterministic
    role-family hint (``roles.family``) so a thin description never produces
    an arbitrary pick among equally-zero scores."""
    if not any(matcher.has_profile(v) for v in ResumeVariant):
        return _fallback_recommend(roles)

    scores = matcher.score_all(job.title, job.description_text or "")
    ranked = sorted(scores.values(), key=lambda s: s.raw_score, reverse=True)
    top = ranked[0]
    runner_up_score = ranked[1].raw_score if len(ranked) > 1 else 0
    margin = top.raw_score - runner_up_score

    if top.raw_score == 0:
        # No keyword signal from any profile -- fall back to the title-gate's
        # role-family hint (never an arbitrary pick among all-zero scores).
        fallback_variant = _FAMILY_TO_RESUME_FALLBACK[roles.family]
        winner = scores.get(fallback_variant, top)
        variant = fallback_variant
    else:
        # Deterministic tie-break: prefer the role-family hint's resume among
        # genuine ties, else fixed order ai > data > backend.
        tied = [s for s in ranked if s.raw_score == top.raw_score]
        if len(tied) > 1:
            hinted = _FAMILY_TO_RESUME_FALLBACK.get(roles.family)
            preferred = next((s for s in tied if s.variant == hinted), None)
            if preferred is not None:
                winner = preferred
            else:
                tied_by_variant = {s.variant: s for s in tied}
                winner = next(
                    tied_by_variant[v]
                    for v in (ResumeVariant.ai, ResumeVariant.data, ResumeVariant.backend)
                    if v in tied_by_variant
                )
        else:
            winner = top
        variant = winner.variant

    confidence = _confidence_for(winner.raw_score, margin)
    reason = _skills_reason(variant, winner)
    pct = _score_to_pct(winner.raw_score)
    return ResumeRecommendation(
        variant=variant, confidence=confidence, reason=reason, match_score_pct=pct, scores=scores
    )
