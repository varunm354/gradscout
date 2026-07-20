"""Load and validate config.yaml into the Pydantic Config model.

Secrets are NOT read here; they come from environment variables only.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from gradscout.models import Config

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_EXAMPLE_CONFIG_PATH = Path("config.example.yaml")

# Any source/watchlist entry whose company or board name contains this
# (case-insensitive) is treated as an unedited placeholder -- config.example.yaml
# ships a "LeverDemo"/"leverdemo" source specifically so this check has a real
# example to catch. No real company should ever legitimately match this.
_DEMO_MARKER = "demo"


class ProductionConfigError(RuntimeError):
    """Raised when config.yaml fails production-safety validation.

    Distinct from FileNotFoundError so callers (e.g. scripts/check_config.py)
    can print an actionable, non-traceback message for the operator instead of
    quietly falling back to demo/example sources.
    """


def _demo_config_reasons(raw_text: str, config: Config, example_text: str | None) -> list[str]:
    """Return a list of human-readable reasons config.yaml looks unedited /
    not production-safe, or an empty list if it passes. Never inspects
    secrets (config.yaml must not contain any)."""
    reasons: list[str] = []

    if example_text is not None and raw_text.strip() == example_text.strip():
        reasons.append("config.yaml is byte-identical to config.example.yaml")

    demo_hits: list[str] = []
    for label, sources in (
        ("greenhouse", config.greenhouse),
        ("lever", config.lever),
        ("ashby", config.ashby),
    ):
        for s in sources:
            if _DEMO_MARKER in s.company.lower() or _DEMO_MARKER in s.board.lower():
                demo_hits.append(f"{label} company={s.company!r} board={s.board!r}")
    for w in config.watchlist:
        if _DEMO_MARKER in w.name.lower():
            demo_hits.append(f"watchlist company={w.name!r}")
    if demo_hits:
        reasons.append(
            "config.yaml still references example/demo source(s): " + "; ".join(demo_hits)
        )

    if not (config.greenhouse or config.lever or config.ashby or config.github_repos):
        reasons.append(
            "config.yaml configures zero job sources "
            "(greenhouse/lever/ashby/github_repos are all empty)"
        )

    return reasons


def load_config(
    path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    require_production: bool = False,
    example_path: str | Path = DEFAULT_EXAMPLE_CONFIG_PATH,
) -> Config:
    """Load config.yaml.

    require_production=False (default): existing local-dev behavior, unchanged
    -- used by ``scripts/run.py`` for local testing, where using the example
    config is expected and documented.

    require_production=True: additionally raise ProductionConfigError if the
    file is missing or still looks like the unedited example/demo config. Used
    only by the GitHub Actions preflight (``scripts/check_config.py``) so a
    scheduled/manual run never silently monitors demo sources.
    """
    path = Path(path)
    if not path.exists():
        if require_production:
            raise ProductionConfigError(
                f"Production config not found at {path}. config.yaml contains no secrets and "
                "must be committed to the repository (on main, alongside code). Copy "
                "config.example.yaml to config.yaml, replace the example watchlist/sources with "
                "your real ones, and commit it before running GradScout for real."
            )
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy config.example.yaml to config.yaml."
        )

    raw_text = path.read_text()
    data = yaml.safe_load(raw_text) or {}
    config = Config.model_validate(data)

    if require_production:
        example = Path(example_path)
        example_text = example.read_text() if example.exists() else None
        reasons = _demo_config_reasons(raw_text, config, example_text)
        if reasons:
            raise ProductionConfigError(
                "config.yaml failed production-safety validation:\n- "
                + "\n- ".join(reasons)
                + "\nEdit config.yaml with your real candidate/watchlist/source settings "
                "(it contains no secrets and may be committed to main) before running GradScout "
                "for real."
            )

    return config
