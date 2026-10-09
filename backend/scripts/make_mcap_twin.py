"""An mcap twin of a LeRobot v3 dataset, for the EEF overlay on mcap (design doc 22 §0, F5.17).

Every episode becomes ``episode_<N>.mcap`` in the check reader's own convention: each camera an H.264
``foxglove.CompressedVideo`` topic ``/observation.images.<key>`` (one access unit per frame, re-encoded),
JSON ``/action`` and ``/observation.state`` with the dataset's rows, and ``/task``; every message at the
frame's own time. Each camera's stream starts ``--lead`` frames into a GOP, as GenRobot / DAS recordings
do: those first messages cannot be decoded (the player starts at the first keyframe, the overlay draws
nothing on them), yet message k is still frame k, so the EEF bundle converted with
``tools/eef_convert.py to-mcap`` points at the same pictures.

    G=~/ws/ws_general/galbot/dataset2
    ../.venv/bin/python scripts/make_mcap_twin.py $G/eef_ds2_lr3 $G/eef_ds2_mcap --lead 3
    ../.venv/bin/python ../tools/eef_convert.py to-mcap --trajectory $G/trajectory.json \\
        --out $G/trajectory_mcap.json --check $G/eef_ds2_mcap
"""
from __future__ import annotations

import argparse
import json
import os
from fractions import Fraction

import numpy as np

PREFIX = "observation.images."


def _episodes(root: str) -> tuple[dict, list[dict]]:
    import pandas as pd

    info = json.load(open(os.path.join(root, "meta", "info.json")))
    files = sorted(os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(root, "meta", "episodes"))
                   for f in fs if f.endswith(".parquet"))
    eps = pd.concat([pd.read_parquet(f) for f in files]).sort_values("episode_index")
    return info, eps.to_dict("records")


def _frames(path: str, start: float, n: int, fps: float):
    """The n pictures of a clip from ``start`` seconds of ``path``, as RGB arrays."""
    import av

    out = []
    with av.open(path) as inp:
        st = inp.streams.video[0]
        tb = float(st.time_base)
        inp.seek(int(start / tb), stream=st, backward=True, any_frame=False)
        for fr in inp.decode(st):
            t = fr.pts * tb
            if t < start - 0.5 / fps:
                continue
            out.append(fr.to_ndarray(format="rgb24"))
            if len(out) == n:
                break
    if len(out) != n:
        raise SystemExit(f"{path}: {len(out)} frames from {start:.3f} s, wanted {n}")
    return out


def _annexb(frames: list[np.ndarray], fps: float, lead: int) -> list[bytes]:
    """One H.264 access unit per frame; with ``lead``, the stream starts that many frames before its
    first keyframe (the keyframe it refers back to is left out)."""
    import av

    h, w = frames[0].shape[:2]
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = w, h, "yuv420p"
    enc.time_base = Fraction(1, int(round(fps)))
    enc.options = {"preset": "veryfast", "tune": "zerolatency", "bf": "0", "g": "30", "forced-idr": "1", "crf": "23"}
    feed = ([frames[0]] if lead else []) + frames            # a frame before the first: the dropped keyframe
    units: list[bytes] = []
    for i, img in enumerate(feed):
        f = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")
        f.pts = i
        if lead and i == lead + 1:                           # the stream's first keyframe is frame `lead`
            f.pict_type = av.video.frame.PictureType.I
        units += [bytes(p) for p in enc.encode(f)]
    units += [bytes(p) for p in enc.encode(None)]
    if len(units) != len(feed):
        raise SystemExit(f"the encoder gave {len(units)} access units for {len(feed)} frames")
    return units[1:] if lead else units


def main() -> None:
    import pandas as pd
    from foxglove_schemas_protobuf.CompressedVideo_pb2 import CompressedVideo
    from mcap_protobuf.writer import Writer

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("lerobot", help="a LeRobot v3 dataset")
    ap.add_argument("out", help="the directory the episode_<N>.mcap files go to")
    ap.add_argument("--lead", type=int, default=3, help="frames before each camera's first keyframe (0: none)")
    ap.add_argument("--episodes", default="", help="comma-separated episode indexes (default: all)")
    a = ap.parse_args()
    info, episodes = _episodes(a.lerobot)
    fps = float(info["fps"])
    cams = [k for k, f in info["features"].items() if f.get("dtype") == "video"]
    data = pd.concat([pd.read_parquet(os.path.join(dp, f)) for dp, _, fs in os.walk(os.path.join(a.lerobot, "data"))
                      for f in fs if f.endswith(".parquet")])
    wanted = {int(x) for x in a.episodes.split(",") if x.strip()}
    os.makedirs(a.out, exist_ok=True)
    t0 = 1_700_000_000_000_000_000
    for ep in episodes:
        idx = int(ep["episode_index"])
        if wanted and idx not in wanted:
            continue
        n = int(ep["length"])
        rows = data[data["episode_index"] == idx].sort_values("frame_index")
        streams = {}
        for key in cams:
            rel = info["video_path"].format(video_key=key, chunk_index=int(ep[f"videos/{key}/chunk_index"]),
                                            file_index=int(ep[f"videos/{key}/file_index"]))
            pics = _frames(os.path.join(a.lerobot, rel), float(ep[f"videos/{key}/from_timestamp"]), n, fps)
            streams[key] = _annexb(pics, fps, a.lead)
        with open(os.path.join(a.out, f"episode_{idx}.mcap"), "wb") as fh:
            w = Writer(fh)
            raw = w._writer
            schema = raw.register_schema(name="vec", encoding="jsonschema", data=b"{}")
            ch = {t: raw.register_channel(t, "json", schema) for t in ("/action", "/observation.state", "/task")}
            task = ep.get("tasks")
            text = str(task[0] if isinstance(task, (list, np.ndarray)) and len(task) else task or "")
            raw.add_message(ch["/task"], log_time=t0, publish_time=t0, data=json.dumps({"data": text}).encode())
            for i, (_, row) in enumerate(rows.iterrows()):
                t = t0 + int(round(i * 1e9 / fps))
                for topic, col in (("/action", "action"), ("/observation.state", "observation.state")):
                    if col in row:
                        raw.add_message(ch[topic], log_time=t, publish_time=t,
                                        data=json.dumps({"data": np.asarray(row[col], float).tolist()}).encode())
                for key, units in streams.items():
                    v = CompressedVideo()
                    v.format = "h264"
                    v.data = units[i]
                    w.write_message(f"/{key}" if key.startswith(PREFIX) else f"/{PREFIX}{key}", v, log_time=t, publish_time=t)
            w.finish()
        print(f"episode {idx}: {n} frames, {len(cams)} cameras")


if __name__ == "__main__":
    main()
