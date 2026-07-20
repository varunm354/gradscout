"""Pydantic models: normalized job schema and configuration schema.

Two job layers:
    RawJob  -> exactly what a collector emits (Phase 2), before normalization.
    Job     -> the normalized, storable record. Carries its originating source
               identity so the DB layer can write the job_sources mapping row.

Deterministic classification fields (eligibility, priority, resume) are optional
here; they are populated by later pipeline stages (Phase 3) and default to
"unclassified" so a freshly normalized job is always valid and never discarded.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --------------------------------------------------------------------------- #
# Enums (stored as their string values)
# --------------------------------------------------------------------------- #
class SourceType(str, Enum):
    greenhouse = "greenhouse"
    lever = "lever"
    ashby = "ashby"
    github_repo = "github_repo"


class EligibilityStatus(str, Enum):
    eligible = "eligible"
    review = "review"
    ineligible = "ineligible"
    unclassified = "unclassified"


class AlertPriority(str, Enum):
    p1 = "p1"
    p2 = "p2"
    p3 = "p3"
    review = "review"
    ineligible = "ineligible"
    unclassified = "unclassified"


class RoleFamily(str, Enum):
    backend = "backend"
    ai = "ai"
    data = "data"
    product = "product"
    other = "other"


class EmploymentType(str, Enum):
    fulltime = "fulltime"
    internship = "internship"
    unknown = "unknown"


class ResumeVariant(str, Enum):
    backend = "backend"
    ai = "ai"
    data = "data"


class ResumeConfidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class CoverageStatus(str, Enum):
    """How a high-priority company is actually being monitored."""

    direct = "direct"            # a native ATS source is configured for it
    indirect = "indirect"       # only represented via a GitHub repo
    not_configured = "not_configured"
    failed = "failed"           # its direct source errored on the latest run


class SourceStatus(str, Enum):
    ok = "ok"              # fetched and fully parsed (zero jobs is still ok)
    partial = "partial"   # fetched, but one or more rows failed to parse
    error = "error"       # fetch/structural failure; no jobs collected


class AlertChannel(str, Enum):
    discord = "discord"


class AlertState(str, Enum):
    pending = "pending"
    sent = "sent"


# --------------------------------------------------------------------------- #
# Job models
# --------------------------------------------------------------------------- #
class RawJob(BaseModel):
    """Collector output, before normalization.

    ``source_company`` is the *source identity slug* (board slug or repo name),
    used for stable source identity and health. ``company`` is the *employer*
    display name (equal to the board owner for ATS sources, or the per-row
    company for GitHub aggregator repos).
    """

    source: SourceType
    source_company: str
    company: str
    source_job_id: str | None = None
    title: str
    location: str | None = None
    description_text: str | None = None
    apply_url: str
    # Raw provenance string exactly as the source gave it (audit only).
    posted_at_raw: str | None = None
    # Parsed timestamp — set ONLY when the source provides a reliable one.
    source_posted_at: datetime | None = None
    raw_blob: dict = Field(default_factory=dict)


class Job(BaseModel):
    """Normalized, storable job. Carries the one source that produced it so the
    DB layer can record a job_sources mapping row and merge across sources."""

    # --- originating source identity (for job_sources mapping) ---
    source: SourceType
    source_company: str
    source_job_id: str | None = None
    apply_url: str

    # --- normalized core ---
    company: str
    company_priority: int = 3
    title: str
    location: str | None = None
    remote: bool | None = None
    description_text: str | None = None
    url_canonical: str

    # --- urgency / provenance (never fabricated) ---
    source_posted_at: datetime | None = None

    # --- classification (filled by later phases) ---
    eligibility_status: EligibilityStatus = EligibilityStatus.unclassified
    eligibility_reasons: list[str] = Field(default_factory=list)
    role_family: RoleFamily = RoleFamily.other
    role_priority: int = 4
    employment_type: EmploymentType = EmploymentType.unknown
    is_new_grad: bool | None = None

    recommended_resume: ResumeVariant | None = None
    resume_confidence: ResumeConfidence | None = None
    resume_reason: str | None = None

    alert_priority: AlertPriority = AlertPriority.unclassified
    llm_used: bool = False
    raw_blob: dict = Field(default_factory=dict)

    @field_validator("company_priority", "role_priority")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("priority must be >= 1")
        return v


class JobRecord(BaseModel):
    """A stored job as read back from the DB, including its internal id and the
    list of sources that have surfaced it."""

    job_id: int
    url_canonical: str
    company: str
    company_priority: int
    title: str
    location: str | None
    remote: bool | None
    description_text: str | None
    apply_url: str
    source_posted_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    eligibility_status: EligibilityStatus
    eligibility_reasons: list[str]
    role_family: RoleFamily
    role_priority: int
    employment_type: EmploymentType
    is_new_grad: bool | None
    recommended_resume: ResumeVariant | None
    resume_confidence: ResumeConfidence | None
    resume_reason: str | None
    alert_priority: AlertPriority
    llm_used: bool
    sources: list[JobSourceRecord] = Field(default_factory=list)


class JobSourceRecord(BaseModel):
    source: SourceType
    source_company: str
    source_job_id: str | None
    apply_url: str
    first_seen_at: datetime
    last_seen_at: datetime


# --------------------------------------------------------------------------- #
# Configuration models
# --------------------------------------------------------------------------- #
class CandidateProfile(BaseModel):
    graduation_year: int = 2027
    degree_level: str = "bachelor"
    role_families: list[str] = Field(default_factory=lambda: ["backend", "ai", "data", "product"])
    resume_variants: list[str] = Field(default_factory=lambda: ["backend", "ai", "data"])


class GreenhouseSource(BaseModel):
    company: str
    board: str
    company_priority: int = 3


class LeverSource(BaseModel):
    company: str
    board: str
    company_priority: int = 3


class AshbySource(BaseModel):
    company: str
    board: str
    company_priority: int = 3


class GithubRepoSource(BaseModel):
    name: str
    url: str
    parser: str = "generic_md"


class WatchlistCompany(BaseModel):
    name: str
    company_priority: int = 1
    aliases: list[str] = Field(default_factory=list)


class NotificationConfig(BaseModel):
    discord_min_priority: AlertPriority = AlertPriority.p2
    send_review_digest: bool = True
    max_alerts_per_run: int = 25
    send_healthy_reports: bool = False
    daily_summary_hour_utc: int = 13
    new_grad_recent_hours: int = 48
    max_years_experience: int = 5


class Config(BaseModel):
    candidate: CandidateProfile = Field(default_factory=CandidateProfile)
    watchlist: list[WatchlistCompany] = Field(default_factory=list)
    greenhouse: list[GreenhouseSource] = Field(default_factory=list)
    lever: list[LeverSource] = Field(default_factory=list)
    ashby: list[AshbySource] = Field(default_factory=list)
    github_repos: list[GithubRepoSource] = Field(default_factory=list)
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)


# --------------------------------------------------------------------------- #
# Phase 3: analysis models
# --------------------------------------------------------------------------- #
class AgentAnalysis(BaseModel):
    """Validated structured output of the bounded LLM job-analysis agent.

    The agent may annotate ambiguity, classify, select a resume and recommend a
    priority. It may NOT override explicit deterministic hard rules — that is
    enforced by the resolver, not by trusting this object.
    """

    model_config = ConfigDict(extra="ignore")

    eligibility_status: EligibilityStatus
    evidence: list[str] = Field(default_factory=list)
    role_family: RoleFamily
    recommended_resume: ResumeVariant | None = None
    resume_confidence: ResumeConfidence | None = None
    priority_recommendation: AlertPriority
    uncertainty_reasons: list[str] = Field(default_factory=list)
    requires_human_review: bool
    deterministic_disagreement: bool = False
    disagreement_reason: str | None = None


class ResolvedAnalysis(BaseModel):
    """The single validated classification result for a job, after comparing the
    deterministic classifier with the optional LLM classifier."""

    eligibility_status: EligibilityStatus
    eligibility_reasons: list[str] = Field(default_factory=list)
    employment_type: EmploymentType
    is_new_grad: bool | None
    role_family: RoleFamily
    recommended_resume: ResumeVariant | None
    resume_confidence: ResumeConfidence | None
    resume_reason: str | None
    company_priority: int
    role_priority: int
    alert_priority: AlertPriority
    llm_used: bool
    decided_by: str
    deterministic_disagreement: bool = False
    disagreement_reason: str | None = None
    requires_human_review: bool = False


# Resolve forward references for models that reference later-declared classes.
JobRecord.model_rebuild()
