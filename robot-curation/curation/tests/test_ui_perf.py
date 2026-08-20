"""公网首屏的两道送分题(2026-08-20 APIG 实测):交付扫描缓存 + gzip/immutable。

实测账本(公网网关):首屏 ≈ 9 s,其中 SSE 首批回调 5.5 s = discover_deliveries
被调两遍 × 2.5 s(55 份交付在 FSX 上几百次 stat);HTML 532 KB + config 259 KB
未压缩穿网关;139 个 JS 分片没有缓存头。
"""
from __future__ import annotations

import json
import os

import pytest

from curation.ui import manifest as M


def _new_delivery(root, name):
    d = root / name / "20260820-000001"
    d.mkdir(parents=True)
    (d / "passed.json").write_text(json.dumps({"数据集": name, "episodes": {}}),
                                   encoding="utf-8")
    return str(root / name)


def test_discover_is_cached_within_ttl_but_sees_new_toplevel_immediately(
        tmp_path, monkeypatch):
    """同根 5 秒内不重扫;顶层新交付一落盘(根 mtime 变)立刻可见。"""
    root = tmp_path / "deliv"
    root.mkdir()
    a = _new_delivery(root, "a")
    calls = []
    real = os.scandir

    def spy(path):
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", spy)
    M.clear_discover_cache()
    assert M.discover_deliveries(str(root)) == [a]
    n1 = len(calls)
    assert n1 > 0
    assert M.discover_deliveries(str(root)) == [a]
    assert len(calls) == n1, "TTL 内第二次调用又扫盘了——缓存没生效"
    # 顶层新交付:根目录 mtime 变 → 缓存失效 → 立刻看见(不等 TTL)
    import time
    time.sleep(0.02)
    b = _new_delivery(root, "b")
    os.utime(str(root), None)
    assert M.discover_deliveries(str(root)) == [a, b]
    assert len(calls) > n1


def test_discover_cache_is_keyed_by_root_and_clearable(tmp_path):
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    r1.mkdir(); r2.mkdir()
    a = _new_delivery(r1, "a")
    b = _new_delivery(r2, "b")
    M.clear_discover_cache()
    assert M.discover_deliveries(str(r1)) == [a]
    assert M.discover_deliveries(str(r2)) == [b]      # 不同根不串
    M.clear_discover_cache()
    assert M.discover_deliveries(str(r1)) == [a]


def test_discover_semantics_unchanged_legacy_nested_and_incomplete(tmp_path):
    """提速不改判据:老布局(passed.json 直接在目录里)/嵌套三层/只有不完整跑批
    (没有 passed.json)的目录不算交付/找到交付不往里钻。"""
    root = tmp_path / "deliv"
    legacy = root / "old"
    legacy.mkdir(parents=True)
    (legacy / "passed.json").write_text("{}", encoding="utf-8")
    nested = _new_delivery(root / "exp" / "grp", "n1")
    incomplete = root / "broken" / "20260820-000002"
    incomplete.mkdir(parents=True)                 # 跑批目录但没有 passed.json
    (root / "n1_inner_should_not_be_scanned").mkdir()
    M.clear_discover_cache()
    found = M.discover_deliveries(str(root))
    assert str(legacy) in found and nested in found
    assert str(root / "broken") not in found
    # 交付内部不再往里钻:跑批子目录本身不会被当成交付
    assert not any(f.endswith("20260820-000001") for f in found)


# ── ASGI 层:gzip + 静态资产 immutable ─────────────────────────────────────

@pytest.fixture
def asgi_client(tmp_path, monkeypatch):
    pytest.importorskip("gradio")
    from starlette.testclient import TestClient

    # pod 上 CURATION_UI_USER/PASSWORD 常驻,不摘掉整套响应都是 401(pod 测试
    # 配置污染那一族);这里测的是压缩/缓存头,与鉴权无关
    monkeypatch.delenv("CURATION_UI_USER", raising=False)
    monkeypatch.delenv("CURATION_UI_PASSWORD", raising=False)

    from curation.ui.app import create_asgi_app
    root = tmp_path / "deliv"
    root.mkdir()
    _new_delivery(root, "d1")
    app = create_asgi_app(str(root), data_root=str(tmp_path / "data"))
    with TestClient(app) as c:
        yield c


