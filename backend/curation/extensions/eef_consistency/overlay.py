"""The EEF marks as drawing layers, for the mini player to paint over the source video (design docs 20, 22 §3).

The model is sent a marked clip that is encoded in memory and never saved. The report instead plays
the camera's own video and draws these layers on a canvas above it. The marks the model saw come from
the same choices as the request (:func:`.opinion.select`) and the same projections: the UMI owner
hand's trail, centre, approach axis and fingers (:mod:`.umi`), or the declared P, A, B and P's past
trail. The overlay adds what only a person looks at: the tool's three axes, the future trail and, when
the CPU measured the episode (a gripper reference was given), the observed P with its trail and the
residual line. Everything is computed from the trajectory bundle and the observation rows; no frame is
decoded.

Every layer says what it is (``id`` by role, ``group``, ``title``), whether it is drawn by default,
which hand it belongs to, and whether the model was shown it (``in_model``, with ``model_color`` when
the model saw it in another colour): the player's menu and its "same as sent to the model" preset
read these.

Coordinates are pixels of the source video (``image_size_wh``), one value per sample frame:
``point`` / ``cross`` ``[x, y]``, ``segment`` / ``arrow`` ``[x1, y1, x2, y2]``, ``polyline``
``[x1, y1, x2, y2, ...]`` with ``null, null`` where the line breaks (a missing pose is never bridged);
``null`` for no mark.
"""
from __future__ import annotations

import math

import numpy as np

from . import geometry as G, history, opinion as OP, review as R

#: at most this many points of a trail per frame (the gaps are kept)
TRAIL_POINTS = 24
#: the farthest a coordinate may be from the picture (a nearly-behind-camera point projects far away)
LIMIT_PX = 100000.0
#: the tool's three axes the overlay adds, from P (as long as the declared approach axis)
AXIS_M = 0.06
AXIS_COLORS = {"x": "#ff3b30", "y": "#34c759", "z": "#2f7bff"}
#: the approach arrow A as the overlay shows it: red is the x axis now (the model saw A red)
AXIS_A_COLOR = "#b26bff"
FUTURE_COLOR = "#a5f3fc"
RESIDUAL_COLOR = "#ffd400"
#: the one hand of an EEF sample
EEF_HAND = "eef"


def _hex(bgr) -> str:
    b, g, r = (int(x) for x in bgr)
    return f"#{r:02x}{g:02x}{b:02x}"


