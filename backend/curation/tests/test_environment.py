"""P0 环境验收:版本坑 / CUDA / 系统工具 / GPU 空闲检测。"""
from __future__ import annotations

import importlib.metadata as md
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_python_version():
    # 3.9 会被 pip 静默降级到 daft 0.6.8 —— 版本坑第一道防线
    assert sys.version_info >= (3, 10), f"Python 必须 ≥3.10,got {sys.version}"


def test_daft_version_pinned():
    v = md.version("daft")
    assert v >= "0.7", f"daft 版本坑!got {v}(3.9 静默降级症状)"
    assert v < "0.8", f"daft 未 pin 在 0.7.x:{v}"


def test_core_imports():
    import cv2      # noqa: F401  opencv-python-headless
    import scipy    # noqa: F401
    import av       # noqa: F401
    import daft     # noqa: F401
    import yaml     # noqa: F401


def test_torch_cuda():
    import torch

    assert torch.cuda.is_available(), "torch 看不到 CUDA"
    assert "H200" in torch.cuda.get_device_name(0)


def test_ffmpeg_present():
    # lerobot 视频解码依赖
    assert shutil.which("ffmpeg"), "ffmpeg 不在 PATH(apt install ffmpeg)"


def test_free_gpus_helper():
    """GPU 使用规定:检测脚本可用,输出是合法 index 列表,且不含有进程的卡。"""
    script = PROJECT_ROOT / "scripts" / "free_gpus.py"
    out = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, check=True
    ).stdout.strip()
    free = [int(x) for x in out.split(",")]  # 非法输出会在此抛错
    assert free, "无空闲 GPU(或解析失败)"
    assert all(0 <= i <= 7 for i in free)

    # 有 compute 进程的卡绝不出现在空闲列表里
    busy_uuids = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=gpu_uuid", "--format=csv,noheader"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    if busy_uuids and busy_uuids != [""]:
        idx_of = {}
        for line in subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines():
            idx, uuid = [x.strip() for x in line.split(",")]
            idx_of[uuid] = int(idx)
        busy = {idx_of[u.strip()] for u in busy_uuids if u.strip() in idx_of}
        assert not (busy & set(free)), f"空闲列表混入了占用卡: {busy & set(free)}"
