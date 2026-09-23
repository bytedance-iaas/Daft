"""trajectory.json reading: container, Schemas, cross-section semantics, truth isolation, self-consistency."""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest

from curation.extensions.eef_consistency import contracts as C
from curation.extensions.eef_consistency import load

from . import synth


@pytest.fixture(scope="module")
def entry():
    return synth.make_entry(0, 45)


@pytest.fixture()
def media_root(tmp_path):
    (tmp_path / "videos/cam").mkdir(parents=True)
    (tmp_path / "videos/cam/episode_000000.mp4").write_bytes(b"\0")
    return tmp_path


def _load(bundle, root=None, **kw):
    return load.load_bundle(json.dumps(bundle).encode(), lerobot_root=root, check_media=root is not None, **kw)


def test_valid_bundle_parses_and_recomputes_within_tolerance(entry, media_root):
    r = _load(synth.make_bundle([entry]), media_root)
    assert r.ok, r.issues
    s = r.samples[0]
    assert s.sample_id == "synthetic_000000" and s.n_frames == 45 and s.pose_type == "absolute"
    assert s.has_absolute_pose and s.eef_mask.all()
    assert r.report["max_reprojection_difference_px"] < 1e-6        # OpenCV vs geometry.py
    assert r.report["total_points_checked"] == 45 * 5
    assert s.clocks["robot_state"].seconds[0] == 0.0
    cam = s.cameras["cam0"]
    assert cam.projection_source == "provided" and set(cam.provided) == set(synth.POINTS)
    assert r.sha256 and len(s.input_hash) == 64


def test_file_path_input_and_hash(tmp_path, entry, media_root):
    p = synth.write_bundle(tmp_path / "trajectory.json", synth.make_bundle([entry]))
    r = load.load_bundle(p, lerobot_root=media_root)
    assert r.ok
    import hashlib
    assert r.sha256 == hashlib.sha256(p.read_bytes()).hexdigest()


def _mutated(entry, fn):
    e = copy.deepcopy(entry)
    fn(e)
    return synth.make_bundle([e])


def _cam(e, i=0):
    return e["frames"][i]["cameras"]["cam0"]


REJECTIONS = {
    "nested truth key": (lambda e: _cam(e, 3)["projection"]["points"]["tcp"].__setitem__("truth", 1), load.FORBIDDEN_KEY),
    "corruption_type anywhere": (lambda e: e["sample"].__setitem__("corruption_type", "x"), load.FORBIDDEN_KEY),
    "unknown field": (lambda e: e["frames"][0].__setitem__("typo", 1), load.SCHEMA),
    "zero quaternion": (lambda e: e["frames"][2]["eef"].__setitem__("quaternion_xyzw", [0, 0, 0, 0]), load.SEMANTIC),
    "non-unit quaternion": (lambda e: e["frames"][2]["eef"].__setitem__("quaternion_xyzw", [0, 0, 0, 2]), load.SEMANTIC),
    "non-SO3 extrinsics": (lambda e: e["calibration"]["calibrations"]["cam0_declared"]["T_reference_camera"][0]
                           .__setitem__(0, 4.0), load.SEMANTIC),
    "duplicate frame index": (lambda e: e["frames"][1].__setitem__("frame_index", 0), load.SEMANTIC),
    "time goes backwards": (lambda e: e["frames"][5].__setitem__("timestamp_s", 0.0), load.SEMANTIC),
    "undefined projection point": (lambda e: _cam(e)["projection"]["points"].__setitem__(
        "unregistered", {"uv_px": [1, 1], "depth_m": None, "status": "valid", "in_frame": True}), load.SEMANTIC),
    "calibration of another camera": (lambda e: e["calibration"]["calibrations"]["cam0_declared"]
                                      .__setitem__("camera_id", "other"), load.SEMANTIC),
    "wrong pose reference frame": (lambda e: e["frames"][0]["eef"].__setitem__("reference_frame", "camera"),
                                   load.SEMANTIC),
    "axis to unknown point": (lambda e: e["sample"]["axis_definitions"]["z"].__setitem__("start_point_id", "nope"),
                              load.SEMANTIC),
    "axis length mismatch": (lambda e: e["sample"]["axis_definitions"]["z"].__setitem__("length_m", 0.07),
                             load.SEMANTIC),
    "frame count mismatch": (lambda e: e["sample"].__setitem__("frame_count", 44), load.SEMANTIC),
    "episode id mismatch": (lambda e: e["sample"]["source"].__setitem__("episode_id", "7"), load.SEMANTIC),
    "absolute media uri": (lambda e: e["sample"]["views"][0]["media"].__setitem__("uri", "/data/x.mp4"), load.SEMANTIC),
    "parent media uri": (lambda e: e["sample"]["views"][0]["media"].__setitem__("uri", "../x.mp4"), load.SEMANTIC),
    "in_frame contradicts pixels": (lambda e: _cam(e)["projection"]["points"]["tcp"].__setitem__("uv_px", [-5.0, 3.0]),
                                    load.SEMANTIC),
    "static calibration with frame pose": (lambda e: _cam(e).__setitem__("T_reference_camera", np.eye(4).tolist()),
                                           load.SEMANTIC),
    "image size differs from media": (lambda e: _cam(e).__setitem__("image_size_wh", [640, 480]), load.SEMANTIC),
    "singular media transform": (lambda e: _cam(e).__setitem__("H_media_from_calibration", [[0, 0, 0]] * 3),
                                 load.SEMANTIC),
    "video frame beyond clip": (lambda e: _cam(e, 44).__setitem__("video_frame_index", 45), load.SEMANTIC),
}


