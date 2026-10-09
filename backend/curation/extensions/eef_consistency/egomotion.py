"""A wrist camera's own motion, from its pictures and from the recorded poses (design doc 22 §5.3, §7).

A camera on the gripper moves with it: the marks of its own hand stay put in the picture and cannot tell
whether the recorded trajectory is right. The camera's motion between two moments can be had twice: from
the recorded poses (its pose at both moments) and from the pictures (ORB matches between the two frames,
undistorted through the camera model, the essential matrix). The relative rotation must agree; a wrong
body->optical rotation, a pose stream ahead of or behind the video, or a VIO that jumped make it disagree.
The direction of travel is reported too, but only as a reading: over half a second a handheld gripper
travels a few centimetres, and the direction the essential matrix gives is too loose to judge by (on the DAS
recordings its median difference was 4-50 degrees with the right convention).

Two uses. ``export-umi-mcap`` checks a few pairs for its report (§7 row 2): :func:`picture_motion`. The EEF
module's opinion checks a whole episode (F5.19): :func:`camera_ego_motion` takes pairs a window apart
(0.5 s) starting every third of a window, so each picture's features are computed once and serve two pairs;
per pair it takes the rotation difference and its share of the poses' own turn; it shifts the poses against
the pictures to find a time offset (the lag of design 12 §8.3: the picture at t shows the pose recorded at
t + lag, so a positive lag is a pose stream that is late); smoothed stretches over the profile's thresholds
become the bad segments, stretches the pictures cannot be matched in are listed as such; the demo profile's
thresholds are uncalibrated. Pure CPU; no model.
"""
from __future__ import annotations

import dataclasses
from typing import Iterable

import numpy as np

ORB_FEATURES = 4000
RATIO = 0.75
#: fewer good matches, or fewer of them on the recovered motion, and the pair says nothing (a white wall, blur):
#: on the DAS recordings pairs under these gave the only large errors (up to 54 degrees), all others stayed under 5
MIN_MATCHES = 100
MIN_INLIERS = 40
#: pictures are matched at most this wide (DAS records 1600 x 1300)
MAX_SIDE = 960
#: the camera must have moved this far for the direction of travel to be compared, metres
MIN_TRAVEL_M = 0.01

#: the whole-episode check (F5.19): the profile's ``ego_motion`` section overrides these (design doc 22 §7 row 10)
DEFAULTS = {
    "pairs_per_window": 3,          # a pair starts every third of a window: a picture is the near end of one pair
                                    # and the far end of another, its features computed once
    "smooth_pairs": 3,              # rolling median over this many pairs before the thresholds
    "rotation_on_deg": 5.0,         # a stretch whose smoothed rotation difference reaches this ...
    "rotation_off_deg": 3.0,        # ... and stays above this is bad ...
    "relative_min": 0.5,            # ... where the difference is at least this share of the poses' own turn
    "min_turn_deg": 2.0,            # (the turn floored at this: a still camera's small difference is noise)
    "min_duration_s": 0.5,
    "max_gap_s": 0.4,
    "bands_deg": [5.0, 10.0, 20.0],     # minor / moderate / severe by a stretch's median rotation difference
    "relative_bands": [0.75, 1.0],      # at least moderate / severe by its median share of the turn: a wrong
                                        # axis convention turns the camera about another axis, by as much again
    "lag_min_s": 0.15,              # a time offset worth naming ...
    "lag_confidence_min": 0.3,      # ... found with this confidence ...
    "lag_gain_deg": 0.5,            # ... that lowers the median rotation difference by this much ...
    "lag_gain_ratio": 1.3,          # ... and by this factor
    "lag_bands_s": [0.15, 0.3, 0.5],
    "excess_on_deg": 2.0,           # a time offset's stretches: where shifting the poses helps this much
    "excess_off_deg": 1.0,
    "min_inliers": 100,             # a pair judged on fewer inliers is unmatched: at a white wall's edge the few
                                    # matches left gave the only large errors of a correct recording
    "coverage_min": 0.5,            # fewer pairs measured than this share, or than min_pairs: unknown
    "min_pairs": 10,
    "unmatched_min_s": 0.5,         # a stretch of unmatched pairs starting over this long is listed
}
BANDS = ("minor", "moderate", "severe")
#: why a camera is not assessed
NOT_WRIST, NO_CALIBRATION, NO_CAMERA_POSES, NO_VIDEO, NO_PAIRS, FRAMES_UNREADABLE = (
    "not_wrist_camera", "calibration_missing", "camera_poses_missing", "video_missing", "no_pairs", "frames_unreadable")
