"""Deterministic role-family classification: backend | ai | data | product | other.

Weighted keyword scoring over title + description. Highest score wins; a generic
software-engineering role with no strong AI/Data/Product signal defaults to
backend. Returns matched-term evidence and a margin used for resume confidence.
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
    ("machine learning engineer", 4),
]
DATA_KEYWORDS = [
    ("data engineer", 4), ("analytics engineer", 4), ("data engineering", 3),
    ("etl", 3), ("elt", 3), ("data pipeline", 3), ("data warehouse", 3),
    ("business intelligence", 3), ("bi developer", 3), ("dbt", 2), ("airflow", 2),
    ("snowflake", 2), ("spark", 2), ("hadoop", 2), ("kafka", 1), ("big data", 3),
    ("data platform", 3), ("experimentation", 2), ("sql", 1), ("analytics", 2),
]
BACKEND_KEYWORDS = [
    ("backend", 3), ("back-end", 3), ("back end", 3), ("distributed systems", 3),
    ("microservices", 3), ("api", 2), ("infrastructure", 2), ("platform engineer", 3),
    ("server-side", 2), ("scalability", 2), ("golang", 2), ("kubernetes", 2),
    ("software engineer", 2), ("software engineering", 2), ("systems engineer", 2),
    ("cloud", 1), ("services", 1), ("full stack", 2), ("full-stack", 2),
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
        return RoleClassification(RoleFamily.other, scores, [], 0, 0)

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