@pytest.mark.parametrize("name", sorted(REJECTIONS))
def test_invalid_inputs_are_rejected_with_a_location(entry, name):
    mutate, code = REJECTIONS[name]
    r = _load(_mutated(entry, mutate))
    assert not r.ok and not r.samples
    assert any(i.code == code for i in r.errors), [i.as_dict() for i in r.errors]
    first = r.errors[0]
    assert first.path or first.frame_index is not None or first.episode_index is not None


def test_a_truth_key_is_located_at_its_sample_frame_camera_and_point(entry):
    r = _load(_mutated(entry, REJECTIONS["nested truth key"][0]))
    (err,) = r.errors
    frame = entry["frames"][3]["frame_index"]
    assert (err.episode_index, err.sample_id, err.frame_index, err.camera_id, err.point_id) == \
        (entry["episode_index"], entry["sample"]["sample_id"], frame, "cam0", "tcp")
    (err,) = _load(_mutated(entry, REJECTIONS["corruption_type anywhere"][0])).errors
    assert err.sample_id == entry["sample"]["sample_id"] and err.frame_index is None


def test_nan_is_rejected_before_anything_else():
    r = load.load_bundle(b'{"schema_version": NaN}')
    assert not r.ok and r.errors[0].code == load.PARSE_ERROR


def test_missing_media_is_an_error_located_at_the_view(entry, tmp_path):
    r = _load(synth.make_bundle([entry]), tmp_path)
    assert not r.ok
    (err,) = [i for i in r.errors if i.code == load.MEDIA]
    assert err.camera_id == "cam0" and err.path.endswith("views/0/media/uri")


def test_tampered_projection_is_kept_and_reported_not_overwritten(entry, media_root):
    e = copy.deepcopy(entry)
    _cam(e, 7)["projection"]["points"]["tcp"]["uv_px"][0] += 3.0
    r = _load(synth.make_bundle([e]), media_root)
    assert r.ok
    (w,) = [i for i in r.warnings if i.code == C.INPUT_INCONSISTENT and i.point_id == "tcp"]
    assert w.frame_index == 7 and w.camera_id == "cam0"
    s = r.samples[0]
    stats = s.consistency["cameras"]["cam0"]["tcp"]
    assert abs(stats["max_px"] - 3.0) < 1e-6 and stats["worst_frame"] == 7
    # the provided value is what gets tested; the recomputation never replaces it
    assert abs(s.cameras["cam0"].provided["tcp"].uv[7, 0] - _cam(entry, 7)["projection"]["points"]["tcp"]["uv_px"][0]
               - 3.0) < 1e-9


def test_duplicate_episode_across_samples(entry):
    r = _load(synth.make_bundle([entry, copy.deepcopy(entry)]))
    assert not r.ok and any("duplicate" in i.message for i in r.errors)


def test_relative_poses_without_anchor_are_parsed_but_not_projectable(entry):
    e = copy.deepcopy(entry)
    import numpy.linalg as la
    from curation.extensions.eef_consistency import geometry as G
    T = [G.pose_matrix(f["eef"]["position_m"], f["eef"]["quaternion_xyzw"]) for f in e["frames"]]
    T0inv = la.inv(T[0])
    from scipy.spatial.transform import Rotation
    for f, Ti in zip(e["frames"], T):
        rel = T0inv @ Ti
        f["eef"].update(pose_type="relative_to_start", reference_frame="eef_at_start",
                        position_m=rel[:3, 3].tolist(), quaternion_xyzw=Rotation.from_matrix(rel[:3, :3]).as_quat().tolist(),
                        relative_to={"frame_index": 0, "convention": "inverse_T0_times_Tt", "anchor_pose": None})
    e["frames"][0]["eef"]["position_m"] = [0.0, 0.0, 0.0]
    e["frames"][0]["eef"]["quaternion_xyzw"] = [0.0, 0.0, 0.0, 1.0]
    r = _load(synth.make_bundle([e]))
    assert r.ok, [i.as_dict() for i in r.errors]
    s = r.samples[0]
    assert s.anchor_missing and not s.has_absolute_pose and s.eef_mask.all()
    assert load.recompute_projection(s, "cam0", "tcp") is None
    # with the anchor the absolute poses come back exactly
    for f in e["frames"]:
        f["eef"]["relative_to"]["anchor_pose"] = {"reference_frame": "robot_base",
                                                  "position_m": entry["frames"][0]["eef"]["position_m"],
                                                  "quaternion_xyzw": entry["frames"][0]["eef"]["quaternion_xyzw"]}
    r = _load(synth.make_bundle([e]))
    assert r.ok and r.samples[0].has_absolute_pose
    assert r.report["max_reprojection_difference_px"] < 1e-6


def test_episode_filter_parses_only_requested_samples(entry):
    e1 = synth.make_entry(1, 20)
    r = _load(synth.make_bundle([entry, e1]), episodes=[1])
    assert r.ok and set(r.samples) == {1}
