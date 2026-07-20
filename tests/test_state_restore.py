"""Offline tests for scripts.state_restore (Phase 5).

Every test injects a FakeGit responder -- no real `git` remote/network call
and no real GitHub Actions run happens here.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from scripts import state_restore as sr


@dataclass
class FakeProc:
    returncode: int = 0
    stdout: object = ""
    stderr: str = ""


class FakeGit:
    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[dict] = []

    def __call__(self, args, *, binary_output: bool = False):
        self.calls.append({"args": list(args), "binary_output": binary_output})
        key = args[0]
        resp = self.responses.get(key)
        if resp is None:
            return FakeProc(returncode=1, stderr=f"no canned response for '{key}'")
        if callable(resp):
            return resp(args)
        return resp


def test_remote_branch_exists_true():
    git = FakeGit({"ls-remote": FakeProc(0, stdout="abc123\trefs/heads/state\n")})
    assert sr.remote_branch_exists("origin", "state", git=git) is True


def test_remote_branch_exists_false_when_no_output():
    git = FakeGit({"ls-remote": FakeProc(0, stdout="")})
    assert sr.remote_branch_exists("origin", "state", git=git) is False


def test_remote_branch_exists_false_on_nonzero_exit():
    git = FakeGit({"ls-remote": FakeProc(2, stderr="fatal: no such remote")})
    assert sr.remote_branch_exists("origin", "state", git=git) is False


def test_restore_db_branch_does_not_exist(tmp_path):
    git = FakeGit({"ls-remote": FakeProc(0, stdout="")})
    result = sr.restore_db(tmp_path / "gradscout.db", "origin", "state", git=git)
    assert result == sr.RestoreResult(branch_exists=False, restored=False, sha=None)
    assert not (tmp_path / "gradscout.db").exists()


def test_restore_db_fetch_failure_raises(tmp_path):
    git = FakeGit(
        {
            "ls-remote": FakeProc(0, stdout="abc123\trefs/heads/state\n"),
            "fetch": FakeProc(1, stderr="unable to access remote"),
        }
    )
    with pytest.raises(RuntimeError, match="fetching it failed"):
        sr.restore_db(tmp_path / "gradscout.db", "origin", "state", git=git)


def test_restore_db_rev_parse_failure_raises(tmp_path):
    git = FakeGit(
        {
            "ls-remote": FakeProc(0, stdout="abc123\trefs/heads/state\n"),
            "fetch": FakeProc(0),
            "rev-parse": FakeProc(1, stderr="ambiguous FETCH_HEAD"),
        }
    )
    with pytest.raises(RuntimeError, match="could not resolve"):
        sr.restore_db(tmp_path / "gradscout.db", "origin", "state", git=git)


def test_restore_db_branch_exists_but_no_db_yet(tmp_path):
    git = FakeGit(
        {
            "ls-remote": FakeProc(0, stdout="abc123\trefs/heads/state\n"),
            "fetch": FakeProc(0),
            "rev-parse": FakeProc(0, stdout="deadbeef\n"),
            "show": FakeProc(128, stderr="fatal: path 'data/gradscout.db' does not exist"),
        }
    )
    db_path = tmp_path / "gradscout.db"
    result = sr.restore_db(db_path, "origin", "state", git=git)
    assert result == sr.RestoreResult(branch_exists=True, restored=False, sha="deadbeef")
    assert not db_path.exists()


def test_restore_db_success_writes_binary_content_exactly(tmp_path):
    payload = b"\x00sqlite-binary-payload\xff\x01"
    git = FakeGit(
        {
            "ls-remote": FakeProc(0, stdout="abc123\trefs/heads/state\n"),
            "fetch": FakeProc(0),
            "rev-parse": FakeProc(0, stdout="deadbeef\n"),
            "show": FakeProc(0, stdout=payload),
        }
    )
    db_path = tmp_path / "nested" / "gradscout.db"
    result = sr.restore_db(db_path, "origin", "state", git=git)
    assert result == sr.RestoreResult(branch_exists=True, restored=True, sha="deadbeef")
    assert db_path.read_bytes() == payload

    show_call = next(c for c in git.calls if c["args"][0] == "show")
    assert show_call["binary_output"] is True


def test_restore_db_uses_branch_fetch_not_arbitrary_sha(tmp_path):
    """Fetches by branch ref (always supported), then resolves the exact
    fetched tip via FETCH_HEAD -- never fetches an arbitrary bare SHA."""
    git = FakeGit(
        {
            "ls-remote": FakeProc(0, stdout="abc123\trefs/heads/state\n"),
            "fetch": FakeProc(0),
            "rev-parse": FakeProc(0, stdout="deadbeef\n"),
            "show": FakeProc(0, stdout=b"x"),
        }
    )
    sr.restore_db(tmp_path / "gradscout.db", "origin", "state", git=git)
    fetch_call = next(c for c in git.calls if c["args"][0] == "fetch")
    assert fetch_call["args"] == ["fetch", "--depth=1", "origin", "state"]


def test_main_prints_status_and_writes_github_output(tmp_path, monkeypatch, capsys):
    fake_result = sr.RestoreResult(branch_exists=True, restored=True, sha="deadbeef")
    monkeypatch.setattr(sr, "restore_db", lambda *a, **k: fake_result)
    out_file = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    rc = sr.main(["--db", str(tmp_path / "db.sqlite"), "--branch", "state"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "exists: True" in captured.out
    assert "previous state restored: True" in captured.out
    content = out_file.read_text()
    assert "branch_exists=true" in content
    assert "restored=true" in content
    assert "sha=deadbeef" in content


def test_main_first_run_reports_no_state_and_empty_sha_output(tmp_path, monkeypatch):
    fake_result = sr.RestoreResult(branch_exists=False, restored=False, sha=None)
    monkeypatch.setattr(sr, "restore_db", lambda *a, **k: fake_result)
    out_file = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    rc = sr.main(["--db", str(tmp_path / "db.sqlite")])

    assert rc == 0
    content = out_file.read_text()
    assert "branch_exists=false" in content
    assert "restored=false" in content
    assert "sha=\n" in content


def test_main_handles_restore_failure_without_crashing(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(sr, "restore_db", boom)
    rc = sr.main(["--db", "x.db"])
    assert rc == 1
    assert "state restore failed" in capsys.readouterr().err
