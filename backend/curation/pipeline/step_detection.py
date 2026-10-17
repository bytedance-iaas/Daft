"""Step boundary candidates measured from the numbers (module ``step_detection``, registry 8.12): TASK-1's
*detected* track, scored against the declared one (``declared_steps``) by the regression scorer.

Two cues, both from the robot's own state (the command when there is no state):

* **a pause of the whole body** - the rest rule of ``motion_quality`` (8.10): the eligible columns normalized by
  their range, the speed of that vector, a rest line ``lo = clamp(3 x p10, 0.005, 0.02)`` range/s with a
  hysteresis at ``2 x lo``, gaps under 0.3 s closed. The *start* of a mid-episode pause is a candidate (HABIT's
  pedal-marked steps: 42 of 43 boundaries have a pause starting within a second); the head and tail idles are
  TASK-11's, not steps. Score = min(1, duration / ``pause_ref_s``): a short hesitation scores low, a 3 s wait
  scores 1.
* **a gripper opening or closing** - a gripper column (the semantics layout's ``gripper_dims``, else the
  embodiment profile's) crossing the middle of its range, debounced to 0.5 s. Score 0.8 (Galaxea's pick-and-place
  steps: a release is a step end 93 % of the time, a grasp 47 %).

Candidates within 0.5 s merge (the higher score stays). The ones at or above ``min_score`` become
``step_candidate`` findings; every candidate stays a reading. Measured on the sample sets (tolerance 1 s): HABIT
22 episodes / 43 boundaries - pauses alone recall 0.56, precision 0.49 at min_score 0.3, both cues recall 0.93,
precision 0.24; Galaxea arrange_mineral_water_bottles 10 / 75 - grippers recall 0.75, precision 0.70; the
numbers are in the registry docstring (8.12). A semantic step with neither a pause nor a gripper event (Galaxea's
push_in_chairs) is not found; that is what a picture-reading track would add.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

import numpy as np

from ..core.contract import CheckResult

#: the rest rule's constants (motion_quality 8.10 / spikes/task11/idle_edges_ruler.py)
MIN_RUN_S, FLOOR_PCT, LO_MULT, LO_ABS, LO_CAP, HI_MULT = 0.3, 10, 3.0, 0.005, 0.02, 2.0
#: a gripper event closer than this to the previous one on the same column is bounce, not a step
GRIPPER_GAP_S = 0.5
#: candidates closer than this merge
MERGE_S = 0.5
GRIPPER_SCORE = 0.8
#: defaults of the module's parameters
MIN_SCORE = 0.3
PAUSE_REF_S = 3.0


def normalized_speed(state: np.ndarray, fps: float) -> np.ndarray:
    """Per-frame speed (range/s) of the state's moving columns normalized by their range; length T-1."""
    s = np.asarray(state, dtype=np.float64)
    if s.ndim != 2 or len(s) < 2:
        return np.zeros(max(len(s) - 1, 0))
    rng = s.max(0) - s.min(0)
    keep = rng > 1e-9
    if not keep.any():
        return np.zeros(len(s) - 1)
    n = (s[:, keep] - s[:, keep].min(0)) / rng[keep]
    return np.linalg.norm(np.diff(n, axis=0), axis=1) * float(fps)


def rest_segments(v: np.ndarray, fps: float) -> list[tuple[int, int]]:
    """``(start, end)`` frame runs at rest: below ``lo`` (hysteresis ``hi`` while at rest), gaps shorter than
    0.3 s closed, runs shorter than 0.3 s dropped."""
    v = np.asarray(v, dtype=np.float64)
    if len(v) == 0:
        return []
    floor = float(np.percentile(v, FLOOR_PCT))
    lo = min(LO_CAP, max(LO_MULT * floor, LO_ABS))
    hi = HI_MULT * lo
    still = np.zeros(len(v), bool)
    at_rest = bool(v[0] < lo)
    for i, x in enumerate(v):
        at_rest = (x < hi) if at_rest else (x < lo)
        still[i] = at_rest
    close = max(1, int(np.ceil(MIN_RUN_S * fps)))
    n = len(still)
    i = 0
    while i < n:                                   # close short gaps between rests
        if not still[i]:
            j = i
            while j < n and not still[j]:
                j += 1
            if 0 < i and j < n and (j - i) < close:
                still[i:j] = True
            i = j
        else:
            i += 1
    segs, i = [], 0
    while i < n:
        if still[i]:
            j = i
            while j < n and still[j]:
                j += 1
            if (j - i) >= close:
                segs.append((i, j))
            i = j
        else:
            i += 1
    return segs


