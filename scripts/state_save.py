"""Persist data/gradscout.db to the dedicated orphan `state` branch (Phase 5;
gzip-compressed as of Phase 5.1 -- see docs/PHASE_5_HANDOFF.md).

Never checks out the `state` branch into the working tree. Builds a new
commit purely from git plumbing (hash-object / mktree / commit-tree) whose
tree contains ONLY `data/gradscout.db.gz` and a tiny machine-managed README --
by construction, the branch can never end up holding anything else, whether
this is the first commit ever made on it or the ten-thousandth.

Phase 5.1: the first real production DB (~135.7 MB) exceeded GitHub's 100 MB
per-file push limit, so the raw `data/gradscout.db` is deterministically
gzip-compressed (``scripts/db_compression.py``) to `data/gradscout.db.gz`
before hashing; that compressed path is the ONLY DB path ever written to the
tree going forward (a legacy raw `data/gradscout.db` from an older commit is
therefore naturally dropped on the next save -- see
``scripts/state_restore.py`` for the read-side backward-compatibility
fallback). If the compressed file would still exceed the limit, this raises a
clear ``StateTooLargeError`` before ever attempting the push.

Race-safety: if the branch already has history (``--prior-sha`` is set, from
``scripts/state_restore.py``'s output), the push uses `--force-with-lease`
keyed to that exact commit. If another run has updated the branch since this
run restored it, the push is rejected atomically by the server instead of
silently overwriting newer state -- this run then fails loudly rather than
destroying data. If the branch does not exist yet (``--prior-sha`` empty), a
plain non-force ref-creation push is used, which is likewise rejected if
another run created the branch first (the new commit has no parent, so it
can never be a fast-forward of a tip that appeared in the meantime).

Git identity (author/committer) is passed as environment variables scoped to
the single `git commit-tree` subprocess call in this script -- never via
`git config --global`, and this script only ever runs inside the CI job.

Every git invocation goes through an injectable `git` callable so tests can
run fully offline with a fake in-memory git responder -- no real git remote
or network call happens in pytest.

Usage:
    python -m scripts.state_save --db data/gradscout.db --branch state \
        --prior-sha "$RESTORE_SHA"
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from scripts.db_compression import compress_db, ensure_within_github_limit

GitRunner = Callable[..., subprocess.CompletedProcess]

DB_PATH_IN_BRANCH = "data/gradscout.db.gz"
README_PATH_IN_BRANCH = "STATE_BRANCH_README.md"
README_TEXT = (
    "# GradScout state branch\n\n"
    "This branch is machine-managed by `.github/workflows/gradscout-monitor.yml` "
    "(via `scripts/state_save.py`). It exists ONLY to durably persist the pipeline "
    "database, gzip-compressed as `data/gradscout.db.gz`, between hourly runs, and "
    "shares no commit history with `main`.\n\n"
    "Do not edit or merge this branch into `main`. Every commit here is produced "
    "by the workflow and contains exactly this README and `data/gradscout.db.gz` -- "
    "nothing else.\n"
)
AUTHOR_NAME = "gradscout-bot"
AUTHOR_EMAIL = "gradscout-bot@users.noreply.github.com"
COMMIT_MESSAGE_PREFIX = "gradscout state"


def default_git(
    args: list[str], *, input: str | None = None, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], input=input, capture_output=True, text=True, env=env)


@dataclass
class SaveResult:
    changed: bool
    pushed: bool
    new_sha: str | None
    compressed_size_bytes: int | None = None


def _blob_sha_for_file(path: Path, git: GitRunner) -> str:
    proc = git(["hash-object", "-w", str(path)])
    if proc.returncode != 0:
        raise RuntimeError(f"hash-object failed for {path}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _blob_sha_for_text(text: str, git: GitRunner) -> str:
    proc = git(["hash-object", "-w", "--stdin"], input=text)
    if proc.returncode != 0:
        raise RuntimeError(f"hash-object --stdin failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _blob_sha_at(commit_sha: str, path_in_tree: str, git: GitRunner) -> str | None:
    proc = git(["rev-parse", f"{commit_sha}:{path_in_tree}"])
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _build_nested_tree(paths_to_blob_sha: dict[str, str], git: GitRunner) -> str:
    """Build (recursively) a tree object whose paths point at the given blob
    SHAs, and return the root tree SHA.

    `git mktree` only accepts immediate children of the tree it builds, so a
    path containing "/" (like "data/gradscout.db") requires first building an
    inner tree for "data" and then referencing it (mode 040000) from the
    outer tree -- this does that generically for any depth.
    """
    top_level_files: dict[str, str] = {}
    top_level_dirs: dict[str, dict[str, str]] = {}
    for path, blob_sha in paths_to_blob_sha.items():
        if "/" in path:
            head, rest = path.split("/", 1)
            top_level_dirs.setdefault(head, {})[rest] = blob_sha
        else:
            top_level_files[path] = blob_sha

    lines = [f"100644 blob {blob_sha}\t{name}" for name, blob_sha in top_level_files.items()]
    for name, sub_entries in top_level_dirs.items():
        sub_tree_sha = _build_nested_tree(sub_entries, git)
        lines.append(f"040000 tree {sub_tree_sha}\t{name}")

    proc = git(["mktree"], input="\n".join(lines) + "\n")
    if proc.returncode != 0:
        raise RuntimeError(f"mktree failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def save_state(
    db_path: Path,
    remote: str,
    branch: str,
    prior_sha: str | None,
    git: GitRunner = default_git,
) -> SaveResult:
    if not db_path.exists():
        raise RuntimeError(f"cannot save state: {db_path} does not exist")

    gz_path = db_path.with_name(db_path.name + ".gz")
    compressed_size = compress_db(db_path, gz_path)
    ensure_within_github_limit(compressed_size, gz_path)

    new_db_sha = _blob_sha_for_file(gz_path, git)
    old_db_sha = _blob_sha_at(prior_sha, DB_PATH_IN_BRANCH, git) if prior_sha else None
    changed = new_db_sha != old_db_sha

    if not changed and prior_sha is not None:
        return SaveResult(
            changed=False, pushed=False, new_sha=prior_sha,
            compressed_size_bytes=compressed_size,
        )

    readme_sha = _blob_sha_for_text(README_TEXT, git)
    tree_sha = _build_nested_tree(
        {README_PATH_IN_BRANCH: readme_sha, DB_PATH_IN_BRANCH: new_db_sha}, git
    )

    commit_args = ["commit-tree", tree_sha, "-m", f"{COMMIT_MESSAGE_PREFIX}: update {db_path.name}"]
    if prior_sha:
        commit_args += ["-p", prior_sha]
    commit_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
    }
    commit = git(commit_args, env=commit_env)
    if commit.returncode != 0:
        raise RuntimeError(f"commit-tree failed: {commit.stderr.strip()}")
    new_sha = commit.stdout.strip()

    refspec = f"{new_sha}:refs/heads/{branch}"
    push_args = ["push", remote, refspec]
    if prior_sha:
        push_args.append(f"--force-with-lease=refs/heads/{branch}:{prior_sha}")
    push = git(push_args)
    if push.returncode != 0:
        raise RuntimeError(
            f"push to '{branch}' was rejected -- another run may have updated state "
            f"concurrently, or the branch already exists unexpectedly. Refusing to force-"
            f"overwrite. git said: {push.stderr.strip()}"
        )

    return SaveResult(
        changed=True, pushed=True, new_sha=new_sha, compressed_size_bytes=compressed_size
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Save data/gradscout.db to the state branch.")
    ap.add_argument("--db", default="data/gradscout.db")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--branch", default="state")
    ap.add_argument(
        "--prior-sha",
        default="",
        help="the state branch tip SHA this run restored from (empty if the branch "
        "did not exist yet), as printed by scripts.state_restore",
    )
    args = ap.parse_args(argv)
    prior_sha = args.prior_sha.strip() or None

    try:
        result = save_state(Path(args.db), args.remote, args.branch, prior_sha)
    except RuntimeError as exc:
        print(f"::error::state save failed: {exc}", file=sys.stderr)
        return 1

    print(f"state changed: {result.changed}")
    print(f"state pushed: {result.pushed}")
    if result.new_sha:
        print(f"state branch tip: {result.new_sha}")
    if result.compressed_size_bytes is not None:
        print(f"state db size (compressed): {result.compressed_size_bytes:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
