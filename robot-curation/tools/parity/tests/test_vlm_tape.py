"""Tape hashing, storage, replay and the request hooks (no v1 needed)."""
from __future__ import annotations

import base64
import gzip
import json
import types

import jsonschema
import pytest
import requests

from parity import vlm_tape as T

from .conftest import load_schema

PNG_A = base64.b64encode(b"image-bytes-A").decode()
PNG_B = base64.b64encode(b"image-bytes-B").decode()


def _payload(img: str = PNG_A, text: str = "Score it", **extra) -> dict:
    return {"model": "m", "temperature": 0.0, "max_tokens": 64, **extra,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}}]}]}


def test_hash_ignores_key_order_and_keeps_every_prompt_character():
    a = _payload()
    b = json.loads(json.dumps(a))
    b = {k: b[k] for k in reversed(list(b))}
    assert T.canonical_request(a)[1] == T.canonical_request(b)[1]
    assert T.canonical_request(_payload(text="Score it."))[1] != T.canonical_request(a)[1]


def test_images_are_replaced_by_their_byte_hash():
    canon, digest = T.canonical_request(_payload())
    url = canon["body"]["messages"][0]["content"][1]["image_url"]["url"]
    assert url["mime"] == "image/jpeg" and url["bytes"] == len(b"image-bytes-A")
    assert "base64" not in json.dumps(canon)
    assert T.canonical_request(_payload(img=PNG_B))[1] != digest


def test_model_parameters_are_part_of_the_hash():
    assert T.canonical_request(_payload())[1] != \
        T.canonical_request(_payload(reasoning_effort="low"))[1]


def _entry(kind, digest, status=200, body="ok", seq=1, exc=None):
    e = {"seq": seq, "kind": kind, "hash": digest, "tag": "probe", "request": {}}
    if exc:
        e.update(outcome="exception", exc_type=exc, exc_message="boom")
    else:
        e.update(outcome="response", status=status, body=body)
    return e


def test_replay_store_is_fifo_per_hash_and_counts_hits():
    store = T.ReplayStore([_entry("logical", "h", body="first", seq=1),
                           _entry("logical", "h", body="second", seq=2),
                           _entry("direct", "h", body="direct", seq=3)])
    assert store.take("logical", "h")["body"] == "first"
    assert store.take("logical", "h")["body"] == "second"
    assert store.take("logical", "h") is None
    assert store.take("direct", "h")["body"] == "direct"
    assert store.hits == {"logical": 2, "direct": 1}


def test_replay_record_store_skips_failures_and_dropped_hashes():
    entries = [_entry("logical", "bad", exc="requests.exceptions.ReadTimeout"),
               _entry("logical", "429", status=429),
               _entry("logical", "drop"), _entry("logical", "keep")]
    store = T.ReplayStore(entries, skip_failures=True, drop_hashes={"drop"})
    assert [store.take("logical", h) for h in ("bad", "429", "drop")] == [None, None, None]
    assert store.take("logical", "keep") is not None


def test_failures_are_counted_per_logical_call():
    entries = [_entry("logical", "a"), _entry("logical", "b", status=500),
               _entry("logical", "c", exc="requests.exceptions.ReadTimeout"),
               _entry("direct", "d", exc="requests.exceptions.ConnectionError"),
               _entry("direct", "d"),
               {"seq": 9, "kind": "llm_failure", "exc_type": "ValueError"}]
    assert [e["hash"] if "hash" in e else e["kind"] for e in T.tape_failures(entries)] == \
        ["b", "c", "llm_failure"]


def test_entries_round_trip_to_response_and_exception():
    r = T.response_from_entry(_entry("logical", "h", status=429, body='{"error": 1}'), "u")
    assert r.status_code == 429 and r.json() == {"error": 1}
    with pytest.raises(requests.exceptions.HTTPError):
        r.raise_for_status()
    exc = T.exception_from_entry(_entry("logical", "h", exc="requests.exceptions.ReadTimeout"))
    assert isinstance(exc, requests.exceptions.ReadTimeout)
    odd = T.exception_from_entry(_entry("logical", "h", exc="no.such.Module"))
    assert isinstance(odd, requests.exceptions.ConnectionError)


# -- hooks against a stand-in for v1's vlm_client ----------------------------

