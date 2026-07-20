"""Production config preflight check for GitHub Actions (Phase 5).

Fails clearly (non-zero exit, human-readable message, never a raw traceback,
never a secret) if config.yaml is missing or still looks like the unedited
example/demo config. Run by .github/workflows/gradscout-monitor.yml BEFORE
the pipeline, for every trigger (schedule, manual dry-run, and manual real
run) -- so a manual dry-run accurately tests deployment readiness, and a
scheduled/real run never silently monitors demo sources.

config.yaml is not a secret: it is supplied by being committed to the
repository (same as code), which is why this check reads it directly from
the working tree rather than from any secret store.

Usage:
    python -m scripts.check_config --config config.yaml
"""

from __future__ import annotations

import argparse
import sys

from gradscout.config import ProductionConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate config.yaml is production-safe.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--example-config", default="config.example.yaml")
    args = ap.parse_args(argv)

    try:
        config = load_config(
            args.config, require_production=True, example_path=args.example_config
        )
    except ProductionConfigError as exc:
        print(f"::error::production config check failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # invalid YAML / schema validation failure, etc.
        print(f"::error::config.yaml failed to load: {exc}", file=sys.stderr)
        return 1

    n_sources = (
        len(config.greenhouse) + len(config.lever) + len(config.ashby) + len(config.github_repos)
    )
    print(
        f"config check: OK ({len(config.watchlist)} watchlist companies, "
        f"{n_sources} sources configured)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
