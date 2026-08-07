"""Phase 6 company/resume-category diversification for alerts and the review digest.

A single prolific poster (e.g. OpenAI) can otherwise dominate every individual
alert and every review-digest slot in a run. ``diversify_by_company`` caps how
many items from the SAME company one run may select and, within a company,
round-robins across resume categories (ai / backend / data / other) so it
prefers "one AI, one Backend, one Data" before ever repeating a category --
before the existing global ``max_alerts_per_run`` / ``MAX_DIGEST_ITEMS`` slice
is applied on top.

Pure and side-effect free: takes/returns the ``sqlite3.Row`` objects from
``gradscout.db.get_pending_alerts`` (never touches the DB itself). Callers
(``gradscout.pipeline``) are responsible for persisting the outcome -- sending
the ``selected`` rows and marking the ``suppressed`` rows via
``gradscout.db.suppress_alert``.
"""

from __future__ import annotations

from typing import Any

# Round-robin order: prefer at most one of each resume category before
# repeating any category. "other" covers jobs with no resume recommendation
# (e.g. hard-ineligible-adjacent edge cases) or a value outside ai/backend/data.
_CATEGORY_ORDER = ("ai", "backend", "data", "other")

# Individual-alert priority rank (lower = more urgent = ranked first within a
# company); "review" is included for the review-digest's use of this same
# function, ordered least urgent since digest rows all share priority="review"
# anyway (created_at then decides).
_PRIORITY_RANK = {"p1": 0, "p2": 1, "p3": 2, "review": 3}


def _priority_rank(row: Any) -> int:
    return _PRIORITY_RANK.get(row["priority"], 4)


def _category_of(row: Any, resume_key: str) -> str:
    keys = row.keys() if hasattr(row, "keys") else row
    value = row[resume_key] if resume_key in keys else None
    return value if value in ("ai", "backend", "data") else "other"


def _select_diverse(items: list[Any], cap: int, resume_key: str) -> list[Any]:
    """Rank a single company's candidate rows internally (most urgent first),
    then round-robin across resume categories, taking at most one per
    category per round, until ``cap`` is reached or candidates are exhausted.

    Ranks by priority only and relies on Python's stable sort to preserve
    ``items``' own relative order as the tiebreak for equal-priority rows --
    which is exactly ``created_at`` ascending for the review digest (the
    order gradscout.db.get_pending_alerts already returns), and newest
    ``source_posted_at`` first for individual alerts (see
    gradscout.pipeline._send_job_alerts, Phase 6.2), so within-company
    selection prefers the freshest job at a given priority without this
    function needing to know which ordering convention its caller used."""
    ranked = sorted(items, key=_priority_rank)
    queues: dict[str, list[Any]] = {c: [] for c in _CATEGORY_ORDER}
    for row in ranked:
        queues[_category_of(row, resume_key)].append(row)

    selected: list[Any] = []
    progressed = True
    while len(selected) < cap and progressed:
        progressed = False
        for category in _CATEGORY_ORDER:
            if len(selected) >= cap:
                break
            queue = queues[category]
            if queue:
                selected.append(queue.pop(0))
                progressed = True
    return selected


def diversify_by_company(
    rows: list[Any],
    *,
    per_company_cap: int,
    resume_key: str = "recommended_resume",
) -> tuple[list[Any], list[Any]]:
    """Split ``rows`` into ``(selected, suppressed)`` under a per-company cap.

    ``rows`` must already be in the desired overall priority order (as
    returned by ``gradscout.db.get_pending_alerts``); that relative order is
    preserved in both output lists. Grouping is by the row's ``company``
    field; within each company's group, ``_select_diverse`` picks up to
    ``per_company_cap`` rows, preferring diversity across resume categories.
    """
    company_order: list[str] = []
    groups: dict[str, list[Any]] = {}
    for row in rows:
        company = row["company"]
        if company not in groups:
            groups[company] = []
            company_order.append(company)
        groups[company].append(row)

    selected_job_ids: set[int] = set()
    for company in company_order:
        chosen = _select_diverse(groups[company], per_company_cap, resume_key)
        selected_job_ids.update(row["job_id"] for row in chosen)

    selected = [row for row in rows if row["job_id"] in selected_job_ids]
    suppressed = [row for row in rows if row["job_id"] not in selected_job_ids]
    return selected, suppressed
