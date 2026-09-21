"""VLM 模型自动发现测试(2026-07-28,同事反馈:单模型服务报模型名是冗余)。

契约:只给 --vlm-endpoint 时——单模型服务从 GET /models 自取;
多模型服务(方舟)报错并列出候选与修法;直连层把沿袭的旧模型名清空,
绝不拿出厂 doubao 模型名去打客户端点。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from curation.adapters.vlm_client import resolve_single_model
from curation.pipeline.config import apply_vlm_direct, load_config


def _serve(models: list[str]):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"data": [{"id": m} for m in models]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1"


def test_single_model_resolved():
    srv, ep = _serve(["nvidia/Cosmos-Reason2-8B"])
    try:
        assert resolve_single_model(ep) == "nvidia/Cosmos-Reason2-8B"
    finally:
        srv.shutdown()


def test_multi_model_raises_with_candidates_and_fix():
    srv, ep = _serve(["doubao-a", "doubao-b", "doubao-c"])
    try:
        with pytest.raises(ValueError) as e:
            resolve_single_model(ep)
        msg = str(e.value)
        assert "doubao-a" in msg and "--vlm-model" in msg   # 列候选 + 给修法
    finally:
        srv.shutdown()


def test_unreachable_raises_actionable():
    with pytest.raises(ValueError) as e:
        resolve_single_model("http://127.0.0.1:1/v1", timeout_s=0.5)
    assert "--vlm-model" in str(e.value)


def test_direct_endpoint_only_clears_stale_model():
    """只给端点 → 旧模型名(出厂 doubao)必须被清空,标记待自动发现。"""
    cfg = apply_vlm_direct(load_config(None), endpoint="http://10.0.0.5:8000/v1")
    v = cfg["checks"]["task_success"]["vlm"]
    assert v["endpoint"] == "http://10.0.0.5:8000/v1"
    assert v["model"] == ""                                  # 不许残留 doubao


def test_direct_endpoint_with_model_keeps_explicit():
    cfg = apply_vlm_direct(load_config(None),
                           endpoint="http://10.0.0.5:8000/v1", model="my/model")
    assert cfg["checks"]["task_success"]["vlm"]["model"] == "my/model"
