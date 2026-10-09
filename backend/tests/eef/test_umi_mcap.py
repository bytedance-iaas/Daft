"""The handheld-gripper mcap exporter (design doc 22 §5.2, §7; F5.18) on a small DAS-like recording
(``das_mcap``): what goes into trajectory.json - each hand in its own VIO world, its poses composed through
the gripper's calibration and paired with its camera frames by the recorder's header time, the timeline on
the checks' clock from the first decodable frame on - and what the report says about it; the loader's 1.1.0
additions and the bridging of short pose gaps (``umi.fill_gaps``)."""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from curation.extensions.eef_consistency import __main__ as cli, load, umi
from curation.extensions.eef_consistency.adapters import umi_mcap as X

from . import das_mcap as F

ROWS = F.N - F.POSE_FROM
CAMS = {"robot0": "robot0_camera0", "robot1": "robot1_camera0"}


@pytest.fixture(scope="module")
def das(tmp_path_factory):
    root = tmp_path_factory.mktemp("das")
    F.make_das(root / "data")
    return root


def _cal(path, **changes) -> str:
    path.write_text(json.dumps(F.calibration(**changes)))
    return str(path)


@pytest.fixture(scope="module")
def exported(das):
    out = das / "out" / "trajectory.json"
    result = X.export(das / "data", _cal(das / "cal.json"), out, dataset_id="das/test")
    report = json.loads((out.parent / "umi-export-report.json").read_text())
    return result, out, json.loads(out.read_text()), report


def _episode(das, name: str, *, ego_check: bool = False, **changes) -> X.Episode:
    cfg = X.read_calibration(_cal(das / f"{name}.json", **changes))
    return X.export_episode(str(das / "data" / "episode_0.mcap"), 0, cfg, uri="episode_0.mcap", dataset_id="t",
                            ego_check=ego_check)


def test_each_hand_is_composed_in_its_own_world_and_paired_by_header_time(exported):
    result, _, bundle, _ = exported
    assert result["samples"] == 1 and result["episodes"] == [{"episode_index": 0, "status": "ok", "rows": ROWS}]
    assert bundle["schema_version"] == "eef-video/1.1.0" and bundle["dataset"]["lerobot_codebase_version"] == "mcap"
    assert bundle["dataset"]["fps"] == pytest.approx(F.FPS, abs=0.01)
    entry = bundle["samples"][0]
    s = entry["sample"]
    assert s["reference_frame"] == "per_hand" and s["umi"]["world_frames"] == "per_hand"
    assert s["umi"]["camera_hands"] == {c: h for h, c in CAMS.items()}
    assert s["source"]["instruction"] == "put the cup on the shelf"           # the recording's metadata
    assert any(n.startswith("按假设值：T_camera_tcp") for n in s["notes"])   # an assumption says so
    assert {v["media"]["topic"] for v in s["views"]} == {f"/{h}/sensor/camera0/compressed" for h in CAMS}
    cals = entry["calibration"]["calibrations"]
    for h, cid in CAMS.items():
        c = cals[cid]
        # the calibration as recorded (320 x 240), stretched onto the 480 x 390 video frame by frame
        assert c["reference_frame"] == f"{h}_vio_world" and c["image_size_wh"] == list(F.CAL_WH)
        assert c["model"] == "opencv_fisheye" and np.allclose(c["K"], F.K_CAL) and np.allclose(c["distortion_coefficients"], F.D)
    assert "camera_info" in cals["robot0_camera0"]["provenance"]["source"]
    assert cals["robot1_camera0"]["provenance"]["source"] == "umi-calibration intrinsics_fallback"
    frames = entry["frames"]
    assert len(frames) == ROWS
    stretch = np.diag([F.WH[0] / F.CAL_WH[0], F.WH[1] / F.CAL_WH[1], 1.0])
    T_ct = np.asarray(F.T_CT, float)
    for r, row in enumerate(frames):
        i = F.POSE_FROM + r
        # the checks' clock: the first pose message is zero; robot0's camera message comes 10 ms after it
        assert row["timestamp_s"] == pytest.approx(r * F.STEP / 1e9 + 0.010, abs=1e-6)
        assert row["eef"] is None
        for h, cid in CAMS.items():
            cam = row["cameras"][cid]
            assert cam["video_frame_index"] == (i - F.PRE if h == "robot0" else i)
            assert np.allclose(cam["H_media_from_calibration"], stretch)
            missing = i in (F.GAP0 if h == "robot0" else F.GAP1)
            hand = row["hands"][h]
            if missing:                                   # no pose within the tolerance: missing, never invented
                assert hand is None and cam["T_reference_camera"] is None and cam["calibration_id"] is None
                continue
            assert np.allclose(cam["T_reference_camera"], F.camera_pose(h, i), atol=1e-9)
            pose = hand["pose"]
            assert pose["reference_frame"] == f"{h}_vio_world" and pose["frame_id"] == h
            tcp = F.camera_pose(h, i) @ T_ct
            assert np.allclose(pose["position_m"], tcp[:3, 3], atol=2e-6)
            assert np.allclose(Rotation.from_quat(pose["quaternion_xyzw"]).as_matrix(), tcp[:3, :3], atol=1e-9)
            assert hand["opening_m"] == pytest.approx(F.opening(h, i))
    # robot0's own clock is the host's; robot1's is not - its video still plays from its own first message
    assert frames[0]["cameras"]["robot0_camera0"]["video_timestamp_s"] == pytest.approx((F.POSE_FROM - F.PRE) * F.STEP / 1e9, abs=1e-6)
    assert frames[0]["cameras"]["robot1_camera0"]["video_timestamp_s"] == pytest.approx(F.POSE_FROM * F.STEP / 1e9, abs=1e-6)


