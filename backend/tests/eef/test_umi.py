"""UMI projection and raw-session export, using independently projected geometry."""
from __future__ import annotations

import copy
import json
import pickle
from fractions import Fraction

import av
import cv2
import numpy as np
import pandas as pd
import pytest
from scipy.spatial.transform import Rotation

from curation.extensions.eef_consistency import geometry as G, load, opinion as OP, review as R, umi
from curation.extensions.eef_consistency import history as trail_history
from curation.extensions.eef_consistency.adapters.umi import export, read_plan, read_calibration


@pytest.fixture
def exported(tmp_path):
    root = tmp_path / "raw"
    root.mkdir()
    n, rate = 16, Fraction(60000, 1001)
    cameras, grippers = [], []
    Tct = np.eye(4)
    Tct[:3, 3] = [0, .086, .21965]
    for j in range(2):
        first = j + 2
        d = root / "demos" / f"demo{j}"
        d.mkdir(parents=True)
        pos = np.array([[.01 * i + j * .15, 0., 0.] for i in range(n + 5)])
        rot = Rotation.from_euler("y", np.arange(n + 5) * .015)
        T = G.se3(rot.as_matrix(), pos)
        E = T[first:first+n] @ Tct
        grippers.append({"tcp_pose": np.c_[E[:, :3, 3], Rotation.from_matrix(E[:, :3, :3]).as_rotvec()],
                         "gripper_width": np.linspace(.08, .02, n)})
        pd.DataFrame({"timestamp": np.arange(n+5) / float(rate), "is_lost": False,
                      **{a: pos[:, k] for k, a in enumerate("xyz")},
                      **{a: rot.as_quat()[:, k] for k, a in enumerate(["q_x", "q_y", "q_z", "q_w"])}}).to_csv(d / "camera_trajectory.csv", index=False)
        with av.open(str(d / "raw_video.mp4"), "w") as out:
            stream = out.add_stream("libx264", rate=rate)
            stream.width, stream.height, stream.pix_fmt = 160, 120, "yuv420p"
            stream.options = {"crf": "0"}
            for i in range(n + 5):
                img = np.zeros((120, 160, 3), np.uint8)
                img[:] = i * 8
                for packet in stream.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                    out.mux(packet)
            for packet in stream.encode():
                out.mux(packet)
        cameras.append({"video_path": f"demo{j}/raw_video.mp4", "video_start_end": (first, first+n)})
    with open(root / "dataset_plan.pkl", "wb") as f:
        pickle.dump([{"episode_timestamps": np.arange(n) / float(rate), "cameras": cameras, "grippers": grippers}], f)
    cal = {"K": [[70, 0, 80], [0, 70, 60], [0, 0, 1]], "model": "opencv_fisheye",
           "distortion_coefficients": [.05, -.01, .002, -.0005], "image_size_wh": [160, 120], "T_camera_tcp": Tct.tolist()}
    config = {"schema_version": "umi-calibration/1", "dataset_id": "test/umi", "instruction": "Transfer objects",
              "T_world_slam": np.eye(4).tolist(), "cameras": {"camera0": cal, "camera1": cal},
              "provenance": {"source": "test", "method": "synthetic", "assurance": "synthetic"}}
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(config))
    dest = tmp_path / "dataset"
    export(root, path, dest)
    result = load.load_bundle(dest / "trajectory.json", lerobot_root=dest)
    assert result.ok, result.report
    return dest, result.samples[0]


def test_raw_export_alignment_and_rotation(exported):
    root, s = exported
    assert s.n_frames == 16 and len(s.cameras) == 2
    df = pd.read_parquet(root / "data/chunk-000/episode_000000.parquet")
    # rot6d columns reconstruct the axis-angle rotation, not Euler angles.
    state = np.stack(df["observation.state"])
    np.testing.assert_allclose(state[:, 3:6], s.hand_poses["robot0"][:, :3, 0], atol=1e-7)
    np.testing.assert_allclose(state[:, 6:9], s.hand_poses["robot0"][:, :3, 1], atol=1e-7)
    for j, cam in enumerate(s.cameras.values()):
        with av.open(str(root / cam.media["uri"])) as inp:
            frames = list(inp.decode(video=0))
        assert len(frames) == 16
        assert abs(int(frames[0].to_ndarray(format="rgb24")[60, 80, 0]) - (j + 2) * 8) < 5


def test_history_uses_current_camera_and_matches_opencv(exported):
    _, s = exported
    frame, ids = 12, np.array([3, 7, 12])
    uv, _ = umi.project(s, "camera0", frame, "robot0", indices=ids)
    cam = s.cameras["camera0"]
    cal = cam.calibration(s)
    T = np.linalg.inv(cam.T_reference_camera[frame])
    p = s.hand_poses["robot0"][ids, :3, 3] @ T[:3, :3].T + T[:3, 3]
    expected, _ = cv2.fisheye.projectPoints(p.reshape(-1, 1, 3), np.zeros(3), np.zeros(3),
                                           np.array(cal["K"], float), np.array(cal["distortion_coefficients"]))
    np.testing.assert_allclose(uv, expected[:, 0], atol=1e-8)
    assert np.linalg.norm(uv[-1] - uv[0]) > 10
    current = np.array([umi.project(s, "camera0", i, "robot0")[0][0] for i in ids])
    np.testing.assert_allclose(current, np.broadcast_to(current[0], current.shape), atol=1e-8)
    np.testing.assert_allclose(uv[-1], current[-1], atol=1e-8)
    s.sample["umi"]["horizon_s"] = .1
    for f in range(s.n_frames):
        history = umi.history_indices(s, f)
        assert history[-1] == f and np.all(history <= f)
        assert np.all(s.t[history] >= s.t[f] - .1)


