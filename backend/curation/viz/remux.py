"""H.264 / H.265 Annex-B samples of an mcap camera -> fragmented mp4, without re-encoding (D60).

A browser plays H.264 (and, where the platform decodes it, H.265) from an mp4, not from the bare
Annex-B access units a ``foxglove.CompressedVideo`` message carries. The episode pass appends the
samples to a file on disk as it reads them; FFmpeg's raw demuxer parses that file and the packets go
into a fragmented mp4 (``frag_keyframe+empty_moov``) one at a time, stamped with the messages' own
times - memory stays at one packet however long the episode (an ABC-130k episode is four 120 MB
streams). H.265 is tagged ``hvc1``, the tag Safari plays.

Packets before the first keyframe are left out: a recording that starts mid-GOP (GenRobot: about a
second of P-frames) cannot decode them, and an mp4 whose first sample is not a sync sample is one a
browser may refuse. The mp4 then starts at the first keyframe, ``lead_s`` later than the first sample.

Packet times are presentation times in decode order, which is right for streams without B-frames
(the robot cameras of the sample set: I and P frames only). A stream whose parser reports frame
reordering is still remuxed and comes back with ``b_frames`` set, so the episode can say so.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from fractions import Fraction

#: the mp4 time base (the usual 90 kHz video clock)
TIME_BASE = 90000


@dataclass
class Remuxed:
    packets: int                  # in the mp4
    skipped: int                  # before the first keyframe, left out
    lead_s: float                 # the mp4's start after the first sample
    b_frames: bool
    width: int | None
    height: int | None


def _stamps(times_s: list[float], count: int, fps: float) -> list[int]:
    """The packets' times: the samples' own when the parser found one packet per sample, else spread
    evenly over the same span."""
    if count == len(times_s) and count:
        return [round(t * TIME_BASE) for t in times_s]
    span = (times_s[-1] - times_s[0]) if len(times_s) > 1 else count / fps
    step = span / max(1, count - 1) if count > 1 else 1.0 / fps
    start = times_s[0] if times_s else 0.0
    return [round((start + i * step) * TIME_BASE) for i in range(count)]


def _open(path, fmt: str):
    import av

    if hasattr(path, "seek"):                          # a stream (the sample pack's tail): from its start
        path.seek(0)
    return av.open(path, format=fmt)


def remux_annexb_file(path, times_s: list[float], codec: str, out_path: str, *, fps: float = 30.0) -> Remuxed:
    """Write the fMP4 of the Annex-B file ``path`` (or a seekable stream of it). ``times_s`` are the
    samples' times from the camera's first frame (the mp4 starts at 0; the episode answer places it
    with ``offset_s``)."""
    import av

    fmt = "hevc" if codec == "h265" else "h264"
    count = 0
    first_key = None
    with _open(path, fmt) as src:                       # first pass: the access units the parser finds
        vs = src.streams.video[0]
        for p in src.demux(vs):
            if p.size:
                if first_key is None and p.is_keyframe:
                    first_key = count
                count += 1
        cc = vs.codec_context
        b_frames, width, height = bool(cc.has_b_frames), (cc.width or None), (cc.height or None)
    skip = first_key or 0                                # no keyframe flag at all: keep everything
    stamps = _stamps(times_s, count, fps)
    base = stamps[skip] if skip < len(stamps) else 0
    tmp = out_path + ".part"
    with _open(path, fmt) as src:
        vs = src.streams.video[0]
        with av.open(tmp, "w", format="mp4",
                     options={"movflags": "frag_keyframe+empty_moov+default_base_moof"}) as out:
            ost = out.add_stream_from_template(vs, opaque=True)      # a stream copy: never open an encoder
            if codec == "h265":
                ost.codec_tag = "hvc1"
            ost.time_base = Fraction(1, TIME_BASE)
            last = -1
            i = 0
            for p in src.demux(vs):
                if not p.size or i >= len(stamps):
                    continue
                i += 1
                if i <= skip:
                    continue
                ts = max(stamps[i - 1] - base, last + 1)
                last = ts
                p.stream = ost
                p.pts = p.dts = ts
                p.time_base = ost.time_base
                out.mux(p)
    os.replace(tmp, out_path)
    return Remuxed(count - skip, skip, round(base / TIME_BASE, 6), b_frames, width, height)
