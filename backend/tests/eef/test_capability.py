"""Per sub-item capability follows the real input (design 12 §5.1)."""
from __future__ import annotations

import copy
import json

from curation.extensions.eef_consistency import capability as CAP
from curation.extensions.eef_consistency import contracts as C
from curation.extensions.eef_consistency import load

from . import synth

ALL_POINTS = {"cam0": set(synth.POINTS)}


def _sample(mutate=None, n=40):
    e = synth.make_entry(0, n)
    if mutate:
        mutate(e)
    r = load.load_bundle(json.dumps(synth.make_bundle([e])).encode(), check_media=False)
    assert r.ok, [i.as_dict() for i in r.errors]
    return r.samples[0]


def _av(cap):
    return {k: v["availability"] for k, v in cap["subitems"].items()}


def test_form_b_with_observations_is_available_everywhere():
    cap = CAP.sample_capability(_sample(), observable=ALL_POINTS)
    av = _av(cap)
    assert cap["availability"] == C.AVAILABLE
    for k in CAP.CORE_SUBITEMS + (C.INPUT_CONSISTENCY,):
        assert av[k] == C.AVAILABLE, k
    assert av[C.VLM_REVIEW] == C.NEEDS_INPUT
    cam = cap["cameras"]["cam0"]
    assert cam["calibration"] == "declared" and set(cam["observable_axes"]) == {"z", "finger_line"}


def test_without_observation_seeds_position_needs_input():
    cap = CAP.sample_capability(_sample(), observable=None)
    assert cap["subitems"][C.POSITION] == {"availability": C.NEEDS_INPUT, "reason_code": C.OBSERVATION_SEED_MISSING}
    assert cap["subitems"][C.STATE_MOTION]["availability"] == C.AVAILABLE
    assert cap["availability"] == C.AVAILABLE                           # state/camera motion still run


def test_observations_of_other_points_only():
    cap = CAP.sample_capability(_sample(), observable={"cam0": {"elbow"}})
    assert cap["subitems"][C.POSITION]["reason_code"] == C.VISUAL_POINT_MAPPING_MISSING
    assert cap["subitems"][C.ORIENTATION]["reason_code"] == C.AXIS_MAPPING_MISSING
    cap = CAP.sample_capability(_sample(), observable={"cam0": {"tcp"}})
    assert cap["subitems"][C.POSITION]["availability"] == C.AVAILABLE
    assert cap["subitems"][C.ORIENTATION]["reason_code"] == C.AXIS_MAPPING_MISSING


def test_form_a_two_dimensional_input_only():
    def to_2d(e):
        e["calibration"] = None
        e["sample"]["calibration_path"] = None
        for f in e["frames"]:
            f["eef"] = None
            f["cameras"]["cam0"]["calibration_id"] = None
    cap = CAP.sample_capability(_sample(to_2d), observable=ALL_POINTS)
    av = _av(cap)
    assert av[C.POSITION] == av[C.ORIENTATION] == av[C.TEMPORAL] == av[C.CAMERA_MOTION] == C.AVAILABLE
    assert cap["subitems"][C.STATE_MOTION]["reason_code"] == C.POSE_SEMANTICS_UNKNOWN
    assert cap["subitems"][C.INPUT_CONSISTENCY]["reason_code"] == C.POSE_SEMANTICS_UNKNOWN


def test_pose_without_projection_or_calibration():
    def strip(e):
        e["calibration"] = None
        e["sample"]["calibration_path"] = None
        for f in e["frames"]:
            f["cameras"]["cam0"]["calibration_id"] = None
            f["cameras"]["cam0"]["projection"] = None
    cap = CAP.sample_capability(_sample(strip), observable=ALL_POINTS)
    assert cap["subitems"][C.POSITION]["reason_code"] == C.CALIBRATION_MISSING
    assert cap["subitems"][C.STATE_MOTION]["availability"] == C.AVAILABLE


def test_wrist_camera_is_spatial_only_and_moving_camera_unsupported():
    cap = CAP.sample_capability(_sample(lambda e: e["sample"]["views"][0].__setitem__("mount", "wrist")),
                                observable=ALL_POINTS)
    assert cap["subitems"][C.POSITION]["availability"] == C.AVAILABLE
    assert cap["subitems"][C.TEMPORAL]["reason_code"] == C.WRIST_CAMERA_SPATIAL_ONLY
    assert cap["subitems"][C.CAMERA_MOTION]["reason_code"] == C.WRIST_CAMERA_SPATIAL_ONLY
    cap = CAP.sample_capability(_sample(lambda e: e["sample"]["views"][0].__setitem__("mount", "moving")),
                                observable=ALL_POINTS)
    assert cap["subitems"][C.POSITION]["reason_code"] == C.MOVING_CAMERA_UNSUPPORTED


def test_index_only_timebase_blocks_temporal_items():
    def index_only(e):
        e["sample"]["timebase"] = "index_only"
        for f in e["frames"]:
            f["timestamp_s"] = None
            f["source_timing"] = []
    cap = CAP.sample_capability(_sample(index_only), observable=ALL_POINTS)
    assert cap["subitems"][C.TEMPORAL]["reason_code"] == C.TIMEBASE_INDEX_ONLY
    assert cap["subitems"][C.STATE_MOTION]["reason_code"] == C.TIMEBASE_INDEX_ONLY
    assert cap["subitems"][C.POSITION]["availability"] == C.AVAILABLE


def test_dataset_level_missing_file_invalid_file_and_missing_episode():
    assert CAP.dataset_capability(None, [0, 1])["reason_code"] == C.TRAJECTORY_MISSING
    bad = load.load_bundle(b'{"schema_version": "eef-video/1.0.0"}')
    out = CAP.dataset_capability(bad, [0, 1])
    assert out["availability"] == C.NEEDS_INPUT and out["reason_code"] == C.TRAJECTORY_INVALID and out["errors"]
    good = load.load_bundle(json.dumps(synth.make_bundle([synth.make_entry(0, 30)])).encode(), check_media=False)
    out = CAP.dataset_capability(good, [0, 1], observable={0: ALL_POINTS})
    assert out["availability"] == C.AVAILABLE
    assert out["episodes"][1] == {"availability": C.UNSUPPORTED, "reason_code": C.PROJECTION_MISSING}
    assert out["counts"] == {C.AVAILABLE: 1, C.UNSUPPORTED: 1}
    out = CAP.dataset_capability(good, [5])
    assert out["availability"] == C.UNSUPPORTED and out["samples_outside_dataset"] == [0]


def test_overall_rule():
    cells = {"a": C.subitem(C.UNSUPPORTED, "x"), "b": C.subitem(C.NEEDS_INPUT, "y")}
    assert C.overall_availability(cells) == C.NEEDS_INPUT
    assert C.overall_availability({"a": C.subitem(C.UNSUPPORTED, "x")}) == C.UNSUPPORTED
    assert C.overall_availability({**cells, "c": C.subitem(C.AVAILABLE)}) == C.AVAILABLE
    assert copy.deepcopy(cells) == cells
