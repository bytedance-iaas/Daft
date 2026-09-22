"""Episodes whose source files are missing are left out, as v1 does (D40, contract 1.4).

v1's rule (``lerobot_reader._v2_missing``): a LeRobot v2 episode without its data
parquet or any camera's video is dropped before anything judges it. v2 drops it
too and says so: ``snapshot`` lists it in ``skipped_episodes`` and no command
given the manifest reads it; without a manifest ``check`` finds it when it reads
and lists it in ``skipped_missing_source``. Either way it has no result line, is
in none of the four lists and not in the total, and the report names it. LeRobot
v3 episodes are never dropped (v1 does not check them): they fail to read.
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

from curation.pipeline.records import latest_results, read_jsonl

from .conftest import ENV_VARS
from .fakevlm_server import FakeVlmServer
from .pipeline import Chain, run

WRIST = "observation.images.wrist"
PARQUET_1 = "data/chunk-000/episode_000001.parquet"
VIDEO_4 = f"videos/chunk-000/{WRIST}/episode_000004.mp4"
MODULES = ("timestamp_check", "kinematic_limits", "motion_quality", "visual_quality",
           "video_action_sync", "task_success", "dedup", "skill_profile")


def _json(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return json.load(fh)


def _eps(run_dir: str, name: str) -> list[int]:
    return [e["episode_index"]
            for e in _json(run_dir, "revisions", "r0001", f"{name}.json")["episodes"]]


@pytest.fixture(scope="module")
def flow(tmp_path_factory, mini_dataset):
    """The whole chain, with the source manifest, on a dataset where episode 1 lacks
    its parquet and episode 4 (unlabelled) its wrist video."""
    tmp = tmp_path_factory.mktemp("missing")
    dataset = str(tmp / "mini")
    shutil.copytree(mini_dataset, dataset)
    for key in (PARQUET_1, VIDEO_4):
        os.remove(os.path.join(dataset, key))
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV_VARS:
            mp.delenv(name, raising=False)
        with FakeVlmServer() as vlm:
            c = Chain(dataset, str(tmp / "run"), vlm.url)
            c.front()
            c.funnel()
            c.post()
            c.deliver(str(tmp / "delivery"))
    return c


def test_snapshot_lists_them_and_no_command_reads_them(flow):
    manifest = _json(flow.rd, "source_manifest.json")
    assert manifest["skipped_episodes"] == [{"episode_index": 1, "missing": [PARQUET_1]},
                                            {"episode_index": 4, "missing": [VIDEO_4]}]
    assert flow.steps["autolabel"].doc["counts"]["total"] == 1       # 6 alone: 4 is out
    numeric = flow.steps["numeric"].doc["modules"]["timestamp_check"]
    assert numeric["episodes"]["total"] == 6
    assert "skipped_missing_source" not in numeric                   # the manifest said so
    for m in MODULES:
        assert not {1, 4} & set(latest_results(flow.rd, m)), m
    assert not os.path.exists(flow.path("skipped_episodes.json"))


def test_they_are_in_no_list_and_not_in_the_total(flow):
    lists = {n: _eps(flow.rd, n) for n in ("passed", "reject", "held", "review")}
    # 0 and 3 abstained (task_verdict), 7 is a duplicate that can be appealed
    assert lists == {"passed": [0, 3, 6], "reject": [2, 5, 7], "held": [],
                     "review": [0, 3, 7]}
    assert flow.steps["funnel"].doc["counts"]["total"] == 6
    assert flow.steps["final"].doc["counts"]["skipped"] == 2
    report = _json(flow.rd, "revisions", "r0001", "report.json")
    assert report["overview"]["counts"]["total"] == 6
    assert report["overview"]["counts"]["skipped"] == 2
    assert report["integrity"]["skipped_episodes"] == [
        {"episode_index": 1, "missing": [PARQUET_1]}, {"episode_index": 4, "missing": [VIDEO_4]}]
    with open(flow.path("revisions", "r0001", "report.md"), encoding="utf-8") as fh:
        md = fh.read()
    assert "缺源文件未质检:2 条" in md and "ep000004:缺 " + VIDEO_4 in md
    exported = _json(flow.rd, "export", "manifest.json")["episodes"]
    assert [e["episode_index"] for e in exported] == [0, 3, 6]


def test_without_a_manifest_check_finds_them_when_it_reads(dataset, tmp_path):
    os.remove(os.path.join(dataset, "data", "chunk-000", "episode_000002.parquet"))
    rd = str(tmp_path / "run")
    res = run("check", "--modules", "timestamp_check,kinematic_limits,motion_quality",
              "--input", dataset, "--run-dir", rd, "--episodes", "0-3")
    assert res.rc == 0, res.doc
    for m in ("timestamp_check", "kinematic_limits", "motion_quality"):
        entry = res.doc["modules"][m]
        assert entry["skipped_missing_source"] == [
            {"episode_index": 2, "missing": ["data/chunk-000/episode_000002.parquet"]}]
        assert entry["episodes"]["total"] == 3 and entry["error_episodes"] == []
        assert sorted(latest_results(rd, m)) == [0, 1, 3]            # no line for 2
    assert _json(rd, "skipped_episodes.json")["skipped_episodes"][0]["episode_index"] == 2
    agg = run("aggregate", "--run-dir", rd, "--phase", "funnel", "--modules",
              "timestamp_check,kinematic_limits,motion_quality", "--episodes", "0-3")
    assert agg.rc == 0, agg.doc
    assert agg.doc["counts"]["total"] == 3 and agg.doc["counts"]["skipped"] == 1
    lines = read_jsonl(os.path.join(rd, "funnel", "verdicts.jsonl"))
    assert [ln["episode_index"] for ln in lines] == [0, 1, 3]


def test_lerobot_v3_episodes_are_not_left_out(tmp_path):
    """v1 never drops a v3 episode for a missing file (its row validation rejects the
    row instead): the episodes are errors of the modules that read them - held, not
    skipped."""
    from tests.export.v3_fixture import make_mini_lerobot_v3

    dataset = make_mini_lerobot_v3(str(tmp_path / "v3"))
    video = f"videos/{WRIST}/chunk-000/file-001.mp4"
    os.remove(os.path.join(dataset, video))
    rd = str(tmp_path / "run")
    snap = run("snapshot", "--input", dataset, "--out", os.path.join(rd, "source_manifest.json"))
    assert snap.rc == 0 and "skipped_episodes" not in snap.doc
    assert any("are not left out" in e.get("msg", "") for e in snap.events)
    res = run("check", "--modules", "visual_quality,video_action_sync", "--input", dataset,
              "--run-dir", rd, "--episodes", "4,5")
    assert res.rc == 0, res.doc
    entry = res.doc["modules"]["visual_quality"]
    assert "skipped_missing_source" not in entry and entry["error_episodes"] == [4, 5]
    rec = latest_results(rd, "visual_quality")[4]
    assert [i["step"] for i in rec["error"]["incidents"]] == ["read"]
    assert "视频文件不存在" in rec["error"]["incidents"][0]["cause"]
    assert not os.path.exists(os.path.join(rd, "skipped_episodes.json"))
