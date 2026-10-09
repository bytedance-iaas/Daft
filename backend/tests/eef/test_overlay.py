"""The EEF marks as the mini player's layers (design doc 22 §3.2): what each layer is and whether it is on
by default or was shown to the model, the tool's three axes (the bundle's own axes from P, else P plus
6 cm through the calibration - checked against OpenCV), P's future, a missing pose never bridged, the
observed group of a measured episode, and the UMI hand's layers."""
from __future__ import annotations

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from curation.extensions.eef_consistency import load, overlay

from . import demo_data, synth
from .test_umi import exported  # noqa: F401 - the UMI fixture


def _scene(tmp_path, n: int = 45, change=None):
    entry = synth.make_entry(n=n)
    if change:
        change(entry)
    path = synth.write_bundle(tmp_path / "trajectory.json", synth.make_bundle([entry]))
    result = load.load_bundle(path, check_media=False)
    assert result.ok, result.report
    return result.samples[0], entry


def _layers(cam: dict) -> dict[str, dict]:
    return {x["id"]: x for x in cam["layers"]}


def _xy(v) -> np.ndarray:
    return np.asarray(v, float).reshape(-1, 2)


def test_every_layer_says_what_it_is(tmp_path):
    s, _ = _scene(tmp_path)
    (cam,) = overlay.episode_overlay(s)
    layers = _layers(cam)
    assert [x["id"] for x in cam["layers"]][-1] == "point"           # P on top
    assert {k: x["group"] for k, x in layers.items()} == {
        "trail_past": "trail_past", "trail_future": "trail_future", "axis_x": "axes", "axis_y": "axes",
        "axis_z": "axes", "axis": "declared", "point": "declared"}
    assert {k for k, x in layers.items() if not x["default_on"]} == {"axis", "trail_future"}
    assert {k for k, x in layers.items() if x["in_model"]} == {"trail_past", "axis", "point"}
    assert {x["hand"] for x in cam["layers"]} == {"eef"}
    assert layers["axis"]["model_color"] == "#ff0000" and layers["axis"]["color"] != "#ff0000"
    assert [layers[f"axis_{k}"]["color"] for k in "xyz"] == ["#ff3b30", "#34c759", "#2f7bff"]
    assert cam["hands"] == [{"id": "eef", "title": "panda_link8", "color": "#ff0000", "opening_m": None}]


def test_the_axes_start_at_p_and_point_along_the_tool(tmp_path):
    s, _ = _scene(tmp_path)
    (cam,) = overlay.episode_overlay(s)
    layers = _layers(cam)
    pos, quat, grip = synth.eef_trajectory(45)
    R = Rotation.from_quat(quat).as_matrix()
    T = synth.camera_pose()
    for f in (0, 20, 44):
        p = _xy(layers["point"]["frames"][f])[0]
        for k, e in zip("xyz", np.eye(3)):
            x1, y1, x2, y2 = layers[f"axis_{k}"]["frames"][f]
            assert np.allclose([x1, y1], p, atol=0.15)
            # x and y are P + 6 cm through the calibration, z is the bundle's own tcp -> tcp_z: the same place
            end = R[f] @ (synth.TCP + overlay.AXIS_M * e) + pos[f]
            uv, _ = synth.cv_project(end[None], T)
            assert np.allclose([x2, y2], uv[0], atol=0.15), (f, k)


def test_the_past_ends_and_the_future_starts_at_p(tmp_path):
    s, _ = _scene(tmp_path)
    (cam,) = overlay.episode_overlay(s)
    layers = _layers(cam)
    f = 20
    p = _xy(layers["point"]["frames"][f])[0]
    past, future = _xy(layers["trail_past"]["frames"][f]), _xy(layers["trail_future"]["frames"][f])
    assert np.allclose(past[-1], p, atol=0.15) and np.allclose(future[0], p, atol=0.15)
    assert not np.allclose(future[-1], p, atol=1.0)                 # it goes somewhere
    assert layers["trail_future"]["frames"][-1] is None             # nothing after the last frame


def test_a_missing_pose_is_never_bridged(tmp_path):
    def drop(entry):
        for f in entry["frames"][18:21]:
            f["eef"] = None
            f["cameras"]["cam0"]["projection"]["points"] = {}

    s, _ = _scene(tmp_path, change=drop)
    (cam,) = overlay.episode_overlay(s)
    layers = _layers(cam)
    assert layers["point"]["frames"][19] is None and layers["axis_x"]["frames"][19] is None
    past = _xy(np.array(layers["trail_past"]["frames"][22], dtype=float))   # its second spans the gap
    gap = np.flatnonzero(np.isnan(past).any(1))
    assert len(gap) and np.isfinite(past[: gap[0]]).all() and np.isfinite(past[gap[-1] + 1:]).all()
    assert np.isfinite(past[-1]).all()                             # still ends at P


