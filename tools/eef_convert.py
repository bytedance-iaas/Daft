"""Convert an EEF trajectory.json between a LeRobot dataset and its mcap twin (F5.13).

The two differ only in where each camera's frames are (``media`` of every view): a LeRobot video file
with its clip (v3 concatenates episodes), or an image topic of the episode's ``.mcap``. Poses,
projections, calibration, points and ``video_frame_index`` stay as they are, so the two datasets must
hold the same frames in the same order (one image message per video frame, same size); observation
seeds and gripper templates are keyed by sample, camera and frame index and are reused unchanged.

    # LeRobot -> mcap; with --check the mcap dataset names the files and every topic is verified
    .venv/bin/python tools/eef_convert.py to-mcap --trajectory lerobot.json --out mcap.json --check /data/ds_mcap
    # mcap -> LeRobot: the dataset's meta/ gives the video files and clips (the videos are not read)
    .venv/bin/python tools/eef_convert.py to-lerobot --trajectory mcap.json --out lerobot.json --dataset /data/ds_lr

A camera's topic is ``/observation.images.<key>`` of its LeRobot video key (the platform's default
mcap naming) and back; ``--map CAMERA=VALUE`` sets another (a topic for to-mcap, a video key for
to-lerobot). Without ``--check``, to-mcap names the files ``--name`` (default ``episode_{episode}.mcap``).
The output is validated as the platform validates an upload; problems are printed and exit 1.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO / "backend") not in sys.path:
    sys.path.insert(0, str(REPO / "backend"))

PREFIX = "observation.images."
_KEY_IN_URI = re.compile(r"(?:^|/)(observation\.images\.[^/]+)(?:/|$)")


class ConvertError(ValueError):
    pass


def _views(doc: dict):
    """(episode, view) of every camera view whose frames are a video."""
    for entry in doc["samples"]:
        for view in entry["sample"]["views"]:
            if view["media"]["kind"] == "video":
                yield int(entry["episode_index"]), view


def _mapping(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        cam, sep, value = p.partition("=")
        if not sep or not cam or not value:
            raise ConvertError(f"--map wants CAMERA=VALUE, got {p!r}")
        out[cam] = value
    return out


# ------------------------------------------------------------------------------ LeRobot -> mcap


def mcap_files(dataset: str) -> dict[int, str]:
    """``{episode: file}`` of a local mcap dataset by the platform's numbering (v1's rule:
    ``episode_<N>.mcap`` by name, otherwise the sorted files 0, 1, ...)."""
    from curation.cli import containers

    return containers.mcap_episodes([n for n in os.listdir(dataset) if n.endswith(".mcap")])


def to_mcap(doc: dict, *, topics: dict[str, str], name: str = "episode_{episode}.mcap",
            files: dict[int, str] | None = None) -> dict:
    out = copy.deepcopy(doc)
    for ep, view in _views(out):
        m, cam = view["media"], view["camera_id"]
        if m.get("topic"):
            raise ConvertError(f"episode {ep} camera {cam}: already an mcap topic ({m['topic']})")
        topic = topics.get(cam)
        if topic is None:
            hit = _KEY_IN_URI.search(m["uri"])
            if hit is None:
                raise ConvertError(f"episode {ep} camera {cam}: no LeRobot video key in {m['uri']}; "
                                   f"give --map {cam}=/topic")
            topic = "/" + hit.group(1)
        if files is not None:
            if ep not in files:
                raise ConvertError(f"episode {ep}: the mcap dataset has no file for it")
            uri = files[ep]
        else:
            uri = name.format(episode=ep)
        m.update(uri=uri, topic=topic, clip_start_s=0, clip_end_s=None)
    out["dataset"] = {**out["dataset"], "lerobot_codebase_version": "mcap",
                      "generator": "tools/eef_convert.py to-mcap"}
    out["media_uri_base"] = "lerobot_root"
    return out


def _first_size(path: str, topic: str) -> tuple[list[int] | None, str]:
    """(width, height) of the topic's first decodable frame, and its codec."""
    import av
    import cv2
    import numpy as np

    from curation.ingest import mcap_reader as MR

    make_reader = MR._mcap_reader_mod()
    factories: dict = {}
    decoders: dict = {}
    ctx = None
    with open(path, "rb") as fh:
        for n, (schema, channel, message) in enumerate(make_reader(fh).iter_messages(topics=[topic])):
            frame = MR._as_frame(MR._decode(channel, schema, message, factories, decoders))
            if frame is None:
                continue
            codec = MR._codec_of(*frame)
            if codec == "jpeg":
                img = cv2.imdecode(np.frombuffer(frame[1], np.uint8), cv2.IMREAD_COLOR)
                return (None if img is None else [int(img.shape[1]), int(img.shape[0])]), codec
            if codec != "h264":
                return None, codec
            ctx = ctx or av.CodecContext.create("h264", "r")
            for f in ctx.decode(av.Packet(frame[1])):
                return [int(f.width), int(f.height)], codec
            if n > 300:
                break
    return None, "h264" if ctx is not None else "none"


