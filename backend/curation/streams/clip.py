"""An mcap episode's camera topics as mp4, in memory.

A browser cannot play an mcap file: its cameras are compressed-image messages inside
zstd chunks. The reader already turns each camera topic into an mp4 for the checks
(``ingest.mcap_reader._materialize_videos``, no re-encoding); this does the very same
muxing into memory, so the Daemon can hand the console a playable video read straight
from the source - nothing written to disk, nothing uploaded.

The bytes are the reader's bytes: same muxers, same timeline (zero = the earlier of the
camera's first frame and the action anchor), same fps - so what the console shows is
exactly what the checks looked at.
"""
from __future__ import annotations

import io
import re

import numpy as np

from .objects import McapObjects

#: the reader's rule for a camera's file name (``ingest.mcap_reader._video_path``)
_UNSAFE = re.compile(r"[^0-9A-Za-z_-]")


def safe_name(camera: str) -> str:
    return _UNSAFE.sub("_", camera)


def episode_clips(objs: McapObjects, name: str, mapping: dict | None = None) -> dict[str, bytes]:
    """``{camera: mp4 bytes}`` of one episode file, every camera in one pass over it."""
    from ..ingest import mcap_reader as MR

    path = objs.object_uri(name)
    open_fn = lambda: objs.open(name)  # noqa: E731 - the reader's opener shape
    mp = MR._effective_mapping(path, mapping, open_fn=open_fn)
    scan = MR._scan_mcap(path, mp, open_fn=open_fn)
    if not scan["video"]:
        return {}

    # The reader's time axis (``_payload``): zero at the first action message, fps from
    # the action rate; without an action topic the first camera frame is the anchor and
    # the frames keep their own spacing at a nominal rate.
    anchor = sorted(scan["action"][0], key=lambda t: t[0]) if scan["action"] and scan["action"][0] else []
    if len(anchor) > 1:
        t0_ns = anchor[0][0]
        t_ns = np.asarray([t for t, _ in anchor], dtype=np.int64)
        span = float((t_ns[-1] - t0_ns) / MR._NS)
        fps = round((len(anchor) - 1) / span, 6) if span > 0 else 30.0
    else:
        t0_ns = min(items[0][0] for items in scan["video"].values() if items)
        fps = 30.0

    out: dict[str, bytes] = {}
    for cam in sorted(scan["video"]):
        items = sorted(scan["video"][cam], key=lambda t: t[0])
        if not items:
            continue
        base_ns = min(items[0][0], t0_ns)
        times = [(t - base_ns) / MR._NS for t, _, _ in items]
        blobs = [b for _, _, b in items]
        codecs = {MR._codec_of(fmt, b) for _, fmt, b in items}
        sink = io.BytesIO()
        if codecs == {"jpeg"}:
            MR.mux_jpeg_frames(blobs, times, sink, fps)
        elif codecs == {"h264"}:
            MR._mux_annexb(blobs, times, sink, fps)
        else:
            raise MR.NotADatasetError(
                f"{path} {cam}: 压缩帧编码认成 {sorted(codecs)},当前只支持 JPEG 与 H.264 Annex-B")
        out[cam] = sink.getvalue()
    return out
