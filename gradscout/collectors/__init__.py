"""Job collectors. One module per source; each is fail-soft (a single source
failure never aborts the run and is recorded in source_health instead).

Planned (Phase 2): greenhouse, lever, ashby, github_repo.
"""
