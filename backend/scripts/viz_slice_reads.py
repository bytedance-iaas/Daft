"""How much the visualizer reads to cut one episode out of a LeRobot v3 camera file (design doc 21 §4).

A v3 camera file holds many episodes; a transcode (and, with ``CURATOR_VIZ_SEGMENT=1``, the player)
takes the episode's own slice, cut at GOP boundaries from a ranged view of the object: the ``moov``
and the episode's bytes, not the whole file. This reads a dataset's episode table, cuts the asked
episodes of one camera the way the Daemon does (``curation.viz.segment.cut`` over a ``RangeFile``)
and says, per episode, the file's size, the bytes and reads it took, the slice and the time::

    ../.venv/bin/python scripts/viz_slice_reads.py tos://bucket/lerobot/so101_depth --region cn-beijing
    ../.venv/bin/python scripts/viz_slice_reads.py /data/datasets/viz_v3 --camera observation.images.top --episodes 0,1,2

TOS keys come from the environment, as for the CLI's input role (``curation.cli.creds``); nothing is
written but the slices, in a temporary directory removed at the end.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from curation.cli.storage import open_storage  # noqa: E402
from curation.streams.rangefile import RangeFile  # noqa: E402
from curation.viz.segment import CUT_BLOCK, cut  # noqa: E402


def _episodes(st, info: dict, camera: str) -> list[dict]:
    """Each episode's file, start and end in that camera's files (meta/episodes/*.parquet)."""
    import pandas as pd

    rows = []
    keys = sorted(k for k in st.list("meta/episodes/") if k.endswith(".parquet"))
    for key in keys:
        df = pd.read_parquet(io.BytesIO(st.read_bytes(key)))
        rows += df.to_dict("records")
    tpl = info.get("video_path") or "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"
    out = []
    for r in rows:
        chunk, file = r.get(f"videos/{camera}/chunk_index"), r.get(f"videos/{camera}/file_index")
        if chunk is None or file is None:
            continue
        out.append({"episode": int(r["episode_index"]),
                    "file": tpl.format(video_key=camera, chunk_index=int(chunk), file_index=int(file)),
                    "from": float(r[f"videos/{camera}/from_timestamp"]), "to": float(r[f"videos/{camera}/to_timestamp"])})
    return sorted(out, key=lambda e: e["episode"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("uri", help="a LeRobot v3 dataset: tos://bucket/prefix or a local directory")
    ap.add_argument("--camera", help="the video feature (default: the first)")
    ap.add_argument("--episodes", help="comma-separated indexes (default: the first, the middle and the last)")
    ap.add_argument("--region", default=None)
    args = ap.parse_args(argv)

    st = open_storage(args.uri, role="input", region=args.region)
    info = json.loads(st.read_bytes("meta/info.json"))
    if not str(info.get("codebase_version") or "").startswith("v3"):
        sys.stderr.write("not a LeRobot v3 dataset: its camera files hold one episode each, nothing to slice\n")
        return 2
    cameras = [k for k, f in (info.get("features") or {}).items() if isinstance(f, dict) and f.get("dtype") == "video"]
    camera = args.camera or (cameras[0] if cameras else None)
    if camera not in cameras:
        sys.stderr.write(f"no camera {camera!r}; the dataset has {cameras}\n")
        return 2
    eps = _episodes(st, info, camera)
    by_index = {e["episode"]: e for e in eps}
    if args.episodes:
        picked = [by_index[int(i)] for i in args.episodes.split(",") if int(i) in by_index]
    else:
        picked = list({e["episode"]: e for e in (eps[0], eps[len(eps) // 2], eps[-1])}.values())

    print(f"{args.uri} · {camera} · {len(eps)} episodes")
    print(f"{'ep':>4} {'file':<44} {'file MB':>8} {'window s':>14} {'read MB':>8} {'read %':>7} {'reads':>6} {'slice MB':>9} {'s':>6}")
    with tempfile.TemporaryDirectory() as tmp:
        for e in picked:
            size = st.stat(e["file"]).size
            stats = {"bytes": 0, "reads": 0}

            def read(start: int, n: int, rel: str = e["file"]) -> bytes:
                data = st.read_range(rel, start, n)
                stats["bytes"] += len(data)
                stats["reads"] += 1
                return data

            t0 = time.monotonic()
            # read as the Daemon reads it (daemon/viz/segments.py)
            seg = cut(RangeFile(read, size, name=e["file"], block=CUT_BLOCK), os.path.join(tmp, f"ep{e['episode']}.mp4"), e["from"], e["to"])
            dt = time.monotonic() - t0
            print(f"{e['episode']:>4} {e['file']:<44} {size / 2**20:>8.1f} {e['from']:>6.1f}–{e['to']:<7.1f} "
                  f"{stats['bytes'] / 2**20:>8.2f} {100 * stats['bytes'] / size:>6.1f}% {stats['reads']:>6} "
                  f"{seg.bytes / 2**20:>9.2f} {dt:>6.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
