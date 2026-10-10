"""No trajectory.json on a handheld gripper's mcap (design doc 22 §5.4, F5.20): the trajectory derived from the
recording, episode by episode, with the built-in DAS DEMO calibration or the task's; and the preflight that lets
such a task run while still asking every other dataset for the file."""
from __future__ import annotations

import json

import numpy as np
import pytest

from curation.extensions.eef_consistency import contracts as C
from curation.extensions.eef_consistency import derive_mcap
from curation.extensions.eef_consistency import preflight as PF
from curation.extensions.eef_consistency.adapters import umi_mcap as X

from . import das_mcap as F


@pytest.fixture(scope="module")
def das(tmp_path_factory):
    root = tmp_path_factory.mktemp("das")
    F.make_das(root / "data")
    return root


def _derived(das, tmp_path, calibration=None):
    return derive_mcap.Derived(root=str(das / "data"), numbering={0: "episode_0.mcap"}, calibration=calibration,
                          out_dir=str(tmp_path / "out"), dataset_id="das")


def test_the_built_in_calibration_is_the_das_demo_assumptions():
    cfg = X.read_calibration(X.BUILTIN_CALIBRATION)
    assert cfg["gripper"] == "das_gripper" and cfg["pose_frame"] == "vio_body_flu"
    assert np.allclose(cfg["body_to_optical"], F.R_BO) and np.allclose(np.asarray(cfg["T_camera_tcp"])[:3, 3], [0, 0.086, 0.17])
    assert X.assumed(cfg) == ["T_camera_tcp", "body_to_optical", "pose_frame"]
    assert "intrinsics_fallback" not in cfg                          # no customer camera in the public repository


def test_an_episode_is_derived_once_and_kept_for_the_overlay(das, tmp_path):
    d = _derived(das, tmp_path)
    s, src = d.sample(0)
    assert s is not None and s.n_frames == F.N - F.POSE_FROM and set(s.cameras) == {"robot0_camera0"}
    assert src["kind"] == "derived" and src["calibration"]["builtin"] and src["status"] == "ok"
    # robot1 has no camera_info and the built-in calibration no intrinsics: not drawn, and said why
    assert src["cameras"]["robot1"]["status"] == "unsupported" and "camera_info" in src["cameras"]["robot1"]["reason"]
    assert src["cameras"]["robot0"]["intrinsics"] == "camera_info" and src["cameras"]["robot0"]["pairing_rate"] > 0.9
    bundle = json.loads(derive_mcap.bundle_path(d.out_dir, 0).read_text())
    assert bundle["schema_version"] == "eef-video/1.1.0" and [e["episode_index"] for e in bundle["samples"]] == [0]
    assert json.loads(derive_mcap.report_path(d.out_dir, 0).read_text())["status"] == "ok"
    assert d.sample(0)[0] is s                                         # cached
    missing = d.sample(7)
    assert missing[0] is None and missing[1]["reason"] == derive_mcap.EPISODE_MISSING


def test_the_calibration_decides_what_can_be_drawn_and_the_digest(das, tmp_path):
    builtin = _derived(das, tmp_path)
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps(F.calibration()))                       # robot1's intrinsics as a fallback
    given = _derived(das, tmp_path / "b", calibration=str(cal))
    s, src = given.sample(0)
    assert set(s.cameras) == {"robot0_camera0", "robot1_camera0"} and not src["calibration"]["builtin"]
    assert src["cameras"]["robot1"]["intrinsics"] == "intrinsics_fallback"
    assert given.sha256 != builtin.sha256 and builtin.sha256 == _derived(das, tmp_path / "c").sha256
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(F.calibration(body_to_optical=[[1, 0, 0], [0, 1, 0], [0, 0, -1]])))
    with pytest.raises(X.ExportError, match="proper rotation"):
        _derived(das, tmp_path / "d", calibration=str(bad))


def test_a_recording_that_is_no_handheld_grippers_says_so(tmp_path):
    from ..viz.mcap_fixtures import make_default

    make_default(str(tmp_path / "arm"))
    d = derive_mcap.Derived(root=str(tmp_path / "arm"), numbering={0: "episode_0.mcap"}, calibration=None,
                       out_dir=str(tmp_path / "out"), dataset_id="arm")
    s, src = d.sample(0)
    assert s is None and src["reason"] == derive_mcap.NOT_DERIVED and "no hand" in src["message"]


def test_the_preflight_lets_a_handheld_gripper_go_without_the_file(das, tmp_path):
    entry = PF.consistency_entry({}, episodes=[0], media_exists=None, lerobot_root=str(das / "data"), handheld=True)
    assert entry["availability"] == C.AVAILABLE and entry["episode_counts"] == {C.AVAILABLE: 1}
    assert entry["subitems"][C.EGO_MOTION]["availability"] == C.AVAILABLE
    assert entry["subitems"][C.POSITION]["reason_code"] == C.OWN_HAND_CAMERA
    assert "derives each episode's trajectory" in entry["notes"][0] and "built-in DAS DEMO" in entry["notes"][0]
    # with the VLM backend still to choose: available, with a reminder (design doc 25 D84); none with the model off
    late = PF.module_entry(entry, vlm_backend=False)
    assert late["availability"] == C.AVAILABLE and late["notes"][-1] == PF.NOTE_VLM_BACKEND_MISSING
    assert PF.module_entry(entry, vlm_backend=False, use_vlm=False) == entry
    # any other dataset still needs the file
    other = PF.consistency_entry({}, episodes=[0], media_exists=None, lerobot_root=str(das / "data"))
    assert other["availability"] == C.NEEDS_INPUT and other["reason_code"] == C.TRAJECTORY_MISSING
    # a handheld gripper gets the model's opinion only; a calibration that cannot be right is refused
    seeds = tmp_path / "seeds.jsonl"
    seeds.write_text("")
    assert PF.consistency_entry({"observation_seeds": str(seeds)}, episodes=[0], media_exists=None,
                                handheld=True)["availability"] == C.UNSUPPORTED
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    refused = PF.consistency_entry({"gripper_calibration": str(bad)}, episodes=[0], media_exists=None, handheld=True)
    assert refused["availability"] == C.UNSUPPORTED and refused["reason_code"] == C.CALIBRATION_INVALID
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps(F.calibration()))
    given = PF.consistency_entry({"gripper_calibration": str(cal)}, episodes=[0], media_exists=None, handheld=True)
    assert given["availability"] == C.AVAILABLE and "uploaded calibration (das_test)" in given["notes"][0]
