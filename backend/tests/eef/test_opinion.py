"""The model's opinion without a gripper reference (design doc 12 §10.5, D-E15): what is drawn.

On dataset2 (DEMO data outside the repo; skipped without it) every camera gets P = tcp, A = z (approach)
and B = y (the fingers' line), B mirrored through P so it crosses both fingers. Episode 2 turns the
gripper 30 degrees about its approach axis: P and A stay put and only B shows it, so B must still be
chosen there although the turn shortens it on one camera.
"""
from __future__ import annotations

import io

import av
import cv2
import numpy as np
import pytest

from curation.extensions.eef_consistency import load
from curation.extensions.eef_consistency import opinion as OP
from curation.extensions.eef_consistency import history, review as R

from . import demo_data, synth


def _sample(ep: int):
    root = demo_data.require("dataset2")
    r = load.load_bundle(root / "trajectory.json", lerobot_root=demo_data.lerobot_root("dataset2"))
    assert r.ok
    return r.samples[ep]


def test_every_camera_gets_the_centre_the_approach_and_the_fingers_line():
    for ep in (0, 2):
        s = _sample(ep)
        for cid in s.cameras:
            frames = [i for i in range(s.n_frames) if s.cameras[cid].video_frame_index[i] >= 0]
            a = OP.pick_axis(s, cid, frames)
            assert (OP.pick_point(s, cid), a, OP.pick_finger_axis(s, cid, frames, a)) == ("tcp", "z", "y")


def test_the_fingers_line_crosses_the_gripper_and_turns_with_it():
    cid = "27432424_left"
    angle = []
    for ep in (0, 2):
        s = _sample(ep)
        start, end = OP._second_axis(s, cid, "y")
        centre = load.declared_track(s, cid, "tcp").uv
        ok = np.isfinite(start).all(1) & np.isfinite(end).all(1)
        assert np.allclose((start[ok] + end[ok]) / 2, centre[ok])          # mirrored through P
        d = end[ok] - start[ok]
        angle.append(np.degrees(np.arctan2(d[:, 1], d[:, 0])))
    turn = np.abs((angle[1] - angle[0] + 180) % 360 - 180)
    assert np.median(turn) > 10                                             # ep2's turn shows on B


@pytest.fixture
def scene(tmp_path):
    path = synth.write_bundle(tmp_path / "trajectory.json", synth.make_bundle([synth.make_entry(n=45)]))
    result = load.load_bundle(path, check_media=False)
    assert result.ok, result.report
    return result.samples[0]


def test_history_projects_past_tool_offsets_in_current_moving_camera(scene):
    s, cid, f, pid = scene, "cam0", 25, "finger_plus_y"
    cam = s.cameras[cid]
    cam.provided.clear()
    cam.T_reference_camera[:, 1, 3] += np.arange(s.n_frames) * .003
    cam.H[:, 0, 2] = np.arange(s.n_frames) * .2
    marks = load.declared_track(s, cid, pid).uv
    ids = history.indices(s.t, f)
    assert ids[0] == 10 and ids[-1] == f
    uv = history.project_eef(s, cid, f, pid, marks[f])
    # Independent OpenCV projection, including each historical gripper opening.
    points = np.array([(s.T_reference_eef[i] @ np.r_[synth.point_offset(pid, s.gripper[i]), 1])[:3]
                       for i in ids])
    expected, _ = synth.cv_project(points, cam.T_reference_camera[f])
    expected[:, 0] += cam.H[f, 0, 2]
    np.testing.assert_allclose(uv, expected, atol=1e-8)
    np.testing.assert_allclose(uv[-1], marks[f], atol=1e-8)
    assert np.linalg.norm(uv[0] - marks[ids[0]]) > 5  # not earlier frames' image coordinates
    s.T_reference_eef[f + 1:, :3, 3] += 100
    np.testing.assert_array_equal(history.project_eef(s, cid, f, pid, marks[f]), uv)


