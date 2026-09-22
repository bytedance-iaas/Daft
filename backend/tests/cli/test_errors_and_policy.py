"""Execution errors are not abstentions (D33), and the CLI's defaults (W3).

* A model call that fails, a camera that does not decode, an episode whose
  caption could not be made: the record is ``verdict = error`` with the
  incidents, even where v1's fallbacks still reach a conclusion, and aggregate
  holds the episode back. A model that answers "can't tell" is an abstention,
  never an error.
* No concurrency and no retry unless asked for (doc 02 §1): one model request
  in flight at a time, one attempt per request; ``--concurrency`` and
  ``--retry`` switch them on and do not change a verdict.
* A source that changed since the snapshot stops the commands (exit 6).
"""
from __future__ import annotations

import json
import os
import shutil
import threading

import pytest
import requests

from curation.pipeline.records import module_dir

from .fakevlm_server import FakeVlmServer
from .pipeline import Chain, comparable, read_jsonl, results, run

WRIST = "observation.images.wrist"


def _copy(vlm_stage, tmp_path, name: str = "run") -> str:
    rd = str(tmp_path / name)
    shutil.copytree(vlm_stage["base"], rd)
    return rd


def _vlm_check(vlm_stage, run_dir: str, url: str, *extra):
    return run("check", "--modules", "task_success", "--input", vlm_stage["dataset"],
               "--run-dir", run_dir, "--episodes", vlm_stage["episodes"],
               "--vlm-endpoint", url, "--vlm-model", "fake-vlm", *extra)


def _posts(server: FakeVlmServer) -> list[dict]:
    return [c for c in server.calls if c["path"].endswith("/chat/completions")]


def _fail_first(n: int, status: int = 503, needle: str = ""):
    """Fail the first ``n`` requests (containing ``needle``) with ``status``."""
    seen = {"n": 0}
    lock = threading.Lock()

    def fail(text, payload):
        if needle not in text:
            return None
        with lock:
            seen["n"] += 1
            return status if seen["n"] <= n else None

    return fail


def _fail_once_each(n: int, status: int = 503):
    """Fail the first attempt of the first ``n`` different requests with ``status``."""
    failed: set[str] = set()
    lock = threading.Lock()

    def fail(text, payload):
        key = json.dumps(payload, sort_keys=True)
        with lock:
            if key in failed or len(failed) >= n:
                return None
            failed.add(key)
            return status

    return fail


def _same_as_reference(run_dir: str, vlm_stage) -> None:
    got = results(run_dir, "task_success")
    ref = vlm_stage["reference"]
    assert {e: comparable(r) for e, r in got.items()} == \
        {e: comparable(r) for e, r in ref.items()}


def _funnel(run_dir: str) -> dict[int, dict]:
    res = run("aggregate", "--run-dir", run_dir, "--phase", "funnel", "--revision", "1",
              "--episodes", "0-7")
    assert res.rc == 0, res.doc
    return {ln["episode_index"]: ln for ln in
            read_jsonl(os.path.join(run_dir, "revisions", "r0001", "verdicts.jsonl"))}


# ---------------------------------------------------------------- defaults

def test_one_model_request_at_a_time_unless_concurrency_is_given(vlm_stage, tmp_path):
    rd = _copy(vlm_stage, tmp_path, "serial")
    with FakeVlmServer(delay_s=0.02) as vlm:
        assert _vlm_check(vlm_stage, rd, vlm.url).rc == 0
    assert vlm.max_in_flight == 1
    _same_as_reference(rd, vlm_stage)

    rd = _copy(vlm_stage, tmp_path, "parallel")
    with FakeVlmServer(delay_s=0.02) as vlm:
        assert _vlm_check(vlm_stage, rd, vlm.url, "--concurrency", "8").rc == 0
    assert vlm.max_in_flight > 1
    _same_as_reference(rd, vlm_stage)                    # concurrency changes no verdict


