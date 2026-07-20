from gradscout.models import ResumeConfidence, ResumeVariant, RoleFamily
from gradscout.resume import recommend_resume
from gradscout.roles import classify_role
from tests.conftest import make_job


def _family(title, desc=""):
    return classify_role(make_job(title, desc)).family


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


# --- resume recommendation ---
def test_resume_ai_for_ai_role():
    roles = classify_role(make_job("ML Engineer", "Deep learning, PyTorch, inference at scale."))
    rec = recommend_resume(roles)
    assert rec.variant == ResumeVariant.ai
    assert rec.confidence in (ResumeConfidence.high, ResumeConfidence.medium)


def test_resume_data_for_data_role():
    roles = classify_role(make_job("Analytics Engineer", "dbt, SQL, data warehouse, BI."))
    assert recommend_resume(roles).variant == ResumeVariant.data


def test_resume_backend_for_generic_swe():
    roles = classify_role(make_job("Software Engineer", "General engineering."))
    assert recommend_resume(roles).variant == ResumeVariant.backend


def test_resume_backend_for_product_role():
    roles = classify_role(make_job("Product Engineer", "Own product features."))
    rec = recommend_resume(roles)
    assert rec.variant == ResumeVariant.backend


def test_resume_other_is_low_confidence_backend():
    roles = classify_role(make_job("Recruiter", "Hire people."))
    rec = recommend_resume(roles)
    assert rec.variant == ResumeVariant.backend
    assert rec.confidence == ResumeConfidence.low
