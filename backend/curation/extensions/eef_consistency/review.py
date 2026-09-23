"""VLM review of EEF-video consistency (design doc 12 §10, F5.6).

The model classifies, it never measures. For each camera of an episode the runner picks up to N
uniform windows and up to N CPU candidate windows (suspect segments of the EEF module; windows past
the budget are dropped and the episode says ``truncated``), at most F frames each. A request carries,
per frame, the raw crop around the gripper (no overlay: where the point is must be judged first) and
the same crop with the declared projection (red circle) and the independent observation (green
cross), labelled with the frame id, plus one downscaled full frame for context; the text gives the
point and axis definitions and what the camera cannot observe. It never carries a fault name, a
ground truth, an injected magnitude or the CPU's own conclusion.

The answer must pass ``eef/review_output.schema.json``, cite only frame ids of the request and say
no measured value (px, mm, cm, degrees); a failing answer gets one repair request, then the window
is ``failed``. The CPU and the model disagreeing is a ``conflict``: both sides are kept and the
episode is flagged for a person. The model saying the green cross follows the wrong object marks the
observation ``tracking_suspect`` (to verify; the DEMO has no second provider to relocate with).

Answers are cached by the bytes sent (image hashes, frame ids, point definitions, prompt, Schema,
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
PROMPT_VERSION = "eef-review-prompt/1"
PREPROCESS = {"crop_min_px": 160, "crop_max_px": 384, "crop_margin": 2.5, "crop_show_min_px": 256,
              "context_max_px": 640, "jpeg_quality": 85}
WINDOW_S = 1.0                                   # a uniform window covers about this long
CANDIDATE_SUBITEMS = (C.POSITION, C.ORIENTATION, C.CAMERA_MOTION)
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

    def as_dict(self) -> dict:
        d = {"camera_id": self.camera_id, "kind": self.kind, "frames": list(self.frames)}
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
    and they mostly overlap; a request shows every point anyway."""
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
                last["reasons"] = list(dict.fromkeys([*last["reasons"], *(s.get("reasons") or [])]))
            else:
                out.append({**s, "evidence_frames": list(s.get("evidence_frames") or []),
                            "targets": [s.get("point_id") or s.get("axis_id")], "reasons": list(s.get("reasons") or [])})
    return out


def select_windows(detail: dict, camera_id: str, reviewable: np.ndarray, fps: float, *,
                   per_camera: int, frames_per_window: int) -> tuple[list[Window], bool]:
    """Candidate windows (CPU suspect segments of this camera, longest first) and uniform windows
    over the frames where the declared point can be judged; ``truncated`` when candidates were
    left out. ``reviewable`` is a per-frame mask (declared point in the media frame)."""
    cands = merge_segments([s for s in detail.get("segments") or []
                            if s.get("camera_id") == camera_id and s.get("subitem") in CANDIDATE_SUBITEMS])
    cands.sort(key=lambda s: (-(s.get("end_frame", 0) - s.get("start_frame", 0)), s.get("start_frame", 0)))
    truncated = len(cands) > per_camera
    windows = [Window(camera_id, "candidate", _spread(int(s["start_frame"]), int(s["end_frame"]), frames_per_window,
                                                      s.get("evidence_frames") or []),
                      subitem=s["subitem"], segment=s)
               for s in cands[:per_camera]]
    idx = np.flatnonzero(reviewable)
    if per_camera and len(idx):
        half = max(1, int(round(WINDOW_S * fps / 2)))
        for c in np.linspace(idx[0], idx[-1], num=per_camera + 2)[1:-1].round().astype(int):
            lo, hi = max(int(idx[0]), int(c) - half), min(int(idx[-1]), int(c) + half)
            ok = np.flatnonzero(reviewable[lo:hi + 1]) + lo
            if len(ok):
                pick = np.linspace(0, len(ok) - 1, num=min(frames_per_window, len(ok))).round().astype(int)
                windows.append(Window(camera_id, "uniform", sorted({int(ok[i]) for i in pick})))
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


def _overlay(img: np.ndarray, declared: dict[str, np.ndarray], observed: dict[str, np.ndarray],
             f: int, scale: float = 1.0, offset=(0, 0)) -> np.ndarray:
    import cv2

    out = img.copy()
    for pid, uv in declared.items():
        if np.isfinite(uv[f]).all():
            c = (int(round((uv[f][0] - offset[0]) * scale)), int(round((uv[f][1] - offset[1]) * scale)))
            cv2.circle(out, c, 7, RED, 2)
    for pid, uv in observed.items():
        if np.isfinite(uv[f]).all():
            c = (int(round((uv[f][0] - offset[0]) * scale)), int(round((uv[f][1] - offset[1]) * scale)))
            cv2.drawMarker(out, c, GREEN, cv2.MARKER_CROSS, 14, 2)
    return out


