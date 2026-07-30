"""GradScout operational entrypoint (Phase 4).

Runs the full pipeline once: collect -> normalize -> upsert (atomic
created/changed/unchanged) -> classify -> persist classification -> enqueue
alerts -> Discord delivery -> review digest -> source failure/recovery
notifications -> once-daily health summary.

Unlike scripts/collect.py (a network-only harness for exercising collectors),
this is the real entrypoint intended for scheduled/manual runs and always
writes to the DB.

Usage:
    python -m scripts.run                       # real run (needs DISCORD_WEBHOOK_URL)
    python -m scripts.run --dry-run             # full pipeline, no Discord sends
    python -m scripts.run --config config.yaml --db data/gradscout.db

No GitHub Actions / state-branch wiring here (Phase 5); this script assumes
the DB file at --db already exists or will be created locally.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone

import httpx

from gradscout import db
from gradscout.collectors.base import DEFAULT_TIMEOUT, USER_AGENT
from gradscout.collectors.factory import build_collectors
from gradscout.config import load_config
from gradscout.llm import JobAnalysisAgent, provider_from_env
from gradscout.logging_setup import setup_logging
from gradscout.notify.discord import DiscordNotifier
from gradscout.pipeline import run_once


def main() -> int:
    ap = argparse.ArgumentParser(description="GradScout operational pipeline run.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--db", default="data/gradscout.db")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="run the full pipeline (collect/classify/persist) but make no Discord "
        "requests and mark no alert sent",
    )
    ap.add_argument(
        "--now",
        default=None,
        help="override the run timestamp (ISO8601, UTC) -- mainly for local testing "
        "of the once-daily summary hour",
    )
    args = ap.parse_args()

    log = setup_logging(logging.INFO)
    config = load_config(args.config)

    now = datetime.fromisoformat(args.now).astimezone(timezone.utc) if args.now else None

    conn = db.connect(args.db)
    db.init_db(conn)

    collectors = build_collectors(config)
    if not collectors:
        log.warning("no collectors configured")

    agent = JobAnalysisAgent(provider_from_env())
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    notifier = DiscordNotifier(webhook_url=webhook_url, dry_run=args.dry_run)

    try:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT) as client:
            stats = run_once(conn, config, collectors, client, notifier, agent, now=now)
    finally:
        notifier.close()
        conn.close()

    log.info(
        "run finished",
        extra={
            "fields": {
                "dry_run": args.dry_run,
                "jobs_seen": stats.jobs_seen,
                "jobs_created": stats.jobs_created,
                "jobs_changed": stats.jobs_changed,
                "alerts_sent": stats.alerts_sent,
                "alerts_pending": stats.alerts_pending,
                "notification_delivery_failures": stats.notification_delivery_failures,
            }
        },
    )
    if stats.notification_delivery_failures:
        # Fail-soft by design (exit code stays 0 -- collection/classification/
        # persistence all still succeeded), but this WARNING line is what
        # makes a partial notification failure impossible to miss in a
        # GitHub Actions log even though the job itself is "green".
        log.warning(
            "one or more Discord notifications failed to deliver this run",
            extra={
                "fields": {
                    "notification_delivery_failures": stats.notification_delivery_failures,
                    "alerts_pending": stats.alerts_pending,
                }
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
