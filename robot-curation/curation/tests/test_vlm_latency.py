"""VLM 调用延时档案测试(2026-07-28,同事需求:定量延时进报告)。

契约:四个调用点(probe/endstate/caption/llm)各自打静态标签;
latency_summary 按标签出分位数;报告渲染成表;错误也计数不丢。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np

from curation.adapters.vlm_client import (latency_record, latency_reset,
                                          latency_rows, latency_summary,
                                          make_endstate_judge)
from curation.export.report import to_markdown


def test_summary_percentiles_and_errors():
    latency_reset()
    for i in range(1, 101):                       # 1..100 秒,分位数一眼可验
        latency_record("probe", float(i))
    latency_record("probe", 999.0, ok=False)      # 错误不进分位数,单独计数
    latency_record("caption", 2.0)
    s = latency_summary()
    p = s["probe"]
    assert p["n"] == 100 and p["errors"] == 1
    assert p["p50_s"] == 50.0 and p["p90_s"] == 90.0 and p["p99_s"] == 99.0
    assert p["max_s"] == 100.0 and abs(p["mean_s"] - 50.5) < 0.01
    assert s["caption"]["n"] == 1
    latency_reset()
    assert latency_summary() == {} and latency_rows() == []


def test_endstate_judge_records_tagged_latency():
    """真 HTTP 走一遍:二值复核的请求应落在 endstate 桶。"""
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps({"choices": [{"message": {"content": "yes"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        latency_reset()
        judge = make_endstate_judge(f"http://127.0.0.1:{srv.server_port}/v1", "m")
        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        judge([frame], [frame], "task")
        s = latency_summary()
        assert "endstate" in s and s["endstate"]["n"] >= 2   # 双问法=至少两请求
        assert s["endstate"]["errors"] == 0
    finally:
        srv.shutdown()
        latency_reset()


def test_report_renders_latency_table():
    report = {"数据集": "x", "dataset": {
        "input_episodes": 1, "hard_gate_filtered": 0, "verdict_keep": 1,
        "verdict_drop": 0, "dedup_removed": 0, "delivered": 1,
        "hard_fail_breakdown": {}, "stuck": {"flagged_episodes": 0, "note": "", "episodes": []},
        "vlm_latency": {
            "probe": {"n": 512, "errors": 0, "mean_s": 21.4, "p50_s": 20.1,
                      "p90_s": 30.2, "p99_s": 41.0, "max_s": 55.3},
            "caption": {"n": 0, "errors": 3}}},
        "episodes": {"dropped": []}, "skills": {"n_episodes": 1, "families": {}}}
    md = to_markdown(report)
    assert "## 模型调用延时" in md
    assert "渐变问询(VOC)" in md and "21.4" in md
    assert "| 画像 caption | 0 | 3 |" in md                 # 全错误的桶也可见


def test_cli_import_quiets_daft_terminal_noise(monkeypatch):
    """cli 导入即默认关闭 Daft 引擎动画/QueryID(setdefault:用户显式设 1 仍能打开)。"""
    import importlib
    import os as _os

    import curation.cli as _cli
    monkeypatch.delenv("DAFT_PROGRESS_BAR", raising=False)
    monkeypatch.delenv("DAFT_SHOW_QUERY_ID", raising=False)
    importlib.reload(_cli)
    assert _os.environ["DAFT_PROGRESS_BAR"] == "0"
    assert _os.environ["DAFT_SHOW_QUERY_ID"] == "0"
    monkeypatch.setenv("DAFT_PROGRESS_BAR", "1")       # 用户显式要看 → 不覆盖
    importlib.reload(_cli)
    assert _os.environ["DAFT_PROGRESS_BAR"] == "1"
