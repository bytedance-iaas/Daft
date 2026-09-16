"""HTTP retries must be classified, bounded and observable."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest
import requests

from curation.adapters import vlm_client as vc


def response(status=200, retry_after=None):
    result = requests.Response()
    result.status_code = status
    result.url = "https://example.test/v1/chat/completions"
    result._content = b'{"choices":[{"message":{"content":"yes"},"finish_reason":"stop"}]}'
    if retry_after is not None:
        result.headers["Retry-After"] = retry_after
    return result


@pytest.fixture
def waits(monkeypatch):
    delays = []
    monkeypatch.setattr(vc._time, "sleep", delays.append)
    vc.latency_reset()
    yield delays
    vc.latency_reset()


@pytest.mark.parametrize(
    "failure",
    [
        408,
        429,
        500,
        502,
        503,
        504,
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.Timeout("timeout"),
        requests.exceptions.ChunkedEncodingError("broken stream"),
    ],
)
def test_transient_retry(failure, waits, caplog):
    calls = []
    gate = threading.BoundedSemaphore(1)

    def send(hard):
        calls.append(hard)
        assert waits == [1, 2, 4][: len(calls) - 1]
        if len(calls) == 4:
            return response()
        if isinstance(failure, Exception):
            raise failure
        return response(failure)

    assert vc.hedged_request(send, tag="llm", timeout_s=10, gate=gate).ok
    assert len(calls) == 4 and waits == [1, 2, 4]
    assert gate.acquire(blocking=False)
    rows = vc.latency_rows()
    assert len(rows) == 4 and len({r[4] for r in rows}) == 1
    assert [r[5] for r in rows] == [0, 1, 2, 3]
    assert "retry 3/3 in 4.0s" in caplog.text


@pytest.mark.parametrize(
    "failure",
    [
        400,
        401,
        403,
        404,
        422,
        501,
        505,
        requests.exceptions.InvalidURL("invalid"),
        requests.exceptions.SSLError("certificate"),
        ValueError("bug"),
    ],
)
def test_permanent_failure(failure, waits, caplog):
    calls = []

    def send(hard):
        calls.append(hard)
        if isinstance(failure, Exception):
            raise failure
        return response(failure)

    if isinstance(failure, Exception):
        with pytest.raises(type(failure)) as err:
            vc.hedged_request(send, tag="llm", timeout_s=10)
        assert err.value is failure
    else:
        with pytest.raises(requests.HTTPError):
            vc.hedged_request(send, tag="llm", timeout_s=10).raise_for_status()
    assert len(calls) == 1 and waits == []
    assert "non-retryable" in caplog.text


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("header,expected", [("5", 5), ("0", 1), ("invalid", 1), ("-2", 1)])
def test_retry_after(status, header, expected, waits):
    seq = [response(status, header), response()]
    assert vc.hedged_request(lambda hard: seq.pop(0), tag="llm", timeout_s=10).ok
    assert waits == [expected]


def test_retry_after_date(monkeypatch, waits):
    now = datetime(2026, 9, 16, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(vc._time, "time", lambda: now)
    header = format_datetime(datetime.fromtimestamp(now + 10, timezone.utc), usegmt=True)
    seq = [response(429, header), response()]
    assert vc.hedged_request(lambda hard: seq.pop(0), tag="llm", timeout_s=10).ok
    assert waits == [10]


def test_long_retry_after_not_retried_early(waits, caplog):
    calls = []

    def send(hard):
        calls.append(hard)
        return response(429, "3600")

    assert vc.hedged_request(send, tag="llm", timeout_s=10).status_code == 429
    assert len(calls) == 1 and waits == []
    assert "not retrying early" in caplog.text


@pytest.mark.parametrize("failure", [503, requests.exceptions.Timeout("last timeout")])
def test_exhaustion(failure, waits, caplog):
    calls = []

    def send(hard):
        calls.append(hard)
        if isinstance(failure, Exception):
            raise failure
        return response(failure)

    if isinstance(failure, Exception):
        with pytest.raises(type(failure)) as err:
            vc.hedged_request(send, tag="llm", timeout_s=10)
        assert err.value is failure
    else:
        assert vc.hedged_request(send, tag="llm", timeout_s=10).status_code == failure
    assert len(calls) == 4 and waits == [1, 2, 4]
    assert "retry limit exhausted" in caplog.text


def test_new_permanent_error_stops_retries(waits):
    seq = [response(503), response(401)]
    assert vc.hedged_request(lambda hard: seq.pop(0), tag="llm", timeout_s=10).status_code == 401
    assert waits == [1]


@pytest.mark.parametrize("status,count", [(401, 1), (503, 4)])
def test_llm_factory_http_error(monkeypatch, waits, status, count):
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        return response(status)

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(requests.HTTPError) as err:
        vc.make_llm_ask("https://example.test/v1", "m")("hello")
    assert err.value.response.status_code == status and len(calls) == count


def test_backoff_releases_gate(monkeypatch, waits):
    gate = threading.BoundedSemaphore(1)
    seq = [response(503), response()]

    def sleep(delay):
        assert gate.acquire(blocking=False)
        gate.release()
        waits.append(delay)

    monkeypatch.setattr(vc._time, "sleep", sleep)
    assert vc.hedged_request(lambda hard: seq.pop(0), tag="llm", timeout_s=10, gate=gate).ok
    assert waits == [1]


def test_hedge_obeys_all_retry_after_headers(waits):
    # The waits fixture replaces sleep; use an Event for actual thread timing.
    calls = []
    lock = threading.Lock()

    def send(hard):
        with lock:
            number = len(calls)
            calls.append(hard)
        if number == 0:
            threading.Event().wait(0.15)
            return response(503)
        if number == 1:
            return response(429, "5")
        return response()

    assert vc.hedged_request(send, tag="llm", timeout_s=0.1).ok
    assert len(calls) == 3 and waits == [5]


def test_hedge_permanent_error_is_not_hidden(waits):
    calls = []
    lock = threading.Lock()

    def send(hard):
        with lock:
            number = len(calls)
            calls.append(hard)
        if number == 0:
            threading.Event().wait(0.15)
            return response(503)
        return response(401)

    assert vc.hedged_request(send, tag="llm", timeout_s=0.1).status_code == 401
    assert len(calls) == 2 and waits == []
