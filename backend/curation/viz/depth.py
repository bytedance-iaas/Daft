"""Depth pictures as frame packs of 16-bit PNGs (design doc 21 §5, D68).

A depth frame pack is a JPEG frame pack's twin (D60): the frames' bytes back to back, read by Range, with
an index of episode times, offsets and sizes. Every frame is a standard 16-bit greyscale PNG
(big-endian samples, millimetres unless the index says otherwise, 0 a hole), so any frame cut out of
the pack opens in an image viewer. A recording that already keeps 16-bit greyscale PNGs (RoboMIND's
``foxglove.CompressedImage``) is passed through untouched; anything else is encoded here - numpy does
PNG's Sub filter and zlib (level 3) the rest: about 87 KB and 10 ms for a 640×480 frame on one core
(PIL's default compression: 75 KB but 33 ms), and zlib lets go of the GIL, so a thread pool scales.

The browser decodes the PNGs itself (``frontend/src/lib/vizDepth.ts``: its own inflate via
``DecompressionStream`` and PNG's five row filters), because ``createImageBitmap`` would bring them
down to 8 bits.
"""
from __future__ import annotations

import io
import json
import os
import re
import struct
import zlib
from typing import Any, Iterable

import numpy as np

PNG_SIG = b"\x89PNG\r\n\x1a\n"
#: frames sampled to find a pack's 2 % / 98 % range (every k-th, about this many)
RANGE_SAMPLES = 30


# ---------------------------------------------------------------- PNG

def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_png16(a: np.ndarray, level: int = 3) -> bytes:
    """A 16-bit greyscale PNG of ``a`` (``uint16 [H, W]``): every row Sub-filtered, zlib at ``level``."""
    a = np.ascontiguousarray(a, dtype=">u2")
    h, w = a.shape
    b = a.view(np.uint8).reshape(h, w * 2)
    raw = np.empty((h, w * 2 + 1), np.uint8)
    raw[:, 0] = 1                                         # filter type 1: Sub (bytes per pixel: 2)
    raw[:, 1:3] = b[:, :2]
    raw[:, 3:] = b[:, 2:] - b[:, :-2]                     # uint8 arithmetic wraps, as PNG's does
    ihdr = struct.pack(">IIBBBBB", w, h, 16, 0, 0, 0, 0)
    return PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw.tobytes(), level)) + _chunk(b"IEND", b"")


def png_header(data: bytes) -> dict | None:
    """``{width, height, bit_depth, color_type, interlace}`` of a PNG, or None."""
    if len(data) < 33 or not data.startswith(PNG_SIG) or data[12:16] != b"IHDR":
        return None
    w, h, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", data[16:29])
    return {"width": w, "height": h, "bit_depth": depth, "color_type": color, "interlace": interlace}


def is_png16(data: bytes) -> bool:
    """A 16-bit greyscale, not interlaced PNG: what a depth pack keeps as it is."""
    hd = png_header(data)
    return bool(hd) and hd["bit_depth"] == 16 and hd["color_type"] == 0 and hd["interlace"] == 0


def decode_png(data: bytes) -> np.ndarray:
    """A PNG's pixels (16-bit greyscale as ``uint16``)."""
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    a = np.asarray(im)
    if a.dtype != np.uint16 and im.mode in ("I;16", "I;16B", "I"):
        a = a.astype(np.uint16)
    return a


# ---------------------------------------------------------------- values

def to_mm(frame: np.ndarray, unit: str = "mm") -> np.ndarray:
    """A depth picture as ``uint16`` millimetres: metres (floats, or ``unit`` m) times 1000; NaN, inf and
    what does not fit become 0 (a hole)."""
    a = np.asarray(frame)
    if a.ndim == 3 and a.shape[-1] == 1:
        a = a[..., 0]
    if a.dtype == np.uint16 and unit == "mm":
        return a
    f = a.astype(np.float64)
    if unit == "m" or np.issubdtype(a.dtype, np.floating):
        f = f * 1000.0
    f = np.where(np.isfinite(f) & (f > 0) & (f < 65535.5), f, 0.0)
    return np.rint(f).astype(np.uint16)


