"""Offline tests for scripts.state_save (Phase 5).

Every test injects a FakeGit responder -- no real `git` remote/network call
and no real GitHub Actions run happens here.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from scripts import state_save as ss


@dataclass
class FakeProc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeGit:
    """Dispatches on the git subcommand (args[0]). A response may be a
    FakeProc or a callable(args, input=None, env=None) -> FakeProc for
    subcommands invoked more than once with different intents (e.g.
    hash-object is called both for the DB file and, via --stdin, the
    README)."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[dict] = []

    def __call__(self, args, *, input=None, env=None):
        self.calls.append({"args": list(args), "input": input, "env": env})
        key = args[0]
        resp = self.responses.get(key)
        if resp is None:
            return FakeProc(returncode=1, stderr=f"no canned response for '{key}'")
        if callable(resp):
            return resp(args, input=input, env=env)
        return resp


def _hash_object_split(file_sha: str, stdin_sha: str):
    def _responder(args, *, input=None, env=None):
        return FakeProc(0, stdout=(f"{stdin_sha}\n" if input is not None else f"{file_sha}\n"))

    return _responder


def test_save_state_missing_db_raises(tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        ss.save_state(tmp_path / "gradscout.db", "origin", "state", prior_sha=None, git=FakeGit({}))


def test_save_state_first_time_creates_orphan_commit_no_force(tmp_path):
    db = tmp_path / "gradscout.db"
    db.write_bytes(b"content-v1")
    git = FakeGit(
        {
            "hash-object": _hash_object_split("dbsha1", "readmesha1"),
            "mktree": FakeProc(0, stdout="treesha1\n"),
            "commit-tree": FakeProc(0, stdout="commitsha1\n"),
            "push": FakeProc(0),
        }
    )
    result = ss.save_state(db, "origin", "state", prior_sha=None, git=git)

    assert result == ss.SaveResult(changed=True, pushed=True, new_sha="commitsha1")

    commit_call = next(c for c in git.calls if c["args"][0] == "commit-tree")
    assert "-p" not in commit_call["args"]  # no parent: first-ever commit
    assert commit_call["env"]["GIT_AUTHOR_NAME"] == ss.AUTHOR_NAME
    assert commit_call["env"]["GIT_AUTHOR_EMAIL"] == ss.AUTHOR_EMAIL

    push_call = next(c for c in git.calls if c["args"][0] == "push")
    assert push_call["args"] == ["push", "origin", "commitsha1:refs/heads/state"]
    assert not any(a.startswith("--force-with-lease") for a in push_call["args"])

    # mktree only accepts immediate children, so "data/gradscout.db" is built
    # as a nested tree: one mktree call for the inner "data" tree (leaf blob
    # named "gradscout.db"), one for the outer root tree (a "data" subtree
    # entry plus the top-level README blob).
    mktree_calls = [c for c in git.calls if c["args"][0] == "mktree"]
    assert len(mktree_calls) == 2
    assert any("gradscout.db" in c["input"] and "data/" not in c["input"] for c in mktree_calls)
    assert any(
        "STATE_BRANCH_README.md" in c["input"] and "\tdata\n" in c["input"]
        for c in mktree_calls
    )


def test_save_state_no_change_skips_commit_and_push(tmp_path):
    db = tmp_path / "gradscout.db"
    db.write_bytes(b"content-unchanged")
    git = FakeGit(
        {
            "hash-object": FakeProc(0, stdout="dbsha-same\n"),
            "rev-parse": FakeProc(0, stdout="dbsha-same\n"),
        }
    )
    result = ss.save_state(db, "origin", "state", prior_sha="priorsha", git=git)

    assert result == ss.SaveResult(changed=False, pushed=False, new_sha="priorsha")
    assert not any(c["args"][0] in ("mktree", "commit-tree", "push") for c in git.calls)


def test_save_state_change_pushes_with_force_with_lease(tmp_path):
    db = tmp_path / "gradscout.db"
    db.write_bytes(b"content-v2")
    git = FakeGit(
        {
            "hash-object": _hash_object_split("dbsha-new", "readmesha"),
            "rev-parse": FakeProc(0, stdout="dbsha-old\n"),
            "mktree": FakeProc(0, stdout="treesha2\n"),
            "commit-tree": FakeProc(0, stdout="commitsha2\n"),
            "push": FakeProc(0),
        }
    )
    result = ss.save_state(db, "origin", "state", prior_sha="priorsha", git=git)

    assert result == ss.SaveResult(changed=True, pushed=True, new_sha="commitsha2")

    commit_call = next(c for c in git.calls if c["args"][0] == "commit-tree")
    assert "-p" in commit_call["args"]
    assert commit_call["args"][commit_call["args"].index("-p") + 1] == "priorsha"

    push_call = next(c for c in git.calls if c["args"][0] == "push")
    assert push_call["args"][:3] == ["push", "origin", "commitsha2:refs/heads/state"]
    assert "--force-with-lease=refs/heads/state:priorsha" in push_call["args"]


def test_save_state_missing_prior_db_blob_counts_as_changed(tmp_path):
    """Branch exists (e.g. a bootstrap/README-only commit) but has no DB blob
    yet at prior_sha -- must be treated as new state, not an error."""
    db = tmp_path / "gradscout.db"
    db.write_bytes(b"content")
    git = FakeGit(
        {
            "hash-object": _hash_object_split("dbsha-new", "readmesha"),
            "rev-parse": FakeProc(128, stderr="fatal: path does not exist in tree"),
            "mktree": FakeProc(0, stdout="treesha\n"),
            "commit-tree": FakeProc(0, stdout="commitsha\n"),
            "push": FakeProc(0),
        }
    )
    result = ss.save_state(db, "origin", "state", prior_sha="priorsha", git=git)
    assert result.changed is True
    assert result.pushed is True


def test_save_state_rejected_push_raises_instead_of_overwriting(tmp_path):
    db = tmp_path / "gradscout.db"
    db.write_bytes(b"content-v2")
    git = FakeGit(
        {
            "hash-object": _hash_object_split("dbsha-new", "readmesha"),
            "rev-parse": FakeProc(0, stdout="dbsha-old\n"),
            "mktree": FakeProc(0, stdout="treesha\n"),
            "commit-tree": FakeProc(0, stdout="commitsha\n"),
            "push": FakeProc(1, stderr="stale info (remote ref updated)"),
        }
    )
    with pytest.raises(RuntimeError, match="rejected"):
        ss.save_state(db, "origin", "state", prior_sha="priorsha", git=git)


def test_save_state_mktree_failure_raises(tmp_path):
    db = tmp_path / "gradscout.db"
    db.write_bytes(b"content")
    git = FakeGit(
        {
            "hash-object": _hash_object_split("dbsha-new", "readmesha"),
            "mktree": FakeProc(1, stderr="bad tree format"),
        }
    )
    with pytest.raises(RuntimeError, match="mktree failed"):
        ss.save_state(db, "origin", "state", prior_sha=None, git=git)


def test_main_prints_status(monkeypatch, capsys):
    fake_result = ss.SaveResult(changed=True, pushed=True, new_sha="commitsha")
    monkeypatch.setattr(ss, "save_state", lambda *a, **k: fake_result)

    rc = ss.main(["--db", "x.db", "--branch", "state", "--prior-sha", "priorsha"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "state changed: True" in out
    assert "state pushed: True" in out
    assert "commitsha" in out


def test_main_empty_prior_sha_becomes_none(monkeypatch):
    captured = {}

    def fake_save_state(db_path, remote, branch, prior_sha, git=None):
        captured["prior_sha"] = prior_sha
        return ss.SaveResult(changed=True, pushed=True, new_sha="sha")

    monkeypatch.setattr(ss, "save_state", fake_save_state)
    ss.main(["--db", "x.db", "--prior-sha", ""])
    assert captured["prior_sha"] is None


def test_main_handles_save_failure_without_crashing(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("push rejected")

    monkeypatch.setattr(ss, "save_state", boom)
    rc = ss.main(["--db", "x.db"])
    assert rc == 1
    assert "state save failed" in capsys.readouterr().err
