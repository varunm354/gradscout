"""Deterministic role-family classification: backend | ai | data | product | other.

Phase 5.1: title-first technical relevance gating (see ``evaluate_title_gate``).
A job's TITLE must show a credible target-role signal before its description is
ever consulted for family scoring -- this is what stops an AI/ML/API mention in
the body of a nontechnical role (compliance, policy, marketing, sales,
biological-safety, fellowships, ...) from making it look like a target role.
Once a title clears the gate, weighted keyword scoring over title + description
picks the specific family (backend/ai/data/product); a generic software-
engineering title with no strong AI/Data/Product signal defaults to backend.
Returns matched-term evidence and a margin used for resume confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gradscout.models import Job, RoleFamily
from gradscout.textmatch import find_first, normalize

# (keyword, weight). Title matches are additionally boosted (see classify_role).
AI_KEYWORDS = [
    ("machine learning", 3), ("ml engineer", 3), ("ai engineer", 3), ("applied ai", 3),
    ("applied ml", 3), ("deep learning", 3), ("llm", 3), ("large language model", 3),
    ("rag", 2), ("generative ai", 3), ("nlp", 2), ("computer vision", 3),
    ("mlops", 3), ("model training", 2), ("inference", 2), ("pytorch", 2),
    ("tensorflow", 2), ("recommendation system", 2), ("ai/ml", 3),
    ("artificial intelligence", 2), ("foundation model", 3), ("ml platform", 3),
    ("machine learning engineer", 4), ("applied scientist", 3), ("research engineer", 2),
]
DATA_KEYWORDS = [
    ("data engineer", 4), ("analytics engineer", 4), ("data engineering", 3),
    ("etl", 3), ("elt", 3), ("data pipeline", 3), ("data warehouse", 3),
    ("business intelligence", 3), ("bi developer", 3), ("dbt", 2), ("airflow", 2),
    ("snowflake", 2), ("spark", 2), ("hadoop", 2), ("kafka", 1), ("big data", 3),
    ("data platform", 3), ("experimentation", 2), ("sql", 1), ("analytics", 2),
    ("data scientist", 2),
]
BACKEND_KEYWORDS = [
    ("backend", 3), ("back-end", 3), ("back end", 3), ("distributed systems", 3),
    ("microservices", 3), ("api", 2), ("infrastructure", 2), ("platform engineer", 3),
    ("server-side", 2), ("scalability", 2), ("golang", 2), ("kubernetes", 2),
    ("software engineer", 2), ("software engineering", 2), ("systems engineer", 2),
    ("cloud", 1), ("services", 1), ("full stack", 2), ("full-stack", 2),
    ("site reliability", 3),
]
PRODUCT_KEYWORDS = [
    ("product engineer", 4), ("product engineering", 4), ("growth engineer", 4),
    ("product-focused", 3), ("product manager", 3), ("associate product manager", 4),
]

_FAMILY_KEYWORDS = {
    RoleFamily.ai: AI_KEYWORDS,
    RoleFamily.data: DATA_KEYWORDS,
    RoleFamily.backend: BACKEND_KEYWORDS,
    RoleFamily.product: PRODUCT_KEYWORDS,
}

# --------------------------------------------------------------------------- #
# Title-first technical relevance gate (Phase 5.1)
# --------------------------------------------------------------------------- #
# Strong nontechnical domain signals. A hit here always overrides a credible
# engineering/scientist phrase found in the SAME title (e.g. "Data Scientist,
# Marketing" is non_target, not target) -- the domain, not the job function,
# is what description keywords would otherwise be free to exploit.
NON_TARGET_TITLE_TOKENS = (
    "compliance",
    "policy",
    "biological safety",
    "marketing",
    "partnerships",
    "partnership",
    "sales",
    "account executive",
    "fellowship",
    "fellowships",
    "fellows",
    "economics",
    # Phase 6 review-digest cleanup: additional hard-nontechnical domains that
    # should never surface in the ambiguous "review" bucket, even if the
    # description happens to mention a technical term (e.g. "Legal Counsel,
    # AI Policy" or "Recruiter, Engineering").
    "legal",
    "counsel",
    "attorney",
    "procurement",
    "vendor management",
    "hr",
    "human resources",
    "people operations",
    "customer success",
    "business affairs",
    "workplace",
    "office coordinator",
    "facilities",
    "finance",
    "accounting",
    "controller",
    "bookkeeper",
    "payroll",
    "recruiting",
    "recruiter",
    "talent acquisition",
    "sourcer",
)

# Phrases that establish a credible, title-first signal that a listing is a
# target technical role, mapped to the family they imply (``None`` = ambiguous,
# left for description keyword scoring to resolve). This intentionally goes a
# little beyond the spec's literal examples -- e.g. "ml engineer" as an alias
# for "machine learning engineer", "research engineer"/"applied scientist" for
# AI-lab titles, "analytics engineer"/"data scientist" as data-adjacent titles
# -- so real-world variants of the same credible families aren't rejected. A
# description can never independently create this signal; see
# ``evaluate_title_gate`` / ``classify_role``.
CREDIBLE_TITLE_FAMILY: dict[str, RoleFamily | None] = {
    "software engineer": RoleFamily.backend,
    "software engineering": RoleFamily.backend,
    "backend engineer": RoleFamily.backend,
    "back-end engineer": RoleFamily.backend,
    "back end engineer": RoleFamily.backend,
    "full-stack engineer": RoleFamily.backend,
    "full stack engineer": RoleFamily.backend,
    "platform engineer": RoleFamily.backend,
    "infrastructure engineer": RoleFamily.backend,
    "site reliability engineer": RoleFamily.backend,
    "systems engineer": RoleFamily.backend,
    "machine learning engineer": RoleFamily.ai,
    "ml engineer": RoleFamily.ai,
    "ai engineer": RoleFamily.ai,
    "applied scientist": RoleFamily.ai,
    "research engineer": RoleFamily.ai,
    "data engineer": RoleFamily.data,
    "analytics engineer": RoleFamily.data,
    "data scientist": None,
    "product engineer": RoleFamily.product,
}


@dataclass
class TitleGate:
    """Result of ``evaluate_title_gate``. A job must clear this gate -- via
    its TITLE alone -- before description keywords are ever consulted for
    role-family scoring (``classify_role``), and before ``eligibility.py``
    will ever mark it eligible for a normal p1/p2/p3 alert."""

    credible: bool
    non_target: bool
    family_hint: RoleFamily | None
    matched_credible: str | None
    matched_non_target: str | None

    @property
    def verdict(self) -> str:
        if self.non_target:
            return "non_target"
        if self.credible:
            return "target"
        return "ambiguous"


def evaluate_title_gate(title: str) -> TitleGate:
    """Classify a job TITLE (never the description) as target / non_target /
    ambiguous. A strong nontechnical domain signal (compliance, policy,
    marketing, sales, ...) always overrides a credible engineering/scientist
    phrase in the same title -- e.g. "Data Scientist, Marketing" is
    non_target, not target -- because the domain, not the job function, is
    what description keywords would otherwise be free to exploit."""
    title_norm = normalize(title)
    non_target_hit = find_first(title_norm, NON_TARGET_TITLE_TOKENS)
    credible_hit = find_first(title_norm, tuple(CREDIBLE_TITLE_FAMILY))
    return TitleGate(
        credible=credible_hit is not None,
        non_target=non_target_hit is not None,
        family_hint=CREDIBLE_TITLE_FAMILY.get(credible_hit) if credible_hit else None,
        matched_credible=credible_hit,
        matched_non_target=non_target_hit,
    )


@dataclass
class RoleClassification:
    family: RoleFamily
    scores: dict[RoleFamily, int]
    evidence: list[str] = field(default_factory=list)
    top_score: int = 0
    margin: int = 0

    @property
    def relevant(self) -> bool:
        return self.family != RoleFamily.other


def classify_role(job: Job) -> RoleClassification:
    gate = evaluate_title_gate(job.title)
    if not gate.credible or gate.non_target:
        # No credible title signal (ambiguous), or a strong nontechnical
        # domain signal in the title -- never scored against the
        # description, so AI/ML/API mentions in the body can never
        # resurrect a nontechnical or ambiguous title into a target role.
        return RoleClassification(RoleFamily.other, {}, [], 0, 0)

    title = normalize(job.title)
    body = normalize(f"{job.title}\n{job.description_text or ''}")

    scores: dict[RoleFamily, int] = {}
    evidence: dict[RoleFamily, list[str]] = {}
    for family, keywords in _FAMILY_KEYWORDS.items():
        total = 0
        matched: list[str] = []
        for kw, weight in keywords:
            if find_first(body, [kw]):
                # Title mentions are stronger signals than body-only mentions.
                boost = 1 if find_first(title, [kw]) else 0
                total += weight + boost
                matched.append(kw)
        scores[family] = total
        evidence[family] = matched

    top_score = max(scores.values()) if scores else 0
    if top_score == 0:
        # The title alone cleared the credible-title gate (e.g. a bland
        # description with no scored keywords) -- fall back to the gate's
        # family hint rather than discarding a title-verified target role.
        family = gate.family_hint or RoleFamily.backend
        return RoleClassification(
            family, scores, [f"title-only: '{gate.matched_credible}'"], 2, 2
        )

    # Deterministic tie-break order: prefer AI, then Data, then Product, then Backend
    # is applied only among genuine ties. Generic SWE (backend baseline) then wins
    # naturally because AI/Data/Product score 0 for it.
    order = [RoleFamily.ai, RoleFamily.data, RoleFamily.product, RoleFamily.backend]
    winner = max(order, key=lambda f: scores.get(f, 0))
    sorted_scores = sorted(scores.values(), reverse=True)
    margin = sorted_scores[0] - (sorted_scores[1] if len(sorted_scores) > 1 else 0)

    return RoleClassification(
        family=winner,
        scores=scores,
        evidence=[f"{winner.value}: {', '.join(evidence[winner])}"] if evidence[winner] else [],
        top_score=top_score,
        margin=margin,
    )
