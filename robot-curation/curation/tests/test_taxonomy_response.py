"""Malformed model output must be retried or diagnosed, never silently accepted."""

from __future__ import annotations

import json

import pytest
import requests

from curation.adapters.vlm_client import make_llm_ask
from curation.dataset_level.taxonomy import TaxonomyResponseError, assign, induce_taxonomy

CAPTION = 'press the "start" button\nthen move C:\\robot'
TAXONOMY = {"families": [{"name": "actuation", "subskills": [{"name": "press", "members": [CAPTION]}]}]}


@pytest.fixture(autouse=True)
def backoff_waits(monkeypatch):
    waits = []
    monkeypatch.setattr("curation.dataset_level.taxonomy.time.sleep", waits.append)
    return waits


@pytest.mark.parametrize(
    "bad",
    [
        '{"families": [{"name": "press the "start" button"}]}',
        '{"families": []',
        "[]",
        '{"families": []}',
        '{"families": [{"name": "actuation", "subskills": [{"name": "press", "members": [1]}]}]}',
    ],
)
@pytest.mark.parametrize("failures", [1, 2, 3])
def test_invalid_response_is_corrected(bad, failures, caplog, backoff_waits):
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        assert backoff_waits == [1, 2, 4][:len(prompts) - 1]
        return bad if len(prompts) <= failures else "```json\n" + json.dumps(TAXONOMY) + "\n```"

    tax = induce_taxonomy([CAPTION], ask, guideline="Group by motion")
    assert tax == TAXONOMY
    assert assign([CAPTION], tax) == (["actuation"], ["press"])
    assert len(prompts) == failures + 1
    assert backoff_waits == [1, 2, 4][:failures]
    assert bad in prompts[1]
    assert "Validation error:" in prompts[1]
    assert "Group by motion" in prompts[1]
    assert "纠正重试" in caplog.text


def test_retry_exhaustion_preserves_response_and_location(backoff_waits):
    bad = '{\n"families": ["missing comma" "here"]}'
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return bad

    with pytest.raises(TaxonomyResponseError) as err:
        induce_taxonomy([CAPTION], ask)
    assert len(prompts) == 4
    assert backoff_waits == [1, 2, 4]
    assert "连续 4 次" in str(err.value)
    assert err.value.raw_response == bad
    assert isinstance(err.value.__cause__, json.JSONDecodeError)
    assert "line 2" in str(err.value)
    assert "missing comma" in str(err.value)


def test_empty_input_does_not_call_model():
    def ask(prompt):
        pytest.fail("empty captions must not call the model")

    assert induce_taxonomy(["", "  "], ask) == {"families": []}


def test_transport_error_is_not_retried_as_json(backoff_waits):
    calls = []

    def ask(prompt):
        calls.append(prompt)
        raise requests.exceptions.Timeout("timeout")

    with pytest.raises(requests.exceptions.Timeout):
        induce_taxonomy([CAPTION], ask)
    assert len(calls) == 1
    assert backoff_waits == []


@pytest.mark.parametrize(
    "reason,content,error",
    [
        ("length", json.dumps(TAXONOMY), "输出被截断"),
        ("length", None, "max_tokens=8192"),
        ("stop", None, "未返回有效文本"),
        ("stop", "<think>reasoning</think>  ", "未返回有效文本"),
        ("content_filter", "", "content_filter"),
        ("stop", json.dumps(TAXONOMY), None),
        (None, "<think>reasoning</think>" + json.dumps(TAXONOMY), None),
    ],
)
def test_llm_completion_status(monkeypatch, reason, content, error, backoff_waits):
    calls = []

    class Response:
        status_code = 200
        ok = True

        def raise_for_status(self):
            pass

        def json(self):
            choice = {"message": {"content": content}}
            if reason is not None:
                choice["finish_reason"] = reason
            return {"choices": [choice]}

    def post(*args, **kwargs):
        calls.append(kwargs)
        return Response()

    monkeypatch.setattr(requests, "post", post)
    ask = make_llm_ask("http://example.test/v1", "test-model")
    if error:
        with pytest.raises(ValueError, match=error):
            induce_taxonomy([CAPTION], ask)
    else:
        assert induce_taxonomy([CAPTION], ask) == TAXONOMY
    assert len(calls) == 1
    assert backoff_waits == []
