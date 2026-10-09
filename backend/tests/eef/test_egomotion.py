"""A wrist camera's own motion against its recorded poses over a whole episode (design doc 22 §5.3, F5.19).

Most cases feed the assessment synthetic picture motions - the true camera motion of a pair, slightly noisy -
against recorded poses that are right, late, in a wrong convention or jump; one case renders a room through the
true poses and goes through the decoding stream, the features and the matching; the DAS recordings are used
when they are on this machine (``demo_data``)."""
from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from curation.extensions.eef_consistency import egomotion as EM

from . import das_mcap as F
from . import demo_data

FPS = 30
N = 300
GAP = 15


def _true(i: int) -> np.ndarray:
    """A handheld camera's pose: travelling, turning a few degrees about each axis."""
    t = i / FPS
    s = lambda period, phase=0.0: math.sin(2 * math.pi * t / period + phase)  # noqa: E731
    T = np.eye(4)
    T[:3, :3] = Rotation.from_euler("xyz", [4 * s(1.7), 6 * s(1.3, 0.5), 9 * s(2.1)], degrees=True).as_matrix()
    T[:3, 3] = [0.1 * t, 0.15 * s(1.5), 0.06 * s(1.1)]
    return T


TRUE = np.stack([_true(i) for i in range(N)])
T_S = np.arange(N) / FPS
ROWS = EM.plan(N, GAP, 3)


def _seen(noise_deg: float = 0.3, blind=range(0), seed: int = 7) -> dict:
    """The pictures' motion of every pair: the true one, with a little rotation noise; None where ``blind``."""
    rng = np.random.default_rng(seed)
    out = {}
    for r in ROWS:
        if r in blind:
            out[r] = None
            continue
        R, t = EM.pose_motion(TRUE[r], TRUE[r + GAP])
        R = Rotation.from_rotvec(np.radians(noise_deg) * rng.standard_normal(3) / math.sqrt(3)).as_matrix() @ R
        out[r] = EM.Motion(R, t / max(np.linalg.norm(t), 1e-9), 300, 400)
    return out


def _assess(T, seen=None, **cfg):
    return EM.assess(T_S, T, seen if seen is not None else _seen(), ROWS, GAP, lag_search_s=1.0,
                     cfg={**EM.settings(), **cfg})


def test_the_right_poses_agree_with_the_pictures():
    got = _assess(TRUE.copy())
    m = got["metrics"]
    assert got["status"] == "ok" and got["segments"] == [] and got["unmatched"] == []
    assert m["rotation_median_deg"] < 1.0 and m["coverage"] == 1.0 and m["pairs"] == len(ROWS)
    assert got["lag"]["lag_s"] == 0.0 and not got["lag"]["flagged"] and got["lag"]["confidence"] > 0.5
    assert got["window_s"] == pytest.approx(0.5)


