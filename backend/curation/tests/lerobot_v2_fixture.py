"""一个最小的 LeRobot v2 数据集，就地合成，供各处单测当源数据用。

原来长在 `test_lerobot_v2_export.py` 里（交付导出的验收用它当源）。导出随 D69 下线、
那个测试模块删掉之后，这个合成器仍有四个模块在用（dsfs、运动学跳过、v1 rejudge），
所以单独放在这里。假 mp4 只有几个字节：用它的测试都不解码。
"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

pytest.importorskip("pandas", reason="本机无 pandas")
pytest.importorskip("pyarrow", reason="本机无 pyarrow(parquet 读写依赖)")

import pandas as pd  # noqa: E402

CHUNKS_SIZE = 2                     # 故意设小:4 条就跨 chunk,能验证新布局的分块
FPS = 10.0
VIDEO_KEYS = ("observation.images.cam_high", "observation.images.cam_wrist")
TASKS = ["pick up the red cube", "place the cube in the box"]
LENGTHS = [6, 5, 7, 4]              # 各条帧数不同:index 重排错了立刻暴露
EP_TASK = [0, 1, 0, 1]              # episode → TASKS 下标


def _fake_mp4(ep: int, cam: str) -> bytes:
    """几个字节的假 mp4:导出只拷贝字节,内容可辨认即可(用于逐字节比对落位)。"""
    return b"\x00\x00\x00\x18ftypmp42" + f"|ep{ep}|{cam}".encode()


def _write_v2_dataset(root: str, n_episodes: int = 4,
                      episodes_stats: bool = False) -> str:
    """就地合成一个最小 v2.0 数据集,返回目录路径。"""
    dim = 6
    info = {
        "codebase_version": "v2.0",
        "robot_type": "testarm",
        "total_episodes": n_episodes,
        "total_frames": sum(LENGTHS[:n_episodes]),
        "total_tasks": len(TASKS),
        "total_videos": n_episodes * len(VIDEO_KEYS),
        "total_chunks": (n_episodes + CHUNKS_SIZE - 1) // CHUNKS_SIZE,
        "chunks_size": CHUNKS_SIZE,
        "fps": FPS,
        "splits": {"train": f"0:{n_episodes}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": ("videos/chunk-{episode_chunk:03d}/{video_key}/"
                       "episode_{episode_index:06d}.mp4"),
        "features": {
            "action": {"dtype": "float32", "shape": [dim],
                       "names": [f"joint_{i}" for i in range(dim)]},
            "observation.state": {"dtype": "float32", "shape": [dim],
                                  "names": [f"joint_{i}" for i in range(dim)]},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
            **{vk: {"dtype": "video", "shape": [64, 64, 3],
                    "names": ["height", "width", "channel"],
                    "info": {"video.fps": FPS}} for vk in VIDEO_KEYS},
        },
    }
    os.makedirs(os.path.join(root, "meta"), exist_ok=True)
    with open(os.path.join(root, "meta", "info.json"), "w") as f:
        json.dump(info, f)

    eps, stats_rows, cursor = [], [], 0
    for ep in range(n_episodes):
        length = LENGTHS[ep]
        chunk = ep // CHUNKS_SIZE
        base = np.arange(length, dtype=np.float32)[:, None] * 0.01 + ep
        df = pd.DataFrame({
            "action": list((base + np.arange(dim, dtype=np.float32)).astype(np.float32)),
            "observation.state": list((base * 0.5).astype(np.float32)),
            "timestamp": (np.arange(length) / FPS).astype(np.float32),
            "frame_index": np.arange(length, dtype=np.int64),
            "episode_index": np.full(length, ep, dtype=np.int64),
            "index": np.arange(cursor, cursor + length, dtype=np.int64),
            "task_index": np.full(length, EP_TASK[ep], dtype=np.int64),
        })
        cursor += length
        p = os.path.join(root, info["data_path"].format(
            episode_chunk=chunk, episode_index=ep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        df.to_parquet(p, index=False)

        for vk in VIDEO_KEYS:
            v = os.path.join(root, info["video_path"].format(
                episode_chunk=chunk, video_key=vk, episode_index=ep))
            os.makedirs(os.path.dirname(v), exist_ok=True)
            with open(v, "wb") as f:
                f.write(_fake_mp4(ep, vk))

        eps.append({"episode_index": ep, "tasks": [TASKS[EP_TASK[ep]]], "length": length})
        stats_rows.append({"episode_index": ep, "stats": {"action": {"mean": [float(ep)]}}})

    with open(os.path.join(root, "meta", "episodes.jsonl"), "w") as f:
        for e in eps:
            f.write(json.dumps(e) + "\n")
    with open(os.path.join(root, "meta", "tasks.jsonl"), "w") as f:
        for i, t in enumerate(TASKS):
            f.write(json.dumps({"task_index": i, "task": t}) + "\n")
    if episodes_stats:                                     # v2.1 风格:逐条统计
        with open(os.path.join(root, "meta", "episodes_stats.jsonl"), "w") as f:
            for s in stats_rows:
                f.write(json.dumps(s) + "\n")
    else:                                                  # v2.0 风格:全局统计
        with open(os.path.join(root, "meta", "stats.json"), "w") as f:
            json.dump({"action": {"mean": [0.0] * dim}}, f)
    return root


