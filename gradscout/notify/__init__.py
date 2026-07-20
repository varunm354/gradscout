"""Notification outputs.

Planned:
    discord.py -> webhook sender (dry-run aware; only marks an alert sent
                  after Discord accepts it, so max_alerts_per_run never loses jobs).
    health.py  -> failure-only immediate alerts + one daily success summary.
"""
