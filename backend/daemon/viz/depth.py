"""Depth streams of LeRobot and Lance datasets: their frame packs (design doc 21 §5.3, D68).

An episode's depth column (``uint16 [H, W]`` millimetres, or ``float32`` metres) is read a batch of rows
at a time - an episode of 640×480 pictures is hundreds of MB - turned into 16-bit PNGs on a few threads
(:func:`curation.viz.depth.encode_png16`), and appended to ``depth/<digest>/ep<N>/<stream>.frames`` with
its index (``.json``: times, offsets, sizes, the 2 % / 98 % range). The first request starts it on the
Daemon's build pool and answers 202 with the progress; later ones read the cache.
"""
from __future__ import annotations

import concurrent.futures as cf
from typing import Callable, Iterable

from curation.viz import depth as D

#: depth pictures encoded at once (zlib lets go of the GIL)
ENCODE_THREADS = 4
#: rows of a depth column read at once
BATCH = 32


def stream_doc(key: str, feature: str, entry: dict, fps: float | None, cameras: dict[str, str]) -> dict:
    """C4 ``VizStream`` of a depth feature."""
    shape = D.depth_shape(entry) or (None, None)
    return {"key": key, "kind": "depth", "name": feature, "unit": "mm", "lines": [], "smart": False,
            "available": True, "reason": None, "sources": [feature], "rate_hz": fps,
            "depth": {"width": shape[1], "height": shape[0], "unit": "mm",
                      "pair_camera": D.pair_camera(feature, cameras)}}


def build_pack(out, key: str, batches: Iterable, shape: tuple[int, int], unit: str, times: list[float],
               total: int, progress: Callable[[float], None]) -> dict:
    """Write the pack of an episode's depth column to ``out``; returns its index (C4 ``VizFrameIndex``)."""
    writer = D.PackWriter(out)
    every = max(1, total // D.RANGE_SAMPLES)
    samples = []
    done = 0
    try:
        with cf.ThreadPoolExecutor(ENCODE_THREADS, thread_name_prefix="viz-depth") as pool:
            for col in batches:
                frames = [None if f is None else D.to_mm(f, unit) for f in D.batch_frames(col, shape)]
                pngs = list(pool.map(lambda a: b"" if a is None else D.encode_png16(a), frames))
                for a, png in zip(frames, pngs):
                    if a is not None and done % every == 0:
                        samples.append(a)
                    writer.add(png)
                    done += 1
                progress(min(0.99, done / max(1, total)))
        writer.finish()
    except BaseException:
        writer.abort()
        raise
    lo, hi = D.value_range(samples)
    return D.index_doc(key, times, writer, shape[1], shape[0], lo, hi)
