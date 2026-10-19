"""Streams split over several columns (Galaxea's action.left_arm …) are read composed (ingest.split_features)."""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

from curation.ingest import split_features as SF

pytest.importorskip("pandas")
pytest.importorskip("pyarrow")
import pandas as pd  # noqa: E402

from curation.ingest import lerobot_reader as lr  # noqa: E402

# Galaxea R1 Lite, as exported (meta/info.json of galaxea_push_in_chairs_20250614_001): no top-level action / state
GALAXEA = {
    "observation.images.head_rgb": {"dtype": "video", "shape": [720, 1280, 3], "names": ["height", "width", "channels"]},
    "observation.state.left_arm": {"dtype": "float64", "shape": [6], "names": [f"/hdas/feedback_arm_left.position[{i}]" for i in range(6)]},
    "observation.state.left_arm.velocities": {"dtype": "float64", "shape": [6], "names": [f"/hdas/feedback_arm_left.velocity[{i}]" for i in range(6)]},
    "observation.state.right_arm": {"dtype": "float64", "shape": [6], "names": [f"/hdas/feedback_arm_right.position[{i}]" for i in range(6)]},
    "observation.state.chassis.imu": {"dtype": "float64", "shape": [10], "names": None},
    "observation.state.chassis": {"dtype": "float64", "shape": [6], "names": [f"/hdas/feedback_chassis.position[{i}]" for i in range(6)]},
    "observation.state.torso": {"dtype": "float64", "shape": [4], "names": [f"/hdas/feedback_torso.position[{i}]" for i in range(4)]},
    "observation.state.left_gripper": {"dtype": "float64", "shape": [1], "names": ["/hdas/feedback_gripper_left.position[0]"]},
    "observation.state.right_gripper": {"dtype": "float64", "shape": [1], "names": ["/hdas/feedback_gripper_right.position[0]"]},
    "observation.state.left_ee_pose": {"dtype": "float64", "shape": [7], "names": [
        "/motion_control/pose_ee_arm_left.pose.position.x", "/motion_control/pose_ee_arm_left.pose.position.y",
        "/motion_control/pose_ee_arm_left.pose.position.z", "/motion_control/pose_ee_arm_left.pose.orientation.x",
        "/motion_control/pose_ee_arm_left.pose.orientation.y", "/motion_control/pose_ee_arm_left.pose.orientation.z",
        "/motion_control/pose_ee_arm_left.pose.orientation.w"]},
    "action.left_gripper": {"dtype": "float64", "shape": [1], "names": ["/motion_target/target_position_gripper_left.position[0]"]},
    "action.right_gripper": {"dtype": "float64", "shape": [1], "names": ["/motion_target/target_position_gripper_right.position[0]"]},
    "action.chassis.velocities": {"dtype": "float64", "shape": [3], "names": ["/motion_target/target_speed_chassis.twist.linear.x", "y", "z"]},
    "action.torso.velocities": {"dtype": "float64", "shape": [6], "names": None},
    "action.left_arm": {"dtype": "float64", "shape": [6], "names": [f"/motion_target/target_joint_state_arm_left.position[{i}]" for i in range(6)]},
    "action.right_arm": {"dtype": "float64", "shape": [6], "names": [f"/motion_target/target_joint_state_arm_right.position[{i}]" for i in range(6)]},
    "timestamp": {"dtype": "float32", "shape": [1], "names": None},
    "frame_index": {"dtype": "int64", "shape": [1], "names": None},
    "task_index": {"dtype": "int64", "shape": [1], "names": None},
}


def test_galaxea_composes_action_and_state_aligned_by_suffix():
    comp = SF.composition(GALAXEA)
    a, s = comp["action"], comp["observation.state"]
    # the shared suffixes first, in the action's order; the rates left out
    assert a["paired"] == ["left_gripper", "right_gripper", "left_arm", "right_arm"]
    assert a["columns"] == ["action.left_gripper", "action.right_gripper", "action.left_arm", "action.right_arm"]
    assert a["left_out"] == ["action.chassis.velocities", "action.torso.velocities"] and a["dim"] == 14
    assert s["columns"][:4] == ["observation.state.left_gripper", "observation.state.right_gripper",
                                "observation.state.left_arm", "observation.state.right_arm"]
    assert s["columns"][4:] == ["observation.state.chassis", "observation.state.torso"]
    assert s["left_out"] == ["observation.state.left_arm.velocities", "observation.state.chassis.imu",
                             "observation.state.left_ee_pose"] and s["dim"] == 24
    # names: the suffix plus the raw name without its topic directory; unnamed dims are indexed
    assert a["names"][:3] == ["left_gripper.target_position_gripper_left.position[0]",
                              "right_gripper.target_position_gripper_right.position[0]",
                              "left_arm.target_joint_state_arm_left.position[0]"]
    assert s["names"][2] == "left_arm.feedback_arm_left.position[0]"
    assert s["names"][14] == "chassis.feedback_chassis.position[0]" and a["names"][7] == "left_arm.target_joint_state_arm_left.position[5]"
    assert s["names"][-1] == "torso.feedback_torso.position[3]"
    lines = SF.describe(comp)
    assert lines[0].startswith("action is composed from 4 columns (action.left_gripper, ") and "left out as rates" in lines[0]
    # the state's pose columns are the arm joints again: out, and the reading stays joint-space
    from curation.ingest.dataset_semantics import _names_look_ee
    assert not _names_look_ee([n.lower() for n in s["names"]])


