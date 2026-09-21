"""去重(文档中的 M6)。⚠️ MVP 只做精确去重——误删不可逆,语义/像素去重待 spike 验证后上 V1。

精确重复的定义(全部相同才算):
- action 字节完全一致(sha256);
- 每路视频的**内容身份**一致:文件内容哈希 + 时间窗 (from_ts, to_ts)。
  v3 合并 mp4 下同一文件被多条 episode 共享 → 只比文件路径会误判,必须带时间窗;
  文件内容哈希按路径缓存(大文件只读一次)。
绝不误删:任何一项不同即不算重复。
"""
from __future__ import annotations

import hashlib
import os
from functools import lru_cache

import numpy as np


@lru_cache(maxsize=4096)
def _file_sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def episode_fingerprint(row: dict) -> str:
    """一条 episode 的内容指纹(action 字节 + 各相机的 文件哈希+时间窗)。"""
    h = hashlib.sha256()
    action = np.ascontiguousarray(np.asarray(row["action"]))
    h.update(action.tobytes())
    h.update(str(action.shape).encode())
    for cam in sorted((row.get("video") or {})):
        v = row["video"][cam]
        h.update(cam.encode())
        if str(v["path"]).startswith("tos://"):
            # 远端视频:用对象的 etag+size 当内容身份,不为算哈希把整段视频拉回来
            from ..ingest import dsfs
            try:
                h.update(dsfs.content_identity(v["path"]).encode())
            except FileNotFoundError:
                h.update(v["path"].encode())
        elif os.path.exists(v["path"]):
            h.update(_file_sha256(v["path"]).encode())
        else:
            h.update(v["path"].encode())      # 文件缺失时退化为路径(validate 会另行报)
        h.update(f"{v['from_ts']:.6f}:{v['to_ts']:.6f}".encode())
    return h.hexdigest()


def exact_dedup(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """返回 (去重后的行, 重复清单)。保留每组指纹的第一条(输入顺序稳定)。

    重复清单元素:{"episode_id", "duplicate_of", "fingerprint"}(进 M8 报告)。
    """
    seen: dict[str, str] = {}
    kept: list[dict] = []
    dropped: list[dict] = []
    for row in rows:
        fp = episode_fingerprint(row)
        if fp in seen:
            dropped.append({"episode_id": row["episode_id"],
                            "duplicate_of": seen[fp], "fingerprint": fp[:16]})
        else:
            seen[fp] = row["episode_id"]
            kept.append(row)
    return kept, dropped


def action_hash(row: dict) -> str:
    """只哈希 action 字节(不碰视频)。两段式去重的第一道:action 不同 → 必非重复,
    无需读视频;action 相同(罕见)才进 episode_fingerprint 验视频内容。
    (2026-07-15 droid 实测:全员视频指纹=哈希 200GB 磨十几分钟且静默;
    两段式把视频哈希压到只剩撞车组,语义不变——判重仍要求 action+视频都同。)"""
    h = hashlib.sha256()
    action = np.ascontiguousarray(np.asarray(row["action"]))
    h.update(action.tobytes())
    h.update(str(action.shape).encode())
    return h.hexdigest()
