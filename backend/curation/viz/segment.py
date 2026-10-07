"""One episode cut out of an mp4 that holds several, at GOP boundaries and without re-encoding (design
doc 21 §4, D69).

LeRobot v3 (and a Lance blob of it) keeps many episodes back to back in one mp4 of up to 500 MB, the
episode table giving each one's ``from_timestamp`` / ``to_timestamp``. :func:`cut` opens the source with
PyAV - a local file, or a :class:`~curation.streams.rangefile.RangeFile` over a TOS object or a blob, so
only the ``moov`` and the episode's own bytes are read - seeks to the keyframe at or before ``start``,
and copies the packets in decode order up to the first keyframe at or after ``end`` (a cut at a GOP
boundary always decodes). Times are moved so the slice's 0 is its first packet's decode time,
``start_s`` in the source; the player places the episode at ``from - start_s`` in it. The output is a
plain mp4 with its ``moov`` in front (``faststart``: written to the local disk first), so a browser seeks
in it without scanning fragments.

The same copy of a whole file (``start`` and ``end`` None) turns an mp4 whose ``moov`` sits at its end
into one with it in front (:func:`moov_at_end` tells which); LeRobot v2's encoder writes them so.

An open GOP (B-frames before a non-IDR I-frame) can lose its last frame or two before ``end``: the
B-frames that show before the keyframe the cut stops at come after it in decode order. Robot recordings
are closed GOPs (LeRobot's AV1 and H.264 encoders), and ``b_frames`` says when a stream reorders at all.

``python -m curation.viz.segment --in SRC --out DST [--from S] [--to E]`` is the same as a command.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from dataclasses import asdict, dataclass
from typing import Callable

#: one slack in seconds for "a keyframe at or after end" (frame times are rounded in the episode table)
EPS = 1e-6


@dataclass
class Segment:
    start_s: float               # the source time of the slice's 0 (its first packet's decode time)
    end_s: float | None          # where the slice stops in the source (a keyframe's time), None = its end
    packets: int
    bytes: int
    b_frames: bool               # some packet shows at another time than it decodes (reordering)

    def as_dict(self) -> dict:
        return asdict(self)


def _stream_of(inp):
    streams = [s for s in inp.streams if s.type == "video"]
    if not streams:
        raise ValueError("no video stream")
    return streams[0]


def keyframe_before(src, t: float | None) -> float:
    """The source time a cut for ``t`` starts at: the decode time of the keyframe at or before ``t`` (what
    the player's ``from_ts`` in a slice is measured from). Reads the ``moov`` and one packet."""
    import av

    if hasattr(src, "seek"):
        src.seek(0)
    with av.open(src) as inp:
        vs = _stream_of(inp)
        tb = vs.time_base
        if t is not None and t > 0:
            inp.seek(int(t / tb), stream=vs, backward=True, any_frame=False)
        for p in inp.demux(vs):
            if not p.size or (p.dts is None and p.pts is None):
                continue
            if not p.is_keyframe:
                continue
            return round(float((p.dts if p.dts is not None else p.pts) * tb), 6)
    return 0.0


def cut(src, out_path: str, start: float | None = None, end: float | None = None) -> Segment:
    """Write the slice of ``src`` covering [start, end) to ``out_path`` (via ``out_path.part``)."""
    import av

    if hasattr(src, "seek"):
        src.seek(0)
    tmp = out_path + ".part"
    base = None
    n = size = 0
    reorders = False
    end_s = None
    with av.open(src) as inp:
        vs = _stream_of(inp)
        tb = vs.time_base
        if start is not None and start > 0:
            inp.seek(int(start / tb), stream=vs, backward=True, any_frame=False)
        with av.open(tmp, "w", format="mp4", options={"movflags": "faststart"}) as out:
            ost = out.add_stream_from_template(vs, opaque=True)      # a stream copy: never an encoder
            ost.time_base = tb
            for p in inp.demux(vs):
                if not p.size or (p.dts is None and p.pts is None):
                    continue
                d = p.dts if p.dts is not None else p.pts
                if base is None:
                    if not p.is_keyframe:          # a stream that starts mid-GOP: from its first keyframe
                        continue
                    base = d
                if end is not None and n and p.is_keyframe and p.pts is not None and float(p.pts * tb) >= end - EPS:
                    end_s = round(float(d * tb), 6)
                    break
                if p.pts is not None and p.dts is not None and p.pts != p.dts:
                    reorders = True
                p.stream = ost
                p.time_base = tb
                p.pts = None if p.pts is None else p.pts - base
                p.dts = None if p.dts is None else p.dts - base
                out.mux(p)
                n += 1
                size += p.size
        if base is None:
            raise ValueError("no keyframe in the requested range")
    os.replace(tmp, out_path)
    return Segment(round(float(base * tb), 6), end_s, n, size, reorders)


def moov_at_end(read: Callable[[int, int], bytes], size: int) -> bool:
    """Whether an mp4's ``moov`` comes after its ``mdat`` (a browser then needs a second request before
    the first frame). Reads the top-level box headers only."""
    pos = 0
    seen_mdat = False
    while pos + 8 <= size:
        head = read(pos, 16)
        if len(head) < 8:
            return False
        n, kind = struct.unpack(">I4s", head[:8])
        if n == 1 and len(head) >= 16:
            n = struct.unpack(">Q", head[8:16])[0]
        elif n == 0:
            n = size - pos
        if kind == b"moov":
            return seen_mdat
        if kind == b"mdat":
            seen_mdat = True
        if n < 8:
            return False
        pos += n
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m curation.viz.segment", description=__doc__.split("\n")[0])
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    ap.add_argument("--from", dest="start", type=float, default=None)
    ap.add_argument("--to", dest="end", type=float, default=None)
    args = ap.parse_args(argv)
    try:
        seg = cut(args.src, args.dst, args.start, args.end)
    except Exception as e:  # noqa: BLE001 - the codec library's own errors
        sys.stderr.write(json.dumps({"error": f"{type(e).__name__}: {e}"[:500]}) + "\n")
        return 1
    sys.stdout.write(json.dumps(seg.as_dict()) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