def test_a_dataset_with_the_stream_declared_is_not_composed():
    feats = {"action": {"dtype": "float32", "shape": [7], "names": None},
             "action.extra": {"dtype": "float32", "shape": [2], "names": None},
             "observation.state.arm": {"dtype": "float32", "shape": [7], "names": None}}
    comp = SF.composition(feats)
    assert "action" not in comp and comp["observation.state"]["columns"] == ["observation.state.arm"]
    assert comp["observation.state"]["paired"] == [] and comp["observation.state"]["names"] == [f"arm[{i}]" for i in range(7)]
    assert SF.composition({"action": {"dtype": "float32", "shape": [7]}}) == {}
    assert SF.composition({"action.velocities": {"dtype": "float32", "shape": [7]}}) == {}     # nothing position-like


def test_info_validation_accepts_a_split_action():
    from curation.ingest.validate import IngestValidationError, validate_info

    base = {"codebase_version": "v2.1", "fps": 15, "data_path": "d", "chunks_size": 1000, "robot_type": "r1lite"}
    validate_info({**base, "features": GALAXEA}, "galaxea")
    with pytest.raises(IngestValidationError, match="没有 'action'"):
        validate_info({**base, "features": {"action.torso.velocities": GALAXEA["action.torso.velocities"]}}, "x")


def test_compose_gives_the_readers_info_the_virtual_features_once():
    info = {"features": dict(GALAXEA)}
    comp = SF.compose(info)
    assert info["features"]["action"] == {"dtype": "float32", "shape": [14], "names": comp["action"]["names"],
                                          "composed_from": comp["action"]["columns"]}
    assert info["features"]["observation.state"]["shape"] == [24]
    assert SF.compose(info) is comp and lr._infer_action_space(info) == "joint" and lr._infer_proprio_space(info) == "joint"
    # the names reach the semantics layer's reading of the action
    assert lr._action_names_list(info)[2] == "left_arm.target_joint_state_arm_left.position[0]"