def check_mcap(doc: dict, dataset: str) -> list[str]:
    """What does not fit between the trajectory's topic views and the mcap files."""
    from mcap.reader import make_reader

    problems: list[str] = []
    counts: dict[str, dict[str, int]] = {}
    for ep, view in _views(doc):
        m, cam = view["media"], view["camera_id"]
        path = os.path.join(dataset, m["uri"])
        where = f"episode {ep} camera {cam} ({m['uri']} {m['topic']})"
        if not os.path.isfile(path):
            problems.append(f"{where}: the file is missing")
            continue
        if path not in counts:
            with open(path, "rb") as fh:
                s = make_reader(fh).get_summary()
            counts[path] = {} if s is None or s.statistics is None else {
                ch.topic: int(s.statistics.channel_message_counts.get(cid, 0)) for cid, ch in s.channels.items()}
        if m["topic"] not in counts[path]:
            problems.append(f"{where}: no such topic (the file has {', '.join(sorted(counts[path])) or 'none'})")
            continue
        size, codec = _first_size(path, m["topic"])
        n = counts[path][m["topic"]]
        if codec not in ("jpeg", "h264"):
            problems.append(f"{where}: frames are {codec}; only JPEG and H.264 are read")
        elif codec == "jpeg" and n != int(m["frame_count"]):
            problems.append(f"{where}: {n} image messages, frame_count says {m['frame_count']}")
        elif codec == "h264" and n < int(m["frame_count"]):
            problems.append(f"{where}: {n} H.264 messages, fewer than frame_count {m['frame_count']}")
        if size is not None and size != list(m["image_size_wh"]):
            problems.append(f"{where}: frames are {size[0]}x{size[1]}, image_size_wh says "
                            f"{m['image_size_wh'][0]}x{m['image_size_wh'][1]}")
    return problems


# ------------------------------------------------------------------------------ mcap -> LeRobot