def value_range(samples: Iterable[np.ndarray]) -> tuple[int | None, int | None]:
    """The 2 % and 98 % values of the samples' valid (non-zero) pixels."""
    vals = [s[s > 0].ravel() for s in samples if s is not None and s.size]
    vals = [v for v in vals if v.size]
    if not vals:
        return None, None
    v = np.concatenate(vals)
    if v.size > 2_000_000:
        v = v[:: v.size // 2_000_000 + 1]
    lo, hi = np.percentile(v, [2, 98])
    return int(lo), int(max(hi, lo + 1))


# ---------------------------------------------------------------- columns

def _is_list(t) -> bool:
    import pyarrow as pa

    return pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_fixed_size_list(t)


def batch_frames(col, shape: tuple[int, int]) -> list[np.ndarray | None]:
    """The pictures of a batch of a depth column (``list<list<uint16>>``, fixed-size lists, ``[H, W, 1]``):
    one array a row, None for a missing one."""
    h, w = shape
    n = len(col)
    if hasattr(col, "combine_chunks"):
        col = col.combine_chunks()
    if col.null_count == 0:
        flat = col
        while _is_list(flat.type):
            flat = flat.flatten()
        vals = flat.to_numpy(zero_copy_only=False)
        if vals.size == n * h * w:
            return list(vals.reshape(n, h, w))
    out: list[np.ndarray | None] = []
    for i in range(n):
        v = col[i].as_py()
        if v is None:
            out.append(None)
            continue
        a = np.asarray(v)
        out.append(a.reshape(h, w) if a.size == h * w else None)
    return out


# ---------------------------------------------------------------- features

#: dtypes a depth picture is stored in
DEPTH_DTYPES = frozenset({"uint16", "uint32", "float32", "float64"})


def depth_shape(feature: dict) -> tuple[int, int] | None:
    """(H, W) of a depth feature (``[H, W]`` or ``[H, W, 1]``)."""
    shape = feature.get("shape")
    if not isinstance(shape, (list, tuple)) or not all(isinstance(x, int) for x in shape):
        return None
    if len(shape) == 3 and shape[-1] == 1:
        shape = shape[:2]
    return (int(shape[0]), int(shape[1])) if len(shape) == 2 else None


def depth_unit(feature: dict) -> str:
    """The unit the column's numbers are in: floats are metres, integers millimetres."""
    return "m" if str(feature.get("dtype")) in ("float32", "float64") else "mm"


def _short(key: str) -> str:
    """A camera or depth feature's short name: ``observation.images.front.depth`` -> ``front``,
    ``observation.depths.camera_front`` -> ``camera_front``."""
    k = key
    for prefix in ("observation.images.", "observation.image.", "observation.depths.", "observation.depth."):
        if k.startswith(prefix):
            k = k[len(prefix):]
            break
    k = re.sub(r"[._-]depths?$", "", k, flags=re.I)
    k = re.sub(r"^depths?[._-]", "", k, flags=re.I)
    return k


def pair_camera(depth_key: str, cameras: dict[str, str]) -> str | None:
    """The camera (``{camera key: feature key}``) a depth feature belongs to, when exactly one matches:
    the feature without its ``depth`` word (``observation.images.front.depth`` -> ``observation.images.front``),
    ``depths`` turned into ``images`` (``observation.depths.camera_front`` -> ``observation.images.camera_front``),
    else the same short name."""
    candidates = {re.sub(r"[._-]depths?$", "", depth_key, flags=re.I),
                  re.sub(r"(^|\.)depths?\.", r"\1images.", depth_key, count=1, flags=re.I)}
    hits = [k for k, f in cameras.items() if f in candidates and f != depth_key]
    if len(hits) == 1:
        return hits[0]
    short = _short(depth_key)
    hits = [k for k, f in cameras.items() if _short(f) == short and f != depth_key]
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------- the pack

class PackWriter:
    """Frames appended to ``<path>.part``; :meth:`finish` renames it onto ``path`` and returns the index."""

    def __init__(self, path: str | os.PathLike):
        self.path = str(path)
        self.fh = open(self.path + ".part", "wb")
        self.offset: list[int] = []
        self.size: list[int] = []
        self.pos = 0

    def add(self, data: bytes) -> None:
        self.offset.append(self.pos)
        self.size.append(len(data))
        self.fh.write(data)
        self.pos += len(data)

    def finish(self) -> None:
        self.fh.close()
        os.replace(self.path + ".part", self.path)

    def abort(self) -> None:
        self.fh.close()
        try:
            os.unlink(self.path + ".part")
        except OSError:
            pass


def index_doc(key: str, times: list[float], writer: PackWriter, width: int | None, height: int | None,
              lo: int | None, hi: int | None, unit: str = "mm") -> dict[str, Any]:
    """C4 ``VizFrameIndex`` of a depth pack (``codec: png16`` and its ``depth`` range)."""
    n = min(len(times), len(writer.size))
    return {"camera": key, "codec": "png16", "width": width, "height": height, "count": n,
            "t": [round(float(x), 6) for x in times[:n]], "offset": writer.offset[:n], "size": writer.size[:n],
            "bytes": writer.pos, "depth": {"unit": unit, "scale": 1.0, "invalid": 0, "lo": lo, "hi": hi}}


def write_index(path: str | os.PathLike, doc: dict) -> None:
    tmp = str(path) + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, str(path))
