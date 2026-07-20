"""SQLite persistence: schema, cross-source dedupe, source health, pending alerts.

Design notes:
  * jobs           - one row per distinct posting, uniquely keyed by url_canonical.
  * job_sources    - many-to-one mapping: every source that surfaced a job is kept,
                     so a role found via a native ATS *and* a GitHub repo merges into
                     one job while both source records are preserved.
  * source_health  - durable per-source status so "no new jobs" is only trusted when
                     the latest check actually succeeded.
  * alerts         - one row per (job, channel). An alert starts 'pending' and only
                     becomes 'sent' via an explicit mark_alert_sent() after a
                     successful delivery, so capped runs never lose alerts.

Uses only the stdlib sqlite3. Timestamps are ISO8601 UTC strings.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from gradscout.models import (
    AlertChannel,
    AlertState,
    Job,
    JobRecord,
    JobSourceRecord,
    SourceStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url_canonical      TEXT NOT NULL UNIQUE,
    company            TEXT NOT NULL,
    company_priority   INTEGER NOT NULL,
    title              TEXT NOT NULL,
    location           TEXT,
    remote             INTEGER,
    description_text   TEXT,
    apply_url          TEXT NOT NULL,
    source_posted_at   TEXT,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    eligibility_status TEXT NOT NULL,
    eligibility_reasons TEXT NOT NULL,
    role_family        TEXT NOT NULL,
    role_priority      INTEGER NOT NULL,
    employment_type    TEXT NOT NULL,
    is_new_grad        INTEGER,
    recommended_resume TEXT,
    resume_confidence  TEXT,
    resume_reason      TEXT,
    alert_priority     TEXT NOT NULL,
    llm_used           INTEGER NOT NULL DEFAULT 0,
    raw_blob           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_jobs_priority ON jobs(alert_priority);

CREATE TABLE IF NOT EXISTS job_sources (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    source         TEXT NOT NULL,
    source_company TEXT NOT NULL,
    source_job_id  TEXT,
    apply_url      TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_sources_job ON job_sources(job_id);
CREATE INDEX IF NOT EXISTS idx_job_sources_ident
    ON job_sources(source, source_company, source_job_id);

CREATE TABLE IF NOT EXISTS source_health (
    source_id        TEXT PRIMARY KEY,
    company          TEXT,
    company_priority INTEGER NOT NULL,
    last_check_at    TEXT NOT NULL,
    last_success_at  TEXT,
    last_status      TEXT NOT NULL,
    last_error       TEXT,
    jobs_seen        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts (
    job_id     INTEGER NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    channel    TEXT NOT NULL,
    priority   TEXT NOT NULL,
    state      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at    TEXT,
    PRIMARY KEY (job_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_alerts_state ON alerts(channel, state);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _bool_to_int(v: bool | None) -> int | None:
    return None if v is None else int(v)


# --------------------------------------------------------------------------- #
# Connection / schema
# --------------------------------------------------------------------------- #
def connect(db_path: str | Path = "data/gradscout.db") -> sqlite3.Connection:
    db_path = Path(db_path)
    if db_path.parent and str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------- #
# Job upsert / cross-source dedupe
# --------------------------------------------------------------------------- #
def _find_source_row(conn: sqlite3.Connection, job: Job) -> sqlite3.Row | None:
    """Locate an existing job_sources row for this posting from the same source,
    by stable source identity first, then by original apply_url."""
    if job.source_job_id is not None:
        row = conn.execute(
            "SELECT id, job_id FROM job_sources "
            "WHERE source=? AND source_company=? AND source_job_id=?",
            (job.source.value, job.source_company, job.source_job_id),
        ).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT id, job_id FROM job_sources WHERE source=? AND apply_url=?",
        (job.source.value, job.apply_url),
    ).fetchone()


def _find_job_id(conn: sqlite3.Connection, job: Job) -> int | None:
    """Merge key: stable source identity OR canonical application URL."""
    src = _find_source_row(conn, job)
    if src is not None:
        return int(src["job_id"])
    row = conn.execute(
        "SELECT job_id FROM jobs WHERE url_canonical=?", (job.url_canonical,)
    ).fetchone()
    return int(row["job_id"]) if row else None


def upsert_job(
    conn: sqlite3.Connection, job: Job, now: datetime | None = None
) -> tuple[int, bool]:
    """Insert or merge a normalized job. Returns (job_id, created).

    Merges by stable source identity OR canonical URL. On merge, refreshes
    last_seen_at and mutable classification fields but preserves first_seen_at,
    and records/refreshes the job_sources mapping row for this source.
    """
    now = now or _utcnow()
    now_s = now.isoformat()
    job_id = _find_job_id(conn, job)

    if job_id is None:
        cur = conn.execute(
            """
            INSERT INTO jobs (
                url_canonical, company, company_priority, title, location, remote,
                description_text, apply_url, source_posted_at, first_seen_at,
                last_seen_at, eligibility_status, eligibility_reasons, role_family,
                role_priority, employment_type, is_new_grad, recommended_resume,
                resume_confidence, resume_reason, alert_priority, llm_used, raw_blob
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job.url_canonical,
                job.company,
                job.company_priority,
                job.title,
                job.location,
                _bool_to_int(job.remote),
                job.description_text,
                job.apply_url,
                _iso(job.source_posted_at),
                now_s,
                now_s,
                job.eligibility_status.value,
                json.dumps(job.eligibility_reasons),
                job.role_family.value,
                job.role_priority,
                job.employment_type.value,
                _bool_to_int(job.is_new_grad),
                job.recommended_resume.value if job.recommended_resume else None,
                job.resume_confidence.value if job.resume_confidence else None,
                job.resume_reason,
                job.alert_priority.value,
                int(job.llm_used),
                json.dumps(job.raw_blob),
            ),
        )
        job_id = int(cur.lastrowid)
        created = True
    else:
        conn.execute(
            """
            UPDATE jobs SET
                last_seen_at=?, company_priority=?, source_posted_at=COALESCE(?, source_posted_at),
                eligibility_status=?, eligibility_reasons=?, role_family=?, role_priority=?,
                employment_type=?, is_new_grad=?, recommended_resume=?, resume_confidence=?,
                resume_reason=?, alert_priority=?, llm_used=?
            WHERE job_id=?
            """,
            (
                now_s,
                job.company_priority,
                _iso(job.source_posted_at),
                job.eligibility_status.value,
                json.dumps(job.eligibility_reasons),
                job.role_family.value,
                job.role_priority,
                job.employment_type.value,
                _bool_to_int(job.is_new_grad),
                job.recommended_resume.value if job.recommended_resume else None,
                job.resume_confidence.value if job.resume_confidence else None,
                job.resume_reason,
                job.alert_priority.value,
                int(job.llm_used),
                job_id,
            ),
        )
        created = False

    _upsert_source(conn, job_id, job, now_s)
    conn.commit()
    return job_id, created


