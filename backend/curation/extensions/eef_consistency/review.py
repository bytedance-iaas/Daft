"""VLM review of EEF-video consistency (design doc 12 §10, F5.6; request package D-E14, F5.10).

Production requests attach continuous RAW/MARKED videos (design doc 13). The sparse crops
described below are retained as report evidence and for historical injected-client tests.

The model classifies, it never measures. For each camera of an episode the runner picks up to N
uniform windows and up to N CPU candidate windows (suspect position / orientation segments of the
EEF module, merged per sub-item; windows past the budget are dropped and the episode says
``truncated``), at most F frames each. Every window asks about ONE point P and at most ONE axis A:
a candidate window about the point / axis the CPU found off, a uniform window about the camera's
best-covered point and its compared axis. A request carries, per frame, the raw crop around the
gripper (nothing drawn: the model finds P itself first) and the same crop marked: the declared P as a
red circle, the independently tracked P as a green cross, the declared A as a red arrow, each marker
labelled P or A; plus one downscaled full frame for context. The text defines P and A only - never a
fault name, a ground truth, an injected magnitude or the CPU's own conclusion.

The answer must pass ``eef/review_output.schema.json``, cite only frame ids of the request and say
no measured value (px, mm, cm, degrees); a failing answer gets one repair request, then the window
is ``failed``. ``votes`` turns an answer into per-sub-item votes (uncertain / not observable do not
vote); the episode verdict that weighs them against the CPU is the runner's (design 12 C.9).

Answers are cached by the bytes sent (video/image hashes, frame ids, the text, prompt and Schema version,
model, preprocessing), so a rerun or ``--resume`` asks nothing it already asked.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import pathlib
import re
from typing import Any, Callable

import numpy as np

from . import contracts as C

DETAIL_SCHEMA_VERSION = "eef-review-detail/0.1"
ANSWER_SCHEMA = "eef/review_output.schema.json"
PROMPT_VERSION = "eef-review-prompt/2"
PREPROCESS = {"crop_min_px": 160, "crop_max_px": 384, "crop_margin": 2.5, "crop_show_min_px": 256,
              "context_max_px": 640, "jpeg_quality": 85}
WINDOW_S = 1.0                                   # a uniform window covers about this long
MIN_AXIS_PX = 20.0                               # an arrow shorter than this says nothing about direction
CANDIDATE_SUBITEMS = (C.POSITION, C.ORIENTATION)       # the model cannot judge time, record or camera motion
VOTED = {C.POSITION: "position_support", C.ORIENTATION: "orientation_support"}
RED, GREEN, WHITE = (0, 0, 255), (0, 255, 0), (255, 255, 255)
#: a measured value in the explanation: a number with a length, pixel or angle unit
_MEASURE_RE = re.compile(r"\d\s*(?:px|pixels?|像素|mm|cm|毫米|厘米|公分|米|m\b|°|度|deg(?:rees?)?)", re.IGNORECASE)

ANSWERED, FAILED = "answered", "failed"
COMPLETED, INCOMPLETE, NOT_REVIEWED = "completed", "incomplete", "not_reviewed"


class ReviewCallError(Exception):
    """A request that got no answer: ``code`` is timeout | http_error | budget_exhausted | ..."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclasses.dataclass
class Window:
    camera_id: str
    kind: str                          # uniform | candidate
    frames: list[int]                  # sample frame indices, ascending
    subitem: str | None = None         # candidate: the sub-item of the CPU segment
    segment: dict | None = None        # candidate: the CPU segment (kept, never sent)
    point_id: str | None = None        # the one point asked about (P)
    axis_id: str | None = None         # the one axis asked about (A), if any

    def as_dict(self) -> dict:
        d = {"camera_id": self.camera_id, "kind": self.kind, "frames": list(self.frames),
             "point_id": self.point_id, "axis_id": self.axis_id}
        if self.subitem:
            d["subitem"] = self.subitem
        if self.segment:
            d["segment"] = {k: self.segment.get(k) for k in ("start_frame", "end_frame", "start_s", "end_s",
                                                            "targets", "reasons")
                            if self.segment.get(k) is not None}
        return d


