"""Small LeRobot datasets for the visualizer tests (design doc 18).

``make_v2`` - three episodes, two cameras (``front`` H.264, ``wrist`` MPEG-4 Part 2, which no browser
plays), a 7-joint arm with a gripper, HABIT-style annotations (``low_level_task_index`` +
``meta/subtasks.jsonl``, ``is_intervention_segment``), an RSS-style placeholder ``subtask`` column and
``task_status`` in the episode table.

``make_v3`` - three episodes in one data file of several row groups and one video file per camera
(``from_timestamp`` / ``to_timestamp`` windows), ``subtask_index`` + ``meta/subtasks.parquet`` and
``language_persistent`` lists carrying the episode's timed subtasks on every frame (HIW).
"""
from __future__ import annotations

import json
import os

import numpy as np

FPS = 10
W, H = 64, 48
JOINTS = [f"joint_{i}" for i in range(7)] + ["gripper"]
LENGTHS = [30, 24, 36]
SUBTASKS = ["reach the cup", "grasp the cup", "lift the cup"]


def _video(path: str, frames: int, codec: str = "h264", seed: int = 0) -> None:
    import av

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with av.open(path, "w") as out:
        s = out.add_stream(codec, rate=FPS)
        s.width, s.height, s.pix_fmt = W, H, "yuv420p"
        if codec == "h264":
            s.options = {"bf": "0", "g": "10"}
        for i in range(frames):
            img = np.full((H, W, 3), 40, np.uint8)
            img[:, (i * 3 + seed) % W] = 255
            for p in s.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                out.mux(p)
        for p in s.encode():
            out.mux(p)


def _arm(n: int, ep: int) -> np.ndarray:
    t = np.arange(n) / FPS
    q = np.stack([np.sin(0.5 * t + k + ep) for k in range(7)] + [(t > 1.0).astype(float) * 0.08], axis=1)
    return q.astype(np.float32)


def _steps(n: int) -> list[int]:
    a, b = n // 3, 2 * n // 3
    return [0] * a + [1] * (b - a) + [2] * (n - b)


def make_v2(root: str) -> str:
    import pandas as pd

    info = {
        "codebase_version": "v2.1", "robot_type": "franka", "total_episodes": 3,
        "total_frames": sum(LENGTHS), "chunks_size": 1000, "fps": FPS,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {
            "observation.state": {"dtype": "float32", "shape": [8], "names": {"motors": JOINTS}},
            "action": {"dtype": "float32", "shape": [8], "names": {"motors": JOINTS}},
            "observation.force": {"dtype": "float32", "shape": [3], "names": None},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
            "low_level_task_index": {"dtype": "int64", "shape": [1], "names": None},
            "is_intervention_segment": {"dtype": "bool", "shape": [1], "names": None},
            "subtask": {"dtype": "string", "shape": [1], "names": None},
            "observation.images.front": {"dtype": "video", "shape": [H, W, 3], "names": ["height", "width", "channel"],
                                         "info": {"video.codec": "h264", "video.width": W, "video.height": H,
                                                  "video.fps": FPS, "video.pix_fmt": "yuv420p"}},
            "observation.images.wrist": {"dtype": "video", "shape": [H, W, 3], "names": ["height", "width", "channel"],
                                         "info": {"video.codec": "mpeg4", "video.width": W, "video.height": H,
                                                  "video.fps": FPS, "video.pix_fmt": "yuv420p"}},
        },
    }
    os.makedirs(os.path.join(root, "meta"), exist_ok=True)
    with open(os.path.join(root, "meta", "info.json"), "w") as fh:
        json.dump(info, fh, indent=1)
    eps, cursor = [], 0
    for ep, n in enumerate(LENGTHS):
        q = _arm(n, ep)
        steps = _steps(n)
        df = pd.DataFrame({
            "observation.state": list(q), "action": list(np.roll(q, -1, axis=0)),
            "observation.force": list(np.zeros((n, 3), np.float32)),
            "timestamp": (np.arange(n) / FPS).astype(np.float32),
            "frame_index": np.arange(n, dtype=np.int64),
            "episode_index": np.full(n, ep, np.int64),
            "index": np.arange(cursor, cursor + n, dtype=np.int64),
            "task_index": np.zeros(n, np.int64),
            "low_level_task_index": np.asarray(steps, np.int64),
            "is_intervention_segment": [5 <= i < 9 for i in range(n)],
            "subtask": ["TODO"] * n,
        })
        path = os.path.join(root, "data", "chunk-000", f"episode_{ep:06d}.parquet")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        df.to_parquet(path)
        for cam, codec in (("observation.images.front", "h264"), ("observation.images.wrist", "mpeg4")):
            _video(os.path.join(root, "videos", "chunk-000", cam, f"episode_{ep:06d}.mp4"), n, codec, ep)
        eps.append({"episode_index": ep, "tasks": ["Lift the cup"], "length": n,
                    "task_status": "recovered" if ep == 1 else "success"})
        cursor += n
    with open(os.path.join(root, "meta", "episodes.jsonl"), "w") as fh:
        fh.writelines(json.dumps(e) + "\n" for e in eps)
    with open(os.path.join(root, "meta", "tasks.jsonl"), "w") as fh:
        fh.write(json.dumps({"task_index": 0, "task": "Lift the cup"}) + "\n")
    with open(os.path.join(root, "meta", "subtasks.jsonl"), "w") as fh:
        fh.writelines(json.dumps({"task_index": i, "task": s}) + "\n" for i, s in enumerate(SUBTASKS))
    return root


