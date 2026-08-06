"""Phase 6 weighted-profile resume matching (gradscout.resume).

Covers the deterministic ``WeightedKeywordResumeMatcher`` scoring all three
profiles, the skills-based explanation (never a bare percentage), score
normalization, and the fallback to the pre-Phase-6 role-family mapping when
no resume_profiles are configured (already covered separately in
tests/test_roles_resume.py).
"""

from __future__ import annotations

from gradscout.models import (
    Config,
    ResumeConfidence,
    ResumeProfileConfig,
    ResumeVariant,
    WeightedTerm,
)
from gradscout.resume import build_matcher_from_config, recommend_resume
from gradscout.roles import classify_role
from tests.conftest import make_job

AI_TERMS = [
    WeightedTerm(term="llm", weight=3, display="LLMs"),
    WeightedTerm(term="embeddings", weight=3),
    WeightedTerm(term="rag", weight=3, display="RAG"),
    WeightedTerm(term="pytorch", weight=2, display="PyTorch"),
]
BACKEND_TERMS = [
    WeightedTerm(term="fastapi", weight=3, display="FastAPI"),
    WeightedTerm(term="distributed systems", weight=3),
    WeightedTerm(term="postgresql", weight=3, display="PostgreSQL"),
    WeightedTerm(term="microservices", weight=2),
]
DATA_TERMS = [
    WeightedTerm(term="etl", weight=3, display="ETL"),
    WeightedTerm(term="data modeling", weight=3),
    WeightedTerm(term="sql optimization", weight=2),
    WeightedTerm(term="tableau", weight=2, display="Tableau"),
]


def _config() -> Config:
    return Config(
        resume_profiles={
            ResumeVariant.ai: ResumeProfileConfig(terms=AI_TERMS),
            ResumeVariant.backend: ResumeProfileConfig(terms=BACKEND_TERMS),
            ResumeVariant.data: ResumeProfileConfig(terms=DATA_TERMS),
        }
    )


def _recommend(title: str, desc: str, config: Config | None = None):
    config = config or _config()
    job = make_job(title, desc)
    roles = classify_role(job)
    matcher = build_matcher_from_config(config)
    return recommend_resume(job, roles, matcher)


def test_ai_job_scores_all_three_profiles_and_recommends_ai():
    rec = _recommend(
        "Machine Learning Engineer, New Grad",
        "Build LLM systems using embeddings and RAG pipelines with PyTorch.",
    )
    assert rec.variant == ResumeVariant.ai
    assert set(rec.scores.keys()) == {ResumeVariant.ai, ResumeVariant.backend, ResumeVariant.data}
    assert rec.scores[ResumeVariant.ai].raw_score > rec.scores[ResumeVariant.backend].raw_score
    assert rec.scores[ResumeVariant.ai].raw_score > rec.scores[ResumeVariant.data].raw_score


def test_backend_job_recommends_backend_with_concrete_skills_in_reason():
    rec = _recommend(
        "Backend Engineer, New Grad",
        "Build distributed systems and REST APIs with FastAPI and PostgreSQL.",
    )
    assert rec.variant == ResumeVariant.backend
    # Requirement: explanation names concrete matched skills, never just a score.
    assert "FastAPI" in rec.reason
    assert "PostgreSQL" in rec.reason or "distributed systems" in rec.reason
    assert "%" not in rec.reason


def test_data_job_recommends_data():
    rec = _recommend(
        "Data Engineer, New Grad",
        "Own ETL pipelines, data modeling, and SQL optimization; dashboard in Tableau.",
    )
    assert rec.variant == ResumeVariant.data
    assert "ETL" in rec.reason or "Tableau" in rec.reason


def test_match_score_pct_is_bounded_and_higher_for_stronger_match():
    weak = _recommend("Backend Engineer, New Grad", "Build software.")
    strong = _recommend(
        "Backend Engineer, New Grad",
        "Distributed systems, FastAPI, PostgreSQL, microservices, REST APIs.",
    )
    assert 0 <= weak.match_score_pct < 100
    assert 0 <= strong.match_score_pct < 100
    assert strong.match_score_pct > weak.match_score_pct


def test_title_mention_boosts_score_over_description_only_mention():
    title_hit = _recommend("FastAPI Backend Engineer", "General engineering.")
    body_hit = _recommend("Backend Engineer, New Grad", "We use FastAPI internally.")
    assert title_hit.scores[ResumeVariant.backend].raw_score > (
        body_hit.scores[ResumeVariant.backend].raw_score
    )


def test_no_keyword_signal_falls_back_to_role_family_hint():
    """A thin description with no configured-profile keyword hits must not
    produce an arbitrary pick among all-zero scores -- it falls back to the
    deterministic role-family hint from the title gate."""
    rec = _recommend("Data Engineer, New Grad", "Join our team.")
    assert all(s.raw_score == 0 for s in rec.scores.values())
    assert rec.variant == ResumeVariant.data
    assert rec.confidence == ResumeConfidence.low


def test_empty_resume_profiles_config_uses_fallback_mapping():
    """No resume_profiles configured at all -> pre-Phase-6 role-family-only
    mapping (never breaks an existing/minimal config)."""
    empty_config = Config()
    rec = _recommend(
        "Machine Learning Engineer, New Grad", "Deep learning at scale.", empty_config
    )
    assert rec.variant == ResumeVariant.ai
    assert rec.scores == {}


def test_confidence_is_high_for_strong_unambiguous_match():
    rec = _recommend(
        "Backend Engineer, New Grad",
        "Distributed systems, FastAPI, PostgreSQL, microservices at scale.",
    )
    assert rec.confidence == ResumeConfidence.high