def _upsert_source(conn: sqlite3.Connection, job_id: int, job: Job, now_s: str) -> None:
    src = _find_source_row(conn, job)
    if src is not None:
        conn.execute(
            "UPDATE job_sources SET last_seen_at=?, job_id=? WHERE id=?",
            (now_s, job_id, src["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO job_sources
                (job_id, source, source_company, source_job_id, apply_url,
                 first_seen_at, last_seen_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                job_id,
                job.source.value,
                job.source_company,
                job.source_job_id,
                job.apply_url,
                now_s,
                now_s,
            ),
        )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def _row_to_record(conn: sqlite3.Connection, row: sqlite3.Row) -> JobRecord:
    src_rows = conn.execute(
        "SELECT source, source_company, source_job_id, apply_url, first_seen_at, "
        "last_seen_at FROM job_sources WHERE job_id=? ORDER BY id",
        (row["job_id"],),
    ).fetchall()
    sources = [
        JobSourceRecord(
            source=s["source"],
            source_company=s["source_company"],
            source_job_id=s["source_job_id"],
            apply_url=s["apply_url"],
            first_seen_at=s["first_seen_at"],
            last_seen_at=s["last_seen_at"],
        )
        for s in src_rows
    ]
    return JobRecord(
        job_id=row["job_id"],
        url_canonical=row["url_canonical"],
        company=row["company"],
        company_priority=row["company_priority"],
        title=row["title"],
        location=row["location"],
        remote=None if row["remote"] is None else bool(row["remote"]),
        description_text=row["description_text"],
        apply_url=row["apply_url"],
        source_posted_at=row["source_posted_at"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        eligibility_status=row["eligibility_status"],
        eligibility_reasons=json.loads(row["eligibility_reasons"]),
        role_family=row["role_family"],
        role_priority=row["role_priority"],
        employment_type=row["employment_type"],
        is_new_grad=None if row["is_new_grad"] is None else bool(row["is_new_grad"]),
        recommended_resume=row["recommended_resume"],
        resume_confidence=row["resume_confidence"],
        resume_reason=row["resume_reason"],
        alert_priority=row["alert_priority"],
        llm_used=bool(row["llm_used"]),
        sources=sources,
    )


def get_job(conn: sqlite3.Connection, job_id: int) -> JobRecord | None:
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return _row_to_record(conn, row) if row else None


def get_job_by_canonical(conn: sqlite3.Connection, url_canonical: str) -> JobRecord | None:
    row = conn.execute(
        "SELECT * FROM jobs WHERE url_canonical=?", (url_canonical,)
    ).fetchone()
    return _row_to_record(conn, row) if row else None


def count_jobs(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"])


# --------------------------------------------------------------------------- #
# Source health
# --------------------------------------------------------------------------- #
def record_source_result(
    conn: sqlite3.Connection,
    source_id: str,
    company: str | None,
    company_priority: int,
    status: SourceStatus,
    error: str | None = None,
    jobs_seen: int = 0,
    now: datetime | None = None,
) -> None:
    now = now or _utcnow()
    now_s = now.isoformat()
    success_s = now_s if status == SourceStatus.ok else None
    conn.execute(
        """
        INSERT INTO source_health
            (source_id, company, company_priority, last_check_at, last_success_at,
             last_status, last_error, jobs_seen)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(source_id) DO UPDATE SET
            company=excluded.company,
            company_priority=excluded.company_priority,
            last_check_at=excluded.last_check_at,
            last_success_at=COALESCE(excluded.last_success_at, source_health.last_success_at),
            last_status=excluded.last_status,
            last_error=excluded.last_error,
            jobs_seen=excluded.jobs_seen
        """,
        (source_id, company, company_priority, now_s, success_s,
         status.value, error, jobs_seen),
    )
    conn.commit()


def get_source_health(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM source_health ORDER BY company_priority, source_id"
    ).fetchall()


# --------------------------------------------------------------------------- #
# Alerts (pending -> sent)
# --------------------------------------------------------------------------- #
def enqueue_alert(
    conn: sqlite3.Connection,
    job_id: int,
    channel: AlertChannel,
    priority: str,
    now: datetime | None = None,
) -> bool:
    """Queue an alert as pending. Idempotent: returns False if an alert for this
    (job, channel) already exists in any state, so we never duplicate."""
    now = now or _utcnow()
    existing = conn.execute(
        "SELECT 1 FROM alerts WHERE job_id=? AND channel=?",
        (job_id, channel.value),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        "INSERT INTO alerts (job_id, channel, priority, state, created_at, sent_at) "
        "VALUES (?,?,?,?,?,NULL)",
        (job_id, channel.value, priority, AlertState.pending.value, now.isoformat()),
    )
    conn.commit()
    return True


def get_pending_alerts(
    conn: sqlite3.Connection, channel: AlertChannel
) -> list[sqlite3.Row]:
    """Pending alerts joined with job context, ordered for prioritized delivery
    (highest-priority company first, then oldest-queued first)."""
    return conn.execute(
        """
        SELECT a.job_id, a.channel, a.priority, a.created_at,
               j.company, j.company_priority, j.title, j.apply_url, j.alert_priority
        FROM alerts a JOIN jobs j ON j.job_id = a.job_id
        WHERE a.channel=? AND a.state=?
        ORDER BY j.company_priority ASC, a.created_at ASC
        """,
        (channel.value, AlertState.pending.value),
    ).fetchall()


def mark_alert_sent(
    conn: sqlite3.Connection,
    job_id: int,
    channel: AlertChannel,
    now: datetime | None = None,
) -> bool:
    """Explicit successful-send operation. Only a pending alert flips to sent.
    Returns True if a row was updated."""
    now = now or _utcnow()
    cur = conn.execute(
        "UPDATE alerts SET state=?, sent_at=? WHERE job_id=? AND channel=? AND state=?",
        (AlertState.sent.value, now.isoformat(), job_id, channel.value,
         AlertState.pending.value),
    )
    conn.commit()
    return cur.rowcount > 0


def get_alert(
    conn: sqlite3.Connection, job_id: int, channel: AlertChannel
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM alerts WHERE job_id=? AND channel=?",
        (job_id, channel.value),
    ).fetchone()
