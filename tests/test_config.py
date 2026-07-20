import pytest

from gradscout.config import ProductionConfigError, load_config
from gradscout.models import AlertPriority


def test_example_config_loads_and_validates():
    cfg = load_config("config.example.yaml")
    assert cfg.candidate.graduation_year == 2027
    assert "backend" in cfg.candidate.resume_variants
    # notification defaults reflect failure-first / no-lost-alerts design
    assert cfg.notifications.send_healthy_reports is False
    assert cfg.notifications.discord_min_priority == AlertPriority.p2
    # watchlist entries carry configurable company_priority
    assert all(w.company_priority >= 1 for w in cfg.watchlist)


def test_example_config_with_require_production_is_unaffected_by_default():
    # Local dev flow (scripts/run.py) never passes require_production, so
    # using the example config locally must keep working exactly as before.
    cfg = load_config("config.example.yaml")
    assert cfg is not None


# --------------------------------------------------------------------------- #
# Phase 5: production-safety validation (require_production=True), used only
# by the GitHub Actions preflight (scripts/check_config.py), never by local
# `scripts.run` usage.
# --------------------------------------------------------------------------- #
def test_require_production_missing_file_raises_production_config_error(tmp_path):
    missing = tmp_path / "config.yaml"
    with pytest.raises(ProductionConfigError, match="not found"):
        load_config(missing, require_production=True)


def test_require_production_rejects_the_unedited_example_config():
    # config.example.yaml ships the "LeverDemo" placeholder and is meant to be
    # copied+edited, never run for real as-is.
    with pytest.raises(ProductionConfigError, match="example/demo"):
        load_config("config.example.yaml", require_production=True)


def test_require_production_rejects_demo_source_even_if_file_edited(tmp_path):
    example_path = tmp_path / "config.example.yaml"
    example_path.write_text("candidate: {}\n")  # different example baseline
    real = tmp_path / "config.yaml"
    real.write_text(
        """
watchlist:
  - name: Acme Corp
    company_priority: 1
lever:
  - company: LeverDemo
    board: leverdemo
    company_priority: 3
"""
    )
    with pytest.raises(ProductionConfigError, match="LeverDemo"):
        load_config(real, require_production=True, example_path=example_path)


def test_require_production_rejects_config_with_zero_sources(tmp_path):
    example_path = tmp_path / "config.example.yaml"
    example_path.write_text("candidate: {}\n")
    real = tmp_path / "config.yaml"
    real.write_text(
        """
watchlist:
  - name: Acme Corp
    company_priority: 1
"""
    )
    with pytest.raises(ProductionConfigError, match="zero job sources"):
        load_config(real, require_production=True, example_path=example_path)


def test_require_production_accepts_a_real_looking_config(tmp_path):
    example_path = tmp_path / "config.example.yaml"
    example_path.write_text("candidate: {}\n")
    real = tmp_path / "config.yaml"
    real.write_text(
        """
candidate:
  graduation_year: 2027
watchlist:
  - name: Acme Corp
    company_priority: 1
greenhouse:
  - company: Acme Corp
    board: acmecorp
    company_priority: 1
"""
    )
    cfg = load_config(real, require_production=True, example_path=example_path)
    assert cfg.watchlist[0].name == "Acme Corp"