def test_one_attempt_per_request_unless_retry_is_given(vlm_stage, tmp_path):
    rd = _copy(vlm_stage, tmp_path, "once")
    with FakeVlmServer(fail=_fail_once_each(2)) as vlm:
        res = _vlm_check(vlm_stage, rd, vlm.url)
    assert res.rc == 0
    entry = res.doc["modules"]["task_success"]
    assert entry["episodes"]["error"] >= 1 and entry["error_episodes"]
    recs = results(rd, "task_success")
    failed = [i for r in recs.values() if r["verdict"] == "error"
              for i in r["error"]["incidents"]]
    assert len(failed) == 2
    assert all(i["attempts"] == 1 and i["cause"] == "server_error" for i in failed), failed

    rd = _copy(vlm_stage, tmp_path, "retried")
    with FakeVlmServer(fail=_fail_once_each(2)) as vlm:
        res = _vlm_check(vlm_stage, rd, vlm.url, "--retry", "1")
    assert res.rc == 0 and res.doc["modules"]["task_success"]["episodes"]["error"] == 0
    assert len(_posts(vlm)) == vlm_stage["reference_posts"] + 2     # each failure sent again
    _same_as_reference(rd, vlm_stage)


def test_reasoning_effort_is_sent_only_when_given(vlm_stage, tmp_path):
    """--vlm-reasoning-effort puts reasoning_effort into every model request of check
    and autolabel; without it no request has the field (v1 never sent one)."""
    rd = _copy(vlm_stage, tmp_path, "plain")
    with FakeVlmServer() as vlm:
        assert _vlm_check(vlm_stage, rd, vlm.url).rc == 0
    assert _posts(vlm) and not any("reasoning_effort" in c["payload"] for c in _posts(vlm))

    before = requests.post
    rd = _copy(vlm_stage, tmp_path, "effort")
    with FakeVlmServer() as vlm:
        assert _vlm_check(vlm_stage, rd, vlm.url, "--vlm-reasoning-effort", "minimal").rc == 0
        os.remove(os.path.join(rd, "autolabel", "captions.jsonl"))
        al = run("autolabel", "--input", vlm_stage["dataset"], "--run-dir", rd, "--episodes",
                 "0-7", "--vlm-endpoint", vlm.url, "--vlm-model", "fake-vlm",
                 "--vlm-reasoning-effort", "minimal")
        assert al.rc == 0, al.doc
    posts = _posts(vlm)
    assert any("All cameras show the SAME robot episode" in c["text"] for c in posts)
    assert posts and all(c["payload"]["reasoning_effort"] == "minimal" for c in posts)
    assert requests.post is before                      # restored after each command


def test_text_calls_are_one_request_with_v1s_body(tmp_path):
    """v1's text client tries four times on its own; under the CLI a text call is one
    request, the outer retry adds tries, and the request body is v1's."""
    from curation.adapters import vlm_client
    from curation.pipeline.vlm_policy import TransportPolicy, installed
    from curation.planner.retry import RetryPolicy

    with FakeVlmServer() as vlm:
        assert vlm_client.make_llm_ask(vlm.url, "fake-vlm")("ping") == "pong"
        with installed(TransportPolicy()):
            assert vlm_client.make_llm_ask(vlm.url, "fake-vlm")("ping") == "pong"
    v1_call, v2_call = _posts(vlm)
    assert v1_call["payload"] == v2_call["payload"]
    assert v2_call["payload"]["messages"] == [{"role": "user", "content": "ping"}]

    with FakeVlmServer(fail=_fail_first(1)) as vlm:
        with installed(TransportPolicy()):
            ask = vlm_client.make_llm_ask(vlm.url, "fake-vlm")
            with pytest.raises(requests.HTTPError) as caught:
                ask("ping")
        assert caught.value.curation_failure["attempts"] == 1
        assert len(_posts(vlm)) == 1                           # v1 alone would try 4 times
    with FakeVlmServer(fail=_fail_first(1)) as vlm:
        policy = TransportPolicy(retry=RetryPolicy(max_retries=1), sleep=lambda s: None)
        with installed(policy):
            assert vlm_client.make_llm_ask(vlm.url, "fake-vlm")("ping") == "pong"
        assert len(_posts(vlm)) == 2 and policy.stats.rescued == 1


