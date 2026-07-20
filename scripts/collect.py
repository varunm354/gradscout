"""Phase 2 live collector harness.

Runs the configured collectors against the network, normalizes results, and
prints a per-source health + sample summary. This is for exercising collectors
only: it never sends alerts (notifications arrive in Phase 4).

Usage:
    python -m scripts.collect --dry-run                       # all sources, no DB writes
    python -m scripts.collect --persist                       # upsert jobs + record health
    python -m scripts.collect --dry-run --no-github           # ATS-only quick test
    python -m scripts.collect --dry-run --source ashby:openai # exactly one source
    python -m scripts.collect --dry-run --source github:simplify --limit 1

--limit only affects how many sample jobs are PRINTED; every row is still parsed.
"""

from __future__ import annotations

import argparse
import logging

import httpx

from gradscout import db
from gradscout.collectors.base import DEFAULT_TIMEOUT, USER_AGENT, Collector, run_collector
from gradscout.collectors.factory import build_collectors
from gradscout.config import load_config
from gradscout.logging_setup import setup_logging
from gradscout.models import SourceType
from gradscout.normalize import normalize

# Friendly short names accepted by --source, mapped to a SourceType value.
_TYPE_ALIASES = {
    "greenhouse": SourceType.greenhouse.value,
    "lever": SourceType.lever.value,
    "ashby": SourceType.ashby.value,
    "github": SourceType.github_repo.value,
    "github_repo": SourceType.github_repo.value,
}


def source_matches(collector: Collector, flt: str) -> bool:
    """Match a --source filter against a collector.

    Accepts the exact source_id (e.g. "greenhouse:stripe") or a friendly
    "type:slug-substring" form (e.g. "github:simplify")."""
    flt = flt.strip().lower()
    if not flt:
        return True
    if flt == collector.source_id.lower():
        return True
    if ":" in flt:
        type_part, slug_part = flt.split(":", 1)
        type_value = _TYPE_ALIASES.get(type_part, type_part)
        return (
            type_value == collector.source_type.value
            and slug_part in collector.slug.lower()
        )
    return flt in collector.source_id.lower()


def main() -> int:
    ap = argparse.ArgumentParser(description="GradScout collector harness (no alerts).")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--db", default="data/gradscout.db")
    ap.add_argument("--persist", action="store_true", help="write jobs + health to DB")
    ap.add_argument("--dry-run", action="store_true", help="no DB writes (default)")
    ap.add_argument("--source", default=None, help="run only matching source(s)")
    ap.add_argument("--no-github", action="store_true", help="skip GitHub repo sources")
    ap.add_argument("--limit", type=int, default=3, help="sample jobs to print per source")
    args = ap.parse_args()

    persist = args.persist and not args.dry_run
    log = setup_logging(logging.INFO)
    config = load_config(args.config)

    collectors = build_collectors(config)
    if args.no_github:
        collectors = [c for c in collectors if c.source_type != SourceType.github_repo]
    if args.source:
        collectors = [c for c in collectors if source_matches(c, args.source)]
    if not collectors:
        log.warning("no collectors matched", extra={"fields": {"source": args.source}})
        return 1

    conn = db.connect(args.db) if persist else None
    if conn is not None:
        db.init_db(conn)

    total_jobs = 0
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT) as client:
        for collector in collectors:
            result = run_collector(collector, client)
            total_jobs += result.jobs_seen
            print(
                f"== {result.source_id}: {result.status.value} | "
                f"jobs_seen={result.jobs_seen} parse_errors={result.parse_errors} "
                f"elapsed_ms={result.elapsed_ms}"
                + (f" | error={result.error}" if result.error else "")
            )
            for raw in result.raw_jobs[: args.limit]:
                job = normalize(raw, company_priority=result.company_priority)
                print(
                    f"   - {job.company} | {job.title} | {job.apply_url} | "
                    f"posted={job.source_posted_at}"
                )
            if conn is not None:
                for raw in result.raw_jobs:
                    db.upsert_job(conn, normalize(raw, result.company_priority))
                db.record_source_result(
                    conn,
                    result.source_id,
                    result.company,
                    result.company_priority,
                    result.status,
                    error=result.error,
                    jobs_seen=result.jobs_seen,
                )

    log.info(
        "collection complete",
        extra={"fields": {"total_jobs": total_jobs, "persisted": persist}},
    )
    if conn is not None:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