def test_the_bundle_is_what_the_platform_accepts(exported, das):
    _, out, _, _ = exported
    result = load.load_bundle(out, lerobot_root=das / "data")
    assert result.ok, result.report
    s = result.samples[0]
    assert s.n_frames == ROWS and set(s.hand_poses) == set(CAMS)
    assert np.isnan(s.hand_poses["robot0"][[i - F.POSE_FROM for i in F.GAP0], 0, 0]).all()


def test_the_module_can_give_its_opinion_on_each_hands_camera(exported, das):
    """The task's preflight: each camera shows its own hand's tool centre, so the model can be asked; the camera's
    own motion is the sub-item that reads the trajectory - its own hand never moves in the picture (§5.3)."""
    from curation.extensions.eef_consistency import preflight as PF

    _, out, _, _ = exported
    entry = PF.consistency_entry({"trajectory_json": str(out)}, episodes=[0, 1], media_exists=None,
                                 lerobot_root=str(das / "data"))
    assert entry["availability"] == "available", entry
    assert entry["subitems"]["ego_motion"] == {"availability": "available", "reason_code": None}
    assert entry["subitems"]["position_2d"] == {"availability": "unsupported", "reason_code": "own_hand_camera"}
    assert entry["episode_counts"] == {"available": 1, "unsupported": 1}       # episode 1 is not in the file
    s = load.load_bundle(out, check_media=False).samples[0]
    assert "tcp" in load.declared_point_ids(s, "robot1_camera0")
    s.hand_poses["robot1"][:] = np.nan                                        # a hand never posed: nothing to show
    assert load.declared_point_ids(s, "robot1_camera0") == [] and "tcp" in load.declared_point_ids(s, "robot0_camera0")


def test_the_report_says_where_everything_came_from_and_what_is_missing(exported):
    result, out, _, report = exported
    assert report["schema_version"] == "umi-export-report/1" and report["generator"] == "eef export-umi-mcap"
    assert report["calibration"]["assumed"] == ["T_camera_tcp"] == result["assumed"]
    assert report["calibration"]["assurance"]["T_camera_tcp"] == "model_assumed"
    assert report["calibration"]["assurance"]["body_to_optical"] == "declared"
    assert report["trajectory"]["path"] == str(out) and report["suspects"] == 0 == result["suspects"]
    (ep,) = report["episodes"]
    assert ep["status"] == "ok" and ep["suspects"] == []
    assert ep["anchor_topic"] == "/robot0/vio/eef_pose" and ep["rows"] == ROWS
    # the frames between the first keyframe and the first pose are not on the checks' clock
    assert ep["dropped_before_anchor"] == F.POSE_FROM - F.KEYINT
    c0, c1 = ep["cameras"]["robot0"], ep["cameras"]["robot1"]
    assert c0["messages"] == F.N - F.PRE and c0["first_decodable"] == F.FIRST_KEY and c0["video_wh"] == list(F.WH)
    assert (c0["intrinsics"], c1["intrinsics"]) == ("camera_info", "intrinsics_fallback")
    assert c0["paired"] == ROWS - len(F.GAP0) and c1["paired"] == ROWS - len(F.GAP1)
    assert c0["pairing_rate"] == pytest.approx(1 - len(F.GAP0) / ROWS, abs=1e-4)
    r0, r1 = F.GAP0[0] - F.POSE_FROM, F.GAP1[0] - F.POSE_FROM
    assert [m[:2] for m in c0["missing_segments"]] == [[r0, r0 + len(F.GAP0) - 1]]
    assert [m[:2] for m in c1["missing_segments"]] == [[r1, r1 + len(F.GAP1) - 1]]
    # the slow stretches are on the episode's timeline, robot1's device clock notwithstanding
    for cam, gap in ((c0, F.GAP0), (c1, F.GAP1)):
        ((a, b),) = cam["slow_pose_segments"]
        assert a == pytest.approx((gap[0] - 1 - F.POSE_FROM) * F.STEP / 1e9, abs=0.03)
        assert b == pytest.approx((gap[-1] + 1 - F.POSE_FROM) * F.STEP / 1e9, abs=0.03)
    assert c0["fx_fy"] == pytest.approx(1.0, abs=1e-3)
    assert ep["hands"]["robot0"]["opening_m"] == [pytest.approx(0.02, abs=1e-3), pytest.approx(0.08, abs=1e-3)]
    # the camera's own motion in its pictures agrees with the poses; robot1's plain pictures say nothing
    ego = ep["ego_motion"]
    assert ego["robot0"]["pairs"] >= 2 and ego["robot0"]["rotation_median_deg"] < 2.0
    assert ego["robot0"]["relative_median"] < 0.3 and ego["robot0"]["direction_median_deg"] < 15
    assert ego["robot1"]["pairs"] == 0 and ego["robot1"]["rotation_median_deg"] is None