# ---------------------------------------------------------------- D33

def test_a_failing_model_call_is_an_error_not_an_abstention(vlm_stage, tmp_path):
    ref = vlm_stage["reference"]
    abstained = [e for e, r in ref.items() if r["verdict"] == "abstain"]
    assert abstained and all(ref[e]["error"] is None for e in abstained)   # "can't tell"

    rd = _copy(vlm_stage, tmp_path)
    review = "Did the robot COMPLETE the task"
    with FakeVlmServer(fail=lambda text, payload: 500 if review in text else None) as vlm:
        res = _vlm_check(vlm_stage, rd, vlm.url)
    assert res.rc == 0                          # an episode's error is not the command's
    recs = results(rd, "task_success")
    hit = sorted(e for e, r in recs.items() if r["verdict"] == "error")
    assert hit and vlm.count(review) >= len(hit)
    for e in hit:
        incidents = recs[e]["error"]["incidents"]
        assert recs[e]["error"]["kind"] == "execution"
        assert incidents and all(i["call_kind"] == "endstate" and i["cause"] == "server_error"
                                 for i in incidents), incidents
    for e in set(recs) - set(hit):              # never reviewed: untouched
        assert comparable(recs[e]) == comparable(ref[e])
    entry = res.doc["modules"]["task_success"]
    assert entry["error_episodes"] == hit and entry["episodes"]["error"] == len(hit)

    lines = _funnel(rd)
    for e in hit:
        assert lines[e]["verdict"] == "held" and lines[e]["error_modules"] == ["task_success"]
        assert lines[e]["reason"].startswith("待补跑")


def test_a_camera_that_does_not_decode_is_an_error(dataset, tmp_path):
    path = os.path.join(dataset, "videos", "chunk-000", WRIST, "episode_000001.mp4")
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 4096)
    rd = str(tmp_path / "run")
    res = run("check", "--modules", Chain.FRAME, "--input", dataset, "--run-dir", rd,
              "--episodes", "0,1")
    assert res.rc == 0, res.doc
    for m in ("visual_quality", "video_action_sync"):
        recs = results(rd, m)
        assert recs[0]["error"] is None
        assert recs[1]["verdict"] == "error", (m, recs[1])
        incidents = recs[1]["error"]["incidents"]
        assert any(i["step"] == "decode" and i.get("camera") == "wrist" for i in incidents), \
            (m, incidents)
        assert res.doc["modules"][m]["error_episodes"] == [1]


def test_a_failed_caption_is_an_error_down_to_the_verdict(vlm_stage, tmp_path):
    rd = _copy(vlm_stage, tmp_path)
    caption = "All cameras show the SAME robot episode"
    with FakeVlmServer(fail=lambda text, payload: 500 if caption in text else None) as vlm:
        al = run("autolabel", "--input", vlm_stage["dataset"], "--run-dir", rd,
                 "--episodes", "0-7", "--vlm-endpoint", vlm.url, "--vlm-model", "fake-vlm")
    assert al.rc == 0, al.doc
    assert al.doc["counts"] == {"total": 2, "ok": 0, "unclear": 0, "error": 2}
    assert al.doc["error_episodes"] == [4, 6]
    lines = {ln["episode_index"]: ln
             for ln in read_jsonl(os.path.join(rd, "autolabel", "captions.jsonl"))}
    for e in (4, 6):
        assert lines[e]["status"] == "error" and lines[e]["caption"] in (None, "")
    with FakeVlmServer() as vlm:                         # the model is back for the check
        res = _vlm_check(vlm_stage, rd, vlm.url)
    assert res.rc == 0
    recs = results(rd, "task_success")
    for e in (4, 6):
        assert recs[e]["verdict"] == "error"
        assert [i["step"] for i in recs[e]["error"]["incidents"]] == ["autolabel"]
    for e in (0, 1, 3, 7):
        assert comparable(recs[e]) == comparable(vlm_stage["reference"][e])
    verdicts = _funnel(rd)
    for e in (4, 6):
        assert verdicts[e]["verdict"] == "held"
        assert verdicts[e]["error_modules"] == ["autolabel"]


