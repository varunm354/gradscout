"""Offline tests for scripts.db_compression (Phase 5.1).

No network, no real git remote. Includes a realistic generated-SQLite
compression test (a real gradscout DB with several thousand synthetic job
rows) that reports raw vs. compressed size, matching the production
incident (a ~135.7 MB raw DB rejected by GitHub's 100 MB file limit).
"""

from __future__ import annotations

import gzip
from datetime import datetime, timezone

import pytest

from gradscout import db
from gradscout.models import Job, SourceType
from gradscout.urls import canonicalize_url
from scripts import db_compression as dc


def _make_job(i: int) -> Job:
    apply_url = f"https://boards.greenhouse.io/company{i % 200}/jobs/{i}"
    return Job(
        source=SourceType.greenhouse,
        source_company=f"company{i % 200}",
        source_job_id=str(i),
        apply_url=apply_url,
        company=f"Company {i % 200}",
        company_priority=(i % 3) + 1,
        title=f"Software Engineer, New Grad #{i}",
        location="Remote" if i % 2 else "New York, NY",
        description_text=(
            "We are looking for a new grad software engineer to join our backend "
            "platform team. You will build distributed systems, APIs, and "
            "infrastructure at scale. Bachelor's degree required. " * 5
        ),
        url_canonical=canonicalize_url(apply_url),
        raw_blob={"sponsorship": "Offers Sponsorship", "degrees": ["Bachelor's"]},
    )


def _build_realistic_db(path, n_rows: int) -> int:
    """Build a real gradscout SQLite DB with n_rows synthetic job postings.
    Returns the raw file size in bytes."""
    conn = db.connect(path)
    db.init_db(conn)
    now = datetime(2027, 1, 1, tzinfo=timezone.utc)
    for i in range(n_rows):
        db.upsert_job(conn, _make_job(i), now=now)
    conn.close()
    return path.stat().st_size


# --------------------------------------------------------------------------- #
# checkpoint_and_close
# --------------------------------------------------------------------------- #
def test_checkpoint_and_close_missing_file_is_a_noop(tmp_path):
    dc.checkpoint_and_close(tmp_path / "does_not_exist.db")  # must not raise


def test_checkpoint_and_close_real_db_succeeds(tmp_path):
    db_path = tmp_path / "gradscout.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.close()
    dc.checkpoint_and_close(db_path)  # must not raise
    assert db_path.exists()


def test_checkpoint_and_close_non_sqlite_content_does_not_raise(tmp_path):
    """A best-effort safety net: garbage content must never crash the save."""
    db_path = tmp_path / "gradscout.db"
    db_path.write_bytes(b"not a real sqlite file at all")
    dc.checkpoint_and_close(db_path)  # must not raise


# --------------------------------------------------------------------------- #
# compress_db / decompress_db round trip
# --------------------------------------------------------------------------- #
def test_compress_then_decompress_round_trips_exactly(tmp_path):
    db_path = tmp_path / "gradscout.db"
    db_path.write_bytes(b"\x00some-binary-sqlite-content\xffvaried-bytes" * 50)
    gz_path = tmp_path / "gradscout.db.gz"

    size = dc.compress_db(db_path, gz_path)
    assert gz_path.exists()
    assert size == gz_path.stat().st_size
    assert size < db_path.stat().st_size  # repetitive content compresses well

    restored_path = tmp_path / "restored.db"
    decompressed_size = dc.decompress_db(gz_path.read_bytes(), restored_path)
    assert restored_path.read_bytes() == db_path.read_bytes()
    assert decompressed_size == db_path.stat().st_size


def test_compress_db_creates_parent_directories(tmp_path):
    db_path = tmp_path / "gradscout.db"
    db_path.write_bytes(b"content")
    gz_path = tmp_path / "nested" / "dir" / "gradscout.db.gz"
    dc.compress_db(db_path, gz_path)
    assert gz_path.exists()


