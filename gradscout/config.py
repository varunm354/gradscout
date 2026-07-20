"""Load and validate config.yaml into the Pydantic Config model.

Secrets are NOT read here; they come from environment variables only.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from gradscout.models import Config

DEFAULT_CONFIG_PATH = Path("config.yaml")


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy config.example.yaml to config.yaml."
        )
    data = yaml.safe_load(path.read_text()) or {}
    return Config.model_validate(data)