def test_html_and_config_are_gzipped(asgi_client):
    r = asgi_client.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip", "HTML 没压缩(532 KB 裸穿网关)"
    r = asgi_client.get("/config", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip", "config 没压缩"
    assert "immutable" not in r.headers.get("cache-control", ""), \
        "动态内容不许 immutable"


def test_assets_get_immutable_cache_header(asgi_client):
    import re
    html = asgi_client.get("/").text
    m = re.search(r'/assets/[A-Za-z0-9_.-]+\.js', html)
    assert m, "HTML 里找不到前端分片引用"
    r = asgi_client.get(m.group(0))
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "public, max-age=31536000, immutable"


def test_sse_stream_is_excluded_from_gzip():
    """事件流绝不能被 gzip 缓冲:压缩缓冲会把 app.load 的结果憋到流结束。
    TestClient 拉无限流会吊死,这里钉中间件配置:starlette 的 GZipMiddleware
    默认排除 text/event-stream,且我们没覆盖这个排除表。"""
    pytest.importorskip("gradio")
    import inspect

    from starlette.middleware import gzip as _gz

    from curation.ui import app as ui_app
    assert "text/event-stream" in _gz.DEFAULT_EXCLUDED_CONTENT_TYPES
    src = inspect.getsource(ui_app.create_asgi_app)
    assert "GZipMiddleware" in src and "excluded" not in src.split("GZipMiddleware", 1)[1].split("\n", 1)[0], \
        "不许给 GZipMiddleware 自定义排除表——会把 SSE 的默认排除盖掉"


# ── 空交付根自举(2026-08-20 同事全新部署撞上的启动即退)────────────────────

def test_build_app_bootstraps_an_empty_delivery_root(tmp_path, monkeypatch):
    """交付根一份交付都没有 → 自动放占位交付,UI 照常起来;占位走 safe_write
    发布通道;再起一次不重写(幂等)。"""
    pytest.importorskip("gradio")
    from curation.export import safe_write
    from curation.ui import app as ui_app

    seen = []
    real = safe_write._publish
    monkeypatch.setattr(safe_write, "_publish",
                        lambda t, d: (seen.append(d), real(t, d))[1])
    root = tmp_path / "empty"
    root.mkdir()
    app = ui_app.build_app(str(root), data_root=str(tmp_path / "data"))
    assert app is not None
    ph = root / ui_app.WELCOME_DELIVERY / ui_app.WELCOME_RUN / "passed.json"
    assert ph.is_file(), "空根没放占位交付,UI 会启动即退(同事部署现场)"
    assert seen == [str(ph)], "占位交付没走 safe_write 发布通道"
    payload = json.loads(ph.read_text(encoding="utf-8"))
    assert "跑质检" in payload["数据集"] and payload["episodes"] == {}
    # 占位交付能被报告页正常加载(不崩、空表)
    m = M.load_delivery(str(ph.parent))
    assert not m.get("load_error")
    # 幂等:再建一次 app 不重写占位
    seen.clear()
    ui_app.build_app(str(root), data_root=str(tmp_path / "data"))
    assert seen == []


def test_build_app_fails_loudly_when_root_unwritable(tmp_path, monkeypatch):
    """占位都放不进去 = 交付根不可写的部署问题,必须响亮失败、话说清。"""
    pytest.importorskip("gradio")
    import os as _os

    from curation.ui import app as ui_app
    root = tmp_path / "ro"
    root.mkdir()
    monkeypatch.setattr(ui_app, "_bootstrap_empty_delivery",
                        lambda r: (_ for _ in ()).throw(PermissionError("ro")))
    with pytest.raises((SystemExit, PermissionError)):
        ui_app.build_app(str(root), data_root=str(tmp_path / "data"))