def to_lerobot(doc: dict, dataset: str, *, keys: dict[str, str]) -> tuple[dict, list[str]]:
    from curation.extensions.eef_consistency.adapters.lerobot_mapping import LeRobot

    plain = [f"episode {ep} camera {v['camera_id']}" for ep, v in _views(doc) if not v["media"].get("topic")]
    if plain:
        raise ConvertError(f"{plain[0]}: not an mcap topic view (to-lerobot converts an mcap trajectory.json)")
    try:
        lr = LeRobot(dataset)
    except (OSError, ValueError, KeyError) as exc:
        raise ConvertError(f"{dataset}: not a LeRobot dataset ({type(exc).__name__}: {exc})") from None
    out = copy.deepcopy(doc)
    problems: list[str] = []
    for ep, view in _views(out):
        m, cam = view["media"], view["camera_id"]
        topic = m.get("topic")
        if not topic:
            raise ConvertError(f"episode {ep} camera {cam}: not an mcap topic ({m['uri']})")
        key = keys.get(cam) or (topic.lstrip("/") if topic.lstrip("/").startswith(PREFIX) else None)
        if key is None:
            raise ConvertError(f"episode {ep} camera {cam}: topic {topic} names no LeRobot video key; "
                               f"give --map {cam}=observation.images.<key>")
        if key not in lr.info.get("features", {}):
            raise ConvertError(f"episode {ep} camera {cam}: the dataset has no video {key}")
        if ep not in lr.episodes:
            raise ConvertError(f"episode {ep}: not in the dataset")
        n = int(m["frame_count"])
        vid = lr.video(ep, key, n)
        m.pop("topic")
        m.update(uri=vid["uri"], clip_start_s=vid["clip_start_s"], clip_end_s=vid["clip_end_s"])
        where = f"episode {ep} camera {cam} ({vid['uri']})"
        if not (lr.root / vid["uri"]).is_file():
            problems.append(f"{where}: the video file is missing")
        span = round((vid["clip_end_s"] - vid["clip_start_s"]) * lr.fps)
        if abs(span - n) > 1:
            problems.append(f"{where}: the clip holds {span} frames at {lr.fps:g} fps, frame_count says {n}")
        if vid["wh"] is not None and list(vid["wh"]) != list(m["image_size_wh"]):
            problems.append(f"{where}: the video is {vid['wh'][0]}x{vid['wh'][1]}, image_size_wh says "
                            f"{m['image_size_wh'][0]}x{m['image_size_wh'][1]}")
    out["dataset"] = {**out["dataset"], "lerobot_codebase_version": lr.version,
                      "generator": "tools/eef_convert.py to-lerobot"}
    out["media_uri_base"] = "lerobot_root"
    return out, problems


# ------------------------------------------------------------------------------ the command


def _validate(path: str, dataset: str | None) -> list[str]:
    """The platform's checks of an upload; the media files only against a dataset given."""
    from curation.extensions.eef_consistency import load

    r = load.load_bundle(path, lerobot_root=dataset, media_exists=None if dataset else (lambda _key: True))
    return [f"{i.path or ''}: {i.message}" for i in r.errors]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/eef_convert.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("to-mcap", help="LeRobot views -> mcap image topics")
    a.add_argument("--check", metavar="MCAP_DIR", help="the local mcap dataset: file names and every topic verified")
    a.add_argument("--name", default="episode_{episode}.mcap", help="file name without --check")
    b = sub.add_parser("to-lerobot", help="mcap image topics -> LeRobot video clips")
    b.add_argument("--dataset", required=True, metavar="LEROBOT_DIR", help="the local LeRobot dataset (meta/ is read)")
    for p in (a, b):
        p.add_argument("--trajectory", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--map", action="append", default=[], metavar="CAMERA=VALUE",
                       help="a camera's topic (to-mcap) or video key (to-lerobot)")
    args = ap.parse_args(argv)
    doc = json.loads(pathlib.Path(args.trajectory).read_text(encoding="utf-8"))
    try:
        if args.cmd == "to-mcap":
            files = mcap_files(args.check) if args.check else None
            out = to_mcap(doc, topics=_mapping(args.map), name=args.name, files=files)
            problems = check_mcap(out, args.check) if args.check else []
            dataset = args.check
        else:
            out, problems = to_lerobot(doc, args.dataset, keys=_mapping(args.map))
            dataset = args.dataset
    except ConvertError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pathlib.Path(args.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    problems += _validate(args.out, dataset)
    views = sum(1 for _ in _views(out))
    print(json.dumps({"out": args.out, "samples": len(out["samples"]), "views": views,
                      "checked_against": dataset or "nothing (no --check: files, topics and frame counts not verified)",
                      "problems": problems}, ensure_ascii=False, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
