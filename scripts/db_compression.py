"""Deterministic gzip compression for the durable SQLite state file (Phase 5.1).

GitHub hard-rejects any single file over 100 MiB pushed to a branch. The first
real production database (~135.7 MB) exceeded that, so the `state` branch push
was silently rejected and every subsequent run started from empty state
(re-alerting everything). Phase 5.1 commits a gzip-compressed
`data/gradscout.db.gz` to the `state` branch instead of the raw file (see
``scripts/state_save.py`` / ``scripts/state_restore.py``).

Compression is made as deterministic as gzip allows: a fixed ``mtime=0`` and a
fixed compression level mean re-compressing byte-identical input always
produces a byte-identical ``.gz`` file. This matters because
``scripts/state_save.py``'s no-op detection compares git blob SHAs of the
compressed artifact -- if compression weren't deterministic, an unchanged DB
would still produce a "changed" commit on every run.
"""

from __future__ import annotations

import gzip
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("gradscout.state.compression")

# GitHub's documented hard per-file limit is 100 MiB. Pushes containing a
# larger blob are rejected outright.
GITHUB_MAX_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_COMPRESSLEVEL = 9


class StateTooLargeError(RuntimeError):
    """Raised when the compressed state file would still exceed GitHub's
    100 MiB per-file limit. This must fail loudly, before ever attempting
    the push (which GitHub would reject anyway), so a run never silently
    proceeds as if state had been saved when it was not."""


def checkpoint_and_close(db_path: Path) -> None:
    """Best-effort safety net: open the SQLite file, force any WAL/journal
    content to be checkpointed into the main file, commit, and close -- so
    compression always operates on a fully consistent, non-open database
    file, even if the process that last wrote it did not exit cleanly.

    Never raises: this is a defensive extra step (the pipeline's own
    ``conn.close()`` in a ``finally`` block already guarantees a clean file
    in the normal case). A missing file, an already-closed/plain file, or
    any sqlite error here is logged and ignored -- the caller reads and
    compresses whatever bytes are actually on disk regardless.
    """
    if not db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning(
            "checkpoint before compression failed (continuing anyway)",
            extra={"fields": {"db_path": str(db_path), "error": repr(exc)}},
        )


def compress_db(
    db_path: Path, gz_path: Path, *, compresslevel: int = DEFAULT_COMPRESSLEVEL
) -> int:
    """Checkpoint+close ``db_path``, then gzip-compress it deterministically
    to ``gz_path`` (fixed ``mtime=0`` -> byte-identical output for
    byte-identical input; no filename embedded). Returns the compressed size
    in bytes."""
    checkpoint_and_close(db_path)
    raw = db_path.read_bytes()
    compressed = gzip.compress(raw, compresslevel=compresslevel, mtime=0)
    gz_path.parent.mkdir(parents=True, exist_ok=True)
    gz_path.write_bytes(compressed)
    return len(compressed)


def decompress_db(gz_bytes: bytes, db_path: Path) -> int:
    """Decompress ``gz_bytes`` (as read from the state branch) to
    ``db_path``. Returns the decompressed size in bytes."""
    raw = gzip.decompress(gz_bytes)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(raw)
    return len(raw)


def ensure_within_github_limit(size_bytes: int, path: Path) -> None:
    """Raise a clear, actionable error if ``size_bytes`` (the compressed
    state file's size) exceeds GitHub's 100 MiB per-file limit."""
    if size_bytes > GITHUB_MAX_FILE_BYTES:
        raise StateTooLargeError(
            f"{path} is {size_bytes:,} bytes after gzip compression, which exceeds "
            f"GitHub's {GITHUB_MAX_FILE_BYTES:,}-byte (100 MiB) per-file limit -- the "
            "push to the 'state' branch would be rejected. The database has grown too "
            "large to persist as a single compressed file; see docs/PHASE_5_HANDOFF.md "
            "for mitigation options (e.g. pruning old ineligible/sent rows) before this "
            "can be saved."
        )
