"""Streaming decode of one view's clip on its real PTS timeline (design 12 §6.2, §11.5).

A LeRobot v3 video concatenates episodes; the clip starts at ``clip_start_s`` of the file's PTS
timeline, so decoding seeks there instead of reading from 0. Frames are numbered from the clip's first
frame (``round((pts - clip_start) * fps)``) and handed out one at a time; nothing keeps the whole clip.
"""
from __future__ import annotations

import dataclasses
import hashlib
from typing import Iterator

import numpy as np


@dataclasses.dataclass
class DecodedFrame:
    index: int                 # frame number within the clip (media frame index)
    pts_s: float               # clip-local presentation time
    gray: np.ndarray           # uint8 (H, W)
    _frame: object = None      # the decoder frame, for on-demand BGR conversion

    def bgr(self) -> np.ndarray:
        return self._frame.to_ndarray(format="bgr24")

    def sha256(self) -> str:
        """sha256 of the decoded BGR bytes (the ``input_image_sha256`` of the observation schema)."""
        return hashlib.sha256(np.ascontiguousarray(self.bgr()).tobytes()).hexdigest()


class DecodeError(RuntimeError):
    pass


def iter_clip(path: str, *, clip_start_s: float, clip_end_s: float | None, fps: float | None,
              frame_count: int | None = None) -> Iterator[DecodedFrame]:
    import av

    try:
        container = av.open(str(path))
    except Exception as exc:  # noqa: BLE001 - any container failure is a decode failure
        raise DecodeError(f"cannot open {path}: {exc}") from exc
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        tb = float(stream.time_base)
        rate = float(fps or stream.average_rate or 0) or None
        half = 0.5 / rate if rate else 1e-3
        if clip_start_s > 0:
            container.seek(int(clip_start_s / tb), stream=stream, backward=True, any_frame=False)
        last = -1
        for fr in container.decode(stream):
            if fr.pts is None:
                continue
            t = fr.pts * tb - clip_start_s
            if t < -half:
                continue
            if clip_end_s is not None and t >= clip_end_s - clip_start_s - half:
                break
            idx = int(round(t * rate)) if rate else last + 1
            if idx <= last:
                continue
            last = idx
            yield DecodedFrame(idx, float(t), fr.to_ndarray(format="gray"), fr)
            if frame_count is not None and idx >= frame_count - 1:
                break
    except DecodeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DecodeError(f"decode failed in {path}: {exc}") from exc
    finally:
        container.close()