def test_a_failed_taxonomy_call_fails_the_skill_profile_module(vlm_stage, tmp_path):
    """The taxonomy is one dataset-level result: when a text call it needs fails for
    good no episode can be filed, and the module fails as a whole (exit 4)."""
    rd = str(tmp_path / "run")
    shutil.copytree(vlm_stage["reference_dir"], rd)
    _funnel(rd)
    keep = os.path.join(rd, "revisions", "r0001", "keep.txt")
    res = run("check", "--modules", "dedup", "--input", vlm_stage["dataset"], "--run-dir", rd,
              "--episodes", "@" + keep, "--survivors-out", str(tmp_path / "dedup.txt"))
    assert res.rc == 0, res.doc
    taxonomy = "Build a TWO-LEVEL skill taxonomy"
    with FakeVlmServer(fail=lambda text, payload: 503 if taxonomy in text else None) as vlm:
        res = run("check", "--modules", "skill_profile", "--input", vlm_stage["dataset"],
                  "--run-dir", rd, "--episodes", "@" + str(tmp_path / "dedup.txt"),
                  "--vlm-endpoint", vlm.url, "--vlm-model", "fake-vlm")
    assert res.rc == 4 and res.doc["error"]["code"] == "module_failed", res.doc
    assert res.doc["error"]["details"]["incidents"][0] == {
        "step": "llm", "call_kind": "llm", "cause": "server_error", "attempts": 1}
    assert vlm.count(taxonomy) == 1                     # one request: no retry by default


def test_a_failed_skill_profile_module_holds_every_episode_until_a_retry(vlm_stage, tmp_path):
    """D41: when skill_profile fails as a whole, every episode it should have filed is
    held and none is delivered; a retry that succeeds releases them all."""
    rd = str(tmp_path / "run")
    shutil.copytree(vlm_stage["reference_dir"], rd)
    _funnel(rd)
    keep = os.path.join(rd, "revisions", "r0001", "keep.txt")
    survivors_file = str(tmp_path / "dedup.txt")
    res = run("check", "--modules", "dedup", "--input", vlm_stage["dataset"], "--run-dir", rd,
              "--episodes", "@" + keep, "--survivors-out", survivors_file)
    assert res.rc == 0, res.doc
    survivors = [int(x) for x in open(survivors_file, encoding="utf-8").read().split()]
    assert survivors
    assert not os.path.exists(module_dir(rd, "skill_profile"))    # no earlier results

    def profile(**server):
        with FakeVlmServer(**server) as vlm:
            return run("check", "--modules", "skill_profile", "--input", vlm_stage["dataset"],
                       "--run-dir", rd, "--episodes", "@" + survivors_file,
                       "--vlm-endpoint", vlm.url, "--vlm-model", "fake-vlm")

    def final():
        res = run("aggregate", "--run-dir", rd, "--phase", "final", "--revision", "1",
                  "--episodes", "0-7", "--input", vlm_stage["dataset"])
        assert res.rc == 0, res.doc
        lists = {}
        for name in ("passed", "held", "reject"):
            with open(os.path.join(rd, "revisions", "r0001", f"{name}.json"),
                      encoding="utf-8") as fh:
                lists[name] = {e["episode_index"]: e for e in json.load(fh)["episodes"]}
        return lists

    taxonomy = "Build a TWO-LEVEL skill taxonomy"
    res = profile(fail=lambda text, payload: 503 if taxonomy in text else None)
    assert res.rc == 4 and res.doc["error"]["code"] == "module_failed", res.doc
    lists = final()
    assert lists["passed"] == {}                        # nothing is delivered
    assert sorted(lists["held"]) == survivors           # every episode it had to file
    for e in survivors:
        assert [(r["module"], r["kind"]) for r in lists["held"][e]["reasons"]] == \
            [("skill_profile", "execution_error")]
        assert "技能画像" in lists["held"][e]["reasons"][0]["text"]

    assert profile().rc == 0                             # the retry succeeds
    lists = final()
    assert lists["held"] == {}
    assert sorted(lists["passed"]) == survivors


