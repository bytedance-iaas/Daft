"""The model's opinion without a gripper reference (design doc 12 §10.5, D-E15): what is drawn.

On dataset2 (DEMO data outside the repo; skipped without it) every camera gets P = tcp, A = z (approach)
and B = y (the fingers' line), B mirrored through P so it crosses both fingers. Episode 2 turns the
gripper 30 degrees about its approach axis: P and A stay put and only B shows it, so B must still be
chosen there although the turn shortens it on one camera.
"""
from __future__ import annotations

import numpy as np

from curation.extensions.eef_consistency import load
from curation.extensions.eef_consistency import opinion as OP

from . import demo_data


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
