#!/usr/bin/env bash
# 环境搭建脚本(容器层易失,重建容器后跑这个恢复;日后演化成 P6 Dockerfile)
# 前置:nvidia/cuda:12.9.1-devel-ubuntu22.04 容器(hao-curator),/data03 挂载
set -euo pipefail

VENV=/data03/hao/venv/curation

# ① 系统依赖(Ubuntu 22.04 apt 默认 python3.10,满足 daft ≥0.7 要求)
apt-get update -qq
apt-get install -y -qq git python3.10 python3.10-venv python3-pip ffmpeg curl tmux

# ② venv 建在持久盘 /data03(容器重建不丢)
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip

# ③ 核心依赖(pin daft 0.7.x;⚠️ 永不用 main;Python 3.9 会被静默降级到 0.6.8)
"$VENV/bin/pip" install "daft[ray]>=0.7,<0.8" numpy scipy opencv-python-headless av \
  torch lerobot huggingface_hub scikit-learn pyyaml pytest matplotlib placo
# placo:M2 FK 用(lerobot RobotKinematics 的引擎);Franka URDF 见 curation/registry/fk.py
# (公开 URDF 常缺 mesh → strip_geometry() 生成纯运动学版;已验零位姿=(0.088,0,0.926))

# ④ 验证(版本坑检查)
"$VENV/bin/python" - <<'EOF'
import sys, importlib.metadata as m
assert sys.version_info >= (3, 10), f"Python 必须 ≥3.10,got {sys.version}"
assert m.version("daft") >= "0.7", f"daft 版本坑!got {m.version('daft')}(3.9 静默降级症状)"
import torch
assert torch.cuda.is_available(), "torch 看不到 CUDA"
print("✅ 环境 OK: python", sys.version.split()[0], "/ daft", m.version("daft"),
      "/ torch", m.version("torch"), "/", torch.cuda.device_count(), "GPU")
EOF

# ⑤ git 在容器里访问宿主挂载盘需要 safe.directory
git config --global --add safe.directory /data03/hao/curation-project || true

# ⑥ HF 凭证在持久盘(匿名下载会被 429;token 已由用户登录到此处,重建容器不丢)
export HF_HOME=/data03/hao/.hf_home
grep -q HF_HOME ~/.bashrc 2>/dev/null || echo 'export HF_HOME=/data03/hao/.hf_home' >> ~/.bashrc

echo "done. 激活: source $VENV/bin/activate"

# curation 包可编辑安装(任意目录可跑 CLI;容器重建后重跑本脚本即恢复)
"$VENV_CURATION/bin/pip" install -e /data03/hao/curation-project 2>/dev/null || /data03/hao/venv/curation/bin/pip install -e /data03/hao/curation-project
