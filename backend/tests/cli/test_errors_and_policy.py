"""Execution errors are not abstentions (D33), and the CLI's defaults (W3).

* A model call that fails or a camera that does not decode: the record is an
  execution error with the incidents, and aggregate holds the episode back. A model that answers "can't tell" is an abstention,
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

import requests

from .fakevlm_server import FakeVlmServer
from .pipeline import Chain, comparable, read_jsonl, results, run, verdict_of

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


def _verdicts(run_dir: str) -> dict[int, dict]:
    """The machine's verdict lines of revision 1."""
    res = run("aggregate", "--run-dir", run_dir, "--revision", "1",
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
    failed = [i for r in recs.values() if r["status"] == "error"
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
    """--vlm-reasoning-effort puts reasoning_effort into every model request of check;
    without it no request has the field (v1 never sent one)."""
    rd = _copy(vlm_stage, tmp_path, "plain")
    with FakeVlmServer() as vlm:
        assert _vlm_check(vlm_stage, rd, vlm.url).rc == 0
    assert _posts(vlm) and not any("reasoning_effort" in c["payload"] for c in _posts(vlm))

    before = requests.post
    rd = _copy(vlm_stage, tmp_path, "effort")
    with FakeVlmServer() as vlm:
        assert _vlm_check(vlm_stage, rd, vlm.url, "--vlm-reasoning-effort", "minimal").rc == 0
    posts = _posts(vlm)
    assert posts and all(c["payload"]["reasoning_effort"] == "minimal" for c in posts)
    assert requests.post is before                      # restored after each command


# ---------------------------------------------------------------- D33

def test_a_failing_model_call_is_an_error_not_an_abstention(vlm_stage, tmp_path):
    ref = vlm_stage["reference"]
    abstained = [e for e, r in ref.items() if verdict_of(r) == "abstain"]
    assert abstained and all(ref[e]["error"] is None for e in abstained)   # "can't tell"

    rd = _copy(vlm_stage, tmp_path)
    judge = "Assess the robot manipulation task"          # the one judgement request (D71)
    with FakeVlmServer(fail=lambda text, payload: 500 if judge in text else None) as vlm:
        res = _vlm_check(vlm_stage, rd, vlm.url)
    assert res.rc == 0                          # an episode's error is not the command's
    recs = results(rd, "task_success")
    hit = sorted(e for e, r in recs.items() if r["status"] == "error")
    assert hit and vlm.count(judge) >= len(hit)
    for e in hit:
        incidents = recs[e]["error"]["incidents"]
        assert recs[e]["error"]["kind"] == "execution"
        assert incidents and all(i["call_kind"] == "probe" and i["cause"] == "server_error"
                                 for i in incidents), incidents
    for e in set(recs) - set(hit):              # never judged: untouched
        assert comparable(recs[e]) == comparable(ref[e])
    entry = res.doc["modules"]["task_success"]
    assert entry["error_episodes"] == hit and entry["episodes"]["error"] == len(hit)

    lines = _verdicts(rd)
    for e in hit:
        assert lines[e]["error_modules"] == ["task_success"]
        if lines[e]["blocking"]:                 # 2 and 5: their timestamps reject them all the same (D35)
            assert lines[e]["verdict"] == "drop" and "不影响结论" in lines[e]["reason"]
        else:
            assert lines[e]["verdict"] == "held" and lines[e]["reason"].startswith("待补跑")
    assert any(lines[e]["verdict"] == "held" for e in hit)


def test_a_camera_that_does_not_decode_is_a_blocking_finding(dataset, tmp_path):
    """D93 (design doc 28 §2): a video the decoder calls corrupt is the episode's fault, not the run's - a blocking
    ``decode_failed`` on that camera, nothing to retry."""
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
        assert recs[1]["status"] == "ok" and recs[1]["error"] is None, (m, recs[1])
        assert [(f["code"], f["scope"]) for f in recs[1]["findings"] if f["code"] == "decode_failed"] == \
            [("decode_failed", {"camera": "wrist"})], (m, recs[1]["findings"])
        incidents = recs[1]["details"]["undecodable"]
        assert any(i["step"] == "decode" and i.get("camera") == "wrist" for i in incidents), \
            (m, incidents)
        assert verdict_of(recs[1]) == "fail"
        assert res.doc["modules"][m]["error_episodes"] == []


def test_an_episode_without_a_task_text_is_not_judged(vlm_stage, tmp_path):
    """D72: 4 and 6 have no annotation. No judgement request is sent for them: their record says
    ``no_task_text`` (an info finding, nothing to review), they are not held, and every other check
    still counts for them."""
    rd = _copy(vlm_stage, tmp_path)
    with FakeVlmServer() as vlm:
        res = _vlm_check(vlm_stage, rd, vlm.url)
    assert res.rc == 0
    assert len(_posts(vlm)) == vlm_stage["reference_posts"]
    recs = results(rd, "task_success")
    for e in (4, 6):
        assert recs[e]["status"] == "ok" and recs[e]["error"] is None
        assert recs[e]["details"]["skipped"] == "no_task_text"
        assert [f["code"] for f in recs[e]["findings"]] == ["task_text_missing"]
        assert {u["reason"] for u in recs[e]["unassessable"]} >= {"no_task_text"}
    verdicts = _verdicts(rd)
    for e in (4, 6):
        assert verdicts[e]["verdict"] == "keep" and verdicts[e]["error_modules"] == []
    assert verdicts[5]["verdict"] == "drop"                        # its timestamps, as ever


def test_a_changed_source_stops_the_commands(dataset, tmp_path):
    rd = str(tmp_path / "run")
    os.makedirs(rd)
    sm = os.path.join(rd, "source_manifest.json")
    assert run("snapshot", "--input", dataset, "--episodes", "0-7", "--out", sm).rc == 0
    guard = ["--input", dataset, "--run-dir", rd, "--source-manifest", sm]
    vlm_args = ["--vlm-model", "fake-vlm", "--vlm-endpoint"]

    # an episode's video: the VLM check stops before any model call ...
    with open(os.path.join(dataset, "videos", "chunk-000", WRIST, "episode_000004.mp4"),
              "ab") as fh:
        fh.write(b"\0")
    with FakeVlmServer() as vlm:
        res = run("check", "--modules", "task_success", *guard, "--episodes", "0-7",
                  *vlm_args, vlm.url)
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
        res = run("check", "--modules", "task_success", *guard, "--episodes", "6",
                  *vlm_args, vlm.url)
    assert res.rc == 6 and res.doc["error"]["code"] == "source_changed", res.doc
    # without the manifest the semantics come from the next readable episode (design doc 28 §1): the stage goes on
    res = run("check", "--modules", Chain.NUMERIC, "--input", dataset, "--run-dir", rd,
              "--episodes", "5-7")
    assert res.rc == 0, res.doc
    assert all(r["status"] == "ok" for r in results(rd, "timestamp_check").values() if r["episode_index"] >= 5)


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
