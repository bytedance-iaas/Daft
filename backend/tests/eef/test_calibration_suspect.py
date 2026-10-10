"""A dataset-level calibration suspect (design doc 25 §7.4, D86): the same constant position offset on one camera
across many episodes is the calibration's, not each episode's."""
from __future__ import annotations

import math

from curation.extensions.eef_consistency import calibration as CS


def _record(offset: tuple[float, float] | None, status: str = "suspect", *, other=(0.3, -0.2), pnp=None,
            source=None) -> dict:
    def camera(st, off):
        points = {} if off is None else {
            pid: {"status": st, "metrics": {"median_u_px": off[0] + d, "median_v_px": off[1] - d}}
            for pid, d in (("tcp", 0.0), ("finger_plus_y", 0.4), ("finger_minus_y", -0.4))}
        return {"subitems": {"position_2d": {"status": st, "points": points}}}

    details = {"cameras": {"ext1": camera(status, offset), "ext2": camera("ok", other)}}
    if pnp is not None:
        details["diagnosis"] = [{"hypothesis": "extrinsics_error", "camera_id": "ext1", "supported": True,
                                 "fitted": {"delta_translation_mm": pnp[0], "delta_rotation_deg": pnp[1]}}]
    if source is not None:
        details["trajectory_source"] = source
    return {"status": "ok", "details": details}


def test_one_offset_on_seven_episodes_is_the_cameras():
    """dataset2 with exterior_1's extrinsics 3 cm off: about 38 px to the left on every episode."""
    source = {"kind": "generated", "camera_sources": {"ext1": "observation.images.ext1", "ext2": "observation.images.ext2"},
              "assumed": ["semantics.pose", "calibration.cameras.observation.images.ext1",
                          "calibration.cameras.observation.images.ext2", "calibration.tool", "timing"]}
    records = {ep: _record((-38.0 + ep * 0.5, -0.4 + 0.2 * ep), pnp=(30.0 + ep, 1.8), source=source)
               for ep in range(7)}
    (s,) = CS.suspects(records)
    r = s["readings"]
    assert s["camera"] == "ext1" and r["episodes"] == list(range(7)) and r["measured_episodes"] == 7
    assert r["share"] == 1.0 and abs(abs(r["direction_deg"]) - 180) < 5 and r["size_px"]["median"] == 36.5
    assert r["pnp"] == {"episodes": 7, "supported": 7, "translation_mm": 33.0, "rotation_deg": 1.8}
    # this camera's and the tool's assumptions, not the other camera's
    assert r["assumed"] == ["calibration.cameras.observation.images.ext1", "calibration.tool"]
    assert "偏左" in s["message"] and "7 / 7 条" in s["message"] and "PnP 修正中位 33 mm、1.8°" in s["message"]
    assert "多半是外参、TCP 偏移或假设值，不是逐条的数据问题" in s["message"]
    assert "按假设值的：calibration.cameras.observation.images.ext1、calibration.tool" in s["message"]
    older = {ep: _record((-38.0, 0.0), source={k: v for k, v in source.items() if k != "camera_sources"}) for ep in range(7)}
    assert len(CS.suspects(older)[0]["readings"]["assumed"]) == 3              # a record without the map: every camera's


def test_too_few_two_ways_too_rare_or_too_uneven_is_not():
    left = (-38.0, 0.0)
    assert CS.suspects({ep: _record(left) for ep in range(4)}) == []                          # fewer than 5
    two_ways = {ep: _record(left if ep % 2 else (38.0, 0.0)) for ep in range(10)}
    assert CS.suspects(two_ways) == []                                                          # no common direction
    rare = {ep: _record(left if ep < 5 else (0.2, 0.1), "suspect" if ep < 5 else "ok") for ep in range(10)}
    assert CS.suspects(rare) == []                                                              # 5 of 10 < 60 %
    uneven = {ep: _record((-(4.0 if ep % 2 else 60.0), 0.0)) for ep in range(8)}
    assert CS.suspects(uneven) == []                                                            # CV >= 0.5
    loose = {ep: _record((-38.0, 0.0) if ep < 6 else (0.0, 38.0)) for ep in range(8)}          # 6 of 8 agree
    (s,) = CS.suspects(loose)
    assert s["readings"]["episodes"] == list(range(6)) and s["readings"]["share"] == 0.75


def test_a_handheld_grippers_assumed_calibration_is_named():
    """design doc 25 §7.4: a handheld gripper run on the DAS DEMO values is what it is for - the message says the
    calibration was assumed."""
    src = {"kind": "derived", "calibration": {"builtin": True, "assumed": ["T_camera_tcp", "body_to_optical", "pose_frame"]}}
    (s,) = CS.suspects({ep: _record((5.0, 30.0), source=src) for ep in range(6)})
    assert s["readings"]["assumed"] == ["T_camera_tcp", "body_to_optical", "pose_frame"]
    assert "偏下" in s["message"] and "按假设值的：T_camera_tcp、body_to_optical、pose_frame" in s["message"]


def test_the_direction_in_words():
    assert [CS.direction_zh(d) for d in (0, 44, 46, 90, 180, -179, -90, -45, 359)] == \
        ["右", "右下", "右下", "下", "左", "左", "上", "右上", "右"]
    assert math.isclose(CS._apart(170, -170), 20)


def test_the_report_carries_it_as_the_modules_dataset_level_finding():
    """findings.dataset_level: one ``calibration_suspect`` (MV-4, medium, ``unit: dataset``, the camera in its scope),
    a valid C2 finding; nothing for a dataset whose offsets do not agree."""
    from curation.contracts import modules as registry
    from curation.contracts import schemas
    from curation.pipeline import findings as F

    records = {ep: _record((-38.0, 0.5)) for ep in range(7)}
    found, readings = F.dataset_level("eef_video_consistency", records)
    (f,) = found
    assert (f["code"], f["item"], f["severity"], f["unit"], f["scope"]) == \
        ("calibration_suspect", "MV-4", "medium", "dataset", {"camera": "ext1"})
    assert f["readings"]["episodes"] == list(range(7)) and readings == {}
    schemas.validate("cli/common.schema.json#/$defs/finding", f)
    spec = registry.finding_code("eef_video_consistency", "calibration_suspect")
    assert (spec.level, spec.scope_kind) == ("info", "dataset")
    errored = {**records, 7: {"status": "error", "details": {}}}
    assert len(F.dataset_level("eef_video_consistency", errored)[0]) == 1              # an error is not measured
    assert F.dataset_level("eef_video_consistency", {ep: _record((-38.0, 0.5) if ep % 2 else (38.0, 0.5))
                                                     for ep in range(8)})[0] == []