def point_text(sample, camera_id: str) -> str:
    lines = []
    for pid, p in (sample.points or {}).items():
        lines.append(f"- point {pid}: {p.get('meaning') or 'no description'}")
    for aid, a in (sample.axes or {}).items():
        lines.append(f"- axis {aid}: from point {a.get('start_point_id')} to point {a.get('end_point_id')}"
                     + (f" ({a['physical_meaning']})" if a.get("physical_meaning") else ""))
    mount = sample.cameras[camera_id].mount
    limits = ["a wrist camera moves with the gripper: judge position and direction only, not motion"
              ] if mount == "wrist" else []
    return "\n".join(lines + [f"- limit: {x}" for x in limits])


def build_prompt(sample, window: Window, frame_ids: list[int]) -> str:
    return "\n".join([
        "You review whether a robot trajectory, projected into a camera image, sits on the gripper it describes.",
        f"Camera {window.camera_id}. Frames {', '.join(map(str, frame_ids))} (the id is printed on every image).",
        "For every frame you get: RAW = a crop around the gripper with nothing drawn; MARKED = the same crop with the "
        "declared point drawn as a RED circle and an independently tracked point as a GREEN cross. The first image "
        "is a downscaled full frame for context. Look at RAW first to find the defined point yourself.",
        "Definitions:",
        point_text(sample, window.camera_id),
        "Answer with ONE JSON object and nothing else, exactly these keys:",
        '{"review_status": "support|refute|uncertain|not_observable", "target_visible": true|false, '
        '"tracking_target_correct": "support|refute|uncertain", '
        '"position_support": "support|refute|uncertain|not_observable", '
        '"orientation_support": "support|refute|uncertain|not_observable", '
        '"background_motion_support": "support|refute|uncertain", '
        '"offset_direction": "none|up|down|left|right|toward_fingers|away_from_fingers|unclear", '
        '"offset_magnitude_class": "none|within_finger_width|one_to_two_finger_widths|over_two_finger_widths|unclear", '
        '"evidence_frame_ids": [frame ids from this request], '
        '"reason_codes": [any of occlusion, motion_blur, out_of_frame, low_resolution, gripper_not_visible, '
        'point_ambiguous, wrong_target, lighting, other], "explanation": "one or two sentences in Chinese"}',
        "support = the RED circle is on the defined point (position) / the declared direction matches the gripper "
        "(orientation) / the background stays still (background). refute = it is visibly not. Use uncertain when you "
        "cannot tell and not_observable when the point or the gripper cannot be seen.",
        "Classes only: do not estimate distances, pixels, millimetres or angles, not even in the explanation. "
        "Offsets are described in finger widths.",
    ])


def build_request(sample, window: Window, frames: dict[int, np.ndarray], declared: dict[str, np.ndarray],
                  observed: dict[str, np.ndarray], *, model: str) -> Request:
    """The request of one window from decoded frames (BGR, media pixels) keyed by sample frame index."""
    import cv2

    ids = [f for f in window.frames if f in frames]
    images: list[dict] = []
    wh = sample.cameras[window.camera_id].image_size_wh
    first = frames[ids[0]]
    h, w = first.shape[:2]
    scale = min(1.0, PREPROCESS["context_max_px"] / max(w, h))
    ctx = cv2.resize(first, (int(round(w * scale)), int(round(h * scale)))) if scale < 1 else first
    images.append({"role": "context", "frame_index": ids[0],
                   "jpeg": _jpeg(_label(_overlay(ctx, declared, observed, ids[0], scale), f"frame {ids[0]} (full)"))})
    for f in ids:
        pts = [uv[f] for uv in declared.values()] + [uv[f] for uv in observed.values()]
        x0, y0, cw, ch = _crop_box(pts, wh)
        crop = frames[f][y0:y0 + ch, x0:x0 + cw]
        k = max(1.0, PREPROCESS["crop_show_min_px"] / max(1, min(cw, ch)))    # small media: enlarge to read
        if k > 1.0:
            crop = cv2.resize(crop, (int(round(cw * k)), int(round(ch * k))), interpolation=cv2.INTER_LINEAR)
        images.append({"role": "raw", "frame_index": f, "jpeg": _jpeg(_label(crop.copy(), f"frame {f} RAW"))})
        marked = _overlay(crop, declared, observed, f, k, (x0, y0))
        images.append({"role": "marked", "frame_index": f, "jpeg": _jpeg(_label(marked, f"frame {f} MARKED"))})
    text = build_prompt(sample, window, ids)
    key = cache_key(text, images, model)
    return Request(window, text, images, ids, key)


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


def conflict(window: Window, answer: dict, cam_cells: dict) -> dict | None:
    """Where the model and the CPU disagree (design 12 §10.2): a CPU candidate the model finds
    consistent, or a CPU ok the model refutes. Uncertain / not observable never conflicts."""
    field = {C.POSITION: "position_support", C.ORIENTATION: "orientation_support",
             C.CAMERA_MOTION: "background_motion_support"}
    if window.kind == "candidate":
        said = answer.get(field[window.subitem])
        if said == "support":
            return {"subitem": window.subitem, "cpu": C.SUSPECT, "vlm": said}
        return None
    for sub, key in field.items():
        cpu = (cam_cells.get(sub) or {}).get("status")
        if cpu == C.OK and answer.get(key) == "refute":
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
