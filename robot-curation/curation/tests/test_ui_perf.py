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


# ── 部署感知默认值(2026-08-20,同事纯直连部署:/data/deliveries 无挂载)────────

def test_mount_backed_requires_under_mount_root_and_existing(tmp_path, monkeypatch):
    from curation.ui import runner
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    (tmp_path / "mnt" / "deliveries").mkdir(parents=True)
    assert runner.is_mount_backed(str(tmp_path / "mnt" / "deliveries"))
    assert not runner.is_mount_backed(str(tmp_path / "mnt" / "missing"))   # 不在
    (tmp_path / "data" / "deliveries").mkdir(parents=True)
    assert not runner.is_mount_backed(str(tmp_path / "data" / "deliveries"))  # 不在挂载下


def test_home_output_url_goes_direct_when_not_mounted(tmp_path, monkeypatch):
    """没挂载 + 有 TOS_BUCKET → 默认输出是桶里的直连地址,绝不把本地盘伪装成桶。"""
    from curation.ui import runner
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    monkeypatch.setenv("TOS_BUCKET", "herbucket")
    local = str(tmp_path / "data" / "deliveries")
    assert runner.home_output_url(local) == "tos://herbucket/deliveries"
    spec = runner.resolve_output_input("tos://herbucket/deliveries", local)
    assert spec["kind"] == "tos", "没挂载时默认地址必须走直连 stage_out,不许按挂载直写本地盘"
    # 挂载承载的实例行为不变
    mounted = tmp_path / "mnt" / "deliveries"
    mounted.mkdir(parents=True)
    assert runner.home_output_url(str(mounted)) == "tos://herbucket/deliveries"
    assert runner.resolve_output_input("tos://herbucket/deliveries", str(mounted))["kind"] == "mount"
    # 用户明填本地交付根仍放行(运营配置的路径,不是自由路径)
    assert runner.resolve_output_input(local, local)["kind"] == "mount"


def test_home_output_url_without_bucket_falls_back_to_path(tmp_path, monkeypatch):
    from curation.ui import runner
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    monkeypatch.delenv("TOS_BUCKET", raising=False)
    local = str(tmp_path / "data" / "deliveries")
    assert runner.home_output_url(local) == local


def test_bucket_url_goes_direct_when_synthesized_root_missing(tmp_path, monkeypatch):
    from curation.ui import runner
    monkeypatch.setenv("TOS_BUCKET", "herbucket")
    synth = {"name": "默认", "bucket": None, "tos_prefix": None,
             "datasets_path": str(tmp_path / "nope")}
    assert runner.bucket_url(synth) == "tos://herbucket/datasets"
    (tmp_path / "yes").mkdir()
    synth["datasets_path"] = str(tmp_path / "yes")
    assert runner.bucket_url(synth) == str(tmp_path / "yes")     # 目录在:原样白名单
    monkeypatch.delenv("TOS_BUCKET", raising=False)
    synth["datasets_path"] = str(tmp_path / "nope")
    assert runner.bucket_url(synth) == str(tmp_path / "nope")    # 没桶:原样(说明行会报没挂上)


def test_deployment_shape_note(tmp_path, monkeypatch):
    from curation.ui import runner
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    for d in ("deliveries", "datasets"):
        (tmp_path / "mnt" / d).mkdir(parents=True)
    monkeypatch.setenv("TOS_BUCKET", "b")
    assert runner.deployment_shape_note(str(tmp_path / "mnt" / "deliveries"),
                                        str(tmp_path / "mnt" / "datasets")) == ""
    note = runner.deployment_shape_note(str(tmp_path / "data" / "deliveries"),
                                        str(tmp_path / "mnt" / "datasets"))
    assert "未挂载" in note and "tos://b/" in note
    monkeypatch.delenv("TOS_BUCKET", raising=False)
    assert "TOS_BUCKET" in runner.deployment_shape_note(str(tmp_path / "x"), str(tmp_path / "y"))


def test_unmounted_instance_defaults_and_autolist_wiring(tmp_path, monkeypatch):
    """同事的部署形态整体过一遍:交付根在本地盘、数据集根不存在、TOS_BUCKET 有。
    两个路径框默认都是直连地址,且 app.load 上挂了自动列表(不让下拉空着等回车)。"""
    pytest.importorskip("gradio")
    import gradio as gr
    from curation.ui.app import build_app
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    monkeypatch.setenv("TOS_BUCKET", "herbucket")
    monkeypatch.delenv("CURATION_CONFIG", raising=False)
    root = tmp_path / "data" / "deliveries"
    root.mkdir(parents=True)
    app = build_app(str(root), data_root=str(tmp_path / "data" / "datasets"))
    cfg = json.loads(json.dumps(app.get_config_file(), default=str))
    vals = {c["props"].get("label"): c["props"].get("value") for c in cfg["components"]
            if c["props"].get("label") in {"数据集 TOS 路径", "输出 TOS 路径", "交付根 TOS 路径"}}
    assert vals["数据集 TOS 路径"] == "tos://herbucket/datasets"
    assert vals["输出 TOS 路径"] == "tos://herbucket/deliveries"
    assert vals["交付根 TOS 路径"] == "tos://herbucket/deliveries"
    loads = [f for f in app.fns.values()
             if any(t[1] == "load" for t in getattr(f, "targets", []))]
    names = {getattr(f.fn, "__name__", "") for f in loads}
    assert "_root_changed" in names and "_rp_root_changed" in names, \
        "没挂载时开门必须自动列一次(跑质检页数据集 + 报告页交付)"