# ---------------------------------------------------------------- source guard

def test_a_changed_source_stops_the_commands(dataset, tmp_path):
    rd = str(tmp_path / "run")
    os.makedirs(rd)
    sm = os.path.join(rd, "source_manifest.json")
    assert run("snapshot", "--input", dataset, "--episodes", "0-7", "--out", sm).rc == 0
    guard = ["--input", dataset, "--run-dir", rd, "--source-manifest", sm]
    vlm_args = ["--vlm-model", "fake-vlm", "--vlm-endpoint"]

    # an unlabelled episode's video: autolabel stops before any model call ...
    with open(os.path.join(dataset, "videos", "chunk-000", WRIST, "episode_000004.mp4"),
              "ab") as fh:
        fh.write(b"\0")
    with FakeVlmServer() as vlm:
        res = run("autolabel", *guard, "--episodes", "0-7", *vlm_args, vlm.url)
    assert res.rc == 6 and res.doc["error"]["code"] == "source_changed", res.doc
    assert res.doc["error"]["details"]["key"].endswith("episode_000004.mp4")
    assert not _posts(vlm)
    # ... a check of other episodes does not read it
    res = run("check", "--modules", Chain.NUMERIC, *guard, "--episodes", "0-3")
    assert res.rc == 0, res.doc

    # a data file of the first episodes: v1 resolves the dataset semantics from them
    # whatever the selection, so every command that reads source data stops
    with open(os.path.join(dataset, "data", "chunk-000", "episode_000000.parquet"), "ab") as fh:
        fh.write(b"\0")
    res = run("check", "--modules", Chain.NUMERIC, *guard, "--episodes", "5-7")
    assert res.rc == 6 and res.doc["error"]["code"] == "source_changed", res.doc
    with FakeVlmServer() as vlm:
        res = run("autolabel", *guard, "--episodes", "6", *vlm_args, vlm.url)
    assert res.rc == 6 and res.doc["error"]["code"] == "source_changed", res.doc
    # without the manifest the broken file ends the stage: no episode can be judged
    res = run("check", "--modules", Chain.NUMERIC, "--input", dataset, "--run-dir", rd,
              "--episodes", "5-7")
    assert res.rc == 4 and "cannot be read" in res.doc["error"]["message"], res.doc


def test_changed_metadata_stops_every_check(dataset, tmp_path):
    rd = str(tmp_path / "run")
    os.makedirs(rd)
    sm = os.path.join(rd, "source_manifest.json")
    assert run("snapshot", "--input", dataset, "--episodes", "0-7", "--out", sm).rc == 0
    with open(os.path.join(dataset, "meta", "episodes.jsonl"), "a", encoding="utf-8") as fh:
        fh.write("\n")
    res = run("check", "--modules", "timestamp_check", "--input", dataset, "--run-dir", rd,
              "--source-manifest", sm, "--episodes", "0")
    assert res.rc == 6 and res.doc["error"]["code"] == "source_changed", res.doc


def test_the_errors_are_listed_by_the_json_output(vlm_stage, tmp_path):
    """The summary carries the input digest, so the Daemon can tell what was judged."""
    rd = _copy(vlm_stage, tmp_path)
    with FakeVlmServer() as vlm:
        res = _vlm_check(vlm_stage, rd, vlm.url)
    entry = res.doc["modules"]["task_success"]
    assert entry["input_digest"].startswith("sha256:")
    assert entry["error_episodes"] == [] and entry["part"] == "0001"
    with open(os.path.join(rd, "checks", "task_success", "results.jsonl"), encoding="utf-8") as fh:
        assert len([json.loads(ln) for ln in fh]) == len(vlm_stage["reference"])