@pytest.mark.parametrize("wrong", ["identity", "transposed", "rolled"])
def test_a_wrong_body_to_optical_rotation_shows_in_the_cameras_own_motion(das, wrong):
    """Design doc 22 §7 row 2: the pictures turn one way, the poses another."""
    R = np.asarray(F.R_BO, float)
    rbo = {"identity": np.eye(3), "transposed": R.T, "rolled": R @ np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]])}[wrong]
    ep = _episode(das, f"wrong_{wrong}", ego_check=True, body_to_optical=rbo.tolist())
    (s,) = [x for x in ep.report["suspects"] if x["row"] == 2]
    assert s["code"] == "calibration_suspect" and s["camera"] == "robot0"
    assert ep.report["ego_motion"]["robot0"]["rotation_median_deg"] > 3.0


def test_the_report_flags_intrinsics_pairing_and_openings_that_do_not_add_up(das):
    fallback = copy.deepcopy(F.calibration()["intrinsics_fallback"])
    fallback["robot1"]["K"] = [[200.0, 0, 160.0], [0, 200.0, 120.0], [0, 0, 1]]   # square pixels before the stretch
    ep = _episode(das, "fxfy", intrinsics_fallback=fallback)
    assert [(x["row"], x["camera"]) for x in ep.report["suspects"]] == [(3, "robot1")]
    assert ep.report["cameras"]["robot1"]["fx_fy"] == pytest.approx(1.5 / 1.625, abs=1e-3)
    ep = _episode(das, "tight", pairing_tolerance_s=0.0005)                 # the poses come 1 ms after the frames
    assert sorted((x["row"], x["code"], x["camera"]) for x in ep.report["suspects"]) == [
        (7, "timing_suspect", "robot0"), (7, "timing_suspect", "robot1")]
    assert ep.report["cameras"]["robot0"]["pairing_rate"] == 0.0
    ep = _episode(das, "scale", opening={"unit": "m", "scale": 10.0})
    assert sorted((x["row"], x["hand"]) for x in ep.report["suspects"]) == [(8, "robot0"), (8, "robot1")]
    mm = _episode(das, "mm", opening={"unit": "mm", "scale": 1000.0})          # the same metres, said another way
    assert mm.report["suspects"] == [] and mm.report["hands"]["robot0"]["opening_m"] == [pytest.approx(0.02, abs=1e-3), pytest.approx(0.08, abs=1e-3)]


def test_a_camera_without_intrinsics_is_left_out_and_says_why(das, tmp_path):
    cal = F.calibration()
    del cal["intrinsics_fallback"]
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(cal))
    out = tmp_path / "out" / "trajectory.json"
    result = X.export(das / "data", path, out, ego_check=False)
    ep = json.loads((out.parent / "umi-export-report.json").read_text())["episodes"][0]
    assert ep["status"] == "ok" and ep["cameras"]["robot1"]["status"] == "unsupported"
    assert "camera_info" in ep["cameras"]["robot1"]["reason"] and "intrinsics_fallback" in ep["cameras"]["robot1"]["reason"]
    s = json.loads(out.read_text())["samples"][0]
    assert s["sample"]["umi"]["camera_hands"] == {"robot0_camera0": "robot0"}
    assert all(set(f["hands"]) == {"robot0"} and set(f["cameras"]) == {"robot0_camera0"} for f in s["frames"])
    assert result["samples"] == 1 and load.load_bundle(out, lerobot_root=das / "data").ok