def make_v3(root: str) -> str:
    import pyarrow as pa
    import pyarrow.parquet as pq

    info = {
        "codebase_version": "v3.0", "robot_type": "so101", "total_episodes": 3, "total_frames": sum(LENGTHS),
        "chunks_size": 1000, "fps": FPS,
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
        "features": {
            "observation.state": {"dtype": "float32", "shape": [8], "names": JOINTS},
            "action": {"dtype": "float32", "shape": [8], "names": JOINTS},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
            "subtask_index": {"dtype": "int64", "shape": [1], "names": None},
            "language_persistent": {"dtype": "language", "shape": [1], "names": None},
            "observation.images.top": {"dtype": "video", "shape": [H, W, 3], "names": ["height", "width", "channels"],
                                       "info": {"video.codec": "h264", "video.width": W, "video.height": H,
                                                "video.fps": FPS, "video.pix_fmt": "yuv420p"}},
        },
    }
    os.makedirs(os.path.join(root, "meta", "episodes", "chunk-000"), exist_ok=True)
    with open(os.path.join(root, "meta", "info.json"), "w") as fh:
        json.dump(info, fh, indent=1)
    cols = {k: [] for k in ("observation.state", "action", "timestamp", "frame_index", "episode_index", "index",
                            "task_index", "subtask_index", "language_persistent")}
    episodes, cursor, video_t = [], 0, 0.0
    for ep, n in enumerate(LENGTHS):
        q = _arm(n, ep)
        steps = _steps(n)
        # HIW's layout: every frame carries all the timed statements of the episode
        timed = [{"role": "assistant", "content": SUBTASKS[k], "style": "subtask",
                  "timestamp": steps.index(k) / FPS} for k in range(len(SUBTASKS))]
        for i in range(n):
            cols["observation.state"].append(q[i].tolist())
            cols["action"].append(q[min(i + 1, n - 1)].tolist())
            cols["timestamp"].append(i / FPS)
            cols["frame_index"].append(i)
            cols["episode_index"].append(ep)
            cols["index"].append(cursor + i)
            cols["task_index"].append(0)
            cols["subtask_index"].append(steps[i])
            cols["language_persistent"].append(timed)
        episodes.append({"episode_index": ep, "length": n, "tasks": ["Lift the cup"],
                         "data/chunk_index": 0, "data/file_index": 0,
                         "dataset_from_index": cursor, "dataset_to_index": cursor + n,
                         "videos/observation.images.top/chunk_index": 0,
                         "videos/observation.images.top/file_index": 0,
                         "videos/observation.images.top/from_timestamp": round(video_t, 6),
                         "videos/observation.images.top/to_timestamp": round(video_t + n / FPS, 6)})
        cursor += n
        video_t += n / FPS
    table = pa.table({
        "observation.state": pa.array(cols["observation.state"], pa.list_(pa.float32(), 8)),
        "action": pa.array(cols["action"], pa.list_(pa.float32(), 8)),
        "timestamp": pa.array(cols["timestamp"], pa.float32()),
        "frame_index": pa.array(cols["frame_index"], pa.int64()),
        "episode_index": pa.array(cols["episode_index"], pa.int64()),
        "index": pa.array(cols["index"], pa.int64()),
        "task_index": pa.array(cols["task_index"], pa.int64()),
        "subtask_index": pa.array(cols["subtask_index"], pa.int64()),
        "language_persistent": pa.array(cols["language_persistent"]),
    })
    path = os.path.join(root, "data", "chunk-000", "file-000.parquet")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pq.write_table(table, path, row_group_size=20)            # several row groups: 90 rows / 20
    pq.write_table(pa.Table.from_pylist(episodes), os.path.join(root, "meta", "episodes", "chunk-000", "file-000.parquet"))
    pq.write_table(pa.table({"task_index": [0], "task": ["Lift the cup"]}), os.path.join(root, "meta", "tasks.parquet"))
    pq.write_table(pa.table({"subtask_index": [0, 1, 2], "subtask": SUBTASKS}), os.path.join(root, "meta", "subtasks.parquet"))
    _video(os.path.join(root, "videos", "observation.images.top", "chunk-000", "file-000.mp4"), sum(LENGTHS), "h264")
    return root