@dataclasses.dataclass
class Request:
    """One window's request: text plus images, and the key its answer is cached under."""

    window: Window
    text: str
    images: list[dict]                 # {"role", "frame_index", "jpeg": bytes}
    frame_ids: list[int]
    key: str
    videos: list = dataclasses.field(default_factory=list)


# ----------------------------------------------------------------------------------- windows


def _spread(lo: int, hi: int, n: int, must: list[int] = ()) -> list[int]:
    """Up to ``n`` frames in [lo, hi]: ``must`` first (worst frames), the rest evenly spaced."""
    out = [f for f in dict.fromkeys(int(x) for x in must) if lo <= f <= hi][:n]
    if len(out) < n:
        for f in np.linspace(lo, hi, num=min(n, hi - lo + 1)).round().astype(int):
            if len(out) >= n:
                break
            if int(f) not in out:
                out.append(int(f))
    return sorted(out)


def merge_segments(segments: list[dict]) -> list[dict]:
    """One candidate per sub-item and stretch of time: the CPU reports a segment per point (or axis)
    and they mostly overlap; the window then asks about the target that was worst (``peaks``)."""
    out: list[dict] = []
    for sub in dict.fromkeys(s["subitem"] for s in segments):
        group = sorted((s for s in segments if s["subitem"] == sub), key=lambda s: (s["start_frame"], s["end_frame"]))
        for s in group:
            last = out[-1] if out and out[-1]["subitem"] == sub else None
            if last is not None and s["start_frame"] <= last["end_frame"]:
                last["end_frame"] = max(last["end_frame"], s["end_frame"])
                last["end_s"] = max(last.get("end_s") or 0.0, s.get("end_s") or 0.0)
                last["evidence_frames"] = list(dict.fromkeys([*last["evidence_frames"], *(s.get("evidence_frames") or [])]))
                last["targets"] = list(dict.fromkeys([*last["targets"], s.get("point_id") or s.get("axis_id")]))
                t = s.get("point_id") or s.get("axis_id")
                last["peaks"][t] = max(last["peaks"].get(t, float("-inf")), float(s.get("peak") or 0.0))
                last["reasons"] = list(dict.fromkeys([*last["reasons"], *(s.get("reasons") or [])]))
            else:
                t = s.get("point_id") or s.get("axis_id")
                out.append({**s, "evidence_frames": list(s.get("evidence_frames") or []), "targets": [t],
                            "peaks": {t: float(s.get("peak") or 0.0)}, "reasons": list(s.get("reasons") or [])})
    return out


def focus(cam: dict, axes: dict) -> tuple[str | None, str | None]:
    """The point and the axis a uniform window asks about: the compared point with the best
    coverage and the first compared axis (``axes``: the sample's axis definitions)."""
    pts = ((cam.get("subitems") or {}).get(C.POSITION) or {}).get("points") or {}
    cov = {pid: ((p.get("coverage") or {}).get("coverage") or 0.0) for pid, p in pts.items()}
    order = list(cam.get("compared_points") or [])
    point = max(order, key=lambda pid: (cov.get(pid, 0.0), -order.index(pid))) if order else None
    axis = next((a for a in cam.get("observed_axes") or [] if a in axes), None)
    return point, axis


def _target(segment: dict, pick: set) -> str | None:
    peaks = {t: v for t, v in (segment.get("peaks") or {}).items() if t in pick}
    return max(peaks, key=peaks.get) if peaks else next((t for t in segment.get("targets") or [] if t in pick), None)


