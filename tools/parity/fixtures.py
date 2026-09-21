"""Synthetic LeRobot v2 dataset for offline parity and integration tests.

The dataset is small (8 episodes, two cameras, a few seconds each) but walks
the paths the parity tools must see:

* ep 2 has a 0.6 s timestamp jump      -> the timestamp hard gate drops it;
* ep 5 is a 0.4 s fragment             -> dropped as a fragment;
* ep 7 is a byte-for-byte copy of ep 3 -> removed by dedup;
* eps 4 and 6 have no task text        -> captioned by the model first.

Videos are real H.264 files, so decoding, optical flow and frame sampling run
exactly as on customer data. The square in each frame moves with joint 0, so
the video-action sync check has a signal to lock onto.
"""
from __future__ import annotations

import json
import os
import shutil

import numpy as np

FPS = 15
CAMERAS = ("observation.images.exterior", "observation.images.wrist")
WIDTH, HEIGHT = 128, 96
DOF = 7
TASKS = ["pick up the red block and place it in the bin", ""]
#: episode -> (length in frames, task index)
EPISODES = {0: (75, 0), 1: (90, 0), 2: (75, 0), 3: (60, 0), 4: (81, 1),
            5: (6, 0), 6: (69, 1)}
DUPLICATE_OF = {7: 3}


def _joint_track(length: int, ep: int) -> np.ndarray:
    """Smooth joint trajectory inside Franka limits; varies per episode."""
    t = np.arange(length) / FPS
    phase = 0.7 * ep
    base = np.array([0.0, 0.2, 0.0, -1.8, 0.0, 1.6, 0.0])
    amp = np.array([0.6, 0.25, 0.3, 0.3, 0.3, 0.3, 0.4])
    freq = np.array([0.35, 0.5, 0.4, 0.45, 0.3, 0.55, 0.25])
    q = base + amp * np.sin(2 * np.pi * freq * t[:, None] + phase)
    return q.astype(np.float32)


def _frames(q: np.ndarray, camera: str, ep: int) -> np.ndarray:
    """Checkerboard background plus a square that follows joint 0."""
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    board = (((yy // 8) + (xx // 8)) % 2 * 60 + 90).astype(np.uint8)
    out = np.empty((len(q), HEIGHT, WIDTH, 3), dtype=np.uint8)
    lo, hi = float(q[:, 0].min()), float(q[:, 0].max())
    span = max(hi - lo, 1e-6)
    for i, row in enumerate(q):
        img = np.repeat(board[:, :, None], 3, axis=2).copy()
        pos = (float(row[0]) - lo) / span
        if camera.endswith("wrist"):
            cy, cx = int(12 + pos * (HEIGHT - 36)), WIDTH // 2 - 12
            color = (40, 160, 220)
        else:
            cy, cx = HEIGHT // 2 - 12, int(12 + pos * (WIDTH - 36))
            color = (220, 50, 40)
        img[cy:cy + 24, cx:cx + 24] = color
        img[2:6, 2 + (ep * 7) % 100: 10 + (ep * 7) % 100] = 255   # per-episode mark
        out[i] = img
    return out


def _write_video(path: str, frames: np.ndarray) -> None:
    import av

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with av.open(path, mode="w") as container:
        stream = container.add_stream("libx264", rate=FPS)
        stream.width, stream.height = WIDTH, HEIGHT
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": "18", "preset": "veryfast", "threads": "1"}
        for img in frames:
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def make_mini_lerobot(root: str, *, overwrite: bool = False) -> str:
    """Write the dataset under ``root`` and return the path."""
    import pandas as pd

    if os.path.exists(os.path.join(root, "meta", "info.json")) and not overwrite:
        return root
    if overwrite and os.path.exists(root):
        shutil.rmtree(root)
    n_eps = len(EPISODES) + len(DUPLICATE_OF)
    lengths = {ep: EPISODES[ep][0] for ep in EPISODES}
    lengths.update({dup: EPISODES[src][0] for dup, src in DUPLICATE_OF.items()})
    names = [f"joint_{i}" for i in range(DOF)]
    info = {
        "codebase_version": "v2.1",
        "robot_type": "franka",
        "total_episodes": n_eps,
        "total_frames": int(sum(lengths.values())),
        "total_tasks": len(TASKS),
        "total_videos": n_eps * len(CAMERAS),
        "total_chunks": 1,
        "chunks_size": 1000,
        "fps": FPS,
        "splits": {"train": f"0:{n_eps}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": ("videos/chunk-{episode_chunk:03d}/{video_key}/"
                       "episode_{episode_index:06d}.mp4"),
        "features": {
            "action": {"dtype": "float32", "shape": [DOF], "names": names},
            "observation.state": {"dtype": "float32", "shape": [DOF], "names": names},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
            **{cam: {"dtype": "video", "shape": [HEIGHT, WIDTH, 3],
                     "names": ["height", "width", "channel"],
                     "info": {"video.fps": FPS, "video.codec": "h264",
                              "video.pix_fmt": "yuv420p"}} for cam in CAMERAS},
        },
    }
    os.makedirs(os.path.join(root, "meta"), exist_ok=True)
    with open(os.path.join(root, "meta", "info.json"), "w") as fh:
        json.dump(info, fh, indent=1)

    episodes_meta, cursor = [], 0
    for ep in range(n_eps):
        src = DUPLICATE_OF.get(ep, ep)
        length, task_idx = EPISODES[src]
        q = _joint_track(length, src)
        state = np.vstack([q[:1], q[:-1]]).astype(np.float32)   # state lags action by 1 frame
        ts = (np.arange(length) / FPS).astype(np.float32)
        if src == 2:
            ts[length // 2:] += np.float32(0.6)                  # timestamp jump
        df = pd.DataFrame({
            "action": list(q), "observation.state": list(state),
            "timestamp": ts,
            "frame_index": np.arange(length, dtype=np.int64),
            "episode_index": np.full(length, ep, dtype=np.int64),
            "index": np.arange(cursor, cursor + length, dtype=np.int64),
            "task_index": np.full(length, task_idx, dtype=np.int64),
        })
        cursor += length
        pq = os.path.join(root, info["data_path"].format(episode_chunk=0, episode_index=ep))
        os.makedirs(os.path.dirname(pq), exist_ok=True)
        df.to_parquet(pq, index=False)
        for cam in CAMERAS:
            dst = os.path.join(root, info["video_path"].format(
                episode_chunk=0, video_key=cam, episode_index=ep))
            if ep in DUPLICATE_OF:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(os.path.join(root, info["video_path"].format(
                    episode_chunk=0, video_key=cam, episode_index=src)), dst)
            else:
                _write_video(dst, _frames(q, cam, src))
        episodes_meta.append({"episode_index": ep, "tasks": [TASKS[task_idx]],
                              "length": length})

    with open(os.path.join(root, "meta", "episodes.jsonl"), "w") as fh:
        for row in episodes_meta:
            fh.write(json.dumps(row) + "\n")
    with open(os.path.join(root, "meta", "tasks.jsonl"), "w") as fh:
        for i, task in enumerate(TASKS):
            fh.write(json.dumps({"task_index": i, "task": task}) + "\n")
    return root


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m parity make-fixture")
    p.add_argument("--out", required=True)
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args(argv)
    print(make_mini_lerobot(args.out, overwrite=args.overwrite))
    return 0