def _fake_client():
    """Minimal stand-in: a hedged path and a text path, both via requests.post."""
    mod = types.SimpleNamespace()

    def hedged_request(send, *, tag, timeout_s, gate=None):
        return send(timeout_s)

    def make_llm_ask(url):
        def llm_ask(prompt):
            r = requests.post(url, json={"model": "m", "messages": [
                {"role": "user", "content": prompt}]}, timeout=5)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        return llm_ask

    mod.hedged_request = hedged_request
    mod.make_llm_ask = make_llm_ask
    return mod


def _vision_call(mod, url, img=PNG_A):
    payload = _payload(img=img)
    r = mod.hedged_request(lambda hard: requests.post(url, json=payload, timeout=hard),
                           tag="probe", timeout_s=5)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _transport(answers: dict):
    def post(url, *a, json=None, **kw):
        text = answers.get(T.canonical_request(json)[1], "7")
        r = requests.models.Response()
        r.status_code, r._content, r.encoding = 200, __import__("json").dumps(
            {"choices": [{"message": {"content": text}}]}).encode(), "utf-8"
        return r
    return {"post": post, "get": post}


URL = "http://model.local/v1/chat/completions"


def test_record_then_replay_serves_the_same_answers(tmp_path):
    mod = _fake_client()
    tape = str(tmp_path / "t.jsonl.gz")
    hooks = T.TapeHooks("record", tape_out=tape, transport=_transport({}))
    hooks.install(mod)
    try:
        ask = mod.make_llm_ask(URL)
        assert _vision_call(mod, URL) == "7" and ask("hello") == "7"
    finally:
        hooks.uninstall()
    header, entries = T.read_tape(tape)
    assert header["tape_schema_version"] == "1.0"
    assert [(e["kind"], e["tag"]) for e in entries] == [("logical", "probe"), ("direct", None)]
    schema = load_schema("parity", "vlm-tape-entry.schema.json")
    with gzip.open(tape, "rt") as fh:
        for line in fh:
            jsonschema.validate(json.loads(line), schema)

    replay = T.TapeHooks("replay", replay_entries=entries)
    replay.install(mod)
    try:
        ask = mod.make_llm_ask(URL)
        assert _vision_call(mod, URL) == "7" and ask("hello") == "7"
        with pytest.raises(requests.exceptions.ConnectionError):
            _vision_call(mod, URL, img=PNG_B)          # never recorded
    finally:
        replay.uninstall()
    s = replay.stats()
    assert s["hits"] == {"logical": 1, "direct": 1} and s["misses"] == 1


def test_replay_record_fills_the_gaps_and_writes_a_complete_tape(tmp_path):
    mod = _fake_client()
    first = str(tmp_path / "a.jsonl.gz")
    hooks = T.TapeHooks("record", tape_out=first, transport=_transport({}))
    hooks.install(mod)
    try:
        _vision_call(mod, URL)
    finally:
        hooks.uninstall()
    _, entries = T.read_tape(first)
    second = str(tmp_path / "b.jsonl.gz")
    hooks = T.TapeHooks("replay-record", tape_out=second, replay_entries=entries,
                        transport=_transport({}))
    hooks.install(mod)
    try:
        _vision_call(mod, URL)                      # replayed
        _vision_call(mod, URL, img=PNG_B)           # live, recorded
    finally:
        hooks.uninstall()
    _, out = T.read_tape(second)
    assert [e.get("replayed_from") for e in out] == [1, None]
    assert not T.tape_failures(out)


def test_text_call_that_finally_fails_leaves_a_marker(tmp_path):
    mod = _fake_client()
    tape = str(tmp_path / "t.jsonl.gz")

    def post(url, *a, **kw):
        raise requests.exceptions.ConnectionError("down")

    hooks = T.TapeHooks("record", tape_out=tape, transport={"post": post, "get": post})
    hooks.install(mod)
    try:
        with pytest.raises(requests.exceptions.ConnectionError):
            mod.make_llm_ask(URL)("hello")
    finally:
        hooks.uninstall()
    _, entries = T.read_tape(tape)
    assert [e["kind"] for e in T.tape_failures(entries)] == ["llm_failure"]


def test_hooked_functions_pickle_by_reference():
    """daft cloudpickles v1's UDF closures; our replacements must survive that."""
    import pickle

    mod = _fake_client()
    hooks = T.TapeHooks("replay", replay_entries=[])
    hooks.install(mod)
    try:
        assert pickle.loads(pickle.dumps(mod.hedged_request)) is T.hooked_hedged_request
        wrapped = mod.make_llm_ask(URL)
        assert isinstance(wrapped, T.LlmAskWrapper)
    finally:
        hooks.uninstall()
