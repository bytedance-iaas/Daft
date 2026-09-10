"""运行配置：全部来自环境变量，代码里不写死任何数据集、桶或路径。

  AUG_LEROBOT_SRC  源 LeRobot 数据集根（tos://... 或本地目录），视频与 parquet 从这里取，封数据集也以它为底。必填。
  AUG_OUT          产物落地的 TOS 前缀（tos://<bucket>/augment/<dataset>）。必填。
  AUG_WORK         本地工作目录（默认 ./augment_work）。
  AUG_CAMERA       要增广的相机键（默认 observation.images.front）。
  AUG_CHUNK/AUG_FILE  源数据集里该相机视频的 chunk / file 序号（默认 0 / 0，即 v3 合并布局的第一个文件）。

参考视频送方舟前的尺寸：Seedance 要求总像素 ≥ ARK_MIN_PIXELS（407696），不足时按原比例放大到刚好够。
"""
from __future__ import annotations

import math
import os
import sys
import time
from datetime import datetime

from .tos_io import TosUrl

ARK_MIN_PIXELS = 407696


def _need(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"环境变量 {name} 未设置（见 augmentation/config.py）")
    return v


def lerobot_src() -> TosUrl:
    return TosUrl.parse(_need("AUG_LEROBOT_SRC"))


def out_prefix() -> TosUrl:
    return TosUrl.parse(_need("AUG_OUT"))


def work_dir() -> str:
    return os.environ.get("AUG_WORK", os.path.abspath("augment_work"))


def camera() -> str:
    return os.environ.get("AUG_CAMERA", "observation.images.front")


def video_rel_path(cam: str | None = None) -> str:
    chunk = int(os.environ.get("AUG_CHUNK", "0")); fidx = int(os.environ.get("AUG_FILE", "0"))
    return f"videos/{cam or camera()}/chunk-{chunk:03d}/file-{fidx:03d}.mp4"


def data_rel_path() -> str:
    chunk = int(os.environ.get("AUG_CHUNK", "0")); fidx = int(os.environ.get("AUG_FILE", "0"))
    return f"data/chunk-{chunk:03d}/file-{fidx:03d}.parquet"


def send_size(width: int, height: int) -> tuple[int, int]:
    """送方舟的参考视频尺寸：像素不足 ARK_MIN_PIXELS 时等比放大到刚好够（偶数），够了就原样。"""
    if width * height >= ARK_MIN_PIXELS:
        return width, height
    s = math.sqrt(ARK_MIN_PIXELS / (width * height))
    w = int(math.ceil(width * s / 2) * 2); h = int(math.ceil(height * s / 2) * 2)
    return w, h


def log(msg: str) -> None:
    print(f"{datetime.now():%m-%d %H:%M:%S} {msg}", flush=True)
