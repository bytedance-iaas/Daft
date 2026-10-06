"""The opinion's marks as drawing layers, for the report to paint over the source video (design doc 20).

The model is sent a marked clip that is encoded in memory and never saved. The report instead plays
the camera's own video and draws these layers on a canvas above it. The layers come from the same
choices as the request (:func:`.opinion.select`) and the same projections: the UMI owner hand's
trail, centre, approach axis and fingers (:mod:`.umi`), or the declared P, A, B and P's past trail.
Everything is computed from the trajectory bundle; no frame is decoded.

Coordinates are pixels of the source video (``image_size_wh``), one value per sample frame:
``point`` ``[x, y]``, ``segment`` / ``arrow`` ``[x1, y1, x2, y2]``, ``polyline`` ``[x1, y1, x2, y2, ...]``
with ``null, null`` where the line breaks (a missing pose is never bridged); ``null`` for no mark.
"""
from __future__ import annotations

import math

import numpy as np

from . import history, opinion as OP, review as R

#: at most this many points of a past trail per frame (the gaps are kept)
TRAIL_POINTS = 24
#: the farthest a coordinate may be from the picture (a nearly-behind-camera point projects far away)
LIMIT_PX = 100000.0


def _hex(bgr) -> str:
    b, g, r = (int(x) for x in bgr)
    return f"#{r:02x}{g:02x}{b:02x}"


def _num(x: float) -> float | None:
    return round(min(max(float(x), -LIMIT_PX), LIMIT_PX), 1) if math.isfinite(x) else None


def _point(uv) -> list | None:
    return [_num(uv[0]), _num(uv[1])] if uv is not None and np.isfinite(uv).all() else None


def _segment(a, b) -> list | None:
    if a is None or b is None or not np.isfinite([a, b]).all():
        return None
    return [_num(a[0]), _num(a[1]), _num(b[0]), _num(b[1])]


def _trail(uv) -> list | None:
    """A past trail thinned to about TRAIL_POINTS, keeping its last point and every gap."""
    if uv is None or len(uv) < 2:
        return None
    step = max(1, math.ceil(len(uv) / TRAIL_POINTS))
    last = len(uv) - 1
    keep = [i for i in range(len(uv)) if i % step == 0 or i == last or not np.isfinite(uv[i]).all()]
    out: list = []
    for i in keep:
        out += [_num(uv[i][0]), _num(uv[i][1])] if np.isfinite(uv[i]).all() else [None, None]
    return out


def _layer(kind: str, color, width: float, frames: list, label: str | None = None) -> dict:
    return {"kind": kind, "label": label, "color": _hex(color), "width": width, "frames": frames}


def _umi_layers(sample, camera_id: str) -> list[dict]:
    from . import umi

    owner = sample.sample["umi"]["camera_hands"][camera_id]
    j = sorted(sample.hand_poses).index(owner)
    color = umi.COLORS[j]
    n = sample.n_frames
    trail, centre, axis, fingers = [None] * n, [None] * n, [None] * n, [None] * n
    for f in range(n):
        uv, _ = umi.project(sample, camera_id, f, owner, indices=umi.history_indices(sample, f))
        trail[f] = _trail(uv)
        c = uv[-1] if len(uv) else None
        centre[f] = _point(c)
        if centre[f] is None:
            continue
        tip, _ = umi.project(sample, camera_id, f, owner, offsets=[0, 0, .06])
        axis[f] = _segment(c, tip[0])
        opening = sample.hand_openings[owner][f]
        if np.isfinite(opening):
            ends, _ = umi.project(sample, camera_id, f, owner,
                                  offsets=[[-opening / 2, 0, 0], [opening / 2, 0, 0]], indices=[f, f])
            fingers[f] = _segment(ends[0], ends[1])
    return [_layer("polyline", color, 2, trail), _layer("segment", color, 3, fingers),
            _layer("segment", color, 3, axis), _layer("point", color, 2, centre, owner)]


def _eef_layers(sample, camera_id: str, pid: str, aid: str | None, bid: str | None, frames: list[int]) -> list[dict]:
    marks = R.marks_for(sample, R.Window(camera_id=camera_id, kind="opinion", frames=frames, point_id=pid,
                                         axis_id=aid), {})
    finger = OP._second_axis(sample, camera_id, bid)
    shown = set(frames)
    n = sample.n_frames
    trail, b, a, p = [None] * n, [None] * n, [None] * n, [None] * n
    for f in range(n):
        if f not in shown:
            continue
        current = marks.declared[f] if marks.declared is not None else None
        trail[f] = _trail(history.project_eef(sample, camera_id, f, pid, current))
        if finger is not None:
            b[f] = _segment(finger[0][f], finger[1][f])
        if marks.axis is not None:
            a[f] = _segment(marks.axis[0][f], marks.axis[1][f])
        p[f] = _point(current)
    return [_layer("polyline", history.COLOR, 2, trail), _layer("segment", OP.ORANGE, 2, b, "B"),
            _layer("arrow", R.RED, 2, a, "A"), _layer("point", R.RED, 2, p, "P")]


def camera_overlay(sample, camera_id: str) -> dict:
    """One camera's layers, or ``{"skipped": reason}`` when its opinion clip would have been skipped."""
    cam = sample.cameras[camera_id]
    picked = OP.select(sample, camera_id)
    base = {"camera_id": camera_id, "image_size_wh": [int(v) for v in cam.image_size_wh],
            "fps": float(cam.media.get("fps") or 0) or None,
            "media_frames": [int(v) if v >= 0 else None for v in cam.video_frame_index]}
    if isinstance(picked, str):
        return {**base, "skipped": picked, "layers": []}
    pid, aid, bid, frames = picked
    layers = _umi_layers(sample, camera_id) if sample.hand_poses else _eef_layers(sample, camera_id, pid, aid, bid,
                                                                                  frames)
    return {**base, "skipped": None, "layers": layers}


def episode_overlay(sample) -> list[dict]:
    return [camera_overlay(sample, cid) for cid in sorted(sample.cameras)]
