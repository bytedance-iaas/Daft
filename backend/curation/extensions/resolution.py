"""Resolution check: does each video actually have the resolution and frame rate it claims?

Three comparisons, all discrete (a value matches or it does not - no soft thresholds):

1. container header vs decoded frames - the stream header can lie (capture stacks have been
   seen writing one size into the header and another into the bitstream);
2. decoded frames vs the declared spec (LeRobot ``meta/info.json``: the video feature's
   ``shape`` and its fps) - catches silent capture fallbacks (USB bandwidth downshifts,
   a camera that dropped its configuration on reconnect);
3. cross-episode / same-camera consistency - NOT judged here. This module sees one video;
   it puts the measured values into ``detail["measured"]`` and the dataset-level
   aggregation compares them across episodes.

Verdicts are three-state (unknown is never a pass): ``passed=True`` only when every
comparison ran and matched; ``passed=False`` on any mismatch; ``passed=None`` with
``detail["reason"]`` when a comparison could not be performed (unreadable file, no
declared spec, unmeasurable fps).

Frame rate tolerance: measured fps within ``REL_FPS_TOL`` (0.5%) of the declared value
passes. This absorbs container timebase rounding (29.97 declared as 30 differs by 0.1%)
while any real fallback (60->30, 30->15) is an integer factor away and always fails.

Pure I/O is isolated in :func:`probe_video`; :func:`judge` is a pure function over the
probe result, so every verdict branch is testable without a video file.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from curation.core.contract import CheckResult

#: Relative fps tolerance (see module docstring for why 0.5%).
REL_FPS_TOL = 0.005

#: How many frames to decode for the measurement (enough for a stable pts-delta median).
DEFAULT_SAMPLE_FRAMES = 12


@dataclass(frozen=True)
class VideoProbe:
    """What one video actually contains (or why we could not look)."""

    ok: bool
    reason: str = ""                     # "file_missing" / "decode_failed" / "no_frames" when not ok
    container_width: int | None = None   # stream header claim
    container_height: int | None = None
    container_fps: float | None = None   # container average_rate
    frame_width: int | None = None       # from the decoded frames themselves
    frame_height: int | None = None
    measured_fps: float | None = None    # median pts delta of the sampled frames
    n_frames: int = 0


def probe_video(path: str, sample_frames: int = DEFAULT_SAMPLE_FRAMES) -> VideoProbe:
    """Decode the first frames of ``path`` and report header claims vs measured reality."""
    import os

    import av

    if not os.path.isfile(path):
        return VideoProbe(ok=False, reason="file_missing")
    try:
        with av.open(path) as container:
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                return VideoProbe(ok=False, reason="no_video_stream")
            c_w = int(stream.codec_context.width or 0) or None
            c_h = int(stream.codec_context.height or 0) or None
            rate = stream.average_rate or stream.guessed_rate
            c_fps = float(rate) if rate else None
            f_w = f_h = None
            times: list[float] = []
            n = 0
            for frame in container.decode(stream):
                if f_w is None:
                    f_w, f_h = int(frame.width), int(frame.height)
                if frame.time is not None:
                    times.append(float(frame.time))
                n += 1
                if n >= sample_frames:
                    break
            if n == 0:
                return VideoProbe(ok=False, reason="no_frames")
            m_fps = None
            if len(times) >= 3:
                deltas = np.diff(np.asarray(times))
                deltas = deltas[deltas > 0]
                if len(deltas):
                    m_fps = float(1.0 / np.median(deltas))
            return VideoProbe(ok=True, container_width=c_w, container_height=c_h,
                              container_fps=c_fps, frame_width=f_w, frame_height=f_h,
                              measured_fps=m_fps, n_frames=n)
    except Exception as exc:  # av raises many concrete types; all mean the same verdict
        return VideoProbe(ok=False, reason=f"decode_failed: {type(exc).__name__}: {exc}")


def judge(probe: VideoProbe, declared: dict[str, Any] | None) -> CheckResult:
    """Pure verdict over a probe result. ``declared``: {"width", "height", "fps"}, any key optional."""
    name = "resolution"
    if not probe.ok:
        return CheckResult(name=name, passed=None, score=None,
                           detail={"reason": probe.reason, "mismatches": []})

    measured = {"width": probe.frame_width, "height": probe.frame_height,
                "fps": None if probe.measured_fps is None else round(probe.measured_fps, 3)}
    container = {"width": probe.container_width, "height": probe.container_height,
                 "fps": None if probe.container_fps is None else round(probe.container_fps, 3)}
    detail: dict[str, Any] = {"measured": measured, "container": container,
                              "declared": dict(declared) if declared else None,
                              "n_frames_sampled": probe.n_frames, "mismatches": []}
    mismatches: list[dict[str, Any]] = detail["mismatches"]

    # 1. header vs frames - a lying container is a defect regardless of any schema
    if (probe.container_width and probe.frame_width
            and (probe.container_width, probe.container_height)
            != (probe.frame_width, probe.frame_height)):
        mismatches.append({"kind": "container_vs_frames",
                           "expected": [probe.container_width, probe.container_height],
                           "actual": [probe.frame_width, probe.frame_height]})

    if not declared or all(declared.get(k) is None for k in ("width", "height", "fps")):
        if mismatches:
            return CheckResult(name=name, passed=False, score=0.0, detail=detail)
        detail["reason"] = "no_declared_spec"
        return CheckResult(name=name, passed=None, score=None, detail=detail)

    # 2. frames vs declared resolution
    d_w, d_h = declared.get("width"), declared.get("height")
    if d_w is not None and d_h is not None and (probe.frame_width, probe.frame_height) != (d_w, d_h):
        mismatches.append({"kind": "declared_resolution", "expected": [d_w, d_h],
                           "actual": [probe.frame_width, probe.frame_height]})

    # 3. measured fps vs declared fps
    d_fps = declared.get("fps")
    fps_unmeasurable = False
    if d_fps:
        m_fps = probe.measured_fps if probe.measured_fps is not None else probe.container_fps
        if m_fps is None:
            fps_unmeasurable = True      # cannot verify a declared value: that is not a pass
        elif abs(m_fps - float(d_fps)) / float(d_fps) > REL_FPS_TOL:
            mismatches.append({"kind": "declared_fps", "expected": float(d_fps),
                               "actual": round(m_fps, 3)})

    if mismatches:
        return CheckResult(name=name, passed=False, score=0.0, detail=detail)
    if fps_unmeasurable:
        detail["reason"] = "fps_unmeasurable"
        return CheckResult(name=name, passed=None, score=None, detail=detail)
    return CheckResult(name=name, passed=True, score=1.0, detail=detail)


def _height_width_from_feature(feat: dict[str, Any]) -> tuple[int | None, int | None]:
    """(height, width) out of a video feature's ``shape``, robust to axis order.

    Datasets in the wild use both [h, w, c] and [c, h, w]. Trust the feature's
    ``names`` when it labels the axes (``names`` may be a dict keyed by group, the
    same shape ``lerobot_reader`` handles for state/action); otherwise fall back to
    a channel heuristic: a leading dim that looks like a channel count (1/3/4) while
    the trailing one does not means channel-first. Hard-coding [h, w, c] would turn
    every episode of a channel-first dataset into a false resolution mismatch.
    """
    shape = feat.get("shape")
    if not isinstance(shape, (list, tuple)) or len(shape) < 2:
        return None, None
    names = feat.get("names")
    if isinstance(names, dict):
        names = next(iter(names.values()), [])
    if isinstance(names, (list, tuple)) and len(names) == len(shape):
        lowered = [str(n).lower() for n in names]
        if "height" in lowered and "width" in lowered:
            return int(shape[lowered.index("height")]), int(shape[lowered.index("width")])
    if len(shape) == 3 and shape[0] in (1, 3, 4) and shape[2] not in (1, 3, 4):
        return int(shape[1]), int(shape[2])   # channel-first
    return int(shape[0]), int(shape[1])       # LeRobot default: [h, w, c]


def declared_from_lerobot(info: dict[str, Any], video_key: str) -> dict[str, Any] | None:
    """Declared spec for one video feature out of a LeRobot ``meta/info.json`` dict.

    Resolution comes from the feature's ``shape`` (axis order resolved by
    :func:`_height_width_from_feature`); fps prefers the feature's own
    ``info["video.fps"]`` and falls back to the dataset-level ``info["fps"]``
    (the convention ``ingest/lerobot_reader.py`` also follows).
    """
    feat = (info.get("features") or {}).get(video_key)
    if not feat:
        return None
    height, width = _height_width_from_feature(feat)
    fps = (feat.get("info") or {}).get("video.fps") or info.get("fps")
    return {"width": width, "height": height, "fps": float(fps) if fps else None}


def resolution_check(video_path: str, declared: dict[str, Any] | None = None,
                     sample_frames: int = DEFAULT_SAMPLE_FRAMES) -> CheckResult:
    """Probe one video and judge it against its declared spec (see module docstring)."""
    return judge(probe_video(video_path, sample_frames=sample_frames), declared)
