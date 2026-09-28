"""The trajectory against the dataset's own record (design doc 12 §8.7, D-E16; F5.15).

tools/parity's mini dataset records Franka joint angles: ``action`` is the joint track, ``observation.state``
the same track one frame late. The uploaded trajectory here is the Panda kinematics of ``action`` (the
flange, or a TCP 0.1034 m in front of it), so the joints record read from ``action`` agrees, the one read
from ``observation.state`` shows a one-frame time offset, a TCP upload agrees once the mapping declares the
TCP and is a declared-constant mismatch when the declaration is wrong, and a relative upload is compared
as relative motion. The mcap twin of the dataset gives the same readings. On the DEMO dataset2 the joints
record finds the injected drift, jitter and 30 deg rotation that the pose columns (the file's own source)
cannot. The comparison is reported only: a record's pass / reject never depends on it.
"""
from __future__ import annotations

import copy
import json
import os

import numpy as np
import pytest

from .pipeline import results, run
from .test_eef_check import CAM, EEF, VLM, _entry, _files, fake_vlm

MAPPING = {"schema_version": "eef-mapping/1.1",
           "record": {"joints": {"key": "action", "units": "rad", "robot": "franka_panda",
                                 "reference_frame": "robot_base"}}}
TCP = [0.0, 0.0, 0.1034]


def _fk(ep: int) -> np.ndarray:
    from parity import fixtures as F

    from curation.extensions.eef_consistency import robots

    src = F.DUPLICATE_OF.get(ep, ep)
    return robots.get("franka_panda").fk(F._joint_track(F.EPISODES[src][0], src).astype(float))


def _pose(T: np.ndarray, frame_id: str) -> dict:
    from scipy.spatial.transform import Rotation

    return {"pose_type": "absolute", "frame_id": frame_id, "reference_frame": "robot_base",
            "position_m": [float(x) for x in T[:3, 3]],
            "quaternion_xyzw": [float(x) for x in Rotation.from_matrix(T[:3, :3]).as_quat()], "relative_to": None,
            "provenance": {"source": "test", "method": "panda kinematics", "assurance": "declared"}}


def _bundle(tmp_path, *, frame: str = "panda_link8", tcp=None, relative: bool = False, name: str = "t.json") -> str:
    """trajectory.json for episodes 0-3: the 2D fixture plus 3D poses from the kinematics of ``action``."""
    entries = []
    for ep in range(4):
        e = _entry(ep)
        T = _fk(ep)
        if tcp is not None:
            off = np.eye(4)
            off[:3, 3] = tcp
            T = T @ off
        e["sample"]["eef_frame"], e["sample"]["reference_frame"] = frame, "robot_base"
        for i, fr in enumerate(e["frames"]):
            if relative:
                rel = np.linalg.inv(T[0]) @ T[i]
                pose = _pose(rel, frame)
                pose.update(pose_type="relative_to_start", reference_frame="eef_at_start",
                            relative_to={"frame_index": 0, "convention": "inverse_T0_times_Tt", "anchor_pose": None})
                fr["eef"] = pose
            else:
                fr["eef"] = _pose(T[i], frame)
        entries.append(e)
    doc = {"schema_version": "eef-video/1.0.0", "container": "trajectory-bundle/1.0",
           "dataset": {"id": "mini", "lerobot_codebase_version": "v2.1", "fps": 15.0, "episode_count": 8},
           "media_uri_base": "lerobot_root", "samples": entries}
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return str(path)


