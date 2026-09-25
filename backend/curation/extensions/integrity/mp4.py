"""The structure of an mp4 file without decoding it (design doc 14 §3.2).

Two reads: the top-level boxes (a 16-byte header per box, seeking past the payloads)
and the ``moov`` box whole. From the video track's sample tables (``stts``, ``stsz``,
``stsc``, ``stco`` / ``co64``) every sample's decode time, byte offset and size follow,
so a truncation or a zeroed block found at a byte offset maps to a time - which
LeRobot v3 needs, where one file holds several episodes one after the other.

Only what LeRobot and v1's mcap muxing write is handled: one video track, 32- or 64-bit
chunk offsets, no fragments (``moof``). Anything else is reported, never guessed.
"""
from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass, field
from typing import Callable

ReadRange = Callable[[int, int], bytes]

#: ftyp + a moov with one video track cannot be smaller (design doc 14 §1)
MIN_BYTES = 512
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf"}


class Mp4Error(Exception):
    """The file is not an mp4 this module can read; the message says where."""


@dataclass
class Box:
    type: bytes
    offset: int
    size: int                     # header included
    header: int                   # 8 or 16

    @property
    def end(self) -> int:
        return self.offset + self.size

    @property
    def payload(self) -> int:
        return self.offset + self.header


@dataclass
class Mp4Layout:
    """The top level of the file. ``truncated_at``: the offset of the first box that runs
    past the end of the file (None: every box fits)."""

    size: int
    boxes: list[Box] = field(default_factory=list)
    truncated_at: int | None = None
    problem: str = ""             # a header that is not a box: the walk stopped there

    def first(self, kind: bytes) -> Box | None:
        return next((b for b in self.boxes if b.type == kind), None)

    def mdat(self) -> list[Box]:
        return [b for b in self.boxes if b.type == b"mdat"]


@dataclass
class Samples:
    """The video track's samples in decode order."""

    timescale: int
    times: list[float]            # decode time of each sample, seconds
    offsets: list[int]            # byte offset of each sample
    sizes: list[int]
    duration_s: float

    def __len__(self) -> int:
        return len(self.times)

    def at_offset(self, offset: int) -> int | None:
        """Index of the sample whose bytes contain ``offset`` (else the one just before it)."""
        if not hasattr(self, "_order"):
            self._order = sorted(range(len(self.offsets)), key=self.offsets.__getitem__)
            self._sorted = [self.offsets[k] for k in self._order]
        i = bisect.bisect_right(self._sorted, offset) - 1
        return self._order[i] if i >= 0 else None

    def first_missing(self, size: int) -> int | None:
        """The first sample (in decode order) whose bytes are not all inside the file."""
        for i, (off, n) in enumerate(zip(self.offsets, self.sizes)):
            if off + n > size:
                return i
        return None

    def count_in(self, start_s: float, end_s: float, eps: float) -> int:
        """Samples with a decode time in ``[start_s, end_s)`` (``eps`` absorbs rounding)."""
        lo = bisect.bisect_left(self.times, start_s - eps)
        hi = bisect.bisect_left(self.times, end_s - eps)
        return max(0, hi - lo)


def walk(read_range: ReadRange, size: int, *, limit: int = 4096) -> Mp4Layout:
    """The top-level boxes; stops at the first box that runs past the end."""
    out = Mp4Layout(size)
    pos = 0
    while pos < size and len(out.boxes) < limit:
        if size - pos < 8:
            out.truncated_at = pos
            break
        hdr = read_range(pos, min(16, size - pos))
        box_size = int.from_bytes(hdr[:4], "big")
        kind = hdr[4:8]
        if not all(0x20 <= b < 0x7F for b in kind):
            out.problem = f"offset {pos}: {hdr[:8].hex()} is not an mp4 box header"
            break
        header = 8
        if box_size == 1:
            if len(hdr) < 16:
                out.truncated_at = pos
                break
            box_size, header = int.from_bytes(hdr[8:16], "big"), 16
        elif box_size == 0:                    # runs to the end of the file
            box_size = size - pos
        if box_size < header:
            out.problem = f"offset {pos}: box {kind.decode('latin-1')} claims {box_size} bytes"
            break
        out.boxes.append(Box(kind, pos, box_size, header))
        if pos + box_size > size:
            out.truncated_at = pos
            break
        pos += box_size
    return out


# ---------------------------------------------------------------- moov