def test_missing_history_data_and_time_gaps_are_not_bridged(scene):
    s, f = scene, 25
    current = load.declared_track(s, "cam0", "tcp").uv[f]
    s.T_reference_eef[17] = np.nan
    s.T_reference_eef[20, :3, 3] = [4, 0, .3]  # behind the camera
    uv = history.project_eef(s, "cam0", f, "tcp", current)
    assert np.isnan(uv[[7, 10]]).all() and np.isfinite(uv[-1]).all()
    blank = np.zeros((60, 120, 3), np.uint8)
    separated = np.array([[10, 30], [20, 30], [np.nan, np.nan], [90, 30], [100, 30]])
    drawing = history.draw(blank, separated, 1)
    assert drawing[:, :25].any() and drawing[:, 85:].any() and not drawing[:, 25:85].any()
    s.t[20:] += .5
    assert list(history.indices(s.t, f)) == list(range(20, 26))
    s.t = None
    assert list(history.indices(history.times(s, "cam0"), f)) == list(range(10, 26))
    s.cameras["cam0"].video_timestamp_s[:] = np.nan
    assert list(history.indices(history.times(s, "cam0"), f)) == list(range(10, 26))
    s.cameras["cam0"].media["fps"] = None
    assert len(history.project_eef(s, "cam0", f, "tcp", current)) == 0


def test_history_never_replaces_supplied_p_or_invents_external_2d_geometry(scene):
    s, f = scene, 25
    original = s.cameras["cam0"].provided["tcp"].uv.copy()
    s.cameras["cam0"].provided["tcp"].uv[f] += [40, 0]
    marks = R.marks_for(s, R.Window("cam0", "opinion", [f], point_id="tcp", axis_id="z"), {})
    trail = history.project_eef(s, "cam0", f, "tcp", marks.declared[f])
    assert len(trail) == 0
    np.testing.assert_array_equal(marks.declared[f], original[f] + [40, 0])
    canvas = np.zeros((synth.H, synth.W, 3), np.uint8)
    np.testing.assert_array_equal(OP._draw(canvas, marks, None, f, 1, trail),
                                  OP._draw(canvas, marks, None, f, 1))
    s.points["tcp"]["model"] = "external_2d"
    assert len(history.project_eef(s, "cam0", f, "tcp", original[f])) == 0


def test_video_shares_history_and_preserves_current_markers(scene, tmp_path):
    import base64

    s, cid, f = scene, "cam0", 25
    cam = s.cameras[cid]
    path = tmp_path / cam.media["uri"]
    path.parent.mkdir(parents=True)
    synth.render_gripper_video(path, n=s.n_frames)
    window = R.Window(cid, "opinion", [f, f + 1], point_id="tcp", axis_id="z")
    marks, finger = R.marks_for(s, window, {}), OP._second_axis(s, cid, "finger_line")
    rendered = list(OP._render(s, cid, marks, finger, str(tmp_path), f, f + 1,
                              {i: i for i in range(s.n_frames)}, 720))
    with av.open(str(path)) as inp:
        raw = list(inp.decode(video=0))[f].to_ndarray(format="bgr24")
    trail = history.project_eef(s, cid, f, "tcp", marks.declared[f])
    expected = R._label(OP._draw(raw, marks, finger, f, 1, trail), f"frame {f + 1}")
    np.testing.assert_array_equal(rendered[0][1], cv2.cvtColor(expected, cv2.COLOR_BGR2RGB))
    old = OP._draw(raw, marks, finger, f, 1)
    new = OP._draw(raw, marks, finger, f, 1, trail)
    assert np.any(old != new)
    for color in (R.RED, OP.ORANGE):
        pixels = np.all(old == color, axis=-1)
        assert pixels.any()
        np.testing.assert_array_equal(new[pixels], old[pixels])
    # A clip starts midway through the episode: its first frame still includes pre-clip history.
    req = OP.build_request(s, cid, [f, f + 1], "tcp", "z", media_root=str(tmp_path), model="test",
                           finger_id="finger_line")
    assert len(req.videos) == 1 and history.prompt() in req.text
    assert "position|orientation|both|action" in req.text
    with av.open(io.BytesIO(base64.b64decode(req.videos[0].url.split(",", 1)[1]))) as inp:
        assert len(list(inp.decode(video=0))) == 2
