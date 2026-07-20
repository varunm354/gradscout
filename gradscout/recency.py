"""Recency evaluation, feeding P1 urgency.

Prefers the source's own reliable posted timestamp; falls back to when
GradScout itself first discovered the job. Never fabricates a date: if
neither is available, the job is simply treated as not recent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def is_recent(
    source_posted_at: datetime | None,
    first_seen_at: datetime | None,
    recent_hours: int,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(timezone.utc)
    basis = source_posted_at or first_seen_at
    if basis is None:
        return False
    if basis.tzinfo is None:
        basis = basis.replace(tzinfo=timezone.utc)
    return (now - basis) <= timedelta(hours=recent_hours)