def test_mounted_instance_keeps_old_defaults_and_no_autolist(tmp_path, monkeypatch):
    """我们自己的形态(挂载承载)逐字节不变:默认值同前、不挂自动列表。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    monkeypatch.setenv("TOS_BUCKET", "curation")
    monkeypatch.delenv("CURATION_CONFIG", raising=False)
    root = tmp_path / "mnt" / "deliveries"; root.mkdir(parents=True)
    ds = tmp_path / "mnt" / "datasets"; ds.mkdir()
    app = build_app(str(root), data_root=str(ds))
    cfg = json.loads(json.dumps(app.get_config_file(), default=str))
    vals = {c["props"].get("label"): c["props"].get("value") for c in cfg["components"]
            if c["props"].get("label") in {"数据集 TOS 路径", "输出 TOS 路径"}}
    assert vals["数据集 TOS 路径"] == str(ds)                   # 合成单桶、目录在:原样
    assert vals["输出 TOS 路径"] == "tos://curation/deliveries"  # 挂载承载写法
    loads = [getattr(f.fn, "__name__", "") for f in app.fns.values()
             if any(t[1] == "load" for t in getattr(f, "targets", []))]
    assert "_root_changed" not in loads and "_rp_root_changed" not in loads


def test_report_root_default_lists_bucket_when_not_mounted(tmp_path, monkeypatch):
    """没挂载的实例:报告页交付根默认地址 = 桶里前缀,开门/回车要**真去桶里列**,
    不许因为"等于默认值"就退回本地目录(那只会列出占位交付 welcome)。"""
    pytest.importorskip("gradio")
    from curation.ui import runner
    from curation.ui.app import build_app
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    monkeypatch.setenv("TOS_BUCKET", "herbucket")
    monkeypatch.delenv("CURATION_CONFIG", raising=False)
    calls = []
    monkeypatch.setattr(runner, "tos_list_deliveries",
                        lambda url, region=None, **k: (calls.append(url), ["d1", "d2"])[1])
    root = tmp_path / "data" / "deliveries"
    root.mkdir(parents=True)
    app = build_app(str(root), data_root=str(tmp_path / "data" / "datasets"))
    fn = next(f.fn for f in app.fns.values()
              if getattr(f.fn, "__name__", "") == "_rp_root_changed")
    upd, note = fn("tos://herbucket/deliveries", "cn-beijing")
    assert calls == ["tos://herbucket/deliveries"], "默认地址没去桶里列"
    assert [v for _l, v in upd["choices"]] == ["tos://herbucket/deliveries/d1",
                                               "tos://herbucket/deliveries/d2"]
    assert "TOS 直连" in note


def test_picker_tick_relists_bucket_in_direct_mode(tmp_path, monkeypatch):
    """切到报告页签的补扫在直连模式下要去桶里重列,不许扫本地把桶清单盖回去
    (7862 模拟实例真机抓到:切一次页签,下拉就从桶清单退回本地占位 welcome)。"""
    pytest.importorskip("gradio")
    from curation.ui import runner
    from curation.ui.app import build_app
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    monkeypatch.setenv("TOS_BUCKET", "herbucket")
    monkeypatch.delenv("CURATION_CONFIG", raising=False)
    monkeypatch.setattr(runner, "tos_list_deliveries",
                        lambda url, region=None, **k: ["d1", ".runs", "d2"][::2])
    root = tmp_path / "data" / "deliveries"
    root.mkdir(parents=True)
    app = build_app(str(root), data_root=str(tmp_path / "data" / "datasets"))
    tick = next(f.fn for f in app.fns.values()
                if getattr(f.fn, "__name__", "") == "_picker_tick")
    upd = tick("tos://herbucket/deliveries/d1", "tos://herbucket/deliveries", "cn-beijing")
    assert [l for l, _v in upd["choices"]] == ["d1", "d2"]
    assert upd["value"] == "tos://herbucket/deliveries/d1"      # 当前选中值带回去
    # 本地模式原样:挂载实例 / 明填本地根 → 扫本地
    upd2 = tick(None, str(root), "")
    assert any("welcome" in str(l) for l, _v in upd2["choices"])


def test_tos_list_deliveries_hides_dot_dirs():
    from curation.ui import runner

    class _S:
        def iter_common_prefixes(self, b, p):
            yield from [".runs", ".probe_details", "aloha-10", "debug"]
    assert runner.tos_list_deliveries("tos://bkt/deliveries", store=_S()) == ["aloha-10", "debug"]
