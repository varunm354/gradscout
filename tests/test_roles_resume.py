from gradscout.models import Config, ResumeConfidence, ResumeVariant, RoleFamily
from gradscout.resume import build_matcher_from_config, recommend_resume
from gradscout.roles import classify_role
from tests.conftest import make_job

# No resume_profiles configured -> recommend_resume falls back to the
# pre-Phase-6 role-family-only mapping (see gradscout.resume._fallback_recommend).
# Dedicated weighted-profile scoring tests live in tests/test_resume_matching.py.
_NO_PROFILE_MATCHER = build_matcher_from_config(Config())


def _family(title, desc=""):
    return classify_role(make_job(title, desc)).family


def _recommend(title, desc=""):
    job = make_job(title, desc)
    roles = classify_role(job)
    return recommend_resume(job, roles, _NO_PROFILE_MATCHER)


# --- role classification ---
def test_backend_classification():
    assert _family("Backend Engineer", "Build APIs and distributed systems.") == RoleFamily.backend


def test_ai_classification():
    assert _family("Machine Learning Engineer", "Train LLMs and build RAG pipelines.") == (
        RoleFamily.ai
    )


def test_data_classification():
    assert _family("Data Engineer", "Own ETL and the data warehouse with dbt and Spark.") == (
        RoleFamily.data
    )


def test_product_classification():
    assert _family("Product Engineer", "Ship product features end to end.") == RoleFamily.product


def test_other_classification():
    assert _family("Account Executive, AI Sales", "Close deals with enterprise clients.") == (
        RoleFamily.other
    )


def test_generic_swe_defaults_to_backend():
    assert _family("Software Engineer, New Grad", "Join our engineering team.") == (
        RoleFamily.backend
    )


# --- Phase 5.1: title-first gating additional credible-family cases ---
def test_site_reliability_engineer_classification():
    assert _family("Site Reliability Engineer, New Grad", "On-call, uptime, incident response.") == (
        RoleFamily.backend
    )


def test_platform_engineer_classification():
    assert _family("Platform Engineer I", "Build internal developer platform tooling.") == (
        RoleFamily.backend
    )


def test_applied_scientist_classification():
    assert _family("Applied Scientist, New Grad", "Research and ship ML models at scale.") == (
        RoleFamily.ai
    )


def test_ai_description_cannot_promote_nontechnical_title():
    """A description stuffed with AI/ML terms must not resurrect a
    nontechnical title into a target role -- the title gate blocks scoring
    the description entirely."""
    assert _family(
        "Marketing Coordinator",
        "Support our AI-powered marketing platform using machine learning and LLMs.",
    ) == RoleFamily.other


def test_ambiguous_title_is_other_even_with_thin_description():
    assert _family("Program Coordinator", "General office support.") == RoleFamily.other


# --- resume recommendation (fallback: no resume_profiles configured) ---
def test_resume_ai_for_ai_role():
    rec = _recommend("ML Engineer", "Deep learning, PyTorch, inference at scale.")
    assert rec.variant == ResumeVariant.ai
    assert rec.confidence in (ResumeConfidence.high, ResumeConfidence.medium)


def test_resume_data_for_data_role():
    assert _recommend("Analytics Engineer", "dbt, SQL, data warehouse, BI.").variant == (
        ResumeVariant.data
    )


def test_resume_backend_for_generic_swe():
    assert _recommend("Software Engineer", "General engineering.").variant == ResumeVariant.backend


def test_resume_backend_for_product_role():
    rec = _recommend("Product Engineer", "Own product features.")
    assert rec.variant == ResumeVariant.backend


def test_resume_other_is_low_confidence_backend():
    rec = _recommend("Recruiter", "Hire people.")
    assert rec.variant == ResumeVariant.backend
    assert rec.confidence == ResumeConfidence.low
