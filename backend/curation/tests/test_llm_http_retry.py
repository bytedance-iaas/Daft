"""Text LLM HTTP retries remain bounded and preserve original exceptions."""

from __future__ import annotations

import threading

import pytest
import requests

from curation.adapters.vlm_client import make_llm_ask


@pytest.mark.parametrize(
    "status,retryable",
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (422, False),
        (408, True),
        (429, True),
        (500, True),
        (502, True),
        (503, True),
        (504, True),
    ],
)
def test_http_status_retry(monkeypatch, caplog, status, retryable):
    calls, waits = [], []
    gate = threading.BoundedSemaphore(1)

    def post(*args, **kwargs):
        calls.append(kwargs["timeout"])
        resp = requests.Response()
        resp.status_code = status
        resp.url = "https://example.test/v1"
        return resp

    def sleep(delay):
        assert gate.acquire(blocking=False)
        gate.release()
        waits.append(delay)

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr("curation.adapters.vlm_client._time.sleep", sleep)
    with pytest.raises(requests.HTTPError) as err:
        make_llm_ask("https://example.test/v1", "m", timeout_s=2, gate=gate)("hello")
    assert err.value.response.status_code == status
    assert calls == [2] * (4 if retryable else 1)
    assert waits == ([1, 2, 4] if retryable else [])
    assert "停止重试" in caplog.text
    assert gate.acquire(blocking=False)


@pytest.mark.parametrize(
    "error,retryable",
    [
        (requests.exceptions.ConnectionError("reset"), True),
        (requests.exceptions.Timeout("timeout"), True),
        (requests.exceptions.ChunkedEncodingError("broken"), True),
        (requests.exceptions.SSLError("certificate"), False),
        (requests.exceptions.InvalidURL("bad URL"), False),
        (ValueError("bug"), False),
    ],
)
def test_transport_errors(monkeypatch, error, retryable):
    calls, waits = [], []
    gate = threading.BoundedSemaphore(1)

    def post(*args, **kwargs):
        calls.append(1)
        raise error

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr("curation.adapters.vlm_client._time.sleep", waits.append)
    with pytest.raises(type(error)) as err:
        make_llm_ask("https://example.test/v1", "m", gate=gate)("hello")
    assert err.value is error
    assert len(calls) == (4 if retryable else 1)
    assert waits == ([1, 2, 4] if retryable else [])
    assert gate.acquire(blocking=False)


@pytest.mark.parametrize("final_status", [200, 401])
def test_retry_stops_on_success_or_permanent_error(monkeypatch, final_status):
    statuses = [503, final_status]
    waits = []

    def post(*args, **kwargs):
        resp = requests.Response()
        resp.status_code = statuses.pop(0)
        resp._content = b'{"choices":[{"message":{"content":"yes"},"finish_reason":"stop"}]}'
        return resp

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr("curation.adapters.vlm_client._time.sleep", waits.append)
    ask = make_llm_ask("https://example.test/v1", "m")
    if final_status == 200:
        assert ask("hello") == "yes"
    else:
        with pytest.raises(requests.HTTPError):
            ask("hello")
    assert not statuses and waits == [1]
