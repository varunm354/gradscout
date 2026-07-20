"""Structural validation of .github/workflows/gradscout-monitor.yml (Phase 5).

Parses the workflow with PyYAML and asserts on its structure. Never invokes
`act`/`actionlint`/GitHub, and never makes a real network call -- fully
offline, like the rest of the suite.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW_PATH = Path(".github/workflows/gradscout-monitor.yml")


def _load() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _triggers(data: dict) -> dict:
    # PyYAML's default (YAML 1.1) resolver parses the bare `on:` mapping key
    # as the boolean True, not the string "on".
    return data.get("on", data.get(True))


def test_workflow_file_exists_and_parses_as_a_mapping():
    assert WORKFLOW_PATH.exists()
    data = _load()
    assert isinstance(data, dict)


def test_hourly_cron_uses_a_non_zero_minute():
    data = _load()
    schedule = _triggers(data)["schedule"]
    assert len(schedule) == 1
    minute_field = schedule[0]["cron"].split()[0]
    assert minute_field not in ("0", "*")


def test_workflow_dispatch_has_dry_run_boolean_input_defaulting_false():
    data = _load()
    dispatch = _triggers(data)["workflow_dispatch"]
    dry_run = dispatch["inputs"]["dry_run"]
    assert dry_run["type"] == "boolean"
    assert dry_run["default"] is False


def test_permissions_are_contents_write_only():
    data = _load()
    assert data["permissions"] == {"contents": "write"}


def test_concurrency_never_cancels_in_progress_runs():
    data = _load()
    concurrency = data["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert "group" in concurrency


def test_concurrency_group_distinguishes_dry_run_from_state_writing_runs():
    data = _load()
    group_expr = data["concurrency"]["group"]
    assert "dry_run" in group_expr
    assert "gradscout-monitor-state" in group_expr
    assert "gradscout-monitor-dryrun" in group_expr


def test_job_uses_python_312_with_pip_caching():
    data = _load()
    steps = data["jobs"]["monitor"]["steps"]
    setup_python = next(s for s in steps if s.get("uses", "").startswith("actions/setup-python"))
    assert setup_python["with"]["python-version"] == "3.12"
    assert setup_python["with"]["cache"] == "pip"
    assert "pyproject.toml" in setup_python["with"]["cache-dependency-path"]


def test_no_unconditional_or_true_anywhere():
    text = WORKFLOW_PATH.read_text()
    assert "|| true" not in text


def test_secrets_are_referenced_not_hardcoded():
    text = WORKFLOW_PATH.read_text()
    assert "secrets.DISCORD_WEBHOOK_URL" in text
    assert "secrets.OPENAI_API_KEY" in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("DISCORD_WEBHOOK_URL:") or stripped.startswith("OPENAI_API_KEY:"):
            assert "secrets." in stripped


def test_dry_run_pipeline_step_never_gets_discord_webhook_secret():
    data = _load()
    steps = data["jobs"]["monitor"]["steps"]
    dry_step = next(s for s in steps if s["name"] == "Run pipeline (dry-run)")
    env = dry_step.get("env", {})
    assert "DISCORD_WEBHOOK_URL" not in env


def test_real_pipeline_step_gets_both_secrets_and_is_gated_on_success_and_mode():
    data = _load()
    steps = data["jobs"]["monitor"]["steps"]
    real_step = next(s for s in steps if s["name"] == "Run pipeline (real)")
    env = real_step.get("env", {})
    assert env.get("DISCORD_WEBHOOK_URL") == "${{ secrets.DISCORD_WEBHOOK_URL }}"
    assert env.get("OPENAI_API_KEY") == "${{ secrets.OPENAI_API_KEY }}"
    assert "success()" in real_step["if"]
    assert "dry_run" in real_step["if"]


def test_save_state_step_only_runs_on_success_and_non_dry_run():
    data = _load()
    steps = data["jobs"]["monitor"]["steps"]
    save_step = next(s for s in steps if s["name"].startswith("Save state"))
    assert "success()" in save_step["if"]
    assert "dry_run" in save_step["if"]


def test_config_preflight_runs_before_restore_and_pipeline_steps():
    data = _load()
    steps = data["jobs"]["monitor"]["steps"]
    names = [s["name"] for s in steps]
    assert names.index("Check production config") < names.index(
        "Restore state from '${{ env.STATE_BRANCH }}' branch"
    )
    assert names.index("Restore state from '${{ env.STATE_BRANCH }}' branch") < names.index(
        "Run pipeline (real)"
    )


def test_state_save_step_passes_restore_sha_output():
    data = _load()
    steps = data["jobs"]["monitor"]["steps"]
    save_step = next(s for s in steps if s["name"].startswith("Save state"))
    assert "steps.restore.outputs.sha" in save_step["run"]


def test_restore_step_has_an_id_for_downstream_output_reference():
    data = _load()
    steps = data["jobs"]["monitor"]["steps"]
    restore_step = next(s for s in steps if s["name"].startswith("Restore state"))
    assert restore_step.get("id") == "restore"