def _mapping(tmp_path, doc: dict, name: str = "record.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return str(path)


def _compare(traj: str, mapping: str, reader, ep: int = 1) -> dict:
    from curation.extensions.eef_consistency import load, profile
    from curation.extensions.eef_consistency import record as RC

    r = load.load_bundle(traj, episodes=[ep], check_media=False)
    assert r.ok, r.errors[:2]
    return RC.strip(RC.compare_episode(r.samples[ep], RC.load_mapping(mapping), reader, profile.load("demo")))


def _lerobot(root):
    from curation.extensions.eef_consistency import record as RC

    return RC.LeRobotRecords(root)


# ------------------------------------------------------------------------------------ the robot and the mapping


def test_panda_kinematics_at_zero_and_named_frames():
    from curation.extensions.eef_consistency import record as RC
    from curation.extensions.eef_consistency import robots

    T = robots.get("franka_panda").fk(np.zeros((1, 7)))[0]
    assert np.allclose(T[:3, 3], [0.088, 0.0, 0.926], atol=1e-9)          # the flange above the base, pointing down
    m = RC.parse_mapping({"schema_version": "eef-mapping/1.1", "record": {"joints": MAPPING["record"]["joints"]}})
    E = m.transform("panda_link8", "panda_hand_tcp", "franka_panda")         # through the robot's named frames
    assert np.allclose(E[:3, 3], [0, 0, 0.1034]) and np.isclose(np.degrees(np.arctan2(E[1, 0], E[0, 0])), -45.0)
    assert m.transform("panda_link8", "somewhere", "franka_panda") is None


@pytest.mark.parametrize("record, words", [
    ({"joints": {"key": "a", "topic": "/a", "units": "rad", "robot": "franka_panda", "reference_frame": "b"}},
     "exactly one"),
    ({"joints": {"key": "a", "units": "rad", "robot": "ur5", "reference_frame": "b"}}, "is not built in"),
    ({"joints": {"key": "a", "units": "degrees", "robot": "franka_panda", "reference_frame": "b"}}, "rad or deg"),
    ({"pose": {"key": "p", "units": {"position": "m", "angle": "rad"}, "frame_id": "f", "reference_frame": "b"}},
     "layout must be one of"),
    ({"pose": {"key": "p", "layout": "xyz_rpy_xyz_extrinsic", "units": {"position": "cm", "angle": "rad"},
               "frame_id": "f", "reference_frame": "b"}}, "m or mm"),
    ({}, "needs pose, joints or both"),
])
def test_the_mapping_is_written_out_never_guessed(record, words):
    from curation.extensions.eef_consistency import record as RC

    with pytest.raises(RC.RecordMappingError, match=words):
        RC.parse_mapping({"schema_version": "eef-mapping/1.1", "record": record})


def test_an_exporter_mapping_is_a_pose_record():
    from curation.extensions.eef_consistency import record as RC

    m = RC.load_mapping(os.path.join(os.path.dirname(__file__), "..", "eef", "mappings", "dataset2.yaml"))
    assert set(m.sources) == {"pose"} and m.sources["pose"].key == "observation.state.cartesian_position"
    assert m.sources["pose"].frame_id == "panda_link8" and m.sources["pose"].layout == "xyz_rpy_xyz_extrinsic"


# ------------------------------------------------------------------------------------ the comparison


def test_the_joints_record_agrees_and_a_late_one_shows_its_time_offset(mini_dataset, tmp_path):
    traj = _bundle(tmp_path)
    ok = _compare(traj, _mapping(tmp_path, MAPPING), _lerobot(mini_dataset))
    src = ok["sources"]["joints"]
    assert ok["status"] == "ok" and src["status"] == "ok" and src["alignment"] == "frame_index"
    assert src["relation"]["declared"] and src["residual"]["position_mm"]["max"] < 0.01
    late = copy.deepcopy(MAPPING)
    late["record"]["joints"]["key"] = "observation.state"                   # the same track one frame late
    got = _compare(traj, _mapping(tmp_path, late, "late.json"), _lerobot(mini_dataset))["sources"]["joints"]
    assert got["status"] == "suspect" and got["reasons"] == ["record_deviation"]
    assert "time_offset" in got["notes"] and abs(got["time_offset"]["lag_frames"] - 1.0) < 0.3     # + : the record is late
    assert got["segments"] and got["curves"]["position_mm"]


def test_a_tcp_upload_is_explained_by_a_declared_frame_and_caught_by_a_wrong_one(mini_dataset, tmp_path):
    traj = _bundle(tmp_path, frame="tcp", tcp=TCP)
    undeclared = _compare(traj, _mapping(tmp_path, MAPPING), _lerobot(mini_dataset))["sources"]["joints"]
    assert undeclared["status"] == "ok" and undeclared["notes"] == ["constant_unchecked"]
    assert not undeclared["relation"]["declared"]
    assert undeclared["relation"]["fitted"]["translation_mm"] == pytest.approx([0.0, 0.0, 103.4], abs=0.05)
    declared = copy.deepcopy(MAPPING)
    declared["record"]["frames"] = {"tcp": {"parent": "panda_link8", "xyz_m": TCP}}
    got = _compare(traj, _mapping(tmp_path, declared, "tcp.json"), _lerobot(mini_dataset))["sources"]["joints"]
    assert got["status"] == "ok" and got["relation"]["declared"] and got["relation"]["deviation"]["translation_norm_mm"] < 0.1
    wrong = copy.deepcopy(declared)
    wrong["record"]["frames"]["tcp"]["xyz_m"] = [0.0, 0.0, 0.16]            # the DROID TCP, not this one
    got = _compare(traj, _mapping(tmp_path, wrong, "wrong.json"), _lerobot(mini_dataset))["sources"]["joints"]
    assert got["status"] == "suspect" and got["reasons"] == ["constant_mismatch"]
    assert got["relation"]["deviation"]["translation_norm_mm"] == pytest.approx(56.6, abs=0.1)
    assert got["residual"]["position_mm"]["p95"] == pytest.approx(56.6, abs=0.1)           # the raw residual shows it
    assert got["residual_after_constant"]["position_mm"]["p95"] < 0.1


def test_a_relative_upload_is_compared_as_relative_motion(mini_dataset, tmp_path):
    traj = _bundle(tmp_path, relative=True)
    got = _compare(traj, _mapping(tmp_path, MAPPING), _lerobot(mini_dataset))["sources"]["joints"]
    assert got["status"] == "ok" and got["residual"]["position_mm"]["max"] < 0.01


def test_what_cannot_be_compared(mini_dataset, tmp_path):
    from curation.extensions.eef_consistency import load, profile
    from curation.extensions.eef_consistency import record as RC

    flat = _files(tmp_path / "flat")                                        # 2D only: no pose to compare
    r = load.load_bundle(flat, episodes=[1], check_media=False)
    got = RC.compare_episode(r.samples[1], RC.load_mapping(_mapping(tmp_path, MAPPING)), _lerobot(mini_dataset),
                             profile.load("demo"))
    assert (got["status"], got["reasons"]) == ("unsupported", ["upload_pose_missing"])
    traj = _bundle(tmp_path)
    assert _compare(traj, _mapping(tmp_path, MAPPING), None)["status"] == "error"          # a reader that fails
    missing = copy.deepcopy(MAPPING)
    missing["record"]["joints"]["key"] = "observation.state.joint_position"
    got = _compare(traj, _mapping(tmp_path, missing, "m.json"), _lerobot(mini_dataset))
    assert (got["status"], got["reasons"]) == ("unsupported", ["record_columns_missing"])
    other = copy.deepcopy(MAPPING)
    other["record"]["joints"]["reference_frame"] = "world"
    got = _compare(traj, _mapping(tmp_path, other, "w.json"), _lerobot(mini_dataset))["sources"]["joints"]
    assert (got["status"], got["reasons"]) == ("unsupported", ["reference_frames_unrelated"])


def test_the_mcap_twin_reads_the_same(mini_dataset, tmp_path):
    pytest.importorskip("mcap_ros2", reason="mcap-ros2-support decodes the fixture's cdr")
    from parity.fixtures import make_mini_mcap

    from curation.extensions.eef_consistency import record as RC

    mcap = make_mini_mcap(str(tmp_path / "mcap"))
    traj = _bundle(tmp_path)
    for key, topic in (("action", "/action"), ("observation.state", "/observation.state")):
        lr = copy.deepcopy(MAPPING)
        lr["record"]["joints"]["key"] = key
        mc = copy.deepcopy(MAPPING)
        del mc["record"]["joints"]["key"]
        mc["record"]["joints"].update(topic=topic, fields="data")
        a = _compare(traj, _mapping(tmp_path, lr, f"lr-{key}.json"), _lerobot(mini_dataset))["sources"]["joints"]
        b = _compare(traj, _mapping(tmp_path, mc, f"mc-{key}.json"), RC.McapRecords(mcap))["sources"]["joints"]
        assert b["alignment"] == "frame_index" and b["status"] == a["status"]
        assert b["residual"] == a["residual"] and b["reasons"] == a["reasons"]


# ------------------------------------------------------------------------------------ on the CLI chain


def test_check_reports_it_and_the_verdict_does_not_move(mini_dataset, tmp_path):
    """With seeds (the CPU path) and without a gripper reference (the model's opinion): the record is in
    the details either way, and the pass / reject of every episode is what it is without a mapping."""
    seeded = _files(tmp_path / "seeded", seed_every=1)
    doc = json.loads(open(seeded).read())
    posed = json.loads(open(_bundle(tmp_path)).read())
    for e, p in zip(doc["samples"], posed["samples"]):
        e["sample"].update(eef_frame="panda_link8", reference_frame="robot_base")
        for fr, pf in zip(e["frames"], p["frames"]):
            fr["eef"] = pf["eef"]
    open(seeded, "w").write(json.dumps(doc))
    late = copy.deepcopy(MAPPING)
    late["record"]["joints"]["key"] = "observation.state"
    mapping = _mapping(tmp_path, late)
    common = ("check", "--modules", EEF, "--input", mini_dataset, "--episodes", "0-3",
              "--param", f"{EEF}.trajectory_json={seeded}", *VLM)
    with fake_vlm(tmp_path / "tape"):
        base = run(*common, "--run-dir", str(tmp_path / "base"))
        withrec = run(*common, "--run-dir", str(tmp_path / "rec"), "--param", f"{EEF}.record_mapping={mapping}")
    assert base.rc == 0 and withrec.rc == 0, withrec.doc
    a, b = results(str(tmp_path / "base"), EEF), results(str(tmp_path / "rec"), EEF)
    assert {e: r["passed"] for e, r in a.items()} == {e: r["passed"] for e, r in b.items()}
    assert all(r["details"]["record"]["status"] == "unsupported" for r in a.values())          # no mapping given
    rec = {e: r["details"]["record"] for e, r in b.items()}
    assert all(r["sources"]["joints"]["status"] == "suspect" for r in rec.values())
    assert all(r["details"]["summary"]["record_consistency"]["status"] == "suspect" for r in b.values())
    assert not [p for r in b.values() for p in r["evidence"] if "/record/" in p]   # no calibration: no overlay
    assert all(r["details"]["record"]["evidence"] == [] for r in b.values())
    assert b[1]["details"]["config_hash"] != a[1]["details"]["config_hash"]      # --resume redoes a line
    # the report: figures, a table row per episode and source, one line in report.md; nothing without a mapping
    for name in ("base", "rec"):
        rd = str(tmp_path / name)
        for phase in ("funnel", "final"):
            assert run("aggregate", "--run-dir", rd, "--phase", phase, "--revision", "1", "--episodes", "0-3",
                       "--modules", EEF, "--input", mini_dataset).rc == 0
        assert run("report", "--run-dir", rd, "--revision", "1", "--modules", EEF).rc == 0
    sections = {}
    for name in ("base", "rec"):
        rep = json.load(open(tmp_path / name / "revisions" / "r0001" / "report.json", encoding="utf-8"))
        (sections[name],) = [x for x in rep["modules"] if x["id"] == EEF]
    assert not any(k.startswith("record_") for k in sections["base"]["summary"])
    s = sections["rec"]["summary"]
    assert (s["record_compared"], s["record_suspect"]) == (4, 4)
    assert s["record_by_source"]["joints"]["suspect"] == 4 and s["record_lag_median_frames"] == pytest.approx(1.0, abs=0.3)
    assert {x["name"] for x in s["record_reasons"]} == {"joints:record_deviation"}
    import pandas as pd

    table = pd.read_parquet(tmp_path / "rec" / "revisions" / "r0001" / "tables" / "eef_record.parquet")
    assert sorted(table["episode_index"]) == [0, 1, 2, 3] and set(table["source"]) == {"joints"}
    assert (table["lag_frames"] > 0.7).all() and (table["position_p95_mm"] > 0).all()
    assert pd.read_parquet(tmp_path / "base" / "revisions" / "r0001" / "tables" / "eef_record.parquet").empty
    md = (tmp_path / "rec" / "revisions" / "r0001" / "report.md").read_text(encoding="utf-8")
    assert "- 轨迹与数据集记录(只报告,不参与判决):关节角正解 一致 0 · 可疑 4;时间差中位 1" in md
    assert "轨迹与数据集记录" not in (tmp_path / "base" / "revisions" / "r0001" / "report.md").read_text(encoding="utf-8")
    # without a gripper reference: the model's opinion, and the record all the same
    posed_only = _bundle(tmp_path, name="posed.json")
    with fake_vlm(tmp_path / "tape2"):
        op = run("check", "--modules", EEF, "--input", mini_dataset, "--episodes", "0-1", "--run-dir",
                 str(tmp_path / "op"), "--param", f"{EEF}.trajectory_json={posed_only}",
                 "--param", f"{EEF}.record_mapping={_mapping(tmp_path, MAPPING, 'ok.json')}", *VLM)
    assert op.rc == 0, op.doc
    got = results(str(tmp_path / "op"), EEF)
    assert all(r["details"]["assessment_mode"] == "vlm_opinion" and r["passed"] is True for r in got.values())
    assert all(r["details"]["record"]["status"] == "ok" for r in got.values())


def test_preflight_says_whether_the_record_can_be_compared(mini_dataset, tmp_path):
    traj = _bundle(tmp_path)
    args = ("preflight", "--input", mini_dataset, "--modules", EEF, "--vlm-backend", "ark",
            "--param", f"{EEF}.trajectory_json={traj}")
    (entry,) = run(*args).doc["modules"]
    assert entry["subitems"]["record_consistency"] == {"availability": "unsupported",
                                                      "reason_code": "record_mapping_missing"}
    (entry,) = run(*args, "--param", f"{EEF}.record_mapping={_mapping(tmp_path, MAPPING)}").doc["modules"]
    assert entry["subitems"]["record_consistency"]["availability"] == "available"
    missing = copy.deepcopy(MAPPING)
    missing["record"]["joints"]["key"] = "observation.state.joint_position"
    (entry,) = run(*args, "--param", f"{EEF}.record_mapping={_mapping(tmp_path, missing, 'm.json')}").doc["modules"]
    assert entry["subitems"]["record_consistency"]["reason_code"] == "record_columns_missing"
    assert any("not in the dataset: observation.state.joint_position" in n for n in entry["notes"])
    ur5 = copy.deepcopy(MAPPING)
    ur5["record"]["joints"]["robot"] = "ur5"
    (entry,) = run(*args, "--param", f"{EEF}.record_mapping={_mapping(tmp_path, ur5, 'u.json')}").doc["modules"]
    assert entry["subitems"]["record_consistency"]["reason_code"] == "robot_model_unknown"
    assert entry["availability"] == "available"                                 # only this sub-item goes


# ------------------------------------------------------------------------------------ the DEMO dataset2


def test_dataset2_the_joints_find_what_the_pose_columns_cannot():
    from ..eef import demo_data

    from curation.extensions.eef_consistency import load, profile
    from curation.extensions.eef_consistency import record as RC

    root = demo_data.require("dataset2")
    m = RC.parse_mapping({"schema_version": "eef-mapping/1.1", "record": {
        "pose": {"key": "observation.state.cartesian_position", "layout": "xyz_rpy_xyz_extrinsic",
                 "units": {"position": "m", "angle": "rad"}, "frame_id": "panda_link8", "reference_frame": "robot_base"},
        "joints": {"key": "observation.state.joint_position", "units": "rad", "robot": "franka_panda",
                   "reference_frame": "robot_base"}}})
    r = load.load_bundle(root / "trajectory.json", check_media=False)
    reader = RC.LeRobotRecords(demo_data.lerobot_root("dataset2"))
    got = {ep: RC.strip(RC.compare_episode(r.samples[ep], m, reader, profile.load("demo"))) for ep in range(7)}
    assert all(g["sources"]["pose"]["status"] == "ok" for g in got.values())       # the file came from these columns
    joints = {ep: (g["sources"]["joints"]["status"], g["sources"]["joints"]["reasons"]) for ep, g in got.items()}
    assert joints == {0: ("ok", []), 1: ("suspect", ["constant_mismatch", "record_deviation"]),
                      2: ("suspect", ["constant_mismatch"]), 3: ("suspect", ["record_deviation"]),
                      4: ("ok", []), 5: ("ok", []), 6: ("ok", [])}
    assert got[2]["sources"]["joints"]["relation"]["deviation"]["rotation_deg"] == pytest.approx(30.0, abs=0.05)
    assert got[1]["sources"]["joints"]["residual"]["position_mm"]["p95"] > 30.0          # raw: not eaten by the fit
    assert [ep for ep, g in got.items() if g["internal"]["consistent"] is False] == [1, 2, 3]


def test_dataset2_overlays_both_tracks_where_the_joints_disagree(tmp_path):
    """The only place the comparison decodes video: frames of the worst stretch, on every calibrated camera."""
    from ..eef import demo_data

    from curation.extensions.eef_consistency import load, profile
    from curation.extensions.eef_consistency import record as RC

    root = demo_data.require("dataset2")
    m = RC.parse_mapping({"schema_version": "eef-mapping/1.1", "record": {
        "joints": {"key": "observation.state.joint_position", "units": "rad", "robot": "franka_panda",
                   "reference_frame": "robot_base"}}})
    lr = str(demo_data.lerobot_root("dataset2"))
    r = load.load_bundle(root / "trajectory.json", lerobot_root=lr, episodes=[1])
    full = RC.compare_episode(r.samples[1], m, RC.LeRobotRecords(lr), profile.load("demo"))
    shown = RC.write_evidence(r.samples[1], full, media_root=lr, out_dir=str(tmp_path), mode="flagged")
    assert shown and {e["camera_id"] for e in shown} == set(r.samples[1].cameras)
    assert all((tmp_path / e["path"]).is_file() and e["legend"] == {"upload": "red", "joints": "orange"} for e in shown)
    frames = sorted({e["frame_index"] for e in shown})                  # spread over the episode, trails apart
    assert len(frames) == 3 and min(np.diff(frames)) >= 2 * RC.TRAIL
    assert RC.write_evidence(r.samples[1], full, media_root=lr, out_dir=str(tmp_path / "off"), mode="off") == []


def test_dataset2_check_lists_its_overlays_in_the_record(tmp_path):
    """Through ``check``: the overlay frames are in the record's evidence and named in ``details.record``, so a
    reader tells them from the CPU's own frames."""
    from ..eef import demo_data

    root = demo_data.require("dataset2")
    mapping = _mapping(tmp_path, {"schema_version": "eef-mapping/1.1", "record": {"joints": {
        "key": "observation.state.joint_position", "units": "rad", "robot": "franka_panda",
        "reference_frame": "robot_base"}}})
    rd = tmp_path / "run"
    with fake_vlm(tmp_path / "tape"):
        res = run("check", "--modules", EEF, "--input", demo_data.lerobot_root("dataset2"), "--episodes", "1",
                  "--run-dir", rd, "--param", f"{EEF}.trajectory_json={root / 'trajectory.json'}",
                  "--param", f"{EEF}.record_mapping={mapping}", *VLM)
    assert res.rc == 0, res.doc
    (r,) = results(str(rd), EEF).values()
    shown = r["details"]["record"]["evidence"]
    assert shown and {e["path"] for e in shown} <= set(r["evidence"])
    assert all((rd / e["path"]).is_file() and "/record/" in e["path"] for e in shown)
    assert r["details"]["record"]["sources"]["joints"]["status"] == "suspect"