LOW_COVERAGE = "pictures_unmatched"


def scene_mask(w: int, h: int, keep: float = 0.62) -> np.ndarray:
    """The scene, not the gripper: the upper ``keep`` of the picture, inside the fisheye circle."""
    import cv2

    mask = np.zeros((h, w), np.uint8)
    cv2.circle(mask, (w // 2, h // 2), int(0.49 * w), 255, -1)
    mask[int(keep * h):, :] = 0
    return mask


@dataclasses.dataclass
class Motion:
    """The camera from frame a to frame b, as ``cv2.recoverPose`` gives it: x_b = R x_a + t, |t| = 1."""

    R: np.ndarray
    t: np.ndarray
    inliers: int
    matches: int


@dataclasses.dataclass
class Features:
    """ORB keypoints of one picture, in its own pixels (matched at most MAX_SIDE wide)."""

    pts: np.ndarray            # (N, 2)
    desc: np.ndarray | None


def _normalized(pts: np.ndarray, K, D, model: str) -> np.ndarray:
    import cv2

    pts = pts.reshape(-1, 1, 2).astype(np.float64)
    if model == "opencv_fisheye":
        return cv2.fisheye.undistortPoints(pts, K, np.asarray(D, float).reshape(-1)[:4])
    if model == "opencv_brown":
        return cv2.undistortPoints(pts, K, np.asarray(D, float))
    return cv2.undistortPoints(pts, K, None)


def features(gray: np.ndarray, mask: np.ndarray | None = None, orb=None) -> Features:
    """The picture's ORB features (inside ``mask``, the picture's size), keypoints in its own pixels."""
    import cv2

    h, w = gray.shape[:2]
    k = min(1.0, MAX_SIDE / max(w, h))
    if k < 1.0:
        size = (int(round(w * k)), int(round(h * k)))
        gray = cv2.resize(gray, size)
        mask = cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST) if mask is not None else None
    orb = orb or cv2.ORB_create(ORB_FEATURES)
    kp, desc = orb.detectAndCompute(gray, mask)
    return Features(np.float64([p.pt for p in kp]).reshape(-1, 2) / k, desc)


def match(fa: Features, fb: Features, K, D, model: str, matcher=None) -> Motion | None:
    """The camera's motion between two pictures' features (``K`` for their size); None when they do not
    match well enough to tell (a white wall, motion blur, too little overlap)."""
    import cv2

    if fa.desc is None or fb.desc is None or len(fa.pts) < MIN_INLIERS or len(fb.pts) < MIN_INLIERS:
        return None
    matcher = matcher or cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = matcher.knnMatch(fa.desc, fb.desc, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < RATIO * p[1].distance]
    if len(good) < MIN_MATCHES:
        return None
    K = np.asarray(K, float)
    na = _normalized(fa.pts[[m.queryIdx for m in good]], K, D, model)
    nb = _normalized(fb.pts[[m.trainIdx for m in good]], K, D, model)
    E, inl = cv2.findEssentialMat(na, nb, np.eye(3), method=cv2.RANSAC, prob=0.999, threshold=2e-3)
    if E is None or E.shape != (3, 3):
        return None
    n, R, t, _ = cv2.recoverPose(E, na, nb, np.eye(3), mask=inl)
    if n < MIN_INLIERS:
        return None
    return Motion(R, t.reshape(3), int(n), len(good))


def picture_motion(gray_a: np.ndarray, gray_b: np.ndarray, K, D, model: str, mask: np.ndarray | None = None) -> Motion | None:
    """The camera's motion between two pictures (grey, the size ``K`` is for); None when they do not match well
    enough to tell."""
    import cv2

    orb = cv2.ORB_create(ORB_FEATURES)
    return match(features(gray_a, mask, orb), features(gray_b, mask, orb), K, D, model)


def pose_motion(T_a: np.ndarray, T_b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The same motion from the camera's recorded poses (``T_reference_camera`` at both moments): (R, t) with
    x_b = R x_a + t, t in metres."""
    T = np.linalg.inv(T_b) @ T_a
    return T[:3, :3], T[:3, 3]


def _angle_deg(R: np.ndarray) -> float:
    return float(np.degrees(np.arccos(np.clip((np.trace(R) - 1.0) / 2.0, -1.0, 1.0))))


def disagreement(seen: Motion, T_a: np.ndarray, T_b: np.ndarray) -> tuple[float, float | None]:
    """(rotation difference, travel-direction difference) in degrees between the picture's motion and the
    poses'; the direction only when the camera travelled at least MIN_TRAVEL_M (it is a line: either sign)."""
    R, t = pose_motion(T_a, T_b)
    rot = _angle_deg(seen.R.T @ R)
    if np.linalg.norm(t) < MIN_TRAVEL_M:
        return rot, None
    cos = abs(float(np.dot(t / np.linalg.norm(t), seen.t / np.linalg.norm(seen.t))))
    return rot, float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))


