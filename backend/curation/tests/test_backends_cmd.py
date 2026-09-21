"""`curation backends` 子命令测试(2026-07-26)。

背景:后端多起来后(方舟+三自托管+客户自有),"我配的后端都活着吗/上面是什么模型"
成了高频问题。子命令一次点名:逐预设探活 + 列服务端模型。
契约:信息型命令恒返回 0(出厂自带 self-hosted-example 占位预设注定不可达,
以退出码报警会让健康部署天天假红);单预设失败不中断其余。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from curation.adapters.vlm_client import list_models
from curation.cli import _cmd_backends, build_parser


@pytest.fixture
def stub_server():
    """本地 OpenAI 兼容桩:/v1/models 返回两个模型;记录收到的鉴权头。"""
    seen = {"auth": None}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["auth"] = self.headers.get("Authorization")
            body = json.dumps({"data": [{"id": "stub/model-A"}, {"id": "stub/model-B"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # 静音
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/v1", seen
    srv.shutdown()


def test_list_models_returns_ids(stub_server):
    base, _ = stub_server
    assert list_models(base) == ["stub/model-A", "stub/model-B"]


def test_list_models_sends_auth_header(stub_server, monkeypatch):
    """配了 api_key_env(方舟场景)必须带 Bearer 头——探活与真调用同一套鉴权。"""
    base, seen = stub_server
    monkeypatch.setenv("FAKE_KEY_ENV", "sk-test-123")
    list_models(base, api_key_env="FAKE_KEY_ENV")
    assert seen["auth"] == "Bearer sk-test-123"


def test_list_models_raises_on_unreachable():
    with pytest.raises(Exception):
        list_models("http://127.0.0.1:1/v1", timeout_s=0.5)   # 端口 1 必拒绝


def test_cmd_backends_reports_ok_and_down(tmp_path, capsys, stub_server, monkeypatch):
    """混合场景:可达预设列模型,不可达标 DOWN,互不影响,恒返回 0。"""
    base, _ = stub_server
    import yaml
    site = tmp_path / "site.yaml"
    site.write_text(yaml.safe_dump({"vlm_backends": {
        "alive": {"endpoint": base, "model": "x"},
        "dead": {"endpoint": "http://127.0.0.1:1/v1", "model": "y"},
    }}))
    rc = _cmd_backends(str(site), timeout=0.5)
    out = capsys.readouterr().out
    assert rc == 0                                       # 信息型,恒 0
    assert "alive" in out and "stub/model-A" in out      # 可达:列出模型
    assert "dead" in out and "不可达" in out              # 不可达:标注但不中断
    assert "ark" in out                                  # 出厂预设也在(叠加语义)


def test_parser_wires_backends():
    args = build_parser().parse_args(["backends", "--timeout", "2"])
    assert args.command == "backends" and args.timeout == 2.0
