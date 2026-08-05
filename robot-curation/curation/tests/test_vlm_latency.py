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
                                          make_endstate_voter,
                                          read_latency_csv)
from curation.export.report import to_markdown


def test_wall_clock_beats_sum_when_calls_overlap():
    """墙钟(2026-07-30):首次发出 → 末次返回。并发重叠时必须 < Σ单次耗时。

    造三次重叠调用:t0 起 10s、t0+1 起 10s、t0+2 起 10s。
    Σ=30s(旧图会这么画),真实墙钟只有 12s —— 差 2.5 倍,数据集一大就是几十倍。
    """
    latency_reset()
    t0 = 1_700_000_000.0
    for i in range(3):
        latency_record("probe", 10.0, started_at=t0 + i)
    s = latency_summary()["probe"]
    assert s["wall_s"] == 12.0
    assert s["wall_s"] < s["n"] * s["mean_s"]          # 严格小于"次数×均值"
    assert s["wall_s"] >= s["max_s"]                   # 且不可能短于最慢的那一次
    # 串行(不重叠)时两者相等,是墙钟的上界而非另一套口径
    latency_reset()
    for i in range(3):
        latency_record("caption", 10.0, started_at=t0 + i * 10)
    assert latency_summary()["caption"]["wall_s"] == 30.0
    latency_reset()


def test_wall_clock_counts_failed_calls_and_needs_timestamps():
    """失败的调用也占了那段时间 → 算进墙钟;一条时刻都没有 → 干脆不给 wall_s。"""
    latency_reset()
    t0 = 1_700_000_000.0
    latency_record("probe", 5.0, started_at=t0)
    latency_record("probe", 30.0, ok=False, started_at=t0 + 2)   # 超时也占时间
    s = latency_summary()["probe"]
    assert s["n"] == 1 and s["errors"] == 1 and s["wall_s"] == 32.0
    latency_reset()
    latency_record("llm", 5.0)                                    # 没传 started_at
    assert "wall_s" not in latency_summary()["llm"]
    latency_reset()


def test_latency_csv_roundtrip_and_legacy_three_column(tmp_path):
    """CSV 读回复算:四列有墙钟;**三列老 CSV** 读得进去但没有墙钟(如实降级)。"""
    new = tmp_path / "new.csv"
    new.write_text("call_type,seconds,ok,started_at\n"
                   "probe,10.0,1,1700000000.0\n"
                   "probe,10.0,1,1700000002.0\n", encoding="utf-8")
    s = latency_summary(read_latency_csv(str(new)))["probe"]
    assert s["n"] == 2 and s["wall_s"] == 12.0
    old = tmp_path / "old.csv"                        # 2026-07-30 之前的交付
    old.write_text("call_type,seconds,ok\nprobe,10.0,1\nprobe,10.0,1\n",
                   encoding="utf-8")
    s_old = latency_summary(read_latency_csv(str(old)))["probe"]
    assert s_old["n"] == 2 and s_old["mean_s"] == 10.0
    assert "wall_s" not in s_old
    # 老 CSV 复算出的档案喂给界面 → 不画图,只给一句说明(绝不退回均值×次数)
    from curation.ui.manifest import NO_WALL_NOTE, latency_bar_html
    html = latency_bar_html({"latency": {"probe": s_old}})
    assert NO_WALL_NOTE in html and "margin:8px 0" not in html


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
        voter = make_endstate_voter(f"http://127.0.0.1:{srv.server_port}/v1", "m")
        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        voter([frame], [frame], "camera A (cam)", "task")
        s = latency_summary()
        assert "endstate" in s and s["endstate"]["n"] >= 2   # 双问法=至少两请求
        assert s["endstate"]["errors"] == 0
        # 真调用路径必须把发出时刻记上(否则界面画不出墙钟)
        assert s["endstate"]["wall_s"] >= s["endstate"]["max_s"]
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