def test_decompress_db_creates_parent_directories(tmp_path):
    payload = gzip.compress(b"hello", mtime=0)
    db_path = tmp_path / "nested" / "gradscout.db"
    dc.decompress_db(payload, db_path)
    assert db_path.read_bytes() == b"hello"


# --------------------------------------------------------------------------- #
# Determinism: identical input -> byte-identical .gz output, every time.
# --------------------------------------------------------------------------- #
def test_compression_is_deterministic_across_repeated_runs(tmp_path):
    db_path = tmp_path / "gradscout.db"
    db_path.write_bytes(b"stable-content" * 1000)

    gz1 = tmp_path / "one.db.gz"
    gz2 = tmp_path / "two.db.gz"
    dc.compress_db(db_path, gz1)
    dc.compress_db(db_path, gz2)

    assert gz1.read_bytes() == gz2.read_bytes()


def test_compression_is_deterministic_across_real_sqlite_rebuilds(tmp_path):
    """Two independently-built (but content-identical) real SQLite DBs must
    compress to byte-identical .gz output -- this is what makes
    state_save.py's blob-SHA no-op detection work on the compressed file."""
    now = datetime(2027, 1, 1, tzinfo=timezone.utc)

    db_path_a = tmp_path / "a.db"
    conn_a = db.connect(db_path_a)
    db.init_db(conn_a)
    db.upsert_job(conn_a, _make_job(1), now=now)
    conn_a.close()

    db_path_b = tmp_path / "b.db"
    conn_b = db.connect(db_path_b)
    db.init_db(conn_b)
    db.upsert_job(conn_b, _make_job(1), now=now)
    conn_b.close()

    gz_a = tmp_path / "a.db.gz"
    gz_b = tmp_path / "b.db.gz"
    dc.compress_db(db_path_a, gz_a)
    dc.compress_db(db_path_b, gz_b)
    assert gz_a.read_bytes() == gz_b.read_bytes()


# --------------------------------------------------------------------------- #
# Realistic generated-SQLite compression: raw vs. compressed size report.
# --------------------------------------------------------------------------- #
def test_realistic_generated_sqlite_compression_reports_size_reduction(tmp_path, capsys):
    db_path = tmp_path / "gradscout.db"
    raw_size = _build_realistic_db(db_path, n_rows=5000)

    gz_path = tmp_path / "gradscout.db.gz"
    compressed_size = dc.compress_db(db_path, gz_path)

    ratio = compressed_size / raw_size
    print(
        f"realistic SQLite compression report: raw={raw_size:,} bytes, "
        f"compressed={compressed_size:,} bytes, ratio={ratio:.3f}"
    )

    # A real SQLite file full of repetitive text (titles/descriptions) should
    # compress substantially -- assert a meaningful (not just nonzero) win.
    assert compressed_size < raw_size
    assert ratio < 0.5
    # Sanity check this is actually a realistic-scale artifact, not a toy.
    assert raw_size > 500_000


def test_realistic_sqlite_compressed_state_is_well_under_github_limit(tmp_path):
    db_path = tmp_path / "gradscout.db"
    _build_realistic_db(db_path, n_rows=5000)
    gz_path = tmp_path / "gradscout.db.gz"
    compressed_size = dc.compress_db(db_path, gz_path)
    dc.ensure_within_github_limit(compressed_size, gz_path)  # must not raise


# --------------------------------------------------------------------------- #
# 100 MiB limit enforcement
# --------------------------------------------------------------------------- #
def test_ensure_within_github_limit_passes_under_limit(tmp_path):
    dc.ensure_within_github_limit(1024, tmp_path / "x.gz")  # must not raise


def test_ensure_within_github_limit_raises_clearly_when_over_limit(tmp_path):
    oversized = dc.GITHUB_MAX_FILE_BYTES + 1
    with pytest.raises(dc.StateTooLargeError, match="exceeds"):
        dc.ensure_within_github_limit(oversized, tmp_path / "gradscout.db.gz")


def test_state_too_large_error_is_a_runtime_error():
    assert issubclass(dc.StateTooLargeError, RuntimeError)


if __name__ == "__main__":
    pytest.main([__file__])