def select_windows(detail: dict, camera_id: str, reviewable: np.ndarray, fps: float, *,
                   per_camera: int, frames_per_window: int, axes: dict | None = None) -> tuple[list[Window], bool]:
    """Candidate windows (CPU suspect position / orientation segments of this camera, longest first)
    and uniform windows over the frames where the declared point can be judged; ``truncated`` when
    candidates were left out. ``reviewable`` is a per-frame mask (declared point in the media frame);
    ``axes`` the sample's axis definitions (the axis start stands for P in an orientation window)."""
    axes = axes or {}
    cam = (detail.get("cameras") or {}).get(camera_id) or {}
    p0, a0 = focus(cam, axes)
    points = set(cam.get("compared_points") or [])
    cands = merge_segments([s for s in detail.get("segments") or []
                            if s.get("camera_id") == camera_id and s.get("subitem") in CANDIDATE_SUBITEMS])
    cands.sort(key=lambda s: (-(s.get("end_frame", 0) - s.get("start_frame", 0)), s.get("start_frame", 0)))
    truncated = len(cands) > per_camera
    windows = []
    for s in cands[:per_camera]:
        frames = _spread(int(s["start_frame"]), int(s["end_frame"]), frames_per_window, s.get("evidence_frames") or [])
        if s["subitem"] == C.POSITION:
            point, axis = _target(s, points) or p0, a0
        else:
            axis = _target(s, set(axes)) or a0
            start = (axes.get(axis) or {}).get("start_point_id")
            point = start if start in points else p0
        windows.append(Window(camera_id, "candidate", frames, subitem=s["subitem"], segment=s, point_id=point,
                              axis_id=axis))
    idx = np.flatnonzero(reviewable)
    if per_camera and len(idx):
        half = max(1, int(round(WINDOW_S * fps / 2)))
        for c in np.linspace(idx[0], idx[-1], num=per_camera + 2)[1:-1].round().astype(int):
            lo, hi = max(int(idx[0]), int(c) - half), min(int(idx[-1]), int(c) + half)
            ok = np.flatnonzero(reviewable[lo:hi + 1]) + lo
            if len(ok):
                pick = np.linspace(0, len(ok) - 1, num=min(frames_per_window, len(ok))).round().astype(int)
                windows.append(Window(camera_id, "uniform", sorted({int(ok[i]) for i in pick}), point_id=p0, axis_id=a0))
    return windows, truncated


# ----------------------------------------------------------------------------------- request


def _jpeg(img: np.ndarray) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, PREPROCESS["jpeg_quality"]])
    assert ok
    return buf.tobytes()


def _crop_box(points: list[np.ndarray], wh: tuple[int, int]) -> tuple[int, int, int, int]:
    pts = np.array([p for p in points if np.isfinite(p).all()], float)
    w, h = wh
    if not len(pts):
        side = min(PREPROCESS["crop_max_px"], w, h)
        return (w - side) // 2, (h - side) // 2, side, side
    lo, hi = pts.min(0), pts.max(0)
    span = float(max(hi - lo)) * PREPROCESS["crop_margin"]
    side = int(min(max(span, PREPROCESS["crop_min_px"]), PREPROCESS["crop_max_px"], w, h))
    cx, cy = (lo + hi) / 2
    x0 = int(min(max(cx - side / 2, 0), w - side))
    y0 = int(min(max(cy - side / 2, 0), h - side))
    return x0, y0, side, side


