"""Offline tests for the GitHub Actions production-config preflight (Phase 5).

Never touches the network or GitHub; only exercises scripts.check_config's
CLI wrapper against local files.
"""

from __future__ import annotations

from scripts import check_config as cc


def test_missing_config_fails_clearly(tmp_path, capsys):
    rc = cc.main(["--config", str(tmp_path / "config.yaml")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "production config check failed" in err
    assert "not found" in err


def test_unedited_example_config_fails(capsys):
    rc = cc.main(["--config", "config.example.yaml", "--example-config", "config.example.yaml"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "production config check failed" in err
    assert "example/demo" in err


def test_real_config_passes(tmp_path, capsys):
    example = tmp_path / "config.example.yaml"
    example.write_text("candidate: {}\n")
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
    rc = cc.main(["--config", str(real), "--example-config", str(example)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "config check: OK" in out
    assert "1 watchlist companies" in out
    assert "1 sources configured" in out


def test_invalid_yaml_fails_without_traceback(tmp_path, capsys):
    example = tmp_path / "config.example.yaml"
    example.write_text("candidate: {}\n")
    real = tmp_path / "config.yaml"
    real.write_text("not: [valid, yaml: because unbalanced brackets\n")
    rc = cc.main(["--config", str(real), "--example-config", str(example)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "config.yaml failed to load" in err
