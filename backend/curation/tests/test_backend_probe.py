"""模型服务探活:失败原因分三类说 + 下拉标签带原因(2026-08-21,同事把 401 当网络问题查了半天)。"""
from __future__ import annotations

import urllib.error

import pytest

from curation.adapters.vlm_client import probe_failure_reason


def _http(code):
    return urllib.error.HTTPError("https://ark/x", code, "x", {}, None)


def test_reason_distinguishes_missing_key_invalid_key_http_and_network(monkeypatch):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    r = probe_failure_reason(_http(401), "ARK_API_KEY")
    assert "密钥未配置" in r and "ARK_API_KEY" in r
    monkeypatch.setenv("ARK_API_KEY", "sk-bad")
    r = probe_failure_reason(_http(401), "ARK_API_KEY")
    assert "密钥无效" in r and "401" in r
    assert "没配 api_key_env" in probe_failure_reason(_http(403), None)
    assert "HTTP 502" in probe_failure_reason(_http(502), "ARK_API_KEY")
    assert "服务不可达(URLError)" in probe_failure_reason(urllib.error.URLError("refused"), None)
    assert "TimeoutError" in probe_failure_reason(TimeoutError(), None)


def test_backend_dropdowns_tolerate_stale_values(tmp_path, monkeypatch):
    """探活改了选项后缀后,在飞的事件可能带着旧值回来 —— 两个「模型服务」下拉必须
    allow_custom_value,否则 Gradio 6 在 preprocess 就报红(2026-08-21 7861 实见)。"""
    pytest.importorskip("gradio")
    import gradio as gr
    monkeypatch.delenv("CURATION_CONFIG", raising=False)
    from curation.ui.app import build_app
    root = tmp_path / "d"; root.mkdir()
    app = build_app(str(root), data_root=str(tmp_path / "x"))
    dds = [b for b in app.blocks.values() if isinstance(b, gr.Dropdown) and b.label == "模型服务"]
    assert len(dds) == 2 and all(d.allow_custom_value for d in dds)


def test_short_reason_for_dropdown():
    from curation.adapters.vlm_client import short_reason
    assert short_reason("密钥未配置:环境变量 ARK_API_KEY 未设置") == "密钥未配置"
    assert short_reason("密钥无效(HTTP 401):环境变量 ARK_API_KEY 的值不被接受") == "密钥无效"
    assert short_reason("需要鉴权(HTTP 403),但预设没配 api_key_env") == "需要鉴权"
    assert short_reason("服务返回 HTTP 502") == "HTTP 502"
    assert short_reason("服务不可达(URLError)") == "服务不可达"