def _lighter(color: str) -> str:
    """Halfway to white: the future of a trail in the hand's colour."""
    rgb = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{(c + 255) // 2:02x}" for c in rgb)


def _num(x: float) -> float | None:
    return round(min(max(float(x), -LIMIT_PX), LIMIT_PX), 1) if math.isfinite(x) else None


def _point(uv) -> list | None:
    return [_num(uv[0]), _num(uv[1])] if uv is not None and np.isfinite(uv).all() else None


def _segment(a, b) -> list | None:
    if a is None or b is None or not np.isfinite([a, b]).all():
        return None
    return [_num(a[0]), _num(a[1]), _num(b[0]), _num(b[1])]


def _trail(uv) -> list | None:
    """A trail thinned to about TRAIL_POINTS, keeping its ends and every gap."""
    if uv is None or len(uv) < 2:
        return None
    step = max(1, math.ceil(len(uv) / TRAIL_POINTS))
    last = len(uv) - 1
    keep = [i for i in range(len(uv)) if i % step == 0 or i == last or not np.isfinite(uv[i]).all()]
    if not any(np.isfinite(uv[i]).all() for i in keep):
        return None
    out: list = []
    for i in keep:
        out += [_num(uv[i][0]), _num(uv[i][1])] if np.isfinite(uv[i]).all() else [None, None]
    return out


def _openings(values) -> list | None:
    """Per sample frame the recorded opening in metres; None when the input has none at all."""
    v = np.asarray(values, float)
    if not len(v) or not np.isfinite(v).any():
        return None
    return [round(float(x), 4) if np.isfinite(x) else None for x in v]


def _layer(lid: str, group: str, kind: str, title: str, color: str, width: float, frames: list, *, hand: str,
           label: str | None = None, default_on: bool = True, in_model: bool = False,
           model_color: str | None = None) -> dict:
    return {"id": lid, "group": group, "title": title, "kind": kind, "label": label, "color": color,
            "width": width, "frames": frames, "default_on": default_on, "hand": hand, "in_model": in_model,
            "model_color": model_color}


def observed_tracks(rows: list[dict], n_frames: int) -> dict[str, np.ndarray]:
    """``{point id: (N, 2) observed media pixels, NaN where not seen}`` from one camera's observation rows
    (the run directory's ``observations/<ep>/<cam>.jsonl``, observation.schema.json)."""
    out: dict[str, np.ndarray] = {}
    for r in rows:
        f = r.get("frame_index")
        if not isinstance(f, int) or not 0 <= f < n_frames or r.get("pixel_space", "media") != "media":
            continue
        for pid, p in (r.get("points") or {}).items():
            uv = (p or {}).get("uv_px")
            if uv is None or len(uv) != 2:
                continue
            out.setdefault(pid, np.full((n_frames, 2), np.nan))[f] = uv
    return out


def _umi_layers(sample, camera_id: str) -> tuple[list[dict], list[dict]]:
    from . import umi

    owner = sample.sample["umi"]["camera_hands"][camera_id]
    color = _hex(umi.COLORS[sorted(sample.hand_poses).index(owner)])
    horizon = sample.sample["umi"]["horizon_s"]
    unit = AXIS_M * np.eye(3)
    n = sample.n_frames
    trail, future, centre, axis, fingers = ([None] * n for _ in range(5))
    axes = {k: [None] * n for k in "xyz"}
    for f in range(n):
        uv, _ = umi.project(sample, camera_id, f, owner, indices=umi.history_indices(sample, f))
        trail[f] = _trail(uv)
        c = uv[-1] if len(uv) else None
        centre[f] = _point(c)
        if centre[f] is None:
            continue
        ahead = history.future_indices(sample.t, f, horizon)
        if len(ahead) > 1:
            fu, _ = umi.project(sample, camera_id, f, owner, indices=ahead)
            future[f] = _trail(fu)
        tip, _ = umi.project(sample, camera_id, f, owner, offsets=[0, 0, umi.AXIS_M])
        axis[f] = _segment(c, tip[0])
        ends, _ = umi.project(sample, camera_id, f, owner, offsets=unit, indices=[f, f, f])
        for k, e in zip("xyz", ends):
            axes[k][f] = _segment(c, e)
        opening = sample.hand_openings[owner][f]
        if np.isfinite(opening):
            ends, _ = umi.project(sample, camera_id, f, owner,
                                  offsets=[[-opening / 2, 0, 0], [opening / 2, 0, 0]], indices=[f, f])
            fingers[f] = _segment(ends[0], ends[1])
    layers = [
        _layer("trail_past", "trail_past", "polyline", "过去轨迹", color, 2, trail, hand=owner, in_model=True),
        _layer("trail_future", "trail_future", "polyline", "未来轨迹", _lighter(color), 2, future, hand=owner,
               default_on=False),
        _layer("finger_axis", "declared", "segment", "两指连线", color, 3, fingers, hand=owner, in_model=True),
        *[_layer(f"axis_{k}", "axes", "arrow", f"坐标轴 {k}", AXIS_COLORS[k], 2, axes[k], hand=owner) for k in "xyz"],
        _layer("axis", "declared", "segment", "接近轴", color, 5, axis, hand=owner, default_on=False, in_model=True),
        _layer("point", "declared", "point", "中心点", color, 2, centre, hand=owner, label=owner, in_model=True),
    ]
    return layers, [{"id": owner, "title": owner, "color": color, "opening_m": _openings(sample.hand_openings[owner])}]


def _eef_axes(sample, camera_id: str, pid: str) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """(start, end) tracks of the tool's x, y and z from P: the bundle's own axis when it declares one from
    P (``x`` or ``local_x`` ...), else P plus AXIS_M along the tool frame through the camera's calibration;
    none without either (a 2D-only input)."""
    from .load import declared_track

    out = {}
    for k in "xyz":
        a = next((a for aid, a in (sample.axes or {}).items()
                  if a.get("start_point_id") == pid and (aid == k or a.get("physical_meaning") == f"local_{k}")), None)
        if a is not None:
            t0, t1 = declared_track(sample, camera_id, pid), declared_track(sample, camera_id, a["end_point_id"])
            if t0 is not None and t1 is not None:
                out[k] = (t0.uv, t1.uv)
    missing = [k for k in "xyz" if k not in out]
    cam = sample.cameras[camera_id]
    cal, definition = cam.calibration(sample), sample.points.get(pid)
    offsets = G.point_offsets(definition, sample.gripper) if definition is not None else None
    if missing and cal is not None and offsets is not None and sample.has_absolute_pose:
        def project(off):
            uv, _ = G.project_chain(sample.T_reference_eef, off, cam.T_reference_camera, cal["K"], cal["model"],
                                    cal["distortion_coefficients"], cam.H)
            return uv

        start = project(offsets)
        for k in missing:
            out[k] = (start, project(offsets + AXIS_M * np.eye(3)["xyz".index(k)]))
    return out


def _eef_layers(sample, camera_id: str, pid: str, aid: str | None, bid: str | None, frames: list[int],
                observed: dict[str, np.ndarray] | None, judged: bool) -> tuple[list[dict], list[dict]]:
    """The declared P, A, B and P's past trail as the opinion's clip drew them, the tool's axes, P's future
    and - measured episodes - the observed P. ``judged``: the model saw the review's windows (P, observed P
    and A), not the opinion's clip."""
    marks = R.marks_for(sample, R.Window(camera_id=camera_id, kind="opinion", frames=frames, point_id=pid,
                                         axis_id=aid), {})
    finger = OP._second_axis(sample, camera_id, bid)
    axes = _eef_axes(sample, camera_id, pid)
    cam = sample.cameras[camera_id]
    obs = (observed or {}).get(pid)
    fixed = cam.mount == "fixed_external"            # a moving camera's past pixels cannot be redrawn now
    t = history.times(sample, camera_id)
    shown = set(frames)
    n = sample.n_frames
    trail, future, b, a, p, op, ot, res = ([None] * n for _ in range(8))
    ax = {k: [None] * n for k in axes}
    for f in range(n):
        if cam.video_frame_index[f] < 0:
            continue
        current = marks.declared[f] if marks.declared is not None and f in shown else None
        if current is not None:
            trail[f] = _trail(history.project_eef(sample, camera_id, f, pid, current))
            future[f] = _trail(history.project_eef_future(sample, camera_id, f, pid, current))
            if finger is not None:
                b[f] = _segment(finger[0][f], finger[1][f])
            if marks.axis is not None:
                a[f] = _segment(marks.axis[0][f], marks.axis[1][f])
            for k, (s0, s1) in axes.items():
                ax[k][f] = _segment(s0[f], s1[f])
            p[f] = _point(current)
        if obs is not None:
            op[f] = _point(obs[f])
            res[f] = _segment(current, obs[f]) if current is not None else None
            if fixed:
                ot[f] = _trail(obs[history.indices(t, f)])
    red = _hex(R.RED)
    layers = [
        _layer("trail_past", "trail_past", "polyline", "过去轨迹", _hex(history.COLOR), 2, trail, hand=EEF_HAND,
               in_model=not judged),
        _layer("trail_future", "trail_future", "polyline", "未来轨迹", FUTURE_COLOR, 2, future, hand=EEF_HAND,
               default_on=False),
        _layer("observed_trail", "observed", "polyline", "观测轨迹", _hex(R.GREEN), 2, ot, hand=EEF_HAND),
        _layer("finger_axis", "declared", "segment", f"两指连线 B（{bid}）", _hex(OP.ORANGE), 2, b, hand=EEF_HAND,
               label="B", in_model=not judged),
        *[_layer(f"axis_{k}", "axes", "arrow", f"坐标轴 {k}", AXIS_COLORS[k], 2, ax[k], hand=EEF_HAND) for k in ax],
        _layer("axis", "declared", "arrow", f"朝向 A（{aid}）", AXIS_A_COLOR, 2, a, hand=EEF_HAND, label="A",
               default_on=False, in_model=True, model_color=red),
        _layer("residual", "residual", "segment", "残差线", RESIDUAL_COLOR, 2, res, hand=EEF_HAND, default_on=False),
        _layer("observed_point", "observed", "cross", f"观测点（{pid}）", _hex(R.GREEN), 2, op, hand=EEF_HAND,
               in_model=judged),
        _layer("point", "declared", "point", f"中心点 P（{pid}）", red, 2, p, hand=EEF_HAND, label="P", in_model=True),
    ]
    return layers, [{"id": EEF_HAND, "title": sample.eef_frame or EEF_HAND, "color": red,
                     "opening_m": _openings(sample.gripper_opening)}]


def camera_overlay(sample, camera_id: str, observed: dict[str, np.ndarray] | None = None,
                   judged: bool = False) -> dict:
    """One camera's layers and hands, or ``{"skipped": reason}`` when its opinion clip would have been
    skipped. ``observed``: the camera's observed tracks (:func:`observed_tracks`) of a measured episode."""
    cam = sample.cameras[camera_id]
    picked = OP.select(sample, camera_id)
    base = {"camera_id": camera_id, "image_size_wh": [int(v) for v in cam.image_size_wh],
            "fps": float(cam.media.get("fps") or 0) or None,
            "media_frames": [int(v) if v >= 0 else None for v in cam.video_frame_index]}
    if isinstance(picked, str):
        return {**base, "skipped": picked, "layers": [], "hands": []}
    pid, aid, bid, frames = picked
    if sample.hand_poses:
        layers, hands = _umi_layers(sample, camera_id)
    else:
        layers, hands = _eef_layers(sample, camera_id, pid, aid, bid, frames, observed, judged)
    return {**base, "skipped": None, "hands": hands,
            "layers": [x for x in layers if any(v is not None for v in x["frames"])]}


def episode_overlay(sample, observed: dict[str, dict[str, np.ndarray]] | None = None,
                    judged: bool = False) -> list[dict]:
    """Every camera's :func:`camera_overlay`; ``observed`` is keyed by camera."""
    return [camera_overlay(sample, cid, (observed or {}).get(cid), judged) for cid in sorted(sample.cameras)]
