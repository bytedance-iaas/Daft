"""Hysteresis segmentation and evidence frame choice (design 12 §9.2, §10.4).

Invalid frames are marked first (NaN) and never counted as below-threshold: a segment may bridge a
short gap of invalid or quiet frames, but its duration only counts the frames it actually covers.
"""
from __future__ import annotations

import dataclasses

import numpy as np


@dataclasses.dataclass
class Segment:
    start: int                 # first frame (inclusive)
    end: int                   # last frame (inclusive)
    start_s: float
    end_s: float
    peak: float
    mean: float
    valid_frames: int
    reasons: list[str] = dataclasses.field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def as_dict(self, **extra) -> dict:
        d = {"start_frame": self.start, "end_frame": self.end, "start_s": round(self.start_s, 3),
             "end_s": round(self.end_s, 3), "duration_s": round(self.duration_s, 3),
             "peak": round(float(self.peak), 3), "mean": round(float(self.mean), 3),
             "valid_frames": self.valid_frames, "reasons": list(self.reasons)}
        d.update(extra)
        return d


def rolling_median(x: np.ndarray, width: int) -> np.ndarray:
    """Centered rolling median over finite values (NaN where the window holds none)."""
    x = np.asarray(x, float)
    half = max(0, int(width) // 2)
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        w = x[max(0, i - half):i + half + 1]
        w = w[np.isfinite(w)]
        if len(w) and np.isfinite(x[i]):
            out[i] = np.median(w)
    return out


def hysteresis(values: np.ndarray, t: np.ndarray, *, on: float, off: float, min_duration_s: float,
               max_gap_s: float, reason: str | None = None) -> list[Segment]:
    """Segments where ``values`` rises to ``on`` and stays above ``off`` (short gaps allowed)."""
    v = np.asarray(values, float)
    t = np.asarray(t, float)
    segs: list[Segment] = []
    active = False
    start = last_hot = None
    for i in range(len(v)):
        x = v[i]
        hot = np.isfinite(x) and x >= (off if active else on)
        if hot:
            if not active:
                active, start = True, i
            last_hot = i
            continue
        if active and t[i] - t[last_hot] > max_gap_s:
            segs.append(_close(v, t, start, last_hot, reason))
            active = False
    if active:
        segs.append(_close(v, t, start, last_hot, reason))
    return [s for s in segs if s.duration_s + _step(t) >= min_duration_s]


def _step(t: np.ndarray) -> float:
    d = np.diff(t[np.isfinite(t)])
    return float(np.median(d)) if len(d) else 0.0


def _close(v, t, a, b, reason) -> Segment:
    w = v[a:b + 1]
    w = w[np.isfinite(w)]
    return Segment(a, b, float(t[a]), float(t[b]), float(w.max()), float(w.mean()), int(len(w)),
                   [reason] if reason else [])


def worst_frames(values: np.ndarray, segment: Segment, k: int = 3) -> list[int]:
    """The ``k`` frames with the largest value inside a segment (evidence, §10.4)."""
    idx = np.arange(segment.start, segment.end + 1)
    v = np.asarray(values, float)[idx]
    ok = np.isfinite(v)
    order = idx[ok][np.argsort(-v[ok])]
    return sorted(int(i) for i in order[:k])