def _split_v2(root: str, lengths=(12, 9), fps=15) -> str:
    """A LeRobot v2.1 dataset with the action and the state split Galaxea-style, no top-level stream."""
    feats = {k: v for k, v in GALAXEA.items() if k not in ("observation.images.head_rgb",)}
    feats["episode_index"] = {"dtype": "int64", "shape": [1], "names": None}
    feats["index"] = {"dtype": "int64", "shape": [1], "names": None}
    info = {"codebase_version": "v2.1", "robot_type": "r1lite", "total_episodes": len(lengths), "total_frames": sum(lengths),
            "total_tasks": 1, "total_videos": 0, "total_chunks": 1, "chunks_size": 1000, "fps": fps,
            "splits": {"train": f"0:{len(lengths)}"},
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4", "features": feats}
    os.makedirs(os.path.join(root, "meta"), exist_ok=True)
    os.makedirs(os.path.join(root, "data", "chunk-000"), exist_ok=True)
    json.dump(info, open(os.path.join(root, "meta", "info.json"), "w"))
    with open(os.path.join(root, "meta", "episodes.jsonl"), "w") as fh:
        for i, n in enumerate(lengths):
            fh.write(json.dumps({"episode_index": i, "tasks": ["push the chair"], "length": n}) + "\n")
    with open(os.path.join(root, "meta", "tasks.jsonl"), "w") as fh:
        fh.write(json.dumps({"task_index": 0, "task": "push the chair"}) + "\n")
    start = 0
    for i, n in enumerate(lengths):
        cols = {"timestamp": (np.arange(n) / fps).astype(np.float32), "frame_index": np.arange(n),
                "episode_index": np.full(n, i), "index": np.arange(start, start + n), "task_index": np.zeros(n, int)}
        for k, f in feats.items():
            if k.startswith("action") or k.startswith("observation.state"):
                w = f["shape"][0]
                base = float(abs(hash(k)) % 7)
                cols[k] = [np.full(w, base, np.float64) + t * 0.01 for t in range(n)]
        pd.DataFrame(cols).to_parquet(os.path.join(root, "data", "chunk-000", f"episode_{i:06d}.parquet"), index=False)
        start += n
    return root


def test_the_reader_composes_the_rows(tmp_path):
    root = _split_v2(str(tmp_path / "ds"))
    rows = lr.read_lerobot_rows(root, validate=True)
    assert len(rows) == 2
    a, s = rows[0]["action"], rows[0]["proprio_state"]
    assert a.shape == (12, 14) and a.dtype == np.float32 and s.shape == (12, 24)
    # the pair left_arm ↔ left_arm sits at the same index on both sides: columns 2..7
    info = lr._load_info(root)
    assert info["features"]["action"]["composed_from"][2] == "action.left_arm"
    assert info["features"]["observation.state"]["composed_from"][2] == "observation.state.left_arm"
    assert np.allclose(a[:, 2], float(abs(hash("action.left_arm")) % 7) + np.arange(12) * 0.01)
    assert np.allclose(s[:, 2], float(abs(hash("observation.state.left_arm")) % 7) + np.arange(12) * 0.01)
    # the rates stayed out: no chassis velocity among the action's columns
    assert "chassis" not in " ".join(info["features"]["action"]["names"])
    # the file on disk is as it was
    assert "action" not in json.load(open(os.path.join(root, "meta", "info.json")))["features"]
    # the meta path (what the pipeline's planning reads) votes the semantics on the composed sample too
    metas = lr.read_lerobot_meta(root)
    assert [m["episode_index"] for m in metas] == [0, 1] and "action_space" in metas[0]


def test_the_semantics_preflight_reads_a_command_against_a_wider_readings_leading_columns():
    """An unregistered robot (no joint limits, no dof): a joint position command is the reading's target, column
    by column. Galaxea's composed reading is wider than its command (24 vs 14) with the paired columns first; the
    preflight compares the leading columns instead of giving up (what left ACT-4 / ACT-8 unassessed)."""
    from curation.ingest.semantics_preflight import preflight

    rng = np.random.default_rng(0)
    t = np.linspace(0, 6, 300)
    state = np.stack([np.sin(t * (1 + 0.3 * j)) * 0.8 for j in range(14)] + [rng.normal(size=300) for _ in range(10)], axis=1)
    action = np.roll(state[:, :14], -1, axis=0) + rng.normal(scale=0.005, size=(300, 14))
    action[:, :2] = (action[:, :2] > 0).astype(float)                    # two grippers, open / closed
    rows = [{"action": action, "proprio_state": state, "fps": 15.0}]
    pf = preflight(rows, names_hint="joint", gripper_hint=(0, 1), paired_prefix=True)
    assert pf["status"] == "confident" and pf["hypothesis"] == "joint_absolute"
    assert pf["action_space"] == "joint" and pf["control_mode"] == "absolute"
    # a reading narrower than the command still says nothing
    pf2 = preflight([{"action": action, "proprio_state": state[:, :10], "fps": 15.0}], names_hint="joint", gripper_hint=(0, 1),
                    paired_prefix=True)
    assert pf2["scores"]["joint_absolute"] <= 0.1
    # and a wider reading that nobody composed says nothing either: only split_features guarantees the paired columns
    # come first (2026-10-19: RH20T's 8-column pose command against its 15-column reading was read as joint space)
    pf3 = preflight(rows, names_hint="joint", gripper_hint=(0, 1))
    assert pf3["scores"]["joint_absolute"] <= 0.1


def test_dimensions_named_as_rates_inside_a_kept_column_stay_out():
    """Galaxea's observation.state.chassis is six wide: feedback_chassis.position[0..2] and
    feedback_chassis.velocity[0..2] - the velocities never rest and hid every pause (2026-10-18)."""
    feats = dict(GALAXEA)
    feats["observation.state.chassis"] = {"dtype": "float64", "shape": [6], "names": [
        f"/hdas/feedback_chassis.position[{i}]" for i in range(3)] + [f"/hdas/feedback_chassis.velocity[{i}]" for i in range(3)]}
    comp = SF.composition(feats)
    s = comp["observation.state"]
    assert s["keep"]["observation.state.chassis"] == [0, 1, 2] and s["dim"] == 24 - 3
    assert s["dropped"] == [f"chassis.feedback_chassis.velocity[{i}]" for i in range(3)]
    assert "chassis.feedback_chassis.velocity[0]" not in s["names"] and "chassis.feedback_chassis.position[2]" in s["names"]
    assert any("dimensions named as rates left out: chassis.feedback_chassis.velocity[0]" in line for line in SF.describe(comp))
    # the array of a frame follows: the three velocity dimensions are not stacked
    import pandas as pd
    info = {"features": feats}
    SF.compose(info)
    n = 4
    frame = pd.DataFrame({k: [np.arange(f["shape"][0], dtype=np.float64) + 10 * i for i in range(n)]
                          for k, f in feats.items() if k.startswith("observation.state.") and isinstance(f.get("shape"), list)})
    arr = SF.stream_array(frame, info, "observation.state")
    assert arr.shape == (n, 24 - 3)
    # a column whose every dimension is a rate is left out whole
    feats2 = {k: f for k, f in GALAXEA.items()}
    feats2["observation.state.chassis"] = {"dtype": "float64", "shape": [6], "names": [f"/hdas/feedback_chassis.velocity[{i}]" for i in range(6)]}
    comp2 = SF.composition(feats2)
    assert "observation.state.chassis" in comp2["observation.state"]["left_out"]
