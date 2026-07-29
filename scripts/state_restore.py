"""Restore data/gradscout.db from the dedicated orphan `state` branch (Phase 5;
gzip-compressed as of Phase 5.1 -- see docs/PHASE_5_HANDOFF.md).

Never checks out the `state` branch into the working tree and never touches
`main`'s files: it only reads loose git objects directly (`git ls-remote`,
`git fetch <branch> --depth=1`, `git show <sha>:<path>`), so it can never
contaminate the main checkout and needs no worktree.

Phase 5.1: the branch now stores `data/gradscout.db.gz` (gzip-compressed,
see ``scripts/db_compression.py`` / ``scripts/state_save.py``) instead of a
raw `data/gradscout.db`. This script tries the compressed path first and
decompresses it; if that path doesn't exist at the fetched tip, it falls
back to reading the legacy raw `data/gradscout.db` path directly (byte for
byte, no decompression) for backward compatibility with any state saved by
an older (pre-Phase-5.1) version of `scripts/state_save.py`.

Handles "no state branch yet", "state branch exists but has no DB yet" (e.g.
right after the workflow's first bootstrap commit), and "state branch has
neither the compressed nor the legacy raw DB path at this tip" as normal,
non-error first-run conditions -- not exceptions. A git failure for any
other reason (fetch error, transport error) IS raised, deliberately:
proceeding as if there were no prior state when the branch actually has real
history would risk a later save silently discarding real state, which is
worse than simply failing the run loudly.

Every git invocation goes through an injectable `git` callable so tests can
run fully offline with a fake in-memory git responder -- no real git remote
or network call happens in pytest.

Usage:
    python -m scripts.state_restore --db data/gradscout.db --branch state
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from scripts.db_compression import decompress_db

GitRunner = Callable[..., subprocess.CompletedProcess]

GZ_DB_PATH_IN_BRANCH = "data/gradscout.db.gz"
LEGACY_DB_PATH_IN_BRANCH = "data/gradscout.db"


def default_git(args: list[str], *, binary_output: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=not binary_output)


@dataclass
class RestoreResult:
    branch_exists: bool
    restored: bool
    sha: str | None
    source: str | None = None  # "compressed" | "legacy" | None


def remote_branch_exists(remote: str, branch: str, git: GitRunner = default_git) -> bool:
    proc = git(["ls-remote", "--exit-code", "--heads", remote, branch])
    return proc.returncode == 0 and bool(proc.stdout.strip())


def restore_db(
    db_path: Path,
    remote: str,
    branch: str,
    db_path_in_branch: str = GZ_DB_PATH_IN_BRANCH,
    legacy_db_path_in_branch: str = LEGACY_DB_PATH_IN_BRANCH,
    git: GitRunner = default_git,
) -> RestoreResult:
    if not remote_branch_exists(remote, branch, git):
        return RestoreResult(branch_exists=False, restored=False, sha=None)

    fetch = git(["fetch", "--depth=1", remote, branch])
    if fetch.returncode != 0:
        raise RuntimeError(
            f"'{branch}' branch exists but fetching it failed: {fetch.stderr.strip()}"
        )

    rev = git(["rev-parse", "FETCH_HEAD"])
    if rev.returncode != 0:
        raise RuntimeError(f"could not resolve fetched '{branch}' tip: {rev.stderr.strip()}")
    sha = rev.stdout.strip()

    show_gz = git(["show", f"{sha}:{db_path_in_branch}"], binary_output=True)
    if show_gz.returncode == 0:
        decompress_db(show_gz.stdout, db_path)
        return RestoreResult(branch_exists=True, restored=True, sha=sha, source="compressed")

    # Backward compatibility: state saved before Phase 5.1 introduced gzip
    # compression stored the raw DB at this legacy path instead.
    show_legacy = git(["show", f"{sha}:{legacy_db_path_in_branch}"], binary_output=True)
    if show_legacy.returncode == 0:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(show_legacy.stdout)
        return RestoreResult(branch_exists=True, restored=True, sha=sha, source="legacy")

    # Branch exists (e.g. a bootstrap/README-only commit) but has no DB yet.
    return RestoreResult(branch_exists=True, restored=False, sha=sha, source=None)


def _write_github_output(result: RestoreResult) -> None:
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    with open(out_path, "a") as f:
        f.write(f"branch_exists={'true' if result.branch_exists else 'false'}\n")
        f.write(f"restored={'true' if result.restored else 'false'}\n")
        f.write(f"sha={result.sha or ''}\n")
        f.write(f"source={result.source or ''}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Restore data/gradscout.db from the state branch.")
    ap.add_argument("--db", default="data/gradscout.db")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--branch", default="state")
    ap.add_argument("--db-path-in-branch", default=GZ_DB_PATH_IN_BRANCH)
    ap.add_argument("--legacy-db-path-in-branch", default=LEGACY_DB_PATH_IN_BRANCH)
    args = ap.parse_args(argv)

    try:
        result = restore_db(
            Path(args.db),
            args.remote,
            args.branch,
            args.db_path_in_branch,
            args.legacy_db_path_in_branch,
        )
    except RuntimeError as exc:
        print(f"::error::state restore failed: {exc}", file=sys.stderr)
        return 1

    print(f"state branch '{args.branch}' exists: {result.branch_exists}")
    print(f"previous state restored: {result.restored}")
    if result.sha:
        print(f"state branch tip: {result.sha}")
    if result.source:
        print(f"state restore source: {result.source}")
    _write_github_output(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
