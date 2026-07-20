"""Notification outputs.

discord.py -> the (single) webhook sender: per-job embeds for urgent/strong
             matches, one batched digest for Eligibility Review jobs, and
             short operational embeds for source failure/recovery and the
             once-daily health summary. Dry-run aware; only gradscout.pipeline
             calls mark_alert_sent(), and only after a successful (2xx)
             delivery.
"""
