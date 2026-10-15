"""Data the dataset declares but an episode lacks, or holds against the declaration (design doc 29, D95-D97).

v1's readers stop at it - a declared column missing, a frame without a value or of another width, a parquet that is
not the episode table's length, an mcap without its action topic - and data integrity used to say "execution error",
held for a retry that never helps; a LeRobot v2 episode without a video was left out (D40). Now each is a finding of
data integrity, and an episode without a source file is judged when the task selected data integrity (D97).
"""
from __future__ import annotations

import json
import os
import shutil

import pandas as pd
import pytest

from curation.extensions.integrity import declared as DC
from curation.pipeline.records import latest_results

from . import integrity_samples as S
from .test_integrity import check, codes, results

MOD = "data_integrity"
NUMERIC = "timestamp_check,kinematic_limits,motion_quality"
WRIST_0 = "videos/chunk-000/observation.images.wrist/episode_000000.mp4"


def _json(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return json.load(fh)


def c2(rec: dict) -> list[tuple[str, str, str]]:
    """The record's findings (C2 2.0): code, checklist item, level."""
    return [(f["code"], f["item"], f["severity"]) for f in rec["findings"]]


# ---------------------------------------------------------------- LeRobot v2


def test_lerobot_v2_what_was_an_execution_error_is_a_finding(cli, dataset, tmp_path):
    S.declare_lerobot_v2(dataset)
    rd = str(tmp_path / "run")
    doc = check(cli, dataset, rd)
    assert doc["episodes"] == {"total": 8, "ok": 8, "error": 0}        # none held, none left out
    assert "skipped_missing_source" not in doc
    recs = results(rd)
    assert {e: codes(r) for e, r in recs.items()} == {
        0: [("reject", "video_missing")],
        1: [("reject", "action_missing")],
        2: [("reject", "column_missing")],
        3: [],
        4: [("reject", "values_invalid")],
        5: [("reject", "data_missing")],
        6: [("reject", "values_invalid")],
        7: [("reject", "length_mismatch"), ("suspect", "count_mismatch"), ("suspect", "count_mismatch")]}
    reasons = {e: r["details"]["reason"] for e, r in recs.items()}
    assert reasons[0].startswith("wrist 相机的视频文件不存在：" + WRIST_0)
    assert reasons[1].startswith("data/chunk-000/episode_000001.parquet 里没有 action 列（info.json 声明了）")
    assert reasons[2].startswith("data/chunk-000/episode_000002.parquet 里没有 observation.state 列（info.json 声明了）")
    assert reasons[4].startswith("observation.state 有 10 帧为空（帧 71–80）")
    assert reasons[5].startswith("数据文件不存在：data/chunk-000/episode_000005.parquet")
    assert reasons[6].startswith("observation.state 每帧 6 维，info.json 声明 7 维")
    assert reasons[7].startswith("数据有 60 帧，episode 表记 70 帧")
    # the registry's codes and items (8.1)
    assert c2(recs[0]) == [("video_missing", "FILE-1", "high")]
    assert c2(recs[1]) == [("action_missing", "STRM-1", "high")]
    assert c2(recs[2]) == [("column_missing", "STRM-1", "high")]
    assert c2(recs[5]) == [("data_missing", "FILE-1", "high")]
    assert c2(recs[6]) == [("values_invalid", "FILE-6", "high")]
    assert c2(recs[7])[0] == ("length_mismatch", "FILE-5", "high")
    # an episode without a source file is judged on it alone: nothing else of it is read
    assert recs[0]["details"]["files"] == [] and recs[5]["details"]["files"] == []


def test_lerobot_v2_the_other_modules_skip_them_and_nothing_is_held(cli, dataset, tmp_path):
    S.declare_lerobot_v2(dataset)
    rd = str(tmp_path / "run")
    check(cli, dataset, rd)
    res = cli("check", "--modules", NUMERIC, "--input", dataset, "--run-dir", rd, "--episodes", "0-7")
    assert res.rc == 0, res.doc
    for m in NUMERIC.split(","):
        entry = res.doc["modules"][m]
        assert entry["episodes"]["error"] == 0 and entry["skipped_reasons"] == {"integrity_rejected": 7}, m
        assert "skipped_missing_source" not in entry
    agg = cli("aggregate", "--run-dir", rd, "--revision", "1", "--episodes", "0-7",
              "--modules", f"{MOD},{NUMERIC}")
    assert agg.rc == 0, agg.doc
    assert agg.doc["counts"]["total"] == 8 and agg.doc["counts"]["held"] == 0
    assert agg.doc["counts"]["skipped"] == 0
    reject = {e["episode_index"]: e for e in _json(rd, "revisions", "r0001", "reject.json")["episodes"]}
    assert {0, 1, 2, 4, 5, 6, 7} <= set(reject)
    first = reject[1]["reasons"][0]
    assert (first["module"], first["code"], first["item"], first["appealable"]) == (MOD, "action_missing", "STRM-1", False)


def test_without_the_module_they_are_still_execution_errors(cli, dataset, tmp_path):
    """D95 is data integrity's: the other modules record the read error as before."""
    S.declare_lerobot_v2(dataset)
    rd = str(tmp_path / "run")
    res = cli("check", "--modules", "timestamp_check", "--input", dataset, "--run-dir", rd, "--episodes", "1,4")
    assert res.rc == 0, res.doc
    assert res.doc["modules"]["timestamp_check"]["error_episodes"] == [1, 4]


# ---------------------------------------------------------------- D97: missing source files and the manifest


@pytest.fixture
def missing_video(dataset, tmp_path):
    """Episode 0 without its wrist video, and the run directory's source manifest (as the Daemon lays it out)."""
    os.remove(os.path.join(dataset, WRIST_0))
    return dataset


def _snapshot(cli, dataset: str, rd: str) -> list[str]:
    out = os.path.join(rd, "source_manifest.json")
    res = cli("snapshot", "--input", dataset, "--out", out)
    assert res.rc == 0, res.doc
    assert res.doc["skipped_episodes"] == [{"episode_index": 0, "missing": [WRIST_0]}]    # the fact, as before
    return ["--source-manifest", out]


def test_with_data_integrity_an_episode_without_its_video_is_judged(cli, missing_video, tmp_path):
    rd = str(tmp_path / "run")
    manifest = _snapshot(cli, missing_video, rd)
    doc = check(cli, missing_video, rd, "0-7", *manifest)
    assert doc["episodes"]["total"] == 8 and doc["findings"] == {"video_missing": 1}
    res = cli("check", "--modules", NUMERIC, "--input", missing_video, "--run-dir", rd, "--episodes", "0-7", *manifest)
    assert res.rc == 0, res.doc
    entry = res.doc["modules"]["timestamp_check"]
    assert entry["episodes"]["total"] == 8 and entry["skipped_reasons"] == {"integrity_rejected": 1}
    assert latest_results(rd, "timestamp_check")[0]["details"]["skipped"] == "integrity_rejected"
    agg = cli("aggregate", "--run-dir", rd, "--revision", "1", "--episodes", "0-7")
    assert agg.rc == 0, agg.doc
    assert agg.doc["counts"]["total"] == 8 and agg.doc["counts"]["skipped"] == 0
    reject = {e["episode_index"]: e for e in _json(rd, "revisions", "r0001", "reject.json")["episodes"]}
    assert reject[0]["reasons"] == [{"module": MOD, "kind": "finding", "code": "video_missing", "item": "FILE-1",
                                     "appealable": False,
                                     "text": "wrist 相机的视频文件不存在：" + WRIST_0}]
    rep = cli("report", "--run-dir", rd, "--revision", "1")
    assert rep.rc == 0, rep.doc
    report = _json(rd, "revisions", "r0001", "report.json")
    assert report["overview"]["counts"]["skipped"] == 0
    assert report["integrity"]["skipped_episodes"] == []


def test_without_data_integrity_it_is_left_out_as_before(cli, missing_video, tmp_path):
    rd = str(tmp_path / "run")
    manifest = _snapshot(cli, missing_video, rd)
    res = cli("check", "--modules", NUMERIC, "--input", missing_video, "--run-dir", rd, "--episodes", "0-7", *manifest)
    assert res.rc == 0, res.doc
    assert res.doc["modules"]["timestamp_check"]["episodes"]["total"] == 7
    agg = cli("aggregate", "--run-dir", rd, "--revision", "1", "--episodes", "0-7")
    assert agg.doc["counts"]["total"] == 7 and agg.doc["counts"]["skipped"] == 1


def test_run_json_says_what_a_run_does_with_them(cli, missing_video, tmp_path):
    """The Daemon freezes ``missing_sources`` into run.json: ``judged`` keeps the episode in every stage's selection
    (a pipeline stage gets it before data integrity has judged it, then skips it, D92); ``left_out`` and an older
    run.json leave it out up front. Kept and not judged yet, the reader finds the file missing: left out then (D40)."""
    for word, kept in (("judged", True), ("left_out", False), (None, False)):
        rd = str(tmp_path / f"run-{word}")
        manifest = _snapshot(cli, missing_video, rd)
        doc = {"schema": "curator.run/1"} if word is None else {"schema": "curator.run/1", "missing_sources": word}
        with open(os.path.join(rd, "run.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        res = cli("check", "--modules", "timestamp_check", "--input", missing_video, "--run-dir", rd,
                  "--episodes", "0-7", *manifest)
        assert res.rc == 0, res.doc
        entry = res.doc["modules"]["timestamp_check"]
        assert entry["episodes"]["total"] == 7 and entry["episodes"]["error"] == 0, word
        assert ("skipped_missing_source" in entry) is kept, word


def test_a_missing_file_that_came_back_is_a_changed_source(cli, missing_video, mini_dataset, tmp_path):
    rd = str(tmp_path / "run")
    manifest = _snapshot(cli, missing_video, rd)
    shutil.copyfile(os.path.join(mini_dataset, WRIST_0), os.path.join(missing_video, WRIST_0))
    res = cli("check", "--modules", MOD, "--input", missing_video, "--run-dir", rd, "--episodes", "0-7", *manifest)
    assert res.rc == 6, res.doc
    assert res.doc["error"]["details"]["key"] == WRIST_0 and res.doc["error"]["details"]["change"] == "added"


# ---------------------------------------------------------------- LeRobot v3


def test_lerobot_v3_one_bad_episode_is_its_own(cli, tmp_path):
    """v1 reads a v3 data file in one pass: one episode's empty frames used to fail all three of its file. Now that
    one is rejected, its neighbours pass in data integrity and are read by the other stages."""
    from tests.cli.v3_fixture import make_mini_lerobot_v3

    ds = make_mini_lerobot_v3(str(tmp_path / "v3"))
    S.declare_lerobot_v3(ds)
    rd = str(tmp_path / "run")
    check(cli, ds, rd, "0-5")
    recs = results(rd)
    assert {e: [c for _, c in codes(r)] for e, r in recs.items()} == {
        0: [], 1: ["values_invalid"], 2: [], 3: ["column_missing"], 4: ["video_missing"], 5: ["video_missing"]}
    assert recs[1]["details"]["reason"].startswith("observation.state 有 6 帧为空（帧 3–8）")
    assert recs[3]["details"]["reason"].startswith(
        "data/chunk-000/file-001.parquet 里没有 observation.state 列（info.json 声明了）")
    assert recs[4]["details"]["reason"].startswith(
        "wrist 相机的视频文件不存在：videos/observation.images.wrist/chunk-000/file-001.mp4")
    res = cli("check", "--modules", NUMERIC, "--input", ds, "--run-dir", rd, "--episodes", "0-5")
    assert res.rc == 0, res.doc
    entry = res.doc["modules"]["timestamp_check"]
    assert entry["episodes"]["error"] == 0 and entry["skipped_reasons"] == {"integrity_rejected": 4}
    judged = {e for e, r in latest_results(rd, "timestamp_check").items() if not r["details"].get("skipped")}
    assert judged == {0, 2}


# ---------------------------------------------------------------- mcap


@pytest.fixture(scope="module")
def mini_mcap(tmp_path_factory) -> str:
    pytest.importorskip("mcap_ros2", reason="mcap-ros2-support writes the fixture")
    from parity.fixtures import make_mini_mcap

    return make_mini_mcap(str(tmp_path_factory.mktemp("mcap") / "mini_mcap"))


def test_mcap_without_its_action_topic(cli, mini_mcap, tmp_path):
    ds = str(tmp_path / "mcap")
    shutil.copytree(mini_mcap, ds)
    S.declare_mcap(ds)
    rd = str(tmp_path / "run")
    doc = check(cli, ds, rd)
    assert doc["episodes"]["error"] == 0
    recs = results(rd)
    assert codes(recs[4]) == [("reject", "action_missing")]        # not also a suspect missing /action
    assert recs[4]["details"]["reason"].startswith("缺动作 topic /action（这条 mcap 里没有它）")
    assert codes(recs[2]) == [("suspect", "stream_missing")]
    assert c2(recs[4]) == [("action_missing", "STRM-1", "high")]


def test_mcap_a_topic_the_mapping_names(cli, mini_mcap, tmp_path):
    """D96: with a confirmed mapping, a camera it names that no episode has is a suspect in each - the other episodes
    need not have it."""
    ds = str(tmp_path / "mcap")
    shutil.copytree(mini_mcap, ds)
    for ep in range(8):
        S.drop_topic(os.path.join(ds, f"episode_{ep}.mcap"), "/observation.images.wrist")
    mapping = {"action": "/action", "state": "/observation.state", "task": "/task",
               "video_prefix": "/observation.images.",
               "video_topics": ["/observation.images.exterior", "/observation.images.wrist"]}
    rd = str(tmp_path / "plain")
    assert check(cli, ds, rd)["findings"] == {}                     # the naming convention is no declaration
    rd = str(tmp_path / "mapped")
    doc = check(cli, ds, rd, "0-7", "--set", "ingest.mcap_mapping=" + json.dumps(mapping))
    assert doc["findings"] == {"stream_missing": 8}
    rec = results(rd)[3]
    assert codes(rec) == [("suspect", "stream_missing")]
    assert rec["details"]["reason"] == "需要人工裁决：缺映射里写明的 topic /observation.images.wrist"


# ---------------------------------------------------------------- the declaration, unit by unit


INFO = {"features": {"action": {"dtype": "float32", "shape": [3]},
                     "observation.state": {"dtype": "float32", "shape": [2]},
                     "timestamp": {"dtype": "float32", "shape": [1]},
                     "observation.images.top": {"dtype": "video", "shape": [8, 8, 3]}}}


def frames(**cols) -> pd.DataFrame:
    return pd.DataFrame(cols)


def test_frame_problems():
    ok = frames(action=[[0.0, 1.0, 2.0]] * 4, **{"observation.state": [[0.0, 1.0]] * 4},
                timestamp=[0.0, 0.1, 0.2, 0.3])
    assert DC.frame_problems(ok, INFO, 4) == []
    assert [p["code"] for p in DC.frame_problems(ok, INFO, 5)] == ["length_mismatch"]
    no_state = ok.drop(columns=["observation.state"])
    assert [(p["code"], p["args"]) for p in DC.frame_problems(no_state, INFO, 4)] == [
        ("column_missing", {"column": "observation.state"})]
    ragged = ok.copy()
    ragged["observation.state"] = [[0.0, 1.0], [0.0], [0.0], [0.0, 1.0]]
    assert DC.frame_problems(ragged, INFO, 4)[0]["message"] == "observation.state 各帧维度不一：1 维 2 帧、2 维 2 帧"
    empty = ok.copy()
    empty["action"] = [[0.0, 1.0, 2.0], None, None, [0.0, 1.0, 2.0]]
    assert DC.frame_problems(empty, INFO, 4)[0]["message"] == "action 有 2 帧为空（帧 1–2）"
    wide = ok.copy()
    wide["action"] = [[0.0, 1.0, 2.0, 3.0]] * 4
    assert DC.frame_problems(wide, INFO, 4)[0]["message"] == "action 每帧 4 维，info.json 声明 3 维"
    no_time = ok.copy()
    no_time["timestamp"] = [0.0, float("nan"), 0.2, 0.3]
    assert [p["code"] for p in DC.frame_problems(no_time, INFO, 4)] == ["timestamps_invalid"]
    # the videos are not parquet columns; action and timestamp are read whatever info.json says
    assert [p["args"]["column"] for p in DC.missing_columns(["observation.state"], INFO, "")] == ["action", "timestamp"]
    assert DC.frames_text([1, 2, 3, 7, 9, 10, 20]) == "帧 1–3、7、9–10…"


def test_mcap_problems_from_the_readers_words():
    from curation.ingest.lerobot_reader import NotADatasetError

    def err(items: str) -> NotADatasetError:
        return NotADatasetError(f"'/d': mcap 里找不到必需 topic({items})。\n  实际见到的 topic: ['/x']")

    assert [(p["code"], p["args"]) for p in DC.mcap_problems(err("动作来源 '/action'"))] == [
        ("action_missing", {"topic": "/action"})]
    assert [p["code"] for p in DC.mcap_problems(err("动作来源 '/action', 视频 '/observation.images.'*"))] == [
        "action_missing", "video_missing"]
    # the mapping is wrong (a field path, no action at all): not this episode's data
    assert DC.mcap_problems(err("动作来源 '/a'(topic 存在但按 fields='x' 没取出任何数值,字段路径/消息形状不对)")) == []
    assert DC.mcap_problems(err("动作 '/action'")) == []
    assert DC.mcap_problems(ValueError("something else")) == []
