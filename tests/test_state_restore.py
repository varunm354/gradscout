"""Offline tests for scripts.state_restore (Phase 5; gzip-compressed Phase 5.1).

Every test injects a FakeGit responder -- no real `git` remote/network call
and no real GitHub Actions run happens here. Compression itself uses the
real (fast, deterministic) gzip module -- no need to fake it.
"""

from __future__ import annotations

import gzip
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


def _show_dispatch(*, gz=None, legacy=None):
    """Build a "show" responder that returns different content depending on
    which path (data/gradscout.db.gz vs. data/gradscout.db) was requested."""

    def _responder(args):
        path = args[1].split(":", 1)[1]
        if path == sr.GZ_DB_PATH_IN_BRANCH:
            return gz if gz is not None else FakeProc(128, stderr="fatal: path does not exist")
        if path == sr.LEGACY_DB_PATH_IN_BRANCH:
            return legacy if legacy is not None else FakeProc(128, stderr="fatal: path does not exist")
        return FakeProc(128, stderr=f"fatal: unexpected path {path}")

    return _responder


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
    assert result == sr.RestoreResult(branch_exists=False, restored=False, sha=None, source=None)
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
    """Neither the compressed nor the legacy raw path exists at this tip
    (e.g. a bootstrap/README-only commit)."""
    git = FakeGit(
        {
            "ls-remote": FakeProc(0, stdout="abc123\trefs/heads/state\n"),
            "fetch": FakeProc(0),
            "rev-parse": FakeProc(0, stdout="deadbeef\n"),
            "show": _show_dispatch(),
        }
    )
    db_path = tmp_path / "gradscout.db"
    result = sr.restore_db(db_path, "origin", "state", git=git)
    assert result == sr.RestoreResult(
        branch_exists=True, restored=False, sha="deadbeef", source=None
    )
    assert not db_path.exists()


def test_restore_db_success_decompresses_gz_content_exactly(tmp_path):
    """The primary (Phase 5.1) path: the branch has data/gradscout.db.gz;
    restore decompresses it to the exact original bytes."""
    original_payload = b"\x00sqlite-binary-payload\xff\x01" * 10
    gz_payload = gzip.compress(original_payload, mtime=0)
    git = FakeGit(
        {
            "ls-remote": FakeProc(0, stdout="abc123\trefs/heads/state\n"),
            "fetch": FakeProc(0),
            "rev-parse": FakeProc(0, stdout="deadbeef\n"),
            "show": _show_dispatch(gz=FakeProc(0, stdout=gz_payload)),
        }
    )
    db_path = tmp_path / "nested" / "gradscout.db"
    result = sr.restore_db(db_path, "origin", "state", git=git)
    assert result == sr.RestoreResult(
        branch_exists=True, restored=True, sha="deadbeef", source="compressed"
    )
    assert db_path.read_bytes() == original_payload

    show_calls = [c for c in git.calls if c["args"][0] == "show"]
    assert all(c["binary_output"] is True for c in show_calls)
    # The compressed path is tried first and succeeds -- the legacy path is
    # never even requested.
    assert show_calls[0]["args"] == ["show", f"deadbeef:{sr.GZ_DB_PATH_IN_BRANCH}"]
    assert len(show_calls) == 1


def test_restore_db_falls_back_to_legacy_raw_path_when_gz_missing(tmp_path):
    """Backward compatibility: state saved before Phase 5.1 has no .gz blob
    at this tip, only the legacy raw data/gradscout.db -- restore must still
    succeed, reading it verbatim (no decompression)."""
    legacy_payload = b"\x00legacy-raw-sqlite-payload\xff"
    git = FakeGit(
        {
            "ls-remote": FakeProc(0, stdout="abc123\trefs/heads/state\n"),
            "fetch": FakeProc(0),
            "rev-parse": FakeProc(0, stdout="deadbeef\n"),
            "show": _show_dispatch(legacy=FakeProc(0, stdout=legacy_payload)),
        }
    )
    db_path = tmp_path / "gradscout.db"
    result = sr.restore_db(db_path, "origin", "state", git=git)
    assert result == sr.RestoreResult(
        branch_exists=True, restored=True, sha="deadbeef", source="legacy"
    )
    assert db_path.read_bytes() == legacy_payload

    show_calls = [c for c in git.calls if c["args"][0] == "show"]
    assert len(show_calls) == 2  # gz tried first (and failed), then legacy
    assert show_calls[0]["args"] == ["show", f"deadbeef:{sr.GZ_DB_PATH_IN_BRANCH}"]
    assert show_calls[1]["args"] == ["show", f"deadbeef:{sr.LEGACY_DB_PATH_IN_BRANCH}"]


def test_restore_db_uses_branch_fetch_not_arbitrary_sha(tmp_path):
    """Fetches by branch ref (always supported), then resolves the exact
    fetched tip via FETCH_HEAD -- never fetches an arbitrary bare SHA."""
    git = FakeGit(
        {
            "ls-remote": FakeProc(0, stdout="abc123\trefs/heads/state\n"),
            "fetch": FakeProc(0),
            "rev-parse": FakeProc(0, stdout="deadbeef\n"),
            "show": _show_dispatch(gz=FakeProc(0, stdout=gzip.compress(b"x", mtime=0))),
        }
    )
    sr.restore_db(tmp_path / "gradscout.db", "origin", "state", git=git)
    fetch_call = next(c for c in git.calls if c["args"][0] == "fetch")
    assert fetch_call["args"] == ["fetch", "--depth=1", "origin", "state"]


def test_main_prints_status_and_writes_github_output(tmp_path, monkeypatch, capsys):
    fake_result = sr.RestoreResult(
        branch_exists=True, restored=True, sha="deadbeef", source="compressed"
    )
    monkeypatch.setattr(sr, "restore_db", lambda *a, **k: fake_result)
    out_file = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    rc = sr.main(["--db", str(tmp_path / "db.sqlite"), "--branch", "state"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "exists: True" in captured.out
    assert "previous state restored: True" in captured.out
    assert "state restore source: compressed" in captured.out
    content = out_file.read_text()
    assert "branch_exists=true" in content
    assert "restored=true" in content
    assert "sha=deadbeef" in content
    assert "source=compressed" in content


def test_main_first_run_reports_no_state_and_empty_sha_output(tmp_path, monkeypatch):
    fake_result = sr.RestoreResult(branch_exists=False, restored=False, sha=None, source=None)
    monkeypatch.setattr(sr, "restore_db", lambda *a, **k: fake_result)
    out_file = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    rc = sr.main(["--db", str(tmp_path / "db.sqlite")])

    assert rc == 0
    content = out_file.read_text()
    assert "branch_exists=false" in content
    assert "restored=false" in content
    assert "sha=\n" in content
    assert "source=\n" in content


def test_main_handles_restore_failure_without_crashing(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(sr, "restore_db", boom)
    rc = sr.main(["--db", "x.db"])
    assert rc == 1
    assert "state restore failed" in capsys.readouterr().err
