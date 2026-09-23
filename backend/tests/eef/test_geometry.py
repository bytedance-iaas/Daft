"""geometry.py against independent implementations (OpenCV projectPoints, SciPy rotations)."""
from __future__ import annotations

import cv2
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from curation.extensions.eef_consistency import geometry as G

from . import synth

RNG = np.random.default_rng(20260923)


def _points_in_front(n=200):
    return np.c_[RNG.uniform(-0.4, 0.4, n), RNG.uniform(-0.3, 0.3, n), RNG.uniform(0.4, 2.0, n)]


def test_quaternion_matches_scipy_and_sign_invariance():
    q = Rotation.random(50, random_state=1).as_quat()
    np.testing.assert_allclose(G.quat_xyzw_to_matrix(q), Rotation.from_quat(q).as_matrix(), atol=1e-12)
    np.testing.assert_allclose(G.quat_xyzw_to_matrix(-q), G.quat_xyzw_to_matrix(q), atol=1e-12)


def test_rotation_log_round_trip_including_near_zero_and_pi():
    rv = Rotation.random(100, random_state=2).as_rotvec()
    rv = np.r_[rv, [[1e-9, 0, 0], [0, 0, 0], [np.pi - 1e-7, 0, 0], [0, 0, np.pi]]]
    R = Rotation.from_rotvec(rv).as_matrix()
    back = Rotation.from_rotvec(G.rotation_log(R)).as_matrix()
    np.testing.assert_allclose(back, R, atol=1e-6)


def test_se3_inverse_and_rigidity():
    T = G.pose_matrix([0.1, -0.2, 0.3], Rotation.random(random_state=3).as_quat())
    np.testing.assert_allclose(G.se3_inverse(T) @ T, np.eye(4), atol=1e-12)
    assert G.is_rigid(T)
    bad = T.copy()
    bad[0, 0] = 4
    assert not G.is_rigid(bad)


@pytest.mark.parametrize("model,coeffs", [
    ("pinhole", []),
    ("opencv_brown", [0.12, -0.25, 0.001, -0.0007, 0.08]),
])
def test_projection_matches_opencv(model, coeffs):
    p = _points_in_front()
    K = synth.K
    uv, depth = G.project_camera_points(p, K, model, coeffs)
    ref, _ = cv2.projectPoints(p.reshape(-1, 1, 3), np.zeros(3), np.zeros(3), K,
                               np.asarray(coeffs, float) if coeffs else None)
    np.testing.assert_allclose(uv, ref.reshape(-1, 2), atol=1e-6)
    np.testing.assert_allclose(depth, p[:, 2])


def test_fisheye_matches_opencv():
    p = _points_in_front()
    coeffs = [0.05, -0.01, 0.002, -0.0005]
    uv, _ = G.project_camera_points(p, synth.K, "opencv_fisheye", coeffs)
    ref, _ = cv2.fisheye.projectPoints(p.reshape(-1, 1, 3), np.zeros((1, 1, 3)), np.zeros((1, 1, 3)), synth.K,
                                       np.asarray(coeffs, float).reshape(4, 1))
    np.testing.assert_allclose(uv, ref.reshape(-1, 2), atol=1e-6)


def test_points_behind_camera_have_no_pixels():
    uv, depth = G.project_camera_points(np.array([[0, 0, -1.0], [0.1, 0, 0.0], [0, 0, 1.0]]), synth.K)
    assert np.isnan(uv[:2]).all() and np.isfinite(uv[2]).all()
    assert depth[0] == -1.0


def test_full_chain_matches_opencv_with_media_transform():
    pos, quat, grip = synth.eef_trajectory(40)
    T_ref_eef = G.pose_matrix(pos, quat)
    T_bc = synth.camera_pose()
    offsets = np.repeat(synth.TCP[None], 40, 0)
    H = np.array([[0.5, 0, 10.0], [0, 0.5, -4.0], [0, 0, 1.0]])       # downscale + crop, format §5
    uv, depth = G.project_chain(T_ref_eef, offsets, T_bc, synth.K, "pinhole", [], H)
    pb = G.reference_points(T_ref_eef, offsets)
    ref, ref_depth = synth.cv_project(pb, T_bc)
    np.testing.assert_allclose(uv, ref * 0.5 + [10.0, -4.0], atol=1e-6)
    np.testing.assert_allclose(depth, ref_depth, atol=1e-9)


def test_linear_gripper_point_model():
    d = synth.POINTS["finger_plus_y"]
    off = G.point_offsets(d, np.array([0.0, 1.0, 0.5, np.nan]))
    np.testing.assert_allclose(off[0], [0, synth.HALF_OPEN, 0.16])
    np.testing.assert_allclose(off[1], [0, 0, 0.16])
    np.testing.assert_allclose(off[2], [0, synth.HALF_OPEN / 2, 0.16])
    assert np.isnan(off[3]).all()
    assert G.point_offsets({"model": "external_2d"}, np.zeros(3)) is None
