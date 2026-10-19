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
#: the gripper reading is median-filtered over this long before an event is read off it (HABIT's bounce)
GRIPPER_SETTLE_S = 0.5
#: an event is the filtered reading crossing out of this band of its range (hysteresis: 40 % down, 60 % up)
GRIPPER_BAND = (0.4, 0.6)
#: candidates closer than this merge
MERGE_S = 0.5
GRIPPER_SCORE = 0.8
#: the end of a pause scores this fraction of its start. On HABIT every matched pause start left its resume as an
#: extra (48 of 111 extras, 2026-10-18) and at half score the sample sets still lost precision (HABIT 0.35 -> 0.27)
#: for one more boundary on Galaxea push_in_chairs, whose labels sit on the resumes after 3 s waits; so a resume
#: stays a reading (0.25 x 1.0 < min_score 0.3) unless a task lowers min_score or raises pause_ref_s's reach
RESUME_FACTOR = 0.25
#: defaults of the module's parameters
MIN_SCORE = 0.3
PAUSE_REF_S = 3.0


def _lag1_rho(d: np.ndarray) -> float:
    x, y = d[:-1] - d[:-1].mean(), d[1:] - d[1:].mean()
    sx, sy = float(np.sqrt(np.dot(x, x))), float(np.sqrt(np.dot(y, y)))
    return float(np.dot(x, y) / (sx * sy)) if sx > 0.0 and sy > 0.0 else 0.0


def voting_columns(state: np.ndarray) -> list[int]:
    """The columns that may say the body moves: the same eligibility as motion_quality's rest rule - a column
    whose whole range is only a few of its typical steps never moved (a sensor's noise, a 4 mm torso reading
    that jumps once), so it gets no vote; a small, coherent back-and-forth keeps its vote through the lag-1
    coherence of its steps. Without it a column like that, normalized by its tiny range, reads as perpetual
    motion and no pause is ever seen (Galaxea 2026-10-18: 0 of 829 frames at rest)."""
    s = np.asarray(state, dtype=np.float64)
    if s.ndim != 2 or len(s) < 2:
        return []
    steps = len(s) - 1
    k = min(20.0, max(4.0, 0.5 * steps))
    out = []
    for j in range(s.shape[1]):
        d = np.diff(s[:, j])
        pos = np.abs(d)[np.abs(d) > 0]
        if not len(pos):
            continue
        med = float(np.median(pos))
        span = float(s[:, j].max() - s[:, j].min())
        ok = span >= k * med
        if not ok and steps >= 8 and span >= 4.0 * med:
            ok = _lag1_rho(d) >= 0.5
        if ok and span > 0.0:
            out.append(j)
    return out


def normalized_speed(state: np.ndarray, fps: float) -> np.ndarray:
    """Per-frame speed (range/s) of the state's voting columns (``voting_columns``) normalized by their range;
    length T-1. Nothing votes: zeros (no claim of motion, none of rest either - the caller sees no pause)."""
    s = np.asarray(state, dtype=np.float64)
    if s.ndim != 2 or len(s) < 2:
        return np.zeros(max(len(s) - 1, 0))
    cols = voting_columns(s)
    if not cols:
        return np.zeros(len(s) - 1)
    s = s[:, cols]
    rng = s.max(0) - s.min(0)
    n = (s - s.min(0)) / rng
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
    """``(frame, "grasp" | "release")`` where a gripper column settles on the other side of its range. The
    reading is median-filtered over ``GRIPPER_SETTLE_S`` (HABIT's gripper bounces 0.27 -> 1.0 -> 0.37 -> 0.87
    inside the second it closes: every crossing of the middle counted as an event before, 2026-10-18), then it
    has to cross a 40 % / 60 % band (hysteresis) and stay there; events closer than ``min_gap_s`` are one."""
    g = np.asarray(g, dtype=np.float64).ravel()
    if len(g) < 2:
        return []
    rng = float(g.max() - g.min())
    if rng < 1e-9:
        return []
    w = int(np.ceil(GRIPPER_SETTLE_S * fps))
    w = max(1, w + (1 - w % 2))                         # odd window
    if w > 1 and len(g) > w:
        pad = w // 2
        padded = np.concatenate([np.full(pad, g[0]), g, np.full(pad, g[-1])])
        g = np.median(np.lib.stride_tricks.sliding_window_view(padded, w), axis=1)
    lo, hi = g.min() + GRIPPER_BAND[0] * rng, g.min() + GRIPPER_BAND[1] * rng
    state = bool(g[0] > (lo + hi) / 2)
    out: list[tuple[int, str]] = []
    for i in range(1, len(g)):
        if not state and g[i] > hi:
            state, kind = True, "grasp"
        elif state and g[i] < lo:
            state, kind = False, "release"
        else:
            continue
        if out and (i - out[-1][0]) / fps < min_gap_s:
            out.pop()                                   # an excursion shorter than the gap: neither edge is an event
            continue
        out.append((int(i), kind))
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
        score = round(min(1.0, dur / pause_ref_s), 2)
        # both ends of a pause are candidates: a step ends when the body stops (pause), and the next one
        # starts when it moves again (resume) - Galaxea's step labels and RSS 2026's task end sit on the resume
        # (2026-10-18: push_in_chairs ep15 declared 43.7 s, the pause there ends at 43.87 s and starts at 40.6 s)
        out.append({"frame": int(a), "t_s": round(a / fps, 3), "cue": "pause", "score": score, "dur_s": round(dur, 2)})
        out.append({"frame": int(b), "t_s": round(b / fps, 3), "cue": "resume",
                    "score": round(min(1.0, dur / pause_ref_s) * RESUME_FACTOR, 2), "dur_s": round(dur, 2)})
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
