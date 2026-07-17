#!/usr/bin/env python3
"""检测空闲 GPU(执行规定,CLAUDE.md「H200 GPU 使用规定」)。

规则:8× H200 与他人共享;某卡上已有 compute 进程就不用那张卡。
用法:
    CUDA_VISIBLE_DEVICES=$(python scripts/free_gpus.py)          # 全部空闲卡
    CUDA_VISIBLE_DEVICES=$(python scripts/free_gpus.py -n 1)     # 只要 1 张
只依赖 nvidia-smi,不依赖 venv(容器 --pid=host,看到的是全机真实占用)。
"""
from __future__ import annotations

import argparse
import subprocess
import sys


def query_free_gpus() -> list[int]:
    """返回无 compute 进程的 GPU index 列表(按 index 升序)。"""
    uuid2idx = {}
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
        check=True, capture_output=True, text=True,
    ).stdout
    for line in out.strip().splitlines():
        idx, uuid = [x.strip() for x in line.split(",")]
        uuid2idx[uuid] = int(idx)

    busy: set[int] = set()
    out = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=gpu_uuid", "--format=csv,noheader"],
        check=True, capture_output=True, text=True,
    ).stdout
    for line in out.strip().splitlines():
        uuid = line.strip()
        if uuid in uuid2idx:
            busy.add(uuid2idx[uuid])

    return sorted(i for i in uuid2idx.values() if i not in busy)


def main() -> int:
    ap = argparse.ArgumentParser(description="打印空闲 GPU index(逗号分隔,可直接喂 CUDA_VISIBLE_DEVICES)")
    ap.add_argument("-n", type=int, default=None, help="最多要几张(默认全部空闲卡)")
    args = ap.parse_args()
    free = query_free_gpus()
    if args.n is not None:
        free = free[: args.n]
    if not free:
        print("", end="")
        print("没有空闲 GPU!", file=sys.stderr)
        return 1
    print(",".join(map(str, free)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