def gripper_events(g: np.ndarray, fps: float, min_gap_s: float = GRIPPER_GAP_S) -> list[tuple[int, str]]:
    """``(frame, "grasp" | "release")`` where a gripper column crosses the middle of its range, debounced."""
    g = np.asarray(g, dtype=np.float64).ravel()
    if len(g) < 2:
        return []
    rng = float(g.max() - g.min())
    if rng < 1e-9:
        return []
    b = (g > g.min() + 0.5 * rng).astype(int)
    d = np.diff(b)
    out: list[tuple[int, str]] = []
    for i in np.where(d != 0)[0] + 1:
        if out and (i - out[-1][0]) / fps < min_gap_s:
            continue
        out.append((int(i), "grasp" if d[i - 1] > 0 else "release"))
    return out


def candidates(stream: np.ndarray, fps: float, gripper_dims, *, pause_ref_s: float = PAUSE_REF_S,
               gripper_stream: np.ndarray | None = None) -> list[dict]:
    """The scored candidates of one episode, by time. ``stream`` carries the body (pauses), ``gripper_stream``
    (default: the same) the gripper columns."""
    s = np.asarray(stream, dtype=np.float64)
    T = len(s)
    dims = tuple(int(j) for j in (gripper_dims or ()) if 0 <= int(j) < s.shape[1])
    body = [j for j in range(s.shape[1]) if j not in dims]
    out: list[dict] = []
    v = normalized_speed(s[:, body], fps) if body and T >= 2 else np.zeros(max(T - 1, 0))
    for a, b in rest_segments(v, fps):
        if a == 0 or b >= len(v):                   # the head and the tail idle are TASK-11, not a step
            continue
        dur = (b - a) / fps
        out.append({"frame": int(a), "t_s": round(a / fps, 3), "cue": "pause",
                    "score": round(min(1.0, dur / pause_ref_s), 2), "dur_s": round(dur, 2)})
    g = np.asarray(gripper_stream, dtype=np.float64) if gripper_stream is not None else s
    for j in dims:
        if j >= g.shape[1]:
            continue
        for fr, kind in gripper_events(g[:, j], fps):
            out.append({"frame": fr, "t_s": round(fr / fps, 3), "cue": kind, "score": GRIPPER_SCORE, "column": j})
    out.sort(key=lambda c: (c["frame"], -c["score"]))
    merged: list[dict] = []
    for c in out:
        if merged and (c["frame"] - merged[-1]["frame"]) / fps < MERGE_S:
            if c["score"] > merged[-1]["score"]:
                merged[-1] = c
            continue
        merged.append(c)
    return merged


def gripper_dims_of(semantics_extras: Any, embodiment_id: Any, registry) -> tuple[int, ...]:
    """The gripper columns the semantics layout names, else the embodiment profile's."""
    try:
        lay = (json.loads(str(semantics_extras) or "{}") or {}).get("layout") or {}
        dims = tuple(int(x) for x in (lay.get("gripper_dims") or ()))
        if dims:
            return dims
    except Exception:  # noqa: BLE001 - no usable extras
        pass
    try:
        return tuple(int(x) for x in (registry.get(str(embodiment_id)).gripper_dims or ()))
    except Exception:  # noqa: BLE001 - unknown embodiment
        return ()


def detect(action, proprio, fps: float, gripper_dims=(), *, min_score: float = MIN_SCORE,
           pause_ref_s: float = PAUSE_REF_S) -> CheckResult:
    """One episode's candidates as a check result: the body from the state (else the command), the gripper
    events from whichever of the two actually moves on the gripper columns."""
    detail: dict = {"min_score": float(min_score), "pause_ref_s": float(pause_ref_s), "fps": float(fps),
                    "gripper_dims": [int(j) for j in (gripper_dims or ())]}
    a = np.asarray(action, dtype=np.float64) if action is not None else None
    p = np.asarray(proprio, dtype=np.float64) if proprio is not None else None
    stream, name = (p, "state") if p is not None and p.ndim == 2 and len(p) >= 3 else (a, "action")
    if stream is None or stream.ndim != 2 or len(stream) < 3:
        detail["reason"] = "没有可用的读数列或指令列"
        return CheckResult(name="step_detection", score=1.0, detail=detail)
    detail["stream"] = name
    detail["n_frames"] = int(len(stream))
    grip = stream
    dims = [int(j) for j in (gripper_dims or ()) if 0 <= int(j) < stream.shape[1]]
    if dims and a is not None and a.ndim == 2 and len(a) == len(stream) and all(j < a.shape[1] for j in dims):
        # a gripper column that never moves in the state (HABIT's zeros) but does in the command: read the command
        still = all(float(stream[:, j].max() - stream[:, j].min()) < 1e-9 for j in dims)
        moves = any(float(a[:, j].max() - a[:, j].min()) >= 1e-9 for j in dims)
        if still and moves:
            grip = a
            detail["gripper_stream"] = "action"
    cands = candidates(stream, fps, dims, pause_ref_s=pause_ref_s, gripper_stream=grip)
    detail["candidates"] = cands
    detail["n_candidates"] = len(cands)
    detail["n_reported"] = sum(1 for c in cands if c["score"] >= min_score)
    detail["cues"] = dict(Counter(c["cue"] for c in cands))
    return CheckResult(name="step_detection", score=1.0, detail=detail)
