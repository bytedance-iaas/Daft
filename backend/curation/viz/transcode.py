"""``python -m curation.viz.transcode``: an H.264 copy of a camera the browser cannot play (D60).

The Daemon runs it in a subprocess (a crash in the native codec library takes only this process)
when a camera's codec is not one a browser decodes (MPEG-4 Part 2, MJPEG ...) or when the player
says it cannot decode the original (AV1 or HEVC without a hardware decoder). Input is a local
video file - the Daemon fetches a TOS object into its cache first - optionally cut to one LeRobot v3
episode (``--from`` / ``--to`` in file seconds); output is a fragmented mp4 (H.264 High, yuv420p,
the source's frame times kept, time 0 at ``--from``) written next to ``--out`` and renamed onto it
when complete, so a reader never sees half a file.

Progress goes to stderr as JSON lines ``{"progress": 0.42}``; the exit code is 0 on success, 2 when
the input has no readable video stream, 1 otherwise (the reason on stderr as ``{"error": "..."}``).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from fractions import Fraction

#: output time base: milliseconds keep variable frame times exact enough for a player
TIME_BASE = Fraction(1, 1000)


def _say(**fields) -> None:
    sys.stderr.write(json.dumps(fields) + "\n")
    sys.stderr.flush()


def transcode(src: str, dst: str, *, start: float | None = None, end: float | None = None,
              max_side: int = 1920, crf: int = 23, preset: str = "veryfast") -> int:
    """Frames written; raises ValueError when the input has no video stream or no frame in range."""
    import av

    lo = float(start) if start is not None else 0.0
    hi = float(end) if end is not None else math.inf
    tmp = dst + ".part"
    written = 0
    with av.open(src) as inp:
        streams = [s for s in inp.streams if s.type == "video"]
        if not streams:
            raise ValueError("no video stream")
        stream = streams[0]
        stream.thread_type = "AUTO"
        duration = None
        if stream.duration is not None and stream.time_base:
            duration = float(stream.duration * stream.time_base)
        elif inp.duration:
            duration = inp.duration / 1e6
        span = (min(hi, duration) if duration else hi) - lo
        if lo > 0:
            inp.seek(int(lo / float(stream.time_base)), stream=stream, backward=True, any_frame=False)
        rate = stream.average_rate or stream.guessed_rate or Fraction(30)
        out = av.open(tmp, "w", format="mp4",
                      options={"movflags": "frag_keyframe+empty_moov+default_base_moof"})
        try:
            enc = None
            last_pts = -1
            next_report = 0.0
            for frame in inp.decode(stream):
                if frame.pts is None:
                    continue
                t = float(frame.pts * stream.time_base)
                if t < lo - 1e-6:
                    continue
                if t >= hi - 1e-6:
                    break
                if enc is None:
                    scale = min(1.0, max_side / max(frame.width, frame.height))
                    enc = out.add_stream("libx264", rate=rate)
                    enc.width = max(2, int(frame.width * scale) // 2 * 2)
                    enc.height = max(2, int(frame.height * scale) // 2 * 2)
                    enc.pix_fmt = "yuv420p"
                    enc.time_base = enc.codec_context.time_base = TIME_BASE
                    gop = max(1, int(round(float(rate) * 2)))
                    enc.options = {"crf": str(crf), "preset": preset, "g": str(gop),
                                   "profile": "high", "bf": "0", "threads": "2"}
                pts = int(round((t - lo) / float(TIME_BASE)))
                if pts <= last_pts:                     # keep timestamps strictly increasing
                    pts = last_pts + 1
                last_pts = pts
                img = frame.reformat(width=enc.width, height=enc.height, format="yuv420p")
                img.pts, img.time_base = pts, TIME_BASE
                for packet in enc.encode(img):
                    out.mux(packet)
                written += 1
                if span and math.isfinite(span) and span > 0:
                    done = min(1.0, (t - lo) / span)
                    if done >= next_report:
                        _say(progress=round(done, 3))
                        next_report = done + 0.05
            if enc is None:
                raise ValueError("no frame in the requested range")
            for packet in enc.encode():
                out.mux(packet)
        finally:
            out.close()
    os.replace(tmp, dst)
    _say(progress=1.0, frames=written)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m curation.viz.transcode", description=__doc__.split("\n")[0])
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    ap.add_argument("--from", dest="start", type=float, default=None)
    ap.add_argument("--to", dest="end", type=float, default=None)
    ap.add_argument("--max-side", type=int, default=1920)
    args = ap.parse_args(argv)
    try:
        transcode(args.src, args.dst, start=args.start, end=args.end, max_side=args.max_side)
    except ValueError as e:
        _say(error=str(e))
        return 2
    except Exception as e:  # noqa: BLE001 - the codec library's own errors; the Daemon shows the text
        _say(error=f"{type(e).__name__}: {e}"[:500])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
