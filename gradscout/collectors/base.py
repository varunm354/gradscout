"""Collector base class, HTTP retry helper, and the fail-soft run wrapper.

Split of concerns for testability:
  * ``Collector.url()``   - the endpoint to fetch (source-specific).
  * ``Collector.fetch()`` - network I/O (bounded timeout + retries); may raise.
  * ``Collector.parse()`` - PURE transform of an already-loaded payload into
                            RawJob rows; offline-testable with saved fixtures.
  * ``run_collector()``   - wraps fetch+parse so one source failing never stops
                            others, and maps outcomes to ok / partial / error.

No generalized scraping framework — just small, explicit classes.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from gradscout.models import RawJob, SourceStatus, SourceType

logger = logging.getLogger("gradscout.collectors")

# Explicit per-phase timeouts (not one vague value) so a stalled SSL body read
# cannot hang the run. Kept short for an hourly utility.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
# httpx's read timeout only bounds the gap BETWEEN chunks; a slow-but-steady
# stream can still take minutes. DEFAULT_MAX_TOTAL is a hard wall-clock ceiling
# on the whole request+body read, enforced while streaming, so no single source
# can hang the run.
DEFAULT_MAX_TOTAL = 45.0
# Bounded retries: at most DEFAULT_RETRIES extra attempts.
DEFAULT_RETRIES = 1
USER_AGENT = "GradScout/0.1 (+https://github.com/) job-monitor"


class TotalReadTimeout(httpx.ReadTimeout):
    """Raised when the whole-request wall-clock budget is exceeded while
    streaming the body. Not retried (the source is simply too slow)."""


# --------------------------------------------------------------------------- #
# Timestamp parsing helpers (never fabricate; return None when unreliable)
# --------------------------------------------------------------------------- #
def parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_epoch(value: Any, *, unit: str) -> datetime | None:
    """unit: 's' (seconds) or 'ms' (milliseconds)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    if unit == "ms":
        num /= 1000.0
    try:
        return datetime.fromtimestamp(num, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# HTTP fetch with bounded retries
# --------------------------------------------------------------------------- #
def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, TotalReadTimeout):
        return False  # source is just slow; retrying only wastes the budget again
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


def _stream_once(
    client: httpx.Client,
    url: str,
    timeout: httpx.Timeout | float,
    max_total: float,
) -> bytes:
    """Fetch a URL, streaming the body under a hard wall-clock deadline."""
    deadline = time.monotonic() + max_total
    with client.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf.extend(chunk)
            if time.monotonic() > deadline:
                raise TotalReadTimeout(
                    f"total read budget {max_total:.0f}s exceeded",
                    request=resp.request,
                )
        return bytes(buf)


def get_bytes(
    client: httpx.Client,
    url: str,
    *,
    retries: int = DEFAULT_RETRIES,
    backoff: float = 0.5,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    max_total: float = DEFAULT_MAX_TOTAL,
) -> bytes:
    """GET with explicit per-phase timeouts, a hard total-read budget, and a
    bounded number of retries.

    Retries only transient failures (connect/transport errors, 429/5xx). 4xx
    (e.g. 404) and total-budget timeouts fail fast. Total attempts = retries + 1,
    so per-source time is bounded regardless of server behavior.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return _stream_once(client, url, timeout, max_total)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < retries and _is_retryable(exc):
                time.sleep(backoff * (2**attempt))
                continue
            raise
    assert last_exc is not None
    raise last_exc


# --------------------------------------------------------------------------- #
# Collector base + result
# --------------------------------------------------------------------------- #
@dataclass
class Collector:
    source_type: SourceType
    company: str            # employer display name (repo name for aggregators)
    slug: str               # source identity slug
    company_priority: int = 3

    @property
    def source_id(self) -> str:
        return f"{self.source_type.value}:{self.slug}"

    def url(self) -> str:
        raise NotImplementedError

    def load(self, raw: bytes) -> Any:
        return json.loads(raw)

    def fetch(self, client: httpx.Client) -> Any:
        raw = get_bytes(client, self.url())
        return self.load(raw)

    def parse(self, payload: Any) -> tuple[list[RawJob], int]:
        """Return (rows, parse_errors). Raise for a structurally invalid payload
        (total failure); count per-row failures as parse_errors (partial)."""
        raise NotImplementedError


@dataclass
class CollectorResult:
    source_id: str
    source_type: SourceType
    company: str
    company_priority: int
    status: SourceStatus
    raw_jobs: list[RawJob] = field(default_factory=list)
    parse_errors: int = 0
    error: str | None = None
    elapsed_ms: float = 0.0

    @property
    def jobs_seen(self) -> int:
        return len(self.raw_jobs)


def run_collector(collector: Collector, client: httpx.Client) -> CollectorResult:
    """Fail-soft execution of one collector. Never raises.

    A timeout while connecting or reading/streaming the body surfaces as an
    httpx.TransportError, which is caught here and returned as a normal error
    result so the caller simply moves on to the next source.
    """
    base = dict(
        source_id=collector.source_id,
        source_type=collector.source_type,
        company=collector.company,
        company_priority=collector.company_priority,
    )
    logger.info(
        "fetching source",
        extra={"fields": {"source_id": collector.source_id, "url": collector.url()}},
    )
    start = time.monotonic()

    def _elapsed_ms() -> float:
        return round((time.monotonic() - start) * 1000, 1)

    try:
        payload = collector.fetch(client)
    except Exception as exc:  # total failure: fetch/transport/HTTP/timeout
        elapsed = _elapsed_ms()
        logger.warning(
            "collector fetch failed",
            extra={"fields": {"source_id": collector.source_id, "error": repr(exc),
                              "elapsed_ms": elapsed}},
        )
        return CollectorResult(
            **base, status=SourceStatus.error, error=repr(exc), elapsed_ms=elapsed
        )

    try:
        rows, parse_errors = collector.parse(payload)
    except Exception as exc:  # structural/malformed payload = total failure
        elapsed = _elapsed_ms()
        logger.warning(
            "collector parse failed",
            extra={"fields": {"source_id": collector.source_id, "error": repr(exc),
                              "elapsed_ms": elapsed}},
        )
        return CollectorResult(
            **base, status=SourceStatus.error, error=repr(exc), elapsed_ms=elapsed
        )

    elapsed = _elapsed_ms()
    status = SourceStatus.ok if parse_errors == 0 else SourceStatus.partial
    error = None if parse_errors == 0 else f"{parse_errors} row(s) failed to parse"
    logger.info(
        "source result",
        extra={"fields": {"source_id": collector.source_id, "status": status.value,
                          "jobs_seen": len(rows), "parse_errors": parse_errors,
                          "elapsed_ms": elapsed}},
    )
    return CollectorResult(
        **base, status=status, raw_jobs=rows, parse_errors=parse_errors,
        error=error, elapsed_ms=elapsed,
    )
