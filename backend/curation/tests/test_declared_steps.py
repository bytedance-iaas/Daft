"""declared_steps (registry 8.11): the steps and the task end a dataset declares, read from its own columns."""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

pytest.importorskip("pandas")
pytest.importorskip("pyarrow")
import pandas as pd  # noqa: E402

from curation.pipeline import declared_steps as DS  # noqa: E402
from curation.pipeline.findings import derive  # noqa: E402
from curation.viz import annotations as A  # noqa: E402

FPS = 10


def _v2(root: str, *, steps: bool, modes: bool, lengths=(40, 30), task_steps: bool = False) -> str:
    """A minimal LeRobot v2.0 dataset: HABIT-like ``low_level_task_index`` + ``meta/subtasks.jsonl`` when
    ``steps``, an RSS-like ``observation.commander_state`` string column when ``modes``, Galaxea-like per-frame
    ``task_index`` (+ ``coarse_task_index``) changing inside the episode, labelled by ``meta/tasks.jsonl``, when
    ``task_steps``."""
    feats = {
        "observation.state": {"dtype": "float32", "shape": [3], "names": ["a", "b", "c"]},
        "action": {"dtype": "float32", "shape": [3], "names": ["a", "b", "c"]},
        "timestamp": {"dtype": "float32", "shape": [1]}, "frame_index": {"dtype": "int64", "shape": [1]},
        "episode_index": {"dtype": "int64", "shape": [1]}, "index": {"dtype": "int64", "shape": [1]},
        "task_index": {"dtype": "int64", "shape": [1]},
    }
    if steps:
        feats["low_level_task_index"] = {"dtype": "int64", "shape": [1]}
    if task_steps:
        feats["coarse_task_index"] = {"dtype": "int64", "shape": [1]}
    if modes:
        feats["observation.commander_state"] = {"dtype": "string", "shape": [1]}
    info = {"codebase_version": "v2.0", "robot_type": "testarm", "total_episodes": len(lengths),
            "total_frames": sum(lengths), "total_tasks": 1, "total_videos": 0, "total_chunks": 1, "chunks_size": 1000,
            "fps": FPS, "splits": {"train": f"0:{len(lengths)}"},
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
            "features": feats}
    os.makedirs(os.path.join(root, "meta"), exist_ok=True)
    os.makedirs(os.path.join(root, "data", "chunk-000"), exist_ok=True)
    json.dump(info, open(os.path.join(root, "meta", "info.json"), "w"))
    with open(os.path.join(root, "meta", "episodes.jsonl"), "w") as fh:
        for i, n in enumerate(lengths):
            fh.write(json.dumps({"episode_index": i, "tasks": ["do the thing"], "length": n}) + "\n")
    with open(os.path.join(root, "meta", "tasks.jsonl"), "w") as fh:
        for i, t in enumerate(["do the thing", "push the chair", "pull the chair"] if task_steps else ["do the thing"]):
            fh.write(json.dumps({"task_index": i, "task": t}) + "\n")
    if steps:
        with open(os.path.join(root, "meta", "subtasks.jsonl"), "w") as fh:
            for i, t in enumerate(["reach", "grasp", "place"]):
                fh.write(json.dumps({"task_index": i, "task": t}) + "\n")
    start = 0
    for i, n in enumerate(lengths):
        df = pd.DataFrame({
            "observation.state": [np.zeros(3, np.float32)] * n, "action": [np.zeros(3, np.float32)] * n,
            "timestamp": (np.arange(n) / FPS).astype(np.float32), "frame_index": np.arange(n),
            "episode_index": np.full(n, i), "index": np.arange(start, start + n), "task_index": np.zeros(n, int)})
        if steps:
            df["low_level_task_index"] = np.where(np.arange(n) < n // 3, 0, np.where(np.arange(n) < 2 * n // 3, 1, 2))
        if task_steps:
            df["task_index"] = np.where(np.arange(n) < n // 2, 1, 2)
            df["coarse_task_index"] = np.zeros(n, int)
        if modes:
            df["observation.commander_state"] = ["inference"] * (n - 8) + ["restore"] * 8
        df.to_parquet(os.path.join(root, "data", "chunk-000", f"episode_{i:06d}.parquet"), index=False)
        start += n
    return root


def test_mode_column_is_a_segment_source():
    info = {"features": {"observation.commander_state": {"dtype": "string", "shape": [1]},
                         "subtask": {"dtype": "string", "shape": [1]}}}
    src = {s.key: s for s in A.detect_sources(info, [])}
    assert src["observation.commander_state"].format == "mode_column"
    assert src["subtask"].format == "string_column"             # a step column is not a mode column
    ann = A.episode_annotations([src["observation.commander_state"]],
                                {"observation.commander_state": ["inference"] * 3 + ["restore"] * 2},
                                [0.0, 0.1, 0.2, 0.3, 0.4])
    assert [(s["label"], s["start_s"], s["end_s"]) for s in ann.tracks[0]["segments"]] == \
        [("inference", 0.0, 0.3), ("restore", 0.3, 0.5)]


def test_declared_steps_reads_habit_like_steps(tmp_path):
    root = _v2(str(tmp_path / "ds"), steps=True, modes=False)
    r = DS.DeclaredSteps.open(root)
    assert [s.format for s in r.sources] == ["index_table"]
    res = r.episode(0)
    d = res.detail
    assert d["tracks"][0]["primary"] and [s["label"] for s in d["tracks"][0]["segments"]] == ["reach", "grasp", "place"]
    assert [s["frames"] for s in d["tracks"][0]["segments"]] == [[0, 12], [13, 25], [26, 39]]
    assert "task_end" not in d
    out = derive("declared_steps", True, 1.0, d, {})
    codes = [f["code"] for f in out.findings]
    assert codes == ["step_declared"] * 3
    assert out.findings[1]["frames"] == [13, 25] and out.findings[1]["readings"]["source"] == "declared"
    assert out.findings[1]["item"] == "TASK-1"
    assert not out.unassessable and out.readings["tracks"][0]["n_steps"] == 3


def test_declared_steps_reads_a_mode_column_task_end(tmp_path):
    root = _v2(str(tmp_path / "ds"), steps=False, modes=True, lengths=(30,))
    r = DS.DeclaredSteps.open(root)
    d = r.episode(0).detail
    assert d["task_end"] == {"time_s": 2.2, "frame": 22, "mode_before": "inference", "mode_after": "restore",
                             "column": "observation.commander_state"}
    out = derive("declared_steps", True, 1.0, d, {})
    assert [f["code"] for f in out.findings] == ["task_end_declared"]
    assert out.findings[0]["time_s"] == [2.2, 2.2] and out.findings[0]["frames"] == [22, 22]
    # a task's own post-task words: "restore" not among them -> no task end, no step: TASK-1 not assessed
    d2 = r.episode(0, post_task_modes=["home"]).detail
    out2 = derive("declared_steps", True, 1.0, d2, {})
    assert not out2.findings and out2.unassessable[0]["item"] == "TASK-1"
    assert out2.readings["tracks"][0]["format"] == "mode_column" and not out2.readings["tracks"][0]["primary"]


def test_declared_steps_without_any_source(tmp_path):
    root = _v2(str(tmp_path / "ds"), steps=False, modes=False)
    r = DS.DeclaredSteps.open(root)
    assert r.reason and not r.sources
    out = derive("declared_steps", True, 1.0, r.episode(0).detail, {})
    assert not out.findings and out.unassessable == [{"item": "TASK-1", "reason": "not_applicable", "message_zh": r.reason}]


def test_declared_steps_labels_a_per_frame_task_index_from_the_v2_task_table(tmp_path):
    root = _v2(str(tmp_path / "ds"), steps=False, modes=False, task_steps=True, lengths=(20,))
    r = DS.DeclaredSteps.open(root)
    assert [s.format for s in r.sources] == ["task_index"] and r.tasks == {0: "do the thing", 1: "push the chair", 2: "pull the chair"}
    d = r.episode(0).detail
    assert [(s["label"], s["frames"]) for s in d["tracks"][0]["segments"]] == [("push the chair", [0, 9]), ("pull the chair", [10, 19])]
    out = derive("declared_steps", True, 1.0, d, {})
    assert out.findings[1]["message_zh"].startswith("第 2 步「pull the chair」")


def test_human_segments_and_the_stuck_stretches_inside_them():
    """8.13: a stuck stretch entirely inside a segment the dataset gives to the person is a protocol halt; one that
    only touches such a segment is not marked. Human segments come from a human-named track or an intervention flag."""
    detail = {"tracks": [
        {"key": "low_level_task_index", "segments": [{"frames": [0, 99], "label": "robot step"}]},
        {"key": "human_role_subtask_index", "segments": [{"frames": [0, 40], "label": "human a"}, {"frames": [41, 99], "label": "human b"}]},
        {"key": "flags", "segments": [{"frames": [60, 70], "label": "is_intervention_segment", "flags": ["is_intervention_segment"]},
                                      {"frames": [80, 85], "label": "is_pad", "flags": ["is_pad"]}]}]}
    segs = DS.human_segments(detail)
    assert [(s["track"], s["frames"]) for s in segs] == [("human_role_subtask_index", [0, 40]), ("human_role_subtask_index", [41, 99]),
                                                         ("flags", [60, 70])]
    stuck = [{"joint": 1, "freeze_start_frame": 10, "freeze_end_frame": 30},     # inside human a
             {"joint": 2, "freeze_start_frame": 35, "freeze_end_frame": 50},     # straddles a -> b: stays stuck
             {"joint": 3, "freeze_start_frame": 62, "freeze_end_frame": 68},     # inside human b (and the intervention)
             {"joint": 4}]
    assert DS.mark_declared_halts(stuck, segs) == 2
    assert stuck[0]["declared_step"] == {"track": "human_role_subtask_index", "label": "human a", "frames": [0, 40]}
    assert "declared_step" not in stuck[1] and stuck[2]["declared_step"]["label"] == "human b" and "declared_step" not in stuck[3]
    assert DS.human_segments({"tracks": [{"key": "low_level_task_index", "segments": [{"frames": [0, 9], "label": "x"}]}]}) == []
