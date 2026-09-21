"""字体离线金丝雀(2026-07-27 U2)。

背景:gradio 前端硬编码请求 fonts.googleapis.com(index.html preconnect +
Index-*.js inject_fonts),与主题配置无关;国内网络首屏挂起 15s+(实测)。
Dockerfile 里用 sed 把域名改写为相对路径(毫秒级本地 404,系统字体兜底)。

本测试 = 补丁的金丝雀:在装了 gradio 的环境(镜像/pod)里扫模板目录,
发现残留即红——升级 gradio 版本后若忘了补丁层,这里第一时间报警。
无 gradio 的环境(Mac 开发机)自动跳过。
"""
from __future__ import annotations

import os
import subprocess

import pytest


def test_gradio_templates_have_no_google_fonts():
    gradio = pytest.importorskip("gradio")
    tdir = os.path.join(os.path.dirname(gradio.__file__), "templates")
    hits = subprocess.run(
        ["grep", "-rl", r"fonts\.googleapis\.com\|fonts\.gstatic\.com", tdir],
        capture_output=True, text=True).stdout.strip()
    assert not hits, (
        f"gradio 模板仍引用 Google 字体(国内首屏会挂起):\n{hits}\n"
        "→ Dockerfile 的字体离线补丁层没生效(升级 gradio 后需确认补丁仍命中)")