def test_world_frames_need_1_1_and_each_hand_stays_in_its_cameras_world(exported, tmp_path):
    _, _, bundle, _ = exported

    def check(doc) -> load.LoadResult:
        path = tmp_path / "t.json"
        path.write_text(json.dumps(doc))
        return load.load_bundle(path, check_media=False)

    assert check(bundle).ok
    old = copy.deepcopy(bundle)                                  # a 1.0.0 file cannot say per_hand
    entry = old["samples"][0]
    for doc in [old, entry["sample"], entry["calibration"], *entry["frames"]]:
        doc["schema_version"] = "eef-video/1.0.0"
    res = check(old)
    assert not res.ok and any("world_frames" in e.message for e in res.errors)
    shared = copy.deepcopy(bundle)                               # without per_hand, one world for every camera
    del shared["samples"][0]["sample"]["umi"]["world_frames"]
    assert not check(shared).ok
    crossed = copy.deepcopy(bundle)                              # a hand's pose in the other hand's world
    for f in crossed["samples"][0]["frames"]:
        if f["hands"]["robot1"]:
            f["hands"]["robot1"]["pose"]["reference_frame"] = "robot0_vio_world"
    assert not check(crossed).ok


def test_short_gaps_are_bridged_as_far_as_asked(exported, das):
    _, out, _, _ = exported
    s = load.load_bundle(out, check_media=False).samples[0]
    assert umi.default_gap_s(s) == pytest.approx(3 / F.FPS, abs=1e-4)
    g0 = [i - F.POSE_FROM for i in F.GAP0]
    g1 = [i - F.POSE_FROM for i in F.GAP1]
    before = copy.deepcopy(s.hand_poses)
    done = umi.fill_gaps(s)
    # robot0's two missing poses lie between poses 3 intervals apart; robot1's six between 7
    assert done == {"max_gap_s": pytest.approx(0.1, abs=1e-4), "frames": {"robot0": len(F.GAP0), "robot1": 0}}
    a, b = g0[0] - 1, g0[-1] + 1
    for k, r in enumerate(g0, 1):
        alpha = k / (b - a)
        assert np.allclose(s.hand_poses["robot0"][r, :3, 3], (1 - alpha) * before["robot0"][a, :3, 3] + alpha * before["robot0"][b, :3, 3])
        assert np.isfinite(s.cameras["robot0_camera0"].T_reference_camera[r]).all()
        assert abs(np.linalg.det(s.hand_poses["robot0"][r, :3, :3]) - 1) < 1e-9
    assert np.isnan(s.hand_poses["robot1"][g1, 0, 0]).all()
    wider = load.load_bundle(out, check_media=False).samples[0]
    assert umi.fill_gaps(wider, 0.25)["frames"] == {"robot0": len(F.GAP0), "robot1": len(F.GAP1)}
    assert np.isfinite(wider.cameras["robot1_camera0"].T_reference_camera[g1]).all()
    narrow = load.load_bundle(out, check_media=False).samples[0]
    assert umi.fill_gaps(narrow, 0.05)["frames"] == {"robot0": 0, "robot1": 0}


def test_the_command_writes_the_bundle_and_the_report(das, tmp_path, capsys):
    out = tmp_path / "x" / "trajectory.json"
    rc = cli.main(["export-umi-mcap", "--mcap-root", str(das / "data"), "--calibration", _cal(tmp_path / "cal.json"),
                   "--out", str(out), "--dataset-id", "das/cli", "--no-ego-check"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0 and doc["samples"] == 1 and doc["trajectory"] == str(out)
    assert json.loads(out.read_text())["dataset"]["id"] == "das/cli"
    assert "ego_motion" not in json.loads((out.parent / "umi-export-report.json").read_text())["episodes"][0]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**F.calibration(), "schema_version": "umi-calibration/1"}))
    assert cli.main(["export-umi-mcap", "--mcap-root", str(das / "data"), "--calibration", str(bad), "--out", str(out)]) == 2
    assert "umi-calibration/2" in capsys.readouterr().err


@pytest.mark.parametrize("change, message", [
    ({"body_to_optical": [[1, 0, 0], [0, 1, 0], [0, 0, -1]]}, "proper rotation"),
    ({"T_camera_tcp": [[2, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]}, "rigid"),
    ({"pairing_tolerance_s": 0}, "umi-calibration/2"),
    ({"opening": {"unit": "inch", "scale": 1}}, "umi-calibration/2"),
])
def test_a_calibration_file_that_cannot_be_right_is_refused(tmp_path, change, message):
    with pytest.raises(X.ExportError, match=message):
        X.read_calibration(_cal(tmp_path / "c.json", **change))
    fallback = copy.deepcopy(F.calibration()["intrinsics_fallback"])
    fallback["robot1"]["K"] = [[0.0, 0, 160.0], [0, 180.0, 120.0], [0, 0, 1]]
    with pytest.raises(X.ExportError, match="invalid K"):
        X.read_calibration(_cal(tmp_path / "k.json", intrinsics_fallback=fallback))
