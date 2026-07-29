# GradScout state branch

This branch is machine-managed by `.github/workflows/gradscout-monitor.yml` (via `scripts/state_save.py`). It exists ONLY to durably persist the pipeline database, gzip-compressed as `data/gradscout.db.gz`, between hourly runs, and shares no commit history with `main`.

Do not edit or merge this branch into `main`. Every commit here is produced by the workflow and contains exactly this README and `data/gradscout.db.gz` -- nothing else.
