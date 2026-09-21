"""A small LeRobot v3.0 dataset for the v3 export tests.

v3 is not compared with v1 (design doc 10, section 3.1); this fixture only has to be
a correct v3.0 dataset that the official loader opens without a warning, laid out
the way v3 is: episodes share files.  Six episodes, two cameras, 15 fps:

* frame tables  ``data/chunk-000/file-000.parquet`` = episodes 0-2, ``file-001`` = 3-5;
* each camera   ``file-000.mp4`` = episodes 0-3, ``file-001.mp4`` = 4-5 (a different
  split from the frame tables on purpose);
* three task texts, per-episode ``stats/*`` columns and ``meta/stats.json`` like
  lerobot writes them.

Frames and joint tracks come from W0's v2 fixture generator (``parity.fixtures``).
"""
from __future__ import annotations

import json
import os
import shutil

import numpy as np

from parity.fixtures import CAMERAS, DOF, FPS, HEIGHT, WIDTH, _frames, _joint_track

LENGTHS = [45, 60, 38, 52, 41, 47]
TASKS = ["pick up the red block and place it in the bin",
         "push the blue cube to the left",
         "open the top drawer"]
EPISODE_TASK = [0, 1, 0, 2, 1, 0]
DATA_FILES = [[0, 1, 2], [3, 4, 5]]
VIDEO_FILES = [[0, 1, 2, 3], [4, 5]]


def _stats(arr: np.ndarray) -> dict:
    keep = arr.ndim == 1
    return {"min": np.min(arr, axis=0, keepdims=keep).tolist(),
            "max": np.max(arr, axis=0, keepdims=keep).tolist(),
            "mean": np.mean(arr, axis=0, keepdims=keep).tolist(),
            "std": np.std(arr, axis=0, keepdims=keep).tolist(),
            "count": [int(len(arr))]}


def episode_frames(ep: int, camera: str) -> np.ndarray:
    """The frames the source shows for ``ep`` on ``camera`` (before encoding)."""
    return _frames(_joint_track(LENGTHS[ep], ep + 10), camera, ep + 10)


def _write_video(path: str, frames: np.ndarray) -> None:
    import av

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with av.open(path, mode="w") as container:
        stream = container.add_stream("libx264", rate=FPS)
        stream.width, stream.height = WIDTH, HEIGHT
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": "18", "preset": "veryfast", "threads": "1"}
        for img in frames:
            for packet in stream.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def make_mini_lerobot_v3(root: str, *, overwrite: bool = False) -> str:
    import pandas as pd

    if os.path.exists(os.path.join(root, "meta", "info.json")) and not overwrite:
        return root
    if os.path.exists(root):
        shutil.rmtree(root)
    names = [f"joint_{i}" for i in range(DOF)]
    info = {
        "codebase_version": "v3.0",
        "robot_type": "franka",
        "total_episodes": len(LENGTHS),
        "total_frames": int(sum(LENGTHS)),
        "total_tasks": len(TASKS),
        "chunks_size": 1000,
        "data_files_size_in_mb": 100,
        "video_files_size_in_mb": 200,
        "fps": FPS,
        "splits": {"train": f"0:{len(LENGTHS)}"},
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
        "features": {
            "action": {"dtype": "float32", "shape": [DOF], "names": names},
            "observation.state": {"dtype": "float32", "shape": [DOF], "names": names},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
            **{cam: {"dtype": "video", "shape": [HEIGHT, WIDTH, 3],
                     "names": ["height", "width", "channels"],
                     "info": {"video.height": HEIGHT, "video.width": WIDTH,
                              "video.codec": "h264", "video.pix_fmt": "yuv420p",
                              "video.is_depth_map": False, "video.fps": FPS,
                              "video.channels": 3, "has_audio": False}}
               for cam in CAMERAS},
        },
    }
    os.makedirs(os.path.join(root, "meta", "episodes", "chunk-000"), exist_ok=True)
    with open(os.path.join(root, "meta", "info.json"), "w") as fh:
        json.dump(info, fh, indent=4)

    frames_df, meta = {}, {}
    cursor = 0
    all_actions = []
    for ep, length in enumerate(LENGTHS):
        q = _joint_track(length, ep + 10)
        state = np.vstack([q[:1], q[:-1]]).astype(np.float32)
        df = pd.DataFrame({
            "action": list(q), "observation.state": list(state),
            "timestamp": (np.arange(length) / FPS).astype(np.float32),
            "frame_index": np.arange(length, dtype=np.int64),
            "episode_index": np.full(length, ep, dtype=np.int64),
            "index": np.arange(cursor, cursor + length, dtype=np.int64),
            "task_index": np.full(length, EPISODE_TASK[ep], dtype=np.int64),
        })
        frames_df[ep] = df
        all_actions.append(q)
        m = {"episode_index": ep, "tasks": [TASKS[EPISODE_TASK[ep]]], "length": length,
             "dataset_from_index": cursor, "dataset_to_index": cursor + length}
        for key, arr in (("action", q), ("observation.state", state)):
            for stat, val in _stats(arr).items():
                m[f"stats/{key}/{stat}"] = val
        meta[ep] = m
        cursor += length

    for fid, eps in enumerate(DATA_FILES):
        path = os.path.join(root, info["data_path"].format(chunk_index=0, file_index=fid))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pd.concat([frames_df[e] for e in eps], ignore_index=True).to_parquet(path, index=False)
        for e in eps:
            meta[e]["data/chunk_index"] = 0
            meta[e]["data/file_index"] = fid
    for cam in CAMERAS:
        for fid, eps in enumerate(VIDEO_FILES):
            path = os.path.join(root, info["video_path"].format(video_key=cam, chunk_index=0,
                                                                file_index=fid))
            clips, t = [], 0
            for e in eps:
                clips.append(episode_frames(e, cam))
                meta[e][f"videos/{cam}/chunk_index"] = 0
                meta[e][f"videos/{cam}/file_index"] = fid
                meta[e][f"videos/{cam}/from_timestamp"] = t / FPS
                t += LENGTHS[e]
                meta[e][f"videos/{cam}/to_timestamp"] = t / FPS
            _write_video(path, np.concatenate(clips))
    rows = []
    for ep in range(len(LENGTHS)):
        meta[ep]["meta/episodes/chunk_index"] = 0
        meta[ep]["meta/episodes/file_index"] = 0
        rows.append(meta[ep])
    pd.DataFrame(rows).to_parquet(
        os.path.join(root, "meta", "episodes", "chunk-000", "file-000.parquet"), index=False)
    pd.DataFrame({"task_index": list(range(len(TASKS)))}, index=pd.Index(TASKS)).to_parquet(
        os.path.join(root, "meta", "tasks.parquet"))
    q_all = np.concatenate(all_actions)
    with open(os.path.join(root, "meta", "stats.json"), "w") as fh:
        json.dump({"action": _stats(q_all), "observation.state": _stats(q_all)}, fh, indent=4)
    return root


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="v3_fixture")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    print(make_mini_lerobot_v3(args.out, overwrite=True))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