def _label(img: np.ndarray, text: str) -> np.ndarray:
    import cv2

    cv2.rectangle(img, (0, 0), (8 + 9 * len(text), 22), (0, 0, 0), -1)
    cv2.putText(img, text, (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, WHITE, 1, cv2.LINE_AA)
    return img


def _px(uv: np.ndarray, scale: float, offset) -> tuple[int, int]:
    return int(round((uv[0] - offset[0]) * scale)), int(round((uv[1] - offset[1]) * scale))


def _tag(img: np.ndarray, at: tuple[int, int], text: str, color) -> None:
    import cv2

    x, y = at
    cv2.putText(img, text, (x + 8, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, (x + 8, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)


def _overlay(img: np.ndarray, marks: "Marks", f: int, scale: float = 1.0, offset=(0, 0)) -> np.ndarray:
    """The declared P (red circle), the tracked P (green cross) and the declared A (red arrow), labelled."""
    import cv2

    out = img.copy()
    if marks.axis is not None:
        a, b = marks.axis[0][f], marks.axis[1][f]
        if np.isfinite(a).all() and np.isfinite(b).all():
            pa, pb = _px(a, scale, offset), _px(b, scale, offset)
            cv2.arrowedLine(out, pa, pb, RED, 2, cv2.LINE_AA, tipLength=0.25)
            _tag(out, pb, "A", RED)
    if marks.declared is not None and np.isfinite(marks.declared[f]).all():
        c = _px(marks.declared[f], scale, offset)
        cv2.circle(out, c, 7, RED, 2)
        _tag(out, (c[0] - 26, c[1]), "P", RED)                 # red label upper left
    if marks.observed is not None and np.isfinite(marks.observed[f]).all():
        c = _px(marks.observed[f], scale, offset)
        cv2.drawMarker(out, c, GREEN, cv2.MARKER_CROSS, 14, 2)
        _tag(out, (c[0], c[1] + 30), "P", GREEN)               # green label lower right
    return out


@dataclasses.dataclass
class Marks:
    """What a window draws, per sample frame: the declared and tracked P and the declared A (start, end)."""

    declared: np.ndarray | None
    observed: np.ndarray | None
    axis: tuple[np.ndarray, np.ndarray] | None

    def points(self, f: int) -> list[np.ndarray]:
        out = [x[f] for x in (self.declared, self.observed) if x is not None]
        if self.axis is not None:
            out += [self.axis[0][f], self.axis[1][f]]
        return out


def _axis_length(sample, camera_id: str, axis_id: str, frames: list[int]) -> float:
    """Median projected length (media px) of a declared axis over the frames; 0 when it cannot be drawn."""
    from .load import declared_track

    a = (sample.axes or {}).get(axis_id)
    if not a:
        return 0.0
    t0, t1 = (declared_track(sample, camera_id, a[k]) for k in ("start_point_id", "end_point_id"))
    if t0 is None or t1 is None:
        return 0.0
    d = np.linalg.norm(t1.uv[frames] - t0.uv[frames], axis=1)
    d = d[np.isfinite(d)]
    return float(np.median(d)) if len(d) else 0.0


def settle_axis(sample, window: Window) -> None:
    """The arrow a window draws must be long enough to show a direction: an orientation candidate keeps
    its axis or asks none; any other window keeps its axis if long enough, else takes the longest."""
    if window.kind == "candidate" and window.subitem == C.ORIENTATION:
        order = [window.axis_id]
    else:
        lengths = {a: _axis_length(sample, window.camera_id, a, window.frames) for a in (sample.axes or {})}
        order = [window.axis_id, *sorted(lengths, key=lambda a: -lengths[a])]
    window.axis_id = next((a for a in dict.fromkeys(order) if a and
                           _axis_length(sample, window.camera_id, a, window.frames) >= MIN_AXIS_PX), None)


def marks_for(sample, window: Window, observed: dict[str, np.ndarray]) -> Marks:
    """Declared tracks come from the file (provided or recomputed projection), the tracked one from
    the module's observation of the same point."""
    from .load import declared_track

    def track(pid):
        t = declared_track(sample, window.camera_id, pid) if pid else None
        return None if t is None else t.uv

    axis = None
    a = (sample.axes or {}).get(window.axis_id) if window.axis_id else None
    if a:
        start, end = track(a.get("start_point_id")), track(a.get("end_point_id"))
        if start is not None and end is not None:
            axis = (start, end)
    return Marks(track(window.point_id), observed.get(window.point_id) if window.point_id else None, axis)


def _meaning(sample, pid: str | None) -> str:
    d = (sample.points or {}).get(pid) or {}
    text = d.get("meaning") or "no description"
    if d.get("model") == "linear_gripper":
        text += " (moves with the gripper opening)"
    return text


def point_text(sample, window: Window, marks: "Marks") -> str:
    lines = []
    if window.point_id:
        lines.append(f"- P = {window.point_id}: {_meaning(sample, window.point_id)}")
    if window.axis_id and marks.axis is not None:
        a = sample.axes[window.axis_id]
        how = "directed" if a.get("directed") else "undirected (either direction is fine)"
        lines.append(f"- A = {window.axis_id}: from {a['start_point_id']} ({_meaning(sample, a['start_point_id'])}) "
                     f"to {a['end_point_id']} ({_meaning(sample, a['end_point_id'])}); {a.get('physical_meaning', '')}, "
                     f"{how}")
    if sample.cameras[window.camera_id].mount == "wrist":
        lines.append("- limit: a wrist camera moves with the gripper")
    return "\n".join(lines)


def build_prompt(sample, window: Window, frame_ids: list[int], marks: "Marks") -> str:
    has_axis = window.axis_id is not None and marks.axis is not None
    has_obs = marks.observed is not None and bool(np.isfinite(marks.observed).all(1).any())
    legend = ["the RED circle labelled P is where the recorded trajectory puts point P"]
    if has_obs:
        legend.append("the GREEN cross labelled P is where an independent tracker found P")
    if has_axis:
        legend.append("the RED arrow labelled A is the recorded direction A")
    return "\n".join([
        "You review ONE point P" + (" and ONE direction A" if has_axis else "") +
        " of a robot gripper, as a recorded trajectory projects them into a camera image.",
        f"Camera {window.camera_id}. Frames {', '.join(map(str, frame_ids))} (the id is printed on every image).",
        "Images: first a downscaled full frame for context; then for every frame RAW (a crop around the gripper with "
        "nothing drawn - find P there yourself first) and MARKED (the same crop) where " + "; ".join(legend) + ".",
        "Definitions:",
        point_text(sample, window, marks),
        "Answer with ONE JSON object and nothing else, exactly these keys:",
        '{"review_status": "support|refute|uncertain|not_observable", "target_visible": true|false, '
        '"tracking_target_correct": "support|refute|uncertain", '
        '"position_support": "support|refute|uncertain|not_observable", '
        '"orientation_support": "support|refute|uncertain|not_observable", '
        '"offset_direction": "none|up|down|left|right|toward_fingers|away_from_fingers|unclear", '
        '"offset_magnitude_class": "none|within_finger_width|one_to_two_finger_widths|over_two_finger_widths|unclear", '
        '"evidence_frame_ids": [frame ids from this request], '
        '"reason_codes": [any of occlusion, motion_blur, out_of_frame, low_resolution, gripper_not_visible, '
        'point_ambiguous, wrong_target, lighting, other], "explanation": "one or two sentences in Chinese"}',
        "position_support: support = the red circle P is on P as defined; refute = it is visibly elsewhere. "
        "tracking_target_correct: support = the green cross P is on P" + ("" if has_obs else " (there is no green cross: "
        "answer uncertain)") + ". orientation_support: " + ("support = the red arrow A points the way A of the gripper "
        "does; refute = it visibly does not." if has_axis else "there is no arrow: answer not_observable.") +
        " review_status repeats position_support. offset_direction / offset_magnitude_class describe where the red "
        "circle is relative to the true P. Use uncertain when you cannot tell and not_observable when P or the gripper "
        "cannot be seen.",
        "Classes only: do not estimate distances, pixels, millimetres or angles, not even in the explanation. "
        "Offsets are described in finger widths.",
    ])


def build_request(sample, window: Window, frames: dict[int, np.ndarray], observed: dict[str, np.ndarray], *,
                  model: str) -> Request:
    """The request of one window from decoded frames (BGR, media pixels) keyed by sample frame index;
    ``observed``: the module's tracked pixels per point id."""
    import cv2

    window.frames = [f for f in window.frames if f in frames]
    settle_axis(sample, window)
    marks = marks_for(sample, window, observed)
    ids = list(window.frames)
    images: list[dict] = []
    wh = sample.cameras[window.camera_id].image_size_wh
    first = frames[ids[0]]
    h, w = first.shape[:2]
    scale = min(1.0, PREPROCESS["context_max_px"] / max(w, h))
    ctx = cv2.resize(first, (int(round(w * scale)), int(round(h * scale)))) if scale < 1 else first
    images.append({"role": "context", "frame_index": ids[0],
                   "jpeg": _jpeg(_label(_overlay(ctx, marks, ids[0], scale), f"frame {ids[0]} (full)"))})
    for f in ids:
        x0, y0, cw, ch = _crop_box(marks.points(f), wh)
        crop = frames[f][y0:y0 + ch, x0:x0 + cw]
        k = max(1.0, PREPROCESS["crop_show_min_px"] / max(1, min(cw, ch)))    # small media: enlarge to read
        if k > 1.0:
            crop = cv2.resize(crop, (int(round(cw * k)), int(round(ch * k))), interpolation=cv2.INTER_LINEAR)
        images.append({"role": "raw", "frame_index": f, "jpeg": _jpeg(_label(crop.copy(), f"frame {f} RAW"))})
        marked = _overlay(crop, marks, f, k, (x0, y0))
        images.append({"role": "marked", "frame_index": f, "jpeg": _jpeg(_label(marked, f"frame {f} MARKED"))})
    text = build_prompt(sample, window, ids, marks)
    key = cache_key(text, images, model)
    return Request(window, text, images, ids, key)


def attach_videos(req: Request, sample, observed: dict, media_root: str, *, options=None) -> Request:
    """Attach continuous RAW/MARKED videos for the selected window, retaining stills for reports."""
    import cv2

    from ...adapters.video_input import encode_rendered_video
    from . import runner

    opts = options or {}
    camera = sample.cameras[req.window.camera_id]
    fps = float(camera.media.get("fps") or 0)
    if not np.isfinite(fps) or fps <= 0:
        raise ValueError("EEF video review requires the camera's frame rate")
    mapping = {int(v): i for i, v in enumerate(camera.video_frame_index) if v >= 0}
    lo = int(camera.video_frame_index[min(req.frame_ids)])
    hi = int(camera.video_frame_index[max(req.frame_ids)])
    marks = marks_for(sample, req.window, observed)

    def rendered(marked):
        for fr in runner._frames(sample, req.window.camera_id, media_root):
            if fr.index < lo:
                continue
            if fr.index > hi:
                break
            bgr = fr.bgr()
            k = min(1.0, int(opts.get("max_side", 720)) / max(bgr.shape[:2]))
            if k < 1:
                bgr = cv2.resize(bgr, (int(bgr.shape[1] * k), int(bgr.shape[0] * k)))
            f = mapping.get(fr.index)
            if marked and f is not None:
                bgr = _overlay(bgr, marks, f, k)
            label = f"frame {f}" if f is not None else f"media frame {fr.index} (no trajectory sample)"
            bgr = _label(bgr, f"{label} {'MARKED' if marked else 'RAW'}")
            yield fr.pts_s, cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    limit = int(opts.get("max_bytes", 32 * 1024 * 1024))
    for role, marked in (("RAW", False), ("MARKED", True)):
        clip = encode_rendered_video(f"{req.window.camera_id} {role}", rendered(marked),
                                     fps=fps, end_s=(hi + 1) / fps, max_bytes=limit)
        req.videos.append(clip)
        limit -= clip.byte_size
    lines = req.text.splitlines()
    lines[2] = ("Videos: RAW is the continuous unmarked camera view; find P there yourself first. "
                "MARKED is the same continuous window with the RED circle P and RED direction A "
                "from the recorded trajectory and, when available, the GREEN tracker cross P. "
                "Frame ids are printed in both videos. Cite only the allowed Frames listed above. "
                "A frame without a trajectory sample has no geometric overlay.")
    req.text = "\n".join(lines)
    req.key = hashlib.sha256(json.dumps({"base": req.key, "protocol": "eef-video-review/1",
        "text": req.text, "fps": float(opts.get("fps", 5)),
        "videos": [c.metadata() for c in req.videos]}, sort_keys=True).encode()).hexdigest()
    return req


def write_evidence(req: Request, directory: str, run_dir: str) -> list[str]:
    """The marked crops of a window the model refuted or the CPU disagrees with (design 12 §10.4),
    as sent; paths relative to the run directory."""
    d = pathlib.Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for img in req.images:
        if img["role"] != "marked":
            continue
        p = d / f"{req.key[:12]}_frame_{img['frame_index']:06d}.jpg"
        p.write_bytes(img["jpeg"])
        out.append(str(p.relative_to(run_dir)).replace("\\", "/"))
    return out


def cache_key(text: str, images: list[dict], model: str) -> str:
    h = hashlib.sha256()
    h.update(json.dumps({"prompt": PROMPT_VERSION, "schema": ANSWER_SCHEMA, "model": model,
                         "preprocess": PREPROCESS, "text": text,
                         "images": [[i["role"], i["frame_index"], hashlib.sha256(i["jpeg"]).hexdigest()]
                                    for i in images]}, sort_keys=True).encode())
    return h.hexdigest()


def data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


# ----------------------------------------------------------------------------------- answers


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def check_answer(text: str, frame_ids: list[int]) -> tuple[dict | None, dict | None]:
    """(answer, None) or (None, {"code", "message"}): malformed_json | schema_violation |
    unknown_frame | measured_value."""
    from curation.contracts import schemas

    try:
        doc = json.loads(_strip_fence(text), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except (ValueError, TypeError) as e:
        return None, {"code": "malformed_json", "message": f"not one JSON object: {e}"[:300]}
    if not isinstance(doc, dict):
        return None, {"code": "malformed_json", "message": "not one JSON object"}
    errs = schemas.errors(ANSWER_SCHEMA, doc)
    if errs:
        return None, {"code": "schema_violation", "message": "; ".join(errs[:3])[:300]}
    unknown = sorted(set(doc["evidence_frame_ids"]) - set(frame_ids))
    if unknown:
        return None, {"code": "unknown_frame", "message": f"frames {unknown} are not in this request"}
    if _MEASURE_RE.search(doc["explanation"]):
        return None, {"code": "measured_value", "message": "the explanation gives a measured value"}
    return doc, None


def repair_text(problem: dict, frame_ids: list[int]) -> str:
    return (f"Your answer was rejected ({problem['code']}: {problem['message']}). Reply again with ONE JSON object "
            f"with exactly the keys asked for, evidence_frame_ids only from {frame_ids}, and no measured values.")


# ----------------------------------------------------------------------------------- judgement


def votes(answer: dict) -> dict[str, str]:
    """The sub-item votes of one answer: support / refute only (uncertain and not observable abstain)."""
    return {sub: answer[key] for sub, key in VOTED.items() if answer.get(key) in ("support", "refute")}


def conflict(window: Window, answer: dict, cam_cells: dict) -> dict | None:
    """Where the model and the CPU disagree on one window (design 12 §10.2): a CPU candidate the
    model finds consistent, or a CPU ok the model refutes. Uncertain / not observable never conflicts."""
    v = votes(answer)
    if window.kind == "candidate":
        if v.get(window.subitem) == "support":
            return {"subitem": window.subitem, "cpu": C.SUSPECT, "vlm": "support"}
        return None
    for sub, said in v.items():
        cpu = (cam_cells.get(sub) or {}).get("status")
        if cpu == C.OK and said == "refute":
            return {"subitem": sub, "cpu": cpu, "vlm": "refute"}
    return None


def ask_window(req: Request, ask: Callable[[Request, list[dict]], str], cache: "Cache") -> dict:
    """One window: cached answer, or the model with at most one repair turn."""
    hit = cache.get(req.key)
    if hit is not None:
        return {"status": ANSWERED, "answer": hit, "attempts": 0, "cache_hit": True}
    history: list[dict] = []
    problem = None
    for attempt in (1, 2):
        try:
            text = ask(req, history)
        except ReviewCallError as e:
            return {"status": FAILED, "failure": {"code": e.code, "message": str(e)[:300]}, "attempts": attempt,
                    "cache_hit": False}
        answer, problem = check_answer(text, req.frame_ids)
        if answer is not None:
            cache.put(req.key, answer)
            return {"status": ANSWERED, "answer": answer, "attempts": attempt, "cache_hit": False}
        history = [{"role": "assistant", "content": text[:4000]},
                   {"role": "user", "content": repair_text(problem, req.frame_ids)}]
    return {"status": FAILED, "failure": problem, "attempts": 2, "cache_hit": False}


class Cache:
    """Validated answers by request key, one small JSON file each (``cache/<key>.json``)."""

    def __init__(self, root: str | None):
        self.root = pathlib.Path(root) if root else None

    def get(self, key: str) -> dict | None:
        if self.root is None:
            return None
        p = self.root / f"{key}.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def put(self, key: str, answer: dict) -> None:
        if self.root is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f"{key}.json.tmp"
        tmp.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.root / f"{key}.json")


def summarize(windows: list[dict], truncated: bool) -> tuple[str, dict]:
    """The episode's review status and counts."""
    s: dict[str, Any] = {"windows": len(windows), "answered": 0, "failed": 0, "conflicts": 0,
                         "tracking_suspect": 0, "cache_hits": 0, "requests": 0}
    for k in ("support", "refute", "uncertain", "not_observable"):
        s[k] = 0
    for w in windows:
        s["requests"] += w.get("attempts", 0)
        s["cache_hits"] += bool(w.get("cache_hit"))
        if w["status"] == ANSWERED:
            s["answered"] += 1
            s[w["answer"]["review_status"]] += 1
            s["conflicts"] += bool(w.get("conflict"))
            s["tracking_suspect"] += w["answer"]["tracking_target_correct"] == "refute"
        else:
            s["failed"] += 1
    s["truncated"] = truncated
    if not windows:
        return NOT_REVIEWED, s
    return (INCOMPLETE if s["failed"] else COMPLETED), s