# ---------------------------------------------------------------- the whole episode (F5.19)

def settings(profile=None) -> dict:
    """DEFAULTS with the profile's ``ego_motion`` section over them."""
    return {**DEFAULTS, **(getattr(profile, "ego_motion", None) or {})}


def plan(n: int, gap: int, per_window: int) -> list[int]:
    """The rows pairs start at: every ``gap / per_window`` rows (rounded), while the far end is on the timeline."""
    stride = max(1, int(round(gap / max(1, per_window))))
    return list(range(0, max(0, n - gap), stride))


def measure(frames: Iterable, media_frame: np.ndarray, rows: list[int], gap: int, K, D, model: str) -> dict[int, Motion | None]:
    """The picture's motion of each pair (row, row + gap) whose two rows have a picture, from a stream of
    decoded frames (``index``, ``gray``) in media order: features once a picture, a pair matched as soon as
    its far picture comes, a picture forgotten when no pair needs it any more. None: the pictures could not
    be matched; a pair missing from the result had no picture at one end (or the stream ended before it)."""
    import cv2

    near_of: dict[int, list[int]] = {}         # media frame -> pairs it starts
    far_of: dict[int, list[int]] = {}          # media frame -> pairs it ends
    for r in rows:
        a, b = int(media_frame[r]), int(media_frame[r + gap])
        if a >= 0 and b >= 0 and b > a:
            near_of.setdefault(a, []).append(r)
            far_of.setdefault(b, []).append(r)
    if not far_of:
        return {}
    pending = {k: len(v) for k, v in near_of.items()}
    last = max(far_of)
    kept: dict[int, Features] = {}
    out: dict[int, Motion | None] = {}
    orb, matcher = cv2.ORB_create(ORB_FEATURES), cv2.BFMatcher(cv2.NORM_HAMMING)
    mask = None
    for fr in frames:
        k = int(fr.index)
        if k > last:
            break
        if k not in near_of and k not in far_of:
            continue
        if mask is None:
            mask = scene_mask(fr.gray.shape[1], fr.gray.shape[0])
        f = features(fr.gray, mask, orb)
        for r in far_of.get(k, ()):
            a = int(media_frame[r])
            near = kept.get(a)
            if near is None:                    # its near picture never came (undecodable)
                continue
            out[r] = match(near, f, K, D, model, matcher)
            pending[a] -= 1
            if pending[a] <= 0:
                kept.pop(a, None)
        if near_of.get(k):
            kept[k] = f
    return out


def _band(value: float, edges) -> int:
    """0 below the first edge, then 1, 2, 3 (minor, moderate, severe)."""
    return int(sum(value >= e for e in edges))


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    out, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        if not f and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def _turn_deg(T_a, T_b) -> float:
    return _angle_deg(pose_motion(T_a, T_b)[0])


