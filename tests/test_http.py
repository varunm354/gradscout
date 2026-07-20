"""HTTP timeout / bounded-retry tests. All mocked -- no real network access."""

import httpx
import pytest

from gradscout.collectors.base import (
    TotalReadTimeout,
    get_bytes,
    run_collector,
)
from gradscout.collectors.greenhouse import GreenhouseCollector
from gradscout.models import SourceStatus

URL = "https://example.test/jobs"


class FakeResp:
    """Stand-in for a streamed httpx.Response."""

    def __init__(self, status: int = 200, chunks=(b"{}",)):
        self.status_code = status
        self._chunks = chunks
        self.request = httpx.Request("GET", URL)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )

    def iter_bytes(self):
        yield from self._chunks


class _Ctx:
    def __init__(self, outcome):
        self._outcome = outcome

    def __enter__(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    def __exit__(self, *args):
        return False


class FakeClient:
    """Replays queued outcomes (FakeResp or Exception) for .stream() and counts calls."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def stream(self, method, url, **kwargs):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        return _Ctx(outcome)


def test_read_timeout_retries_are_bounded_then_raises():
    client = FakeClient([httpx.ReadTimeout("read timed out")] * 5)
    with pytest.raises(httpx.ReadTimeout):
        get_bytes(client, URL, retries=1, backoff=0)
    assert client.calls == 2  # 1 initial + 1 retry, not unbounded


def test_connect_timeout_is_retried_then_succeeds():
    client = FakeClient([httpx.ConnectTimeout("slow"), FakeResp(200)])
    body = get_bytes(client, URL, retries=1, backoff=0)
    assert body == b"{}"
    assert client.calls == 2


def test_server_error_retried_then_succeeds():
    client = FakeClient([FakeResp(503), FakeResp(200)])
    body = get_bytes(client, URL, retries=2, backoff=0)
    assert body == b"{}"
    assert client.calls == 2


def test_client_error_404_is_not_retried():
    client = FakeClient([FakeResp(404), FakeResp(200)])
    with pytest.raises(httpx.HTTPStatusError):
        get_bytes(client, URL, retries=2, backoff=0)
    assert client.calls == 1  # fail fast, no retry


def test_total_read_budget_caps_slow_stream_and_is_not_retried():
    # A steady multi-chunk stream that individually never trips the read timeout.
    # max_total=0 forces the wall-clock budget to trip after the first chunk.
    client = FakeClient([FakeResp(200, chunks=(b"a", b"b", b"c"))])
    with pytest.raises(TotalReadTimeout):
        get_bytes(client, URL, retries=2, backoff=0, max_total=0.0)
    assert client.calls == 1  # slow source is not retried


def test_run_collector_returns_error_on_timeout_and_continues():
    c = GreenhouseCollector("Acme", "acme")

    def timeout_fetch(client=None):
        raise httpx.ReadTimeout("stalled reading body")

    c.fetch = timeout_fetch
    result = run_collector(c, client=None)
    assert result.status == SourceStatus.error
    assert "ReadTimeout" in result.error
    assert result.jobs_seen == 0
    assert result.elapsed_ms >= 0
