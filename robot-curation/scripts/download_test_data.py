#!/usr/bin/env python
"""测试数据下载(PLAN.md §1.1 ⑤;数据落持久盘 /data03/hao/data)。

小数据集(冒烟/功能)走 snapshot_download 全量。
大数据集(DROID/Bridge,LeRobot v2 格式)⚠️ 不能用 snapshot_download —— 它先递归列全 repo 树
(~10 万文件,分页 API 会被匿名 429 限流;2026-07-02 实测)。v2 文件路径可构造
(data/chunk-XXX/episode_XXXXXX.parquet),直接按路径逐文件下载,零树列表。

用法:
    python scripts/download_test_data.py                 # 只拉小数据集
    python scripts/download_test_data.py --big --mirror  # 也拉 DROID/Bridge 子集(走 hf-mirror)
    python scripts/download_test_data.py --only-big --mirror --droid-eps 5000   # 扩规模(P5)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# HF 凭证在持久盘(2026-07-02 用户已登录,匿名会被 429 限流;重建容器不丢)
os.environ.setdefault("HF_HOME", "/data03/hao/.hf_home")

# --mirror 必须在 import huggingface_hub 之前设 env(constants 在 import 时读取);
# 不依赖 shell 传 env(2026-07-02 实测后台任务里 shell 前缀赋值丢失过一次)。
# ⚠️ 实测限制:mirror 的 HEAD 响应缺 HF 元数据头,hf_hub_download(逐文件)会报
# FileMetadataError → --mirror 只对 snapshot_download(小数据集)可用;
# 大数据集逐文件路径默认直连官方(429 烧配额的是树列表 API,逐文件 resolve 走 CDN 没事)。
if "--mirror" in sys.argv:
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    os.environ["HF_HUB_DISABLE_XET"] = "1"  # mirror 不支持 xet 存储协议

from huggingface_hub import hf_hub_download, snapshot_download  # noqa: E402

DATA_ROOT = "/data03/hao/data"

SMALL = [  # 冒烟/功能,全量(hub 官方,已是 v3.0 格式)
    "lerobot/pusht",
    "lerobot/svla_so100_pickplace",
    "lerobot/aloha_sim_insertion_human",
    "henry-guo/so101-pick-place",     # SO-101 真机 47条 1.3GB(2026-07-11 加,robot_type=so_follower)
]

# 大型主演示(IPEC 转换,v2 格式):先拉前 N episode 够开发;P5 规模压测再加 --droid-eps
BIG_DEFAULT_EPISODES = {
    "IPEC-COMMUNITY/droid_lerobot": 1000,        # ≈22GB(全量 1.67TB/76k eps)
    "IPEC-COMMUNITY/bridge_orig_lerobot": 1000,  # ≈7GB(全量 387GB/53k eps)
}

V2_META_FILES = ["meta/info.json", "meta/episodes.jsonl", "meta/tasks.jsonl", "meta/stats.json"]


def _retry(fn, what: str, attempts: int = 3):
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if i == attempts:
                raise
            print(f"  重试 {i}/{attempts} {what}({type(e).__name__}),30s 后…", flush=True)
            time.sleep(30)


def fetch_small(repo_id: str) -> None:
    name = repo_id.split("/")[-1]
    print(f"==> {repo_id} (全量)", flush=True)
    _retry(
        lambda: snapshot_download(
            repo_id, repo_type="dataset",
            local_dir=os.path.join(DATA_ROOT, name), max_workers=4,
        ),
        repo_id,
    )
    print(f"<== {repo_id} done", flush=True)


def fetch_v2_episodes(repo_id: str, n_episodes: int) -> None:
    """LeRobot v2:按可构造路径逐文件拉前 n_episodes 条,零树列表。"""
    name = repo_id.split("/")[-1]
    local = os.path.join(DATA_ROOT, name)
    print(f"==> {repo_id} (前 {n_episodes} episodes,零树列表)", flush=True)

    for f in V2_META_FILES:
        try:
            _retry(lambda f=f: hf_hub_download(
                repo_id, f, repo_type="dataset", local_dir=local), f, attempts=2)
        except Exception as e:  # noqa: BLE001
            print(f"  meta 可选文件 {f} 拉取失败(跳过): {type(e).__name__}", flush=True)

    info = json.load(open(os.path.join(local, "meta/info.json")))
    assert info["codebase_version"].startswith("v2"), (
        f"{repo_id} 不是 v2 格式({info['codebase_version']}),路径构造逻辑不适用")
    chunks_size = info["chunks_size"]
    n = min(n_episodes, info["total_episodes"])
    video_keys = [k for k, v in info["features"].items() if v["dtype"] == "video"]

    files = []
    for ep in range(n):
        chunk = ep // chunks_size
        files.append(info["data_path"].format(episode_chunk=chunk, episode_index=ep))
        for vk in video_keys:
            files.append(info["video_path"].format(
                episode_chunk=chunk, video_key=vk, episode_index=ep))

    def pull(path: str) -> str:
        _retry(lambda: hf_hub_download(repo_id, path, repo_type="dataset", local_dir=local),
               path, attempts=3)
        return path

    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(pull, p) for p in files]
        for fut in as_completed(futures):
            fut.result()  # 失败在此抛出
            done += 1
            if done % 200 == 0:
                print(f"  {name}: {done}/{len(files)} files", flush=True)
    print(f"<== {repo_id} done ({len(files)} files)", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--big", action="store_true", help="也拉 DROID/Bridge 子集")
    ap.add_argument("--only-big", action="store_true", help="跳过小数据集")
    ap.add_argument("--mirror", action="store_true", help="走 hf-mirror.com(429 限流时)")
    ap.add_argument("--droid-eps", type=int, default=None, help="DROID episode 数(P5 扩规模用)")
    ap.add_argument("--bridge-eps", type=int, default=None, help="Bridge episode 数")
    args = ap.parse_args()

    os.makedirs(DATA_ROOT, exist_ok=True)
    if not args.only_big:
        for repo in SMALL:
            fetch_small(repo)
    if args.big or args.only_big:
        eps = dict(BIG_DEFAULT_EPISODES)
        if args.droid_eps:
            eps["IPEC-COMMUNITY/droid_lerobot"] = args.droid_eps
        if args.bridge_eps:
            eps["IPEC-COMMUNITY/bridge_orig_lerobot"] = args.bridge_eps
        for repo, n in eps.items():
            fetch_v2_episodes(repo, n)


if __name__ == "__main__":
    main()