def test_world_change_cancels_and_other_hand_is_independent(exported):
    _, s = exported
    reference, _ = umi.project(s, "camera0", 2, "robot1", indices=[2, 5, 9])
    W = G.se3(Rotation.from_euler("xyz", [.2, -.4, .8]).as_matrix(), [2, 3, -1])
    for key in s.hand_poses:
        s.hand_poses[key] = W @ s.hand_poses[key]
    for cam in s.cameras.values():
        cam.T_reference_camera = W @ cam.T_reference_camera
    changed, _ = umi.project(s, "camera0", 2, "robot1", indices=[2, 5, 9])
    np.testing.assert_allclose(changed, reference, atol=1e-8)
    own, _ = umi.project(s, "camera0", 2, "robot0")
    s.hand_poses["robot1"][:, 0, 3] += .1
    np.testing.assert_allclose(umi.project(s, "camera0", 2, "robot0")[0], own)
    assert not np.allclose(umi.project(s, "camera0", 2, "robot1", indices=[2, 5, 9])[0], reference)
    # Each camera's rendered pixels must be independent of the other hand's data.
    img = np.zeros((120, 160, 3), np.uint8)
    for cid, owner in s.sample["umi"]["camera_hands"].items():
        other = next(hand for hand in s.hand_poses if hand != owner)
        rendered = umi.draw(img, s, cid, 2, 1.)
        changed = copy.deepcopy(s)
        changed.hand_poses[other][:] = np.nan
        changed.hand_openings[other][:] = .999
        np.testing.assert_array_equal(umi.draw(img, changed, cid, 2, 1.), rendered)


def test_missing_pose_and_timestamp_gaps_are_not_joined(exported):
    _, s = exported
    s.hand_poses["robot1"][5] = np.nan
    uv, _ = umi.project(s, "camera0", 3, "robot1", indices=[3, 5, 7])
    assert np.isnan(uv[1]).all() and np.isfinite(uv[[0, 2]]).all()
    s.t[6:] += .5
    assert list(umi.history_indices(s, 5)) == [0, 1, 2, 3, 4, 5]
    assert list(umi.history_indices(s, 8)) == [6, 7, 8]
    s.sample["umi"]["horizon_s"] = 0
    assert list(umi.history_indices(s, 3)) == [3]


def test_rejects_unanchored_or_mismatched_hands(exported, tmp_path):
    root, _ = exported
    original = json.loads((root / "trajectory.json").read_text())
    for change in ("reference", "missing", "quaternion"):
        doc = copy.deepcopy(original)
        hands = doc["samples"][0]["frames"][0]["hands"]
        if change == "reference":
            hands["robot1"]["pose"]["reference_frame"] = "different_world"
        elif change == "missing":
            del hands["robot1"]
        else:
            hands["robot1"]["pose"]["quaternion_xyzw"] = [0, 0, 0, 0]
        path = tmp_path / f"{change}.json"
        path.write_text(json.dumps(doc))
        assert not load.load_bundle(path, check_media=False).ok


def test_continuous_raw_marked_and_action_evidence(exported, tmp_path):
    root, s = exported
    seen = []

    def ask(req, history):
        assert len(req.videos) == 2
        assert req.videos[0].camera.endswith("RAW") and req.videos[1].camera.endswith("MARKED")
        assert req.videos[0].sha256 != req.videos[1].sha256
        assert "CURRENT frame" in req.text and "approximately fixed" in req.text
        assert "PAST recorded TCP positions" in req.text
        assert trail_history.prompt(1, "TCP") in req.text
        owner = s.sample["umi"]["camera_hands"][req.window.camera_id]
        assert f"Only the camera's own hand {owner} is annotated and assessed" in req.text
        seen.append(req)
        return json.dumps({"gripper_visible": True, "segments": [{"start_frame": 2, "end_frame": 6,
                          "aspect": "action", "confidence": .8, "evidence_frames": [4], "observation": "开合时机不符"}],
                          "summary": "需要检查抓取时机"})

    op = OP.opinion_episode(s, media_root=str(root), ask=ask, cache=R.Cache(str(tmp_path / "cache")), model="test",
                            allowed_mounts=["wrist"])
    assert op["status"] == "answered" and op["requests"] == 2 and op["flagged"]
    for cam in op["cameras"].values():
        assert cam["segments"][0]["start_frame"] == 1


def test_pickle_globals_and_missing_calibration_are_rejected(tmp_path):
    path = tmp_path / "bad.pkl"
    path.write_bytes(pickle.dumps(eval))
    with pytest.raises(ValueError, match="unsupported pickle global"):
        read_plan(path)
    path = tmp_path / "cal.json"
    path.write_text('{"schema_version": "umi-calibration/1"}')
    with pytest.raises(ValueError, match="dataset_id"):
        read_calibration(path)
