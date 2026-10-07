"""camera_defects end to end (registry 1.14): ``check --modules task_success`` also writes the
rider's records, out of the per-camera answers of the one judgement request it makes anyway (D71)
- no request of its own, no ticking, no vote - and the rider then shows up in the report.
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

from curation.adapters.video_vlm import CAMERA_CHECK_PROTOCOL
from curation.contracts import modules as registry

from .fakevlm_server import FakeVlmServer
from .pipeline import Chain, comparable, results, run

MOD = "camera_defects"
ITEMS = ("glitch", "shake", "contamination")
LEVELS = {"none", "minor", "severe", "unknown"}
ASK = 'more field "cameras"'                             # the per-camera block every judgement asks for
REVIEW = "Independently review ONLY this camera"         # the two-pass review: must never be sent


def _check(p, run_dir, url, modules="task_success"):
    return run("check", "--modules", modules, "--input", p["dataset"], "--run-dir", run_dir,
               "--episodes", p["episodes"], "--vlm-endpoint", url, "--vlm-model", "fake-vlm")


@pytest.fixture(scope="module")
def ran(vlm_stage, tmp_path_factory) -> dict:
    """The VLM stage on a copy of the prepared run directory, asking for task_success only."""
    run_dir = str(tmp_path_factory.mktemp("camera-defects") / "run")
    shutil.copytree(vlm_stage["base"], run_dir)
    with FakeVlmServer() as vlm:
        res = _check(vlm_stage, run_dir, vlm.url)
        assert res.rc == 0, res.doc
        posts = [c["text"] for c in vlm.calls if c["path"].endswith("/chat/completions")]
    return {"dir": run_dir, "posts": posts, "doc": res.doc}


def test_every_episode_gets_a_record_with_all_three_items_without_being_asked(vlm_stage, ran):
    task = results(ran["dir"], "task_success")
    cam = results(ran["dir"], MOD)
    assert set(cam) == set(task) == set(vlm_stage["reference"])
    for rec in cam.values():
        # advisory: every finding it reports is info under the default policy, never a vote (P18)
        assert rec["status"] == "ok" and rec["error"] is None and "score" not in rec["readings"]
        assert all(registry.finding_code(MOD, f["code"]).level == "info" for f in rec["findings"])
        d = rec["details"]
        assert d["protocol"] == CAMERA_CHECK_PROTOCOL and d["source"] == "task_success.cameras"
        assert set(d["items"]) == set(ITEMS) and d["cams"]
        assert set(d["items"].values()) <= LEVELS         # one status per item, nothing nested
        assert set(d["per_camera"]) == set(d["cams"])
        for cam_entry in d["per_camera"].values():
            assert set(ITEMS) <= set(cam_entry)
    # the fake model answers by camera and window: over the fixture the levels differ
    statuses = {r["details"]["items"][i] for r in cam.values() for i in ITEMS}
    assert len(statuses - {"unknown"}) >= 2, statuses


def test_the_rider_costs_no_request_and_no_repair(vlm_stage, ran):
    """Every judgement request carries the per-camera block (the rider rides in it), no review
    request per camera is ever sent, and no answer was sent back for repair (the count of
    requests is one per episode; nothing else)."""
    judged = [t for t in ran["posts"] if "Assess the robot manipulation task" in t]
    assert judged and all(ASK in t for t in judged)
    assert not any(REVIEW in t for t in ran["posts"])
    assert not any("Invalid answer:" in t for t in ran["posts"])
    assert len(ran["posts"]) == vlm_stage["reference_posts"]
    assert set(ran["doc"]["modules"]) == {"task_success", MOD}


def test_task_success_is_the_same_with_and_without_the_rider_listed(vlm_stage, ran, tmp_path):
    """Naming camera_defects next to task_success changes nothing: the same requests, the same
    records (the rider is always on)."""
    other = str(tmp_path / "listed")
    shutil.copytree(vlm_stage["base"], other)
    with FakeVlmServer() as vlm:
        res = _check(vlm_stage, other, vlm.url, modules="task_success,camera_defects")
        assert res.rc == 0, res.doc
        posts = [c["text"] for c in vlm.calls if c["path"].endswith("/chat/completions")]
    assert posts == ran["posts"]
    for m in ("task_success", MOD):
        a = {ep: comparable(r) for ep, r in results(ran["dir"], m).items()}
        b = {ep: comparable(r) for ep, r in results(other, m).items()}
        assert a == b, m
    assert {ep: comparable(r) for ep, r in results(ran["dir"], "task_success").items()} \
        == {ep: comparable(r) for ep, r in vlm_stage["reference"].items()}


def test_the_rider_cannot_be_asked_for_alone(vlm_stage, tmp_path):
    run_dir = str(tmp_path / "alone")
    shutil.copytree(vlm_stage["base"], run_dir)
    with FakeVlmServer() as vlm:
        res = _check(vlm_stage, run_dir, vlm.url, modules=MOD)
        assert not vlm.calls
    assert res.rc != 0 and res.doc["error"]["code"] == "usage", res.doc
    assert "task_success" in res.doc["error"]["message"]
    assert not os.path.exists(os.path.join(run_dir, "checks", MOD))


def test_resume_redoes_an_episode_whose_defect_report_is_from_an_older_protocol(vlm_stage, ran, tmp_path):
    """A report written before this protocol is work to redo: --resume re-judges exactly that
    episode, host and rider together, and leaves the ones already answered alone."""
    run_dir = str(tmp_path / "resume")
    shutil.copytree(ran["dir"], run_dir)
    part = os.path.join(run_dir, "checks", MOD, "parts")
    [name] = [f for f in sorted(os.listdir(part)) if f.endswith(".jsonl")]
    path = os.path.join(part, name)
    with open(path, "rb") as fh:                       # same length in, same length out: the
        raw = fh.read()                               # part index addresses records by offset
    old, new = b'"camera-check/2"', b'"camera-check/0"'
    assert len(old) == len(new) and raw.count(old) == len(results(run_dir, MOD))
    with open(path, "wb") as fh:
        fh.write(raw.replace(old, new, 1))
    stale = [json.loads(ln)["episode_index"] for ln in open(path, encoding="utf-8")
             if '"camera-check/0"' in ln]
    assert len(stale) == 1

    with FakeVlmServer() as vlm:
        res = run("check", "--modules", "task_success", "--input", vlm_stage["dataset"],
                  "--run-dir", run_dir, "--episodes", vlm_stage["episodes"],
                  "--vlm-endpoint", vlm.url, "--vlm-model", "fake-vlm", "--resume")
        assert res.rc == 0, res.doc
        assert [c for c in vlm.calls if c["path"].endswith("/chat/completions")]
    n = len(results(run_dir, MOD))
    for m in ("task_success", MOD):                    # one redone, the rest left as they were
        assert res.doc["modules"][m]["skipped_existing"] == n - 1, res.doc["modules"][m]
        assert res.doc["modules"][m]["error_episodes"] == []
    redone = results(run_dir, MOD)[stale[0]]
    assert redone["details"]["protocol"] == CAMERA_CHECK_PROTOCOL
    assert set(redone["details"]["items"]) == set(ITEMS)


def test_the_verdict_ignores_the_rider_and_the_report_shows_it(vlm_stage, ran, tmp_path):
    """keep / drop are those of a run that never had the rider's records; the report has the
    section (advisory) and the per-camera table."""
    bare = str(tmp_path / "bare")
    shutil.copytree(vlm_stage["reference_dir"], bare)        # task_success only, no rider records
    shutil.rmtree(os.path.join(bare, "checks", MOD), ignore_errors=True)
    for rd in (bare, ran["dir"]):
        res = run("aggregate", "--run-dir", rd, "--phase", "funnel", "--revision", "1",
                  "--episodes", "0-7")
        assert res.rc == 0, res.doc
    lists = {}
    for rd in (bare, ran["dir"]):
        with open(os.path.join(rd, "revisions", "r0001", "keep.txt"), encoding="utf-8") as fh:
            lists[rd] = fh.read()
    assert lists[bare] == lists[ran["dir"]]

    with FakeVlmServer() as vlm:
        c = Chain(vlm_stage["dataset"], ran["dir"], vlm.url)
        c.post()
    with open(c.path("revisions", "r0001", "report.json"), encoding="utf-8") as fh:
        report = json.load(fh)
    [sec] = [s for s in report["modules"] if s["id"] == MOD]
    s = sec["summary"]
    n = len(results(ran["dir"], MOD))
    for item in ITEMS:                                   # one counts dict per item, every episode in it
        assert set(s[item]) == LEVELS and sum(s[item].values()) == n, (item, s[item])
    assert s["cameras"] >= n and s["cameras_unanswered"] < s["cameras"]
    assert 0.0 <= s["clean_ratio_mean"] <= 1.0
    with open(c.path("revisions", "r0001", "report.md"), encoding="utf-8") as fh:
        md = fh.read()
    assert "### 镜头画面缺陷" in md and "不影响判决" in md
    [table] = [t for t in sec["tables"] if t["id"] == MOD]
    assert table["rows"] >= s["cameras"] and os.path.exists(c.path("revisions", "r0001", table["file"]))
