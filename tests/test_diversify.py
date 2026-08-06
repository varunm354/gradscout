"""Phase 6 company/resume-category diversification (gradscout.diversify).

Pure unit tests against plain dict rows shaped like the sqlite3.Row objects
``gradscout.db.get_pending_alerts`` returns (dicts support the same
``row["key"]``/``row.keys()`` access this module relies on).
"""

from __future__ import annotations

from gradscout.diversify import diversify_by_company


def _row(job_id, company, *, priority="p2", created_at="t0", resume=None):
    return {
        "job_id": job_id,
        "company": company,
        "priority": priority,
        "created_at": created_at,
        "recommended_resume": resume,
    }


def _ids(rows):
    return [r["job_id"] for r in rows]


def test_under_cap_all_selected_none_suppressed():
    rows = [_row(1, "Acme"), _row(2, "Acme")]
    selected, suppressed = diversify_by_company(rows, per_company_cap=3)
    assert _ids(selected) == [1, 2]
    assert suppressed == []


def test_over_cap_excess_from_same_company_suppressed():
    rows = [_row(i, "OpenAI") for i in range(1, 6)]  # 5 jobs, one company
    selected, suppressed = diversify_by_company(rows, per_company_cap=3)
    assert len(selected) == 3
    assert len(suppressed) == 2
    # Overall relative order preserved (no shuffling among same-priority/company rows).
    assert _ids(selected) == [1, 2, 3]
    assert _ids(suppressed) == [4, 5]


def test_prefers_one_of_each_resume_category_before_repeating():
    """6 jobs from one company: 2 ai, 2 backend, 2 data. A cap of 3 must pick
    exactly one of each category first, not e.g. all 2 ai + 1 backend."""
    rows = [
        _row(1, "OpenAI", resume="ai"),
        _row(2, "OpenAI", resume="ai"),
        _row(3, "OpenAI", resume="backend"),
        _row(4, "OpenAI", resume="backend"),
        _row(5, "OpenAI", resume="data"),
        _row(6, "OpenAI", resume="data"),
    ]
    selected, suppressed = diversify_by_company(rows, per_company_cap=3)
    selected_resumes = {r["recommended_resume"] for r in selected}
    assert selected_resumes == {"ai", "backend", "data"}
    assert len(selected) == 3
    assert len(suppressed) == 3


def test_second_round_robin_pass_fills_remaining_cap():
    """Cap of 5 with 2 ai / 2 backend / 2 data (6 total): one full round (3)
    plus a second partial round picks up the 2 remaining categories with
    leftovers, never leaving cap headroom unused while candidates remain."""
    rows = [
        _row(1, "OpenAI", resume="ai"),
        _row(2, "OpenAI", resume="ai"),
        _row(3, "OpenAI", resume="backend"),
        _row(4, "OpenAI", resume="backend"),
        _row(5, "OpenAI", resume="data"),
        _row(6, "OpenAI", resume="data"),
    ]
    selected, suppressed = diversify_by_company(rows, per_company_cap=5)
    assert len(selected) == 5
    assert len(suppressed) == 1


def test_urgency_ranked_within_company_before_round_robin():
    """Within one company, a p1 job is preferred over a p2/p3 job of the same
    resume category (more urgent items win the category's queue slot first)."""
    rows = [
        _row(1, "Acme", priority="p3", resume="ai", created_at="t0"),
        _row(2, "Acme", priority="p1", resume="ai", created_at="t1"),
    ]
    selected, suppressed = diversify_by_company(rows, per_company_cap=1)
    assert _ids(selected) == [2]
    assert _ids(suppressed) == [1]


def test_multiple_companies_each_get_their_own_cap():
    rows = [_row(i, "OpenAI") for i in range(1, 5)] + [_row(i, "Anthropic") for i in range(5, 8)]
    selected, suppressed = diversify_by_company(rows, per_company_cap=2)
    selected_by_company: dict[str, int] = {}
    for r in selected:
        selected_by_company[r["company"]] = selected_by_company.get(r["company"], 0) + 1
    assert selected_by_company == {"OpenAI": 2, "Anthropic": 2}
    assert len(suppressed) == 3  # 2 excess OpenAI + 1 excess Anthropic


def test_unrecognized_resume_value_treated_as_other_category():
    """A row with no resume recommendation (None) or a value outside
    ai/backend/data still participates fairly in round-robin as its own
    'other' bucket, rather than being silently dropped or crashing."""
    rows = [
        _row(1, "Acme", resume=None),
        _row(2, "Acme", resume=None),
        _row(3, "Acme", resume="ai"),
    ]
    selected, suppressed = diversify_by_company(rows, per_company_cap=2)
    assert len(selected) == 2
    assert len(suppressed) == 1
    # "ai" (a real category) and one "other" should both make it in round 1.
    assert 3 in _ids(selected)


def test_empty_rows_returns_empty():
    selected, suppressed = diversify_by_company([], per_company_cap=3)
    assert selected == []
    assert suppressed == []


def test_review_priority_rows_supported_same_as_individual_alerts():
    """The review digest reuses this same function with priority='review' for
    every row -- urgency ranking degrades gracefully to created_at order."""
    rows = [_row(i, "Acme", priority="review", created_at=f"t{i}") for i in range(5)]
    selected, suppressed = diversify_by_company(rows, per_company_cap=3)
    assert len(selected) == 3
    assert len(suppressed) == 2
    assert _ids(selected) == [0, 1, 2]  # earliest-created preferred within one category