def test_a_late_pose_stream_is_found_as_a_time_offset():
    """The picture at t shows the pose recorded at t + lag: a stream 0.5 s late is lag +0.5 (design 12 §8.3)."""
    late = np.concatenate([np.full((GAP, 4, 4), np.nan), TRUE[:-GAP]])
    got = _assess(late)
    assert got["status"] == "suspect" and got["lag"]["flagged"]
    assert got["lag"]["lag_s"] == pytest.approx(0.5, abs=0.04)
    assert got["lag"]["median_at_lag_deg"] < 1.0 < got["lag"]["median_at_zero_deg"]
    reasons = {s["reason"] for s in got["segments"]}
    assert reasons == {"time_offset"}                         # shifting explains it: no rotation stretch on top
    seg = got["segments"][0]
    assert seg["unit"] == "s" and seg["magnitude"] == pytest.approx(0.5, abs=0.04) and seg["band"] == "severe"
    assert seg["start_frame"] <= seg["evidence_frames"][0] < seg["evidence_frames"][1] <= seg["end_frame"]
    early = np.concatenate([TRUE[GAP // 3:], np.full((GAP // 3, 4, 4), np.nan)])
    assert _assess(early)["lag"]["lag_s"] == pytest.approx(-GAP // 3 / FPS, abs=0.04)


def test_a_wrong_body_to_optical_rotation_is_severe():
    """The camera turning about another axis than the poses say: as much difference again as the turn itself."""
    R_bo = np.asarray(F.R_BO, float)
    C = np.eye(4)
    C[:3, :3] = R_bo.T                                        # the rotation forgotten
    wrong = np.einsum("nij,jk->nik", TRUE, C)
    got = _assess(wrong)
    assert got["status"] == "suspect" and not got["lag"]["flagged"]
    assert got["segments"] and {s["reason"] for s in got["segments"]} == {"rotation"}
    assert {s["band"] for s in got["segments"]} == {"severe"}
    assert got["metrics"]["relative_median"] > 1.0


def test_a_stretch_without_texture_is_listed_never_judged():
    blind = range(ROWS[20], ROWS[35] + 1)
    got = _assess(TRUE.copy(), _seen(blind=blind))
    assert got["status"] == "ok" and got["segments"] == []
    (u,) = got["unmatched"]
    assert u["reason"] == EM.LOW_COVERAGE and u["start_frame"] == ROWS[20] and u["end_frame"] == ROWS[35] + GAP
    assert got["metrics"]["coverage"] == pytest.approx(1 - 16 / len(ROWS), abs=1e-3)
    mostly_blind = _assess(TRUE.copy(), _seen(blind=range(0, ROWS[-12])))
    assert mostly_blind["status"] == "unknown" and mostly_blind["reason"] == EM.LOW_COVERAGE
    assert mostly_blind["segments"] == [] and mostly_blind["lag"] is None


def test_a_jump_of_the_poses_is_a_rotation_stretch_where_it_is():
    """A VIO that turned 12 degrees off for a second and came back."""
    jumped = TRUE.copy()
    off = np.eye(4)
    off[:3, :3] = Rotation.from_euler("y", 12, degrees=True).as_matrix()
    jumped[150:180] = jumped[150:180] @ off
    got = _assess(jumped)
    assert got["status"] == "suspect"
    segs = [s for s in got["segments"] if s["reason"] == "rotation"]
    assert segs and all(135 <= s["start_frame"] and s["end_frame"] <= 200 for s in segs)
    assert max(s["magnitude"] for s in segs) >= 10.0


def test_the_pairs_share_their_pictures():
    """Every third of a window: the far picture of a pair is the near picture of a later one."""
    assert EM.plan(30, 15, 3) == [0, 5, 10]
    used = {r for r in ROWS} | {r + GAP for r in ROWS}
    assert len(used) < 2 * len(ROWS)


def _rendered(n: int, wh=(400, 325), lag_rows: int = 0):
    """A rendered room seen by a moving fisheye camera, and the poses recorded ``lag_rows`` late."""
    rays = F._rays(wh)
    T = np.stack([_true(i) @ F.body_to_camera() for i in range(n)])
    pictures = [F.render(T[i], rays, wh) for i in range(n)]
    recorded = np.concatenate([np.full((lag_rows, 4, 4), np.nan), T[:n - lag_rows]]) if lag_rows else T
    return pictures, recorded, F.stretched_K(wh)


def test_rendered_pictures_measure_the_camera_and_find_a_late_stream():
    """The decoding stream, the features and the matching, on pictures of a scene with depth."""
    n = 120
    pictures, T, K = _rendered(n)
    rows = EM.plan(n, GAP, 3)
    frames = (SimpleNamespace(index=i, gray=g) for i, g in enumerate(pictures))
    seen = EM.measure(frames, np.arange(n), rows, GAP, K, F.D, "opencv_fisheye")
    assert set(seen) == set(rows) and sum(m is not None for m in seen.values()) >= len(rows) - 3
    t = np.arange(n) / FPS
    cfg = {**EM.settings(), "min_inliers": 40}               # small pictures: fewer features than a real camera's
    right = EM.assess(t, T, seen, rows, GAP, cfg=cfg)
    assert right["status"] == "ok" and right["metrics"]["rotation_median_deg"] < 2.0
    late = np.concatenate([np.full((GAP, 4, 4), np.nan), T[:-GAP]])
    got = EM.assess(t, late, seen, rows, GAP, cfg=cfg)
    assert got["status"] == "suspect" and got["lag"]["lag_s"] == pytest.approx(0.5, abs=0.07)
    # a stream without the near pictures of the first pairs (frames before the first keyframe)
    partial = EM.measure((SimpleNamespace(index=i, gray=g) for i, g in enumerate(pictures) if i >= 10),
                         np.arange(n), rows, GAP, K, F.D, "opencv_fisheye")
    assert 0 not in partial and 5 not in partial and 10 in partial


def test_the_episode_says_good_or_bad_in_one_sentence():
    ok = _assess(TRUE.copy())
    late = _assess(np.concatenate([np.full((GAP, 4, 4), np.nan), TRUE[:-GAP]]))
    out = EM.summarize({"robot0_camera0": ok, "robot1_camera0": late}, assumed="按假设值：T_camera_tcp 是假设")
    assert out["status"] == "suspect" and out["verdict"] == "bad" and out["uncalibrated"]
    assert out["worst"]["camera"] == "robot1_camera0" and out["worst"]["reason"] == "time_offset"
    assert out["explanation_zh"].startswith("腕部相机的运动与记录的位姿不一致：robot1_camera0：位姿约晚 0.5 s（第 ")
    assert out["assumed"].startswith("按假设值")
    good = EM.summarize({"robot0_camera0": ok})
    assert good["verdict"] == "good" and good["explanation_zh"].startswith("腕部相机画面里的转动与记录的位姿一致（旋转差中位 ")
    blind = _assess(TRUE.copy(), _seen(blind=range(0, N)))
    assert EM.summarize({"robot0_camera0": blind})["verdict"] == "unknown"


def test_the_report_counts_episodes_cameras_and_stretches():
    from curation.extensions.eef_consistency import report as RP

    ok = _assess(TRUE.copy())
    late = _assess(np.concatenate([np.full((GAP, 4, 4), np.nan), TRUE[:-GAP]]))
    blind = _assess(TRUE.copy(), _seen(blind=range(0, N)))
    results = {0: {"details": {"ego_motion": EM.summarize({"a": ok, "b": late})}},
               1: {"details": {"ego_motion": EM.summarize({"a": blind})}}, 2: {"details": {}}}
    s = RP.ego_summary(results)
    assert (s["ego_motion_episodes"], s["ego_motion_suspect"], s["ego_motion_unknown"]) == (2, 1, 1)
    assert (s["ego_motion_cameras"], s["ego_motion_cameras_suspect"]) == (3, 1)
    assert {x["name"]: x["count"] for x in s["ego_motion_bands"]}["severe"] == len(late["segments"])
    assert s["ego_motion_reasons"] == [{"name": "time_offset", "count": len(late["segments"])}]
    assert s["ego_motion_lag_median_s"] == pytest.approx(0.5, abs=0.04)
    rows = RP.table_rows("eef_ego_motion", results)
    assert [(r["episode_index"], r["camera"], r["status"]) for r in rows] == [(0, "a", "ok"), (0, "b", "suspect"), (1, "a", "unknown")]
    assert rows[1]["worst_band"] == "severe" and rows[2]["reason"] == EM.LOW_COVERAGE
    assert RP.ego_summary({2: {"details": {}}}) == {}


def test_only_a_wrist_camera_with_poses_and_a_calibration_is_read(tmp_path):
    from curation.extensions.eef_consistency import load, umi
    from curation.extensions.eef_consistency.adapters import umi_mcap

    data = tmp_path / "das"
    F.make_das(data)
    (tmp_path / "cal.json").write_text(__import__("json").dumps(F.calibration()))
    out = tmp_path / "out" / "trajectory.json"
    umi_mcap.export(data, tmp_path / "cal.json", out, ego_check=False)
    s = load.load_bundle(out, check_media=False).samples[0]
    umi.fill_gaps(s)
    from curation.extensions.eef_consistency import observations as O

    got = EM.camera_ego_motion(s, "robot0_camera0", lambda: O.view_frames(s, "robot0_camera0", data))
    assert got["status"] in ("ok", "unknown") and got["metrics"]["attempted"] > 0
    plain = EM.camera_ego_motion(s, "robot1_camera0", lambda: O.view_frames(s, "robot1_camera0", data))
    assert plain["status"] == "unknown" and plain["reason"] == EM.LOW_COVERAGE     # nothing to match in plain pictures
    s.cameras["robot0_camera0"].mount = "fixed_external"
    assert EM.camera_ego_motion(s, "robot0_camera0", lambda: iter(())) is None
    s.cameras["robot1_camera0"].T_reference_camera[:] = np.nan
    assert EM.camera_ego_motion(s, "robot1_camera0", lambda: iter(()))["reason"] == EM.NO_CAMERA_POSES


# ---------------------------------------------------------------- the DAS recordings, when they are here

DAS = demo_data.ROOT / "dataset2" / "umi_das"
DAS_BUNDLE = demo_data.ROOT / "umi" / "export2" / "trajectory.json"
needs_das = pytest.mark.skipif(not (DAS_BUNDLE.is_file() and (DAS / "episode_0.mcap").is_file()),
                               reason="DAS recordings and their export not present (design doc 22 §4)")


def _das(variant: str = ""):
    from curation.extensions.eef_consistency import load, umi
    from curation.extensions.eef_consistency import observations as O
    from curation.extensions.eef_consistency import profile as P

    s = load.load_bundle(DAS_BUNDLE, check_media=False, episodes=[0]).samples[0]
    umi.fill_gaps(s)
    cam = s.cameras["robot1_camera0"]
    if variant == "late":
        cam.T_reference_camera = np.concatenate([np.full((15, 4, 4), np.nan), cam.T_reference_camera[:-15]])
    elif variant == "identity":
        C = np.eye(4)
        C[:3, :3] = np.asarray(F.R_BO, float).T
        cam.T_reference_camera = np.einsum("nij,jk->nik", cam.T_reference_camera, C)
    return EM.camera_ego_motion(s, "robot1_camera0", lambda: O.view_frames(s, "robot1_camera0", DAS),
                                profile=P.load("demo"))


@needs_das
def test_das_recording_with_the_right_convention_is_good():
    got = _das()
    assert got["status"] == "ok" and got["metrics"]["rotation_median_deg"] < 2.0 and not got["lag"]["flagged"]


@needs_das
def test_das_recording_with_late_poses_or_a_forgotten_rotation_is_bad():
    late = _das("late")
    assert late["status"] == "suspect" and late["lag"]["lag_s"] == pytest.approx(0.5, abs=0.07)
    wrong = _das("identity")
    assert wrong["status"] == "suspect" and "severe" in {s["band"] for s in wrong["segments"]}
