"""The record mapping drafted from a LeRobot dataset's metadata (design doc 12 §8.7, D-E17; F5.16).

The draft reads only what ``info.json`` says - the columns' component names and ``robot_type`` - and
lists every step it takes by convention; a source it cannot draft unambiguously is left out with the
reason. The drafted mapping is an ordinary ``eef-mapping/1.1`` document: the pose's point is not
declared (``frame_id: null``) and both records are in the upload's base (``reference_frame: "@upload"``).
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from . import demo_data

JOINTS = [f"joint_{i}" for i in range(7)]
RPY = ["x", "y", "z", "roll", "pitch", "yaw"]


def _f(names, dtype="float32"):
    return {"dtype": dtype, "shape": [len(names)], "names": names}


def _draft(features: dict, robot_type="franka") -> dict:
    from curation.extensions.eef_consistency import record_draft as D

    return D.draft({"robot_type": robot_type, "features": features})


def _codes(d: dict, source: str) -> list[str]:
    return [a["code"] for a in d["assumptions"] if a.get("source") == source]


def test_dataset2_drafts_the_measured_columns_and_says_what_it_assumed():
    from curation.extensions.eef_consistency import record_draft as D

    demo_data.require("dataset2")
    d = D.draft(json.loads((demo_data.lerobot_root("dataset2") / "meta" / "info.json").read_text()))
    assert d["document"] == {"schema_version": "eef-mapping/1.1", "record": {
        "pose": {"key": "observation.state.cartesian_position", "layout": "xyz_rpy_xyz_extrinsic",
                 "units": {"position": "m", "angle": "rad"}, "frame_id": None, "reference_frame": "@upload"},
        "joints": {"key": "observation.state.joint_position", "units": "rad", "robot": "franka_panda",
                   "reference_frame": "@upload"}}}
    assert d["not_drafted"] == []
    assert _codes(d, "pose") == ["observation_not_action", "euler_extrinsic_xyz", "units_by_convention",
                                 "pose_frame_undeclared", "same_base_as_upload"]
    assert _codes(d, "joints") == ["robot_from_robot_type", "picked_dedicated", "observation_not_action",
                                   "units_by_convention", "joints_tip_frame", "same_base_as_upload"]
    skipped = {a["source"]: a["args"]["skipped"] for a in d["assumptions"] if a["code"] == "observation_not_action"}
    assert skipped == {"pose": ["action.cartesian_position", "action.original"],       # the commands, never
                       "joints": ["action.joint_position", "action"]}                 # velocities or extrinsics


def test_the_mini_dataset_has_joints_only():
    d = _draft({"action": _f(JOINTS), "observation.state": _f(JOINTS), "timestamp": _f(["t"])})
    assert d["document"]["record"] == {"joints": {"key": "observation.state", "units": "rad", "robot": "franka_panda",
                                                  "reference_frame": "@upload"}}
    assert d["not_drafted"] == [{"code": "no_named_pose_column", "source": "pose"}]


def test_a_run_inside_a_longer_vector_is_a_slice_and_names_may_be_a_dict():
    d = _draft({"observation.state": {"dtype": "float64", "shape": [8], "names": {"motors": [*RPY, "gripper", "x2"]}},
                "observation.joints": _f(["gripper", *JOINTS])})
    rec = d["document"]["record"]
    assert rec["pose"]["slice"] == [0, 6] and rec["joints"]["slice"] == [1, 8]
    assert "slice_from_names" in _codes(d, "pose") and "slice_from_names" in _codes(d, "joints")


def test_quaternions_in_either_order():
    for names, layout, order in ((["x", "y", "z", "qx", "qy", "qz", "qw"], "xyz_quat_xyzw", "xyzw"),
                                 (["x", "y", "z", "qw", "qx", "qy", "qz"], "xyz_quat_wxyz", "wxyz")):
        d = _draft({"observation.eef_pose": _f(names)}, robot_type=None)
        assert d["document"]["record"]["pose"]["layout"] == layout
        assert {"code": "quaternion_order", "source": "pose", "args": {"order": order}} in d["assumptions"]


@pytest.mark.parametrize("features, robot_type, why", [
    ({"observation.a": _f(RPY), "observation.b": _f(RPY)}, None, ("pose", "ambiguous")),
    ({"observation.state": _f(JOINTS)}, "ur5e", ("joints", "robot_not_built_in")),
    ({"observation.state": _f(JOINTS)}, "", ("joints", "no_robot_type")),
    ({"observation.state": _f([f"joint_{i}" for i in range(14)])}, "franka", ("joints", "no_named_joint_column")),
    ({"observation.state": _f(["joint_0", "joint_1", "joint_2", "joint_4", "joint_5", "joint_6", "joint_7"])},
     "franka", ("joints", "no_named_joint_column")),
    ({"observation.state.cartesian_velocity": _f(RPY), "camera_extrinsics.left": _f(RPY),
      "action.cartesian_position": _f(RPY)}, None, ("pose", "no_named_pose_column")),
    ({"observation.state": _f(RPY, dtype="string")}, None, ("pose", "no_named_pose_column")),
])
def test_what_is_not_drafted_says_why(features, robot_type, why):
    d = _draft(features, robot_type)
    assert (why[0], why[1]) in {(n.get("source"), n["code"]) for n in d["not_drafted"]}
    assert why[0] not in ((d["document"] or {}).get("record") or {})


def test_nothing_drafted_is_a_null_document_and_other_formats_are_not_drafted():
    from curation.extensions.eef_consistency import record_draft as D

    assert _draft({}, robot_type=None)["document"] is None
    assert D.not_drafted("mcap") == {"document": None, "assumptions": [],
                                     "not_drafted": [{"code": "format_not_drafted", "args": {"format": "mcap"}}]}


def test_the_draft_is_a_mapping_the_comparison_reads():
    from curation.extensions.eef_consistency import record as RC

    d = _draft({"observation.state": _f([*RPY, *JOINTS])})
    m = RC.parse_mapping(d["document"])
    assert m.sources["pose"].frame_id is None and m.sources["pose"].reference_frame == RC.UPLOAD_FRAME
    assert m.sources["joints"].frame_id == "panda_link8"
    assert m.transform(None, "panda_link8") is None and m.transform("panda_link8", None) is None
    assert np.allclose(m.transform("panda_link8", "panda_link8"), np.eye(4))


@pytest.mark.parametrize("spec, words", [
    ({"key": "p", "layout": "xyz_rpy_xyz_extrinsic", "units": {"position": "m", "angle": "rad"},
      "reference_frame": "b"}, "null when the point"),                                  # frame_id left out
    ({"key": "p", "layout": "xyz_rpy_xyz_extrinsic", "units": {"position": "m", "angle": "rad"},
      "frame_id": "", "reference_frame": "b"}, "null when the point"),
    ({"key": "p", "layout": "xyz_rpy_xyz_extrinsic", "units": {"position": "m", "angle": "rad"},
      "frame_id": None}, "'@upload'"),                                                   # reference_frame left out
])
def test_unknown_is_written_out_never_left_out(spec, words):
    from curation.extensions.eef_consistency import record as RC

    with pytest.raises(RC.RecordMappingError, match=words):
        RC.parse_mapping({"schema_version": "eef-mapping/1.1", "record": {"pose": spec}})