def test_observed_rows_become_tracks():
    rows = [{"frame_index": 2, "pixel_space": "media", "points": {"tcp": {"uv_px": [10.0, 20.0]}, "x": {"uv_px": None}}},
            {"frame_index": 3, "pixel_space": "calibration", "points": {"tcp": {"uv_px": [1.0, 1.0]}}},
            {"frame_index": 9, "points": {"tcp": {"uv_px": [5.0, 5.0]}}},
            {"points": {"tcp": {"uv_px": [7.0, 7.0]}}}]
    tracks = overlay.observed_tracks(rows, 5)
    assert set(tracks) == {"tcp"} and tracks["tcp"].shape == (5, 2)
    assert np.allclose(tracks["tcp"][2], [10, 20]) and np.isnan(tracks["tcp"][[0, 1, 3, 4]]).all()


def _observed(s, entry, dx: float) -> dict:
    track = np.array([f["cameras"]["cam0"]["projection"]["points"]["tcp"]["uv_px"] for f in entry["frames"]])
    return {"cam0": {"tcp": track + [dx, 0.0]}}


def test_a_measured_episode_draws_the_observed_p(tmp_path):
    s, entry = _scene(tmp_path)
    (cam,) = overlay.episode_overlay(s, _observed(s, entry, 4.0), judged=True)
    layers = _layers(cam)
    assert layers["observed_point"]["kind"] == "cross" and layers["observed_point"]["default_on"]
    assert not layers["residual"]["default_on"]
    assert {k for k, x in layers.items() if x["in_model"]} == {"point", "axis", "observed_point"}
    x1, y1, x2, y2 = layers["residual"]["frames"][10]
    assert np.allclose([x2 - x1, y2 - y1], [4.0, 0.0], atol=0.15)
    trail = _xy(layers["observed_trail"]["frames"][10])
    assert np.allclose(trail[-1], _xy(layers["observed_point"]["frames"][10])[0])


def test_a_moving_camera_has_no_observed_trail(tmp_path):
    def moving(entry):
        entry["sample"]["views"][0]["mount"] = "moving"

    s, entry = _scene(tmp_path, change=moving)
    (cam,) = overlay.episode_overlay(s, _observed(s, entry, 4.0), judged=True)
    assert "observed_trail" not in _layers(cam) and "observed_point" in _layers(cam)


def test_a_umi_camera_draws_its_own_hand(exported):  # noqa: F811
    _, s = exported
    cams = overlay.episode_overlay(s)
    for cam, hand in zip(cams, ("robot0", "robot1")):
        layers = _layers(cam)
        assert set(layers) == {"trail_past", "trail_future", "finger_axis", "axis_x", "axis_y", "axis_z", "axis", "point"}
        assert {x["hand"] for x in cam["layers"]} == {hand} and layers["point"]["label"] == hand
        assert {k for k, x in layers.items() if x["in_model"]} == {"trail_past", "finger_axis", "axis", "point"}
        assert cam["hands"][0]["id"] == hand
        assert cam["hands"][0]["opening_m"] == pytest.approx(list(np.round(s.hand_openings[hand], 4)))
        f = 8
        p = _xy(layers["point"]["frames"][f])[0]
        assert np.allclose(_xy(layers["axis_z"]["frames"][f])[0], p)
        z, a = np.reshape(layers["axis_z"]["frames"][f], (2, 2)), np.reshape(layers["axis"]["frames"][f], (2, 2))
        assert np.allclose(z[0], a[0])                     # the approach is the tool z, drawn longer (umi.AXIS_M)
        dz, da = z[1] - z[0], a[1] - a[0]
        assert abs(dz[0] * da[1] - dz[1] * da[0]) < 1e-6 * np.hypot(*dz) * np.hypot(*da) + 0.5 and dz @ da > 0
        assert np.allclose(_xy(layers["trail_future"]["frames"][f])[0], p, atol=0.15)
        assert layers["trail_future"]["color"] != layers["trail_past"]["color"]


def test_dataset2_declares_its_axes_and_b_turns_in_episode_two():
    root = demo_data.require("dataset2")
    r = load.load_bundle(root / "trajectory.json", check_media=False, episodes=[0, 2])
    assert r.ok
    turn = []
    for ep in (0, 2):
        cams = overlay.episode_overlay(r.samples[ep])
        cam = next(c for c in cams if c["camera_id"] == "27432424_left")
        layers = _layers(cam)
        assert layers["point"]["title"] == "中心点 P（tcp）" and layers["finger_axis"]["title"] == "两指连线 B（y）"
        assert cam["hands"][0]["opening_m"][0] == pytest.approx(0.085)
        # the bundle's own tcp -> tcp_y, as it provided it
        tcp_y = [f["cameras"]["27432424_left"]["projection"]["points"]["tcp_y"]["uv_px"]
                 for f in demo_data_frames(root, ep)]
        ends = np.array([v[2:] for v in layers["axis_y"]["frames"]], float)
        assert np.allclose(ends, tcp_y, atol=0.06)
        d = ends - np.array([v[:2] for v in layers["axis_y"]["frames"]], float)
        turn.append(np.degrees(np.arctan2(d[:, 1], d[:, 0])))
    assert np.median(np.abs((turn[1] - turn[0] + 180) % 360 - 180)) > 10


def demo_data_frames(root, ep: int) -> list[dict]:
    import json

    doc = json.loads((root / "trajectory.json").read_text())
    return next(e for e in doc["samples"] if e["episode_index"] == ep)["frames"]