def _children(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            size, header = int.from_bytes(data[pos + 8:pos + 16], "big"), 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise Mp4Error(f"box {kind!r} at {pos} of the moov does not fit")
        yield kind, pos + header, pos + size
        pos += size


def _find(data: bytes, start: int, end: int, path: list[bytes]):
    """Every box at ``path`` below ``[start, end)`` as (payload start, end)."""
    if not path:
        yield start, end
        return
    for kind, s, e in _children(data, start, end):
        if kind == path[0]:
            yield from _find(data, s, e, path[1:])


def _full(data: bytes, s: int) -> tuple[int, int]:
    """(version, payload after the full-box header)."""
    return data[s], s + 4


def _video_track(moov: bytes) -> tuple[int, int]:
    for s, e in _find(moov, 0, len(moov), [b"trak"]):
        for hs, _he in _find(moov, s, e, [b"mdia", b"hdlr"]):
            _v, p = _full(moov, hs)
            if moov[p + 4:p + 8] == b"vide":
                return s, e
    raise Mp4Error("the moov has no video track")


def samples(moov: bytes) -> Samples:
    """The video track's sample table; ``moov`` is the box's payload."""
    s, e = _video_track(moov)
    mdhd = next(_find(moov, s, e, [b"mdia", b"mdhd"]), None)
    stbl = next(_find(moov, s, e, [b"mdia", b"minf", b"stbl"]), None)
    if mdhd is None or stbl is None:
        raise Mp4Error("the video track has no mdhd or stbl")
    version, p = _full(moov, mdhd[0])
    if version == 1:
        timescale, duration = struct.unpack_from(">IQ", moov, p + 16)
    else:
        timescale, duration = struct.unpack_from(">II", moov, p + 8)
    if not timescale:
        raise Mp4Error("the video track's timescale is 0")
    boxes = {kind: (bs, be) for kind, bs, be in _children(moov, *stbl)}

    def table(kind: bytes) -> tuple[int, int]:
        if kind not in boxes:
            raise Mp4Error(f"the video track has no {kind.decode()}")
        _v, q = _full(moov, boxes[kind][0])
        return q, boxes[kind][1]

    # stts: decode deltas
    q, _ = table(b"stts")
    (n,) = struct.unpack_from(">I", moov, q)
    times: list[float] = []
    t = 0
    for i in range(n):
        count, delta = struct.unpack_from(">II", moov, q + 4 + 8 * i)
        for _ in range(count):
            times.append(t / timescale)
            t += delta
    # stsz: sizes
    q, _ = table(b"stsz")
    fixed, count = struct.unpack_from(">II", moov, q)
    sizes = [fixed] * count if fixed else list(struct.unpack_from(f">{count}I", moov, q + 8))
    # chunk offsets
    if b"stco" in boxes:
        q, _ = table(b"stco")
        (nc,) = struct.unpack_from(">I", moov, q)
        chunk_offsets = list(struct.unpack_from(f">{nc}I", moov, q + 4))
    else:
        q, _ = table(b"co64")
        (nc,) = struct.unpack_from(">I", moov, q)
        chunk_offsets = list(struct.unpack_from(f">{nc}Q", moov, q + 4))
    # stsc: samples per chunk, run-length coded by first chunk (1-based)
    q, _ = table(b"stsc")
    (ns,) = struct.unpack_from(">I", moov, q)
    runs = [struct.unpack_from(">III", moov, q + 4 + 12 * i)[:2] for i in range(ns)]
    offsets: list[int] = []
    for r, (first, per_chunk) in enumerate(runs):
        last = runs[r + 1][0] - 1 if r + 1 < len(runs) else len(chunk_offsets)
        for chunk in range(first, last + 1):
            if chunk - 1 >= len(chunk_offsets):
                break
            pos = chunk_offsets[chunk - 1]
            for _ in range(per_chunk):
                k = len(offsets)
                if k >= len(sizes):
                    break
                offsets.append(pos)
                pos += sizes[k]
    if not (len(times) == len(sizes) == len(offsets)):
        raise Mp4Error(f"the sample tables disagree: {len(times)} times, {len(sizes)} sizes, "
                       f"{len(offsets)} offsets")
    return Samples(timescale, times, offsets, sizes, duration / timescale)


def read_moov(read_range: ReadRange, layout: Mp4Layout) -> bytes | None:
    box = layout.first(b"moov")
    if box is None or box.end > layout.size:
        return None
    return read_range(box.payload, box.end - box.payload)