def assess(t: np.ndarray, T: np.ndarray, seen: dict[int, Motion | None], rows: list[int], gap: int, *,
           lag_search_s: float = 1.0, cfg: dict | None = None) -> dict:
    """The sub-item of one camera from its pairs' picture motions ``seen`` and its recorded poses ``T``
    (``T_reference_camera`` per row, NaN where missing): status, metrics, the bad segments and the stretches
    the pictures could not be matched in."""
    from . import segments as SG

    cfg = cfg or settings()
    t = np.asarray(t, float)
    n = len(t)
    step = float(np.median(np.diff(t))) if n > 1 else 1 / 30
    posed = np.isfinite(T[:, 0, 0])
    seen = {r: (m if m is not None and m.inliers >= cfg["min_inliers"] else None) for r, m in seen.items()}
    attempted = [r for r in rows if r in seen and posed[r] and posed[r + gap]]
    measured = [r for r in attempted if seen[r] is not None]
    out = {"status": None, "reason": None, "window_s": round(gap * step, 3),
           "stride_s": round((rows[1] - rows[0]) * step, 3) if len(rows) > 1 else None,
           "metrics": {"pairs": len(measured), "attempted": len(attempted),
                       "coverage": round(len(measured) / len(attempted), 3) if attempted else None},
           "segments": [], "unmatched": [], "lag": None}
    # stretches the pictures could not be matched in (a white wall, blur): listed, never judged
    if attempted:
        miss = np.array([seen[r] is None for r in attempted])
        for a, b in _runs(miss):
            r0, r1 = attempted[a], attempted[b] + gap
            if t[attempted[b]] - t[r0] >= cfg["unmatched_min_s"]:      # pair starts, as for the segments
                out["unmatched"].append({"start_frame": int(r0), "end_frame": int(r1), "start_s": round(float(t[r0]), 3),
                                         "end_s": round(float(t[r1]), 3), "reason": LOW_COVERAGE})
    if not attempted:
        out.update(status="unsupported", reason=NO_PAIRS)
        return out

    def readings(delta: int) -> tuple[np.ndarray, np.ndarray, list[int]]:
        e, turn, used = [], [], []
        for r in measured:
            a, b = r + delta, r + gap + delta
            if 0 <= a and b < n and posed[a] and posed[b]:
                e.append(disagreement(seen[r], T[a], T[b])[0])
                turn.append(_turn_deg(T[a], T[b]))
                used.append(r)
        return np.array(e), np.array(turn), used

    e0, turn0, used0 = readings(0)
    m = out["metrics"]
    if len(e0):
        rel0 = e0 / np.maximum(turn0, cfg["min_turn_deg"])
        dirs = [d for r in used0 if (d := disagreement(seen[r], T[r], T[r + gap])[1]) is not None]
        m.update(rotation_median_deg=round(float(np.median(e0)), 2),
                 rotation_p95_deg=round(float(np.percentile(e0, 95)), 2),
                 turn_median_deg=round(float(np.median(turn0)), 2),
                 relative_median=round(float(np.median(rel0)), 2),
                 direction_median_deg=round(float(np.median(dirs)), 1) if dirs else None)
    if len(measured) < cfg["min_pairs"] or len(measured) < cfg["coverage_min"] * len(attempted):
        out.update(status="unknown", reason=LOW_COVERAGE)
        return out
    # the time offset: shift the poses against the pictures, the smallest median rotation difference
    span = max(1, int(round(lag_search_s / step)))
    scan: dict[int, float] = {}
    for d in range(-span, span + 1):
        e, _, used = readings(d)
        if len(used) >= max(5, len(measured) // 2):
            scan[d] = float(np.median(e))
    best = min(scan, key=lambda d: (scan[d], abs(d)))
    at0 = scan.get(0, float(np.median(e0)))
    spread = float(np.median(list(scan.values())))
    confidence = max(0.0, min(1.0, 1.0 - scan[best] / spread)) if spread > 0 else 0.0
    lag_s = round(best * step, 3)
    offset = (abs(lag_s) >= cfg["lag_min_s"] and confidence >= cfg["lag_confidence_min"]
              and at0 - scan[best] >= cfg["lag_gain_deg"] and at0 >= cfg["lag_gain_ratio"] * scan[best])
    out["lag"] = {"lag_s": round(lag_s, 3), "confidence": round(confidence, 2), "flagged": bool(offset),
                  "median_at_zero_deg": round(at0, 2), "median_at_lag_deg": round(scan[best], 2),
                  "searched_s": round(span * step, 3)}
    m.update(lag_s=round(lag_s, 3), lag_confidence=round(confidence, 2))
    # the bad stretches: rotation (with the poses at the found offset when there is one), then the offset itself
    delta = best if offset else 0
    e, turn, used = readings(delta)
    rel = e / np.maximum(turn, cfg["min_turn_deg"])
    starts = np.array([t[r] for r in used])
    value = SG.rolling_median(np.where(rel >= cfg["relative_min"], e, 0.0), cfg["smooth_pairs"])
    # a stretch of pair starts at least min_duration_s long (frame times are thirds: a hair of slack): one bad
    # pair or two is noise
    shortest = cfg["min_duration_s"] - 1e-6
    for sg in SG.hysteresis(value, starts, on=cfg["rotation_on_deg"], off=cfg["rotation_off_deg"],
                            min_duration_s=shortest, max_gap_s=cfg["max_gap_s"]):
        idx = list(range(sg.start, sg.end + 1))
        mag = float(np.median(e[idx]))
        rmed = float(np.median(rel[idx]))
        band = max(_band(mag, cfg["bands_deg"]), 1 + _band(rmed, cfg["relative_bands"]) if rmed >= cfg["relative_bands"][0] else 0)
        worst = idx[int(np.argmax(e[idx]))]
        out["segments"].append(_segment(t, used[sg.start], used[sg.end] + gap, "rotation", round(mag, 2), "deg",
                                        max(1, min(3, band)), [used[worst], used[worst] + gap],
                                        relative=round(rmed, 2)))
    if offset:
        e_shift = {r: x for r, x in zip(used, e)}
        e_zero = {r: x for r, x in zip(used0, e0)}
        both = [r for r in used0 if r in e_shift]
        excess = np.array([e_zero[r] - e_shift[r] for r in both])
        value = SG.rolling_median(excess, cfg["smooth_pairs"])
        found = SG.hysteresis(value, np.array([t[r] for r in both]), on=cfg["excess_on_deg"], off=cfg["excess_off_deg"],
                              min_duration_s=shortest, max_gap_s=cfg["max_gap_s"])
        band = max(1, min(3, _band(round(abs(lag_s), 3), cfg["lag_bands_s"])))
        if not found and both:                  # an offset over the whole episode, no stretch standing out
            worst = both[int(np.argmax(excess))]
            out["segments"].append(_segment(t, both[0], both[-1] + gap, "time_offset", round(abs(lag_s), 3), "s",
                                            band, [worst, worst + gap], lag_s=round(lag_s, 3)))
        for sg in found:
            idx = list(range(sg.start, sg.end + 1))
            worst = both[idx[int(np.argmax(excess[idx]))]]
            out["segments"].append(_segment(t, both[sg.start], both[sg.end] + gap, "time_offset", round(abs(lag_s), 3),
                                            "s", band, [worst, worst + gap], lag_s=round(lag_s, 3)))
    out["segments"].sort(key=lambda s: (-s["band_level"], s["start_frame"]))
    out["status"] = "suspect" if out["segments"] else "ok"
    return out


def _segment(t, r0: int, r1: int, reason: str, magnitude: float, unit: str, band: int, evidence: list[int], **more) -> dict:
    return {"start_frame": int(r0), "end_frame": int(r1), "start_s": round(float(t[r0]), 3), "end_s": round(float(t[r1]), 3),
            "reason": reason, "magnitude": magnitude, "unit": unit, "band": BANDS[band - 1], "band_level": band,
            "evidence_frames": [int(x) for x in evidence], **more}


def camera_ego_motion(sample, camera_id: str, frames, *, window_s: float = 0.5, lag_search_s: float = 1.0,
                      profile=None) -> dict | None:
    """The ``ego_motion`` sub-item of one camera (design doc 22 §5.3): None for a camera that is not on a
    gripper; ``unsupported`` with a reason when its calibration, poses or video are missing. ``frames``: the
    camera's decoded frames (a callable giving the iterator, called only when there is something to measure)."""
    from .video import DecodeError

    cam = sample.cameras[camera_id]
    if cam.mount != "wrist":
        return None
    cfg = settings(profile)
    cal = cam.calibration(sample)
    posed = np.isfinite(cam.T_reference_camera[:, 0, 0]) if cam.T_reference_camera.ndim == 3 else np.zeros(sample.n_frames, bool)
    why = (NO_CALIBRATION if cal is None else NO_CAMERA_POSES if posed.sum() < 2 or sample.t is None
           else NO_VIDEO if cam.media.get("kind") != "video" else None)
    if why:
        return {"status": "unsupported", "reason": why, "metrics": {}, "segments": [], "unmatched": [], "lag": None}
    t = np.asarray(sample.t, float)
    step = float(np.median(np.diff(t))) if len(t) > 1 else 1 / 30
    gap = max(1, int(round(window_s / step)))
    rows = plan(sample.n_frames, gap, int(cfg["pairs_per_window"]))
    first = int(np.flatnonzero(np.isfinite(cam.H[:, 0, 0]))[0]) if np.isfinite(cam.H[:, 0, 0]).any() else None
    Hm = cam.H[first] if first is not None else np.eye(3)
    K = np.asarray(cal["K"], float).copy()
    K[0] *= Hm[0, 0]
    K[1] *= Hm[1, 1]
    try:
        seen = measure(frames() if callable(frames) else frames, cam.video_frame_index, rows, gap, K,
                       cal["distortion_coefficients"], cal["model"])
    except (DecodeError, OSError) as exc:
        return {"status": "unknown", "reason": FRAMES_UNREADABLE, "message": str(exc)[:200], "metrics": {},
                "segments": [], "unmatched": [], "lag": None}
    return assess(t, cam.T_reference_camera, seen, rows, gap, lag_search_s=lag_search_s, cfg=cfg)


def _seconds(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def describe(camera_id: str, seg: dict, more: str = "") -> str:
    """One segment in words: 「位姿约晚 0.47 s（第 12–1490 帧，中）」; ``more`` after the frames (「等 4 段」)."""
    band = {"minor": "轻", "moderate": "中", "severe": "重"}[seg["band"]]
    where = f"第 {seg['start_frame'] + 1}–{seg['end_frame'] + 1} 帧{more}，{band}"
    if seg["reason"] == "time_offset":
        lag = seg.get("lag_s") or 0.0
        return f"{camera_id}：位姿约{'晚' if lag > 0 else '早'} {_seconds(abs(lag))} s（{where}）"
    return f"{camera_id}：画面里的转动与位姿差 {seg['magnitude']:.1f}°（{where}）"


def _weight(seg: dict) -> tuple:
    """How bad a segment is: its band, then its size (a second of time offset weighs like 10 degrees), then its length."""
    size = seg["magnitude"] if seg["unit"] == "deg" else 10 * seg["magnitude"]
    return seg["band_level"], size, seg["end_s"] - seg["start_s"]


def _phrases(camera_id: str, segs: list[dict]) -> list[str]:
    """A camera's segments in words: a time offset once (its longest stretch), then at most two rotation stretches."""
    out = []
    timed = [s for s in segs if s["reason"] == "time_offset"]
    if timed:
        out.append(describe(camera_id, max(timed, key=_weight), f"等 {len(timed)} 段" if len(timed) > 1 else ""))
    turned = [s for s in segs if s["reason"] == "rotation"]
    out += [describe(camera_id, s) for s in turned[:2]]
    if len(turned) > 2:
        out[-1] += f"，另有 {len(turned) - 2} 段"
    return out


def summarize(cameras: dict[str, dict], *, assumed: str | None = None, profile=None) -> dict:
    """The episode's ``ego_motion``: every camera's sub-item, the status over them (suspect if any camera is,
    ok if any is ok, else unknown / unsupported), good / bad with one sentence, the worst segment."""
    statuses = [c["status"] for c in cameras.values()]
    status = ("suspect" if "suspect" in statuses else "ok" if "ok" in statuses
              else "unknown" if "unknown" in statuses else "unsupported")
    worst_cam, worst = None, None
    for cid, c in cameras.items():
        for s in c.get("segments") or []:
            if worst is None or _weight(s) > _weight(worst):
                worst_cam, worst = cid, s
    if status == "suspect":
        parts = [p for cid, c in cameras.items() for p in _phrases(cid, c.get("segments") or [])]
        sentence = "腕部相机的运动与记录的位姿不一致：" + "；".join(parts)
    elif status == "ok":
        meds = [f"{c['metrics']['rotation_median_deg']:.1f}°" for c in cameras.values()
                if c["status"] == "ok" and c["metrics"].get("rotation_median_deg") is not None]
        sentence = "腕部相机画面里的转动与记录的位姿一致（旋转差中位 " + " / ".join(meds) + "）"
        if "unknown" in statuses:
            sentence += "；有的相机画面匹配不足，没判"
    elif status == "unknown":
        sentence = "腕部相机的画面匹配不足（白墙、模糊或动得太少），判断不了"
    else:
        sentence = "没有可以比对的腕部相机"
    out = {"status": status, "verdict": {"suspect": "bad", "ok": "good"}.get(status, "unknown"),
           "explanation_zh": sentence, "cameras": cameras,
           "worst": {"camera": worst_cam, **worst} if worst is not None else None,
           "uncalibrated": not bool(getattr(profile, "calibrated", False)),
           "threshold_profile": profile.summary() if profile is not None else None}
    if assumed:
        out["assumed"] = assumed
    return out
