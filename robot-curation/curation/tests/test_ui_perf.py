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
    assert runner.bucket_url(synth) == ""    # 没桶+目录不在:留空(rerun 侧裸进入,2026-08-26)
    synth["datasets_path"] = "tos://mybkt/ds"
    assert runner.bucket_url(synth) == "tos://mybkt/ds"          # 显式 tos:// 原样保留


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
            if c["props"].get("label") in {"数据集目录", "交付目录", "交付目录"}}
    assert vals["数据集目录"] == "tos://herbucket/datasets"
    assert vals["交付目录"] == "tos://herbucket/deliveries"
    assert vals["交付目录"] == "tos://herbucket/deliveries"
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
            if c["props"].get("label") in {"数据集目录", "交付目录"}}
    assert vals["数据集目录"] == str(ds)                   # 合成单桶、目录在:原样
    assert vals["交付目录"] == "tos://curation/deliveries"  # 挂载承载写法
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
    upd, note, _rgu = fn("tos://herbucket/deliveries", "cn-beijing")
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
    # ⚠️ 补扫**不许回写 value**(2026-08-25 复盘 ③):cur 是事件发起时的前端快照,
    # 兜底 Timer 的迟到响应会带着旧值把用户刚选的交付拽回去(显示与内容脱节,实测)。
    # 只换 choices,选中值靠"不动它"保留;cur 只用于把自定义路径补进选项列表。
    assert "value" not in upd
    # 本地模式原样:挂载实例 / 明填本地根 → 扫本地
    upd2 = tick(None, str(root), "")
    assert any("welcome" in str(l) for l, _v in upd2["choices"])


def test_tos_list_deliveries_hides_dot_dirs():
    from curation.ui import runner

    class _S:
        def iter_common_prefixes(self, b, p):
            yield from [".runs", ".probe_details", "aloha-10", "debug"]
    assert runner.tos_list_deliveries("tos://bkt/deliveries", store=_S()) == ["aloha-10", "debug"]


def test_radio_groups_are_one_frame_not_per_option_pills():
    """issue #54 / #59-1:单选(多选)组按 Arco——选项不各自成框,整组一个框。
    钉 CSS 形状:组容器 .wrap 有边框,选项 label 去边框。"""
    from curation.ui.app import _ARCO_CSS
    css = "".join(_ARCO_CSS.split())
    assert '.wrap:has(>label>input[type="radio"])' in css
    grp = css.split('.wrap:has(>label>input[type="radio"])', 1)[1]
    assert "border:1pxsolidvar(--arco-border)!important" in grp.split("}", 1)[0]
    opt = css.split('.wrap>label:has(>input[type="radio"])', 1)[1].split("}", 1)[0]
    assert "border:none!important" in opt and "background:transparent!important" in opt


def test_scan_radio_tips_cover_full_and_quick():
    """「完整质检」与「快速质检」都带悬停问号(2026-08-20 用户要);提示按实际流水线写,
    加一项只改 SCAN_TIPS。页面 head 里注入的是整张表,不再是单个名字。"""
    pytest.importorskip("gradio")
    from curation.ui import app as ui_app
    assert set(ui_app.SCAN_TIPS) == {ui_app.FULL_SCAN, ui_app.QUICK_SCAN}
    full = ui_app.SCAN_TIPS[ui_app.FULL_SCAN]
    for kw in ("时间戳", "运动学", "运动质量", "视觉质量", "同步", "任务成败", "去重", "技能"):
        assert kw in full, f"完整质检提示漏了「{kw}」"
    head = ui_app.presentation()["head"]
    # 注入的是合并表:静态两项 + 数据来源一项(2026-08-28,键=source_label())
    from curation.ingest import public_catalog
    for key, tip in {**ui_app.SCAN_TIPS,
                     public_catalog.source_label():
                         ui_app.PUBLIC_SOURCE_TIP}.items():
        assert json.dumps(key, ensure_ascii=False) in head, f"问号表缺键「{key}」"
        assert json.dumps(tip, ensure_ascii=False) in head, f"问号表缺「{key}」的文案"
    assert "不是完整的镜像站" in ui_app.PUBLIC_SOURCE_TIP   # 同事点名的预期管理
    assert "__TIPS__" not in head and "__NAME__" not in head


def test_data_sig_tracks_file_changes(tmp_path):
    """数据指纹:关键文件一动就变,不动就稳定;缺文件按 (0,0) 记,从无到有也算变化。"""
    from curation.ui.manifest import data_sig
    run = tmp_path / "d" / "20260825-000000"
    run.mkdir(parents=True)
    (run / "passed.json").write_text("{}")
    s1 = data_sig(str(run), str(tmp_path / "d"))
    assert s1 == data_sig(str(run), str(tmp_path / "d"))     # 稳定
    import os
    os.utime(run / "passed.json", ns=(1, 1))
    s2 = data_sig(str(run), str(tmp_path / "d"))
    assert s2 != s1                                          # mtime 变 → 指纹变
    hd = tmp_path / "d" / "human-decisions"
    hd.mkdir()
    (hd / "task_verdicts.csv").write_text("episode_id\n")
    assert data_sig(str(run), str(tmp_path / "d")) != s2     # 裁决 CSV 出现 → 变


def test_report_tab_reloads_when_data_changed_on_disk(tmp_path):
    """切回报告页要能发现盘上数据变了(2026-08-25 复盘 ②):存在挂在事件上的
    _stale_reload,指纹没变全 no-op,变了返回整套重载(与 _load 同一批输出)。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    root = tmp_path / "deliveries"
    root.mkdir()
    app = build_app(str(root), data_root=str(tmp_path / "datasets"))
    fns = [f.fn for f in app.fns.values()
           if getattr(f.fn, "__name__", "") == "_stale_reload"]
    assert fns, "报告页没接失效重载 —— CLI rejudge 后页面只能 ⌘R 才更新"
    reload_fn = fns[0]
    # 空 state(还没打开任何交付)→ 全 no-op,不抛
    out = reload_fn(None)
    assert isinstance(out, tuple) and out[0] is None


def _mk_delivery(root, name, run, marker_files=("passed.json",)):
    d = root / name / run
    d.mkdir(parents=True)
    for f in marker_files:
        (d / f).write_text('{"episodes": {}}')
    return root / name


def test_most_recent_delivery_wins_by_latest_run(tmp_path):
    """复盘 ⑥:报告页默认选**最近跑过**的交付,不是字母序第一个。"""
    from curation.ui.manifest import most_recent_delivery
    root = tmp_path
    _mk_delivery(root, "aloha-10", "20260701-000000")
    newest = _mk_delivery(root, "zz-later-name-but-old", "20260702-000000")
    newest2 = _mk_delivery(root, "droid-50-guide", "20260825-015215")
    got = most_recent_delivery(str(root), [str(root / n) for n in
                                           ("aloha-10", "droid-50-guide",
                                            "zz-later-name-but-old")])
    assert got == str(newest2)
    del newest


def test_report_picker_defaults_to_most_recent(tmp_path):
    """⑥ 接线:build_app 后 picker 的初值 = 最近跑批的交付(不是 choices[0])。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    root = tmp_path / "deliveries"
    _mk_delivery(root, "aaa-old", "20260101-000000")
    fresh = _mk_delivery(root, "zzz-fresh", "20260825-120000")
    app = build_app(str(root), data_root=str(tmp_path / "datasets"))
    pickers = [b for b in app.blocks.values()
               if b.__class__.__name__ == "Dropdown"
               and getattr(b, "label", "") == "交付名"]   # 跑质检页有同名 Textbox,按类型滤
    assert pickers and pickers[0].value == str(fresh)


def test_queue_tables_tall_enough_to_avoid_scroll_hijack(tmp_path):
    """复盘 ⑨:两张裁决队列表 max_height ≥ 980 —— 420 时鼠标悬在表上滚轮只滚
    表内,页面滚动被劫持;典型队列十几二十条本可整表放下。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    root = tmp_path / "deliveries"
    _mk_delivery(root, "d1", "20260825-000000")
    app = build_app(str(root), data_root=str(tmp_path / "datasets"))
    hs = {getattr(b, "elem_id", ""): getattr(b, "max_height", None)
          for b in app.blocks.values()
          if getattr(b, "elem_id", "") in ("audit-queue", "appeal-queue")}
    assert hs.get("audit-queue", 0) >= 980 and hs.get("appeal-queue", 0) >= 980


def test_backend_dropdown_announces_probing_then_probe_clears_it(tmp_path):
    """复盘 ⑤:模型服务下拉首屏 info=「正在检测…」(探活要出网 1-4s,空框不说话
    像坏了);_do_probe 的更新把 info 摘掉(结果已缀在选项上)。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    root = tmp_path / "deliveries"
    _mk_delivery(root, "d1", "20260825-000000")
    app = build_app(str(root), data_root=str(tmp_path / "datasets"))
    dds = [b for b in app.blocks.values()
           if b.__class__.__name__ == "Dropdown"
           and getattr(b, "label", "") == "模型服务"]
    assert dds and any("正在检测" in str(getattr(b, "info", "")) for b in dds)
    probe = [f.fn for f in app.fns.values()
             if getattr(f.fn, "__name__", "") == "_do_probe"]
    assert probe
    upd = probe[0](None, None)
    # 摘「正在检测」必须用空串:gradio 6.9 把 update(info=None) 当"此字段
    # 不更新",None 摘不掉(2026-08-27 用户截图抓出,修复即此断言)
    assert upd[0].get("info", "sentinel") == ""


def test_adjudication_queue_title_and_status_radio(tmp_path, monkeypatch):
    """人工裁决队列(2026-08-25 用户改名)+ 两组筛选(问题类型 × 状态)——
    表是台账,裁决执行后条目留底可见;两组选项带计数、随交付在 _load 里
    动态重建(构建期为空,与 mg_filter 同款),elem_id 挂着药丸样式。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    monkeypatch.delenv("CURATION_CONFIG", raising=False)
    root = tmp_path / "deliveries"; root.mkdir()
    app = build_app(str(root), data_root=str(tmp_path / "data"))
    cfg = json.loads(json.dumps(app.get_config_file(), default=str))
    html = " ".join(str(c["props"].get("value", ""))
                    for c in cfg["components"] if c["type"] == "html")
    assert "人工裁决队列" in html and "待裁决队列" not in html
    radios = [c["props"] for c in cfg["components"] if c["type"] == "radio"]
    by_id = {r.get("elem_id"): r for r in radios}
    assert by_id["mg-status"]["label"] == "状态"
    assert by_id["mg-filter"]["label"] == "问题类型"     # 长解释句已删(要简洁)
    assert by_id["mg-status"]["choices"] == []           # 选项随交付在 _load 重建


def test_rerun_shape_dead_path_default_left_empty(tmp_path, monkeypatch):
    """rerun 侧的部署形态(2026-08-26 实测):helm 传了 --data-root /data/datasets
    而 pod 里没这目录、TOS_BUCKET 也没有 → 裸进入「数据集目录」不许糊死路径,
    值留空、placeholder 说话;深链填框逻辑独立不受影响。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    monkeypatch.delenv("TOS_BUCKET", raising=False)
    monkeypatch.delenv("CURATION_TOS_MOUNT", raising=False)
    monkeypatch.delenv("CURATION_CONFIG", raising=False)
    monkeypatch.delenv("CURATION_DATA_ROOT", raising=False)
    root = tmp_path / "deliveries"
    root.mkdir()
    app = build_app(str(root), data_root=str(tmp_path / "no" / "such" / "dir"))
    cfg = json.loads(json.dumps(app.get_config_file(), default=str))
    tin = next(c["props"] for c in cfg["components"]
               if c["props"].get("label") == "数据集目录")
    assert tin.get("value") in ("", None), \
        f"死路径被糊进默认值:{tin.get('value')!r}"
    assert tin.get("placeholder"), "值留空后 placeholder 必须在,不然框空得莫名其妙"
    # 三态说明行同形态配套(2026-08-27 用户实报):留空形态不许拿内部死路径
    # 报「没挂上,请检查部署」,要给指导语
    note = next(c["props"] for c in cfg["components"]
                if c["props"].get("elem_id") == "rn-ds-note")
    assert "没挂上" not in str(note.get("value", "")), "留空形态报了部署事故警告"
    assert "tos://" in str(note.get("value", "")), "留空形态该给填写指导语"


def test_rp_root_refresh_keeps_valid_selection(tmp_path, monkeypatch):
    """交付下拉刷新保值(2026-08-27 用户实报:点一下交付目录框再点走,
    选中的交付名"消失"):重列后现值仍有效就保住,失效才清。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    monkeypatch.delenv("CURATION_CONFIG", raising=False)
    root = tmp_path / "deliv"
    root.mkdir()
    a = _new_delivery(root, "keepme")
    app = build_app(str(root), data_root=str(tmp_path / "data"))
    fns = [f.fn for f in app.fns.values()
           if getattr(f.fn, "__name__", "") == "_rp_root_changed"]
    assert fns
    upd, _note, _rgu = fns[0]("", "", cur=a)
    assert upd.get("value") == a, "现值仍在选项里,却被清掉了"
    upd2, _, _ = fns[0]("", "", cur=str(root / "gone"))
    assert upd2.get("value") is None, "失效的值必须清"


def test_bucket_mapped_source_fallback(tmp_path, monkeypatch):
    """源数据集路径跨机回退(2026-08-27 rerun 侧实报):挂载实例产的交付记
    /mnt/tos/... 本地路径,直连实例打开时按 .tos-origin.json 映射回桶,
    验证 meta/info.json 存在才用;验不过 / 没 origin → 维持"找不到"。"""
    import json as _json

    from curation.ui import runner

    droot = tmp_path / "cache" / "droid-50"
    run = droot / "20260825-015215"
    run.mkdir(parents=True)
    (run / "passed.json").write_text(_json.dumps(
        {"episodes": {}, "源数据集路径": "/mnt/tos/datasets/no-such-ds-portability-test"}),
        encoding="utf-8")
    (droot / "latest").write_text("20260825-015215", encoding="utf-8")
    monkeypatch.setenv("CURATION_TOS_MOUNT", "/mnt/tos")

    # 没 origin:找不到(不猜)
    assert runner.source_dataset_of(str(droot)) is None

    (droot / runner.TOS_ORIGIN_NAME).write_text(_json.dumps(
        {"delivery_url": "tos://curation/deliveries/droid-50-guide",
         "run": "20260825-015215", "region": "cn-beijing"}), encoding="utf-8")
    from curation.ingest import dsfs
    monkeypatch.setattr(dsfs, "exists", lambda p: "no-such-ds-portability-test" in str(p))
    assert (runner.source_dataset_of(str(droot))
            == "tos://curation/datasets/no-such-ds-portability-test")
    # 桶里验不过 → 维持找不到
    monkeypatch.setattr(dsfs, "exists", lambda p: False)
    assert runner.source_dataset_of(str(droot)) is None


def test_source_video_lane_speaks_tos(tmp_path, monkeypatch):
    """轨迹页视频源档跨机可移植(2026-08-27 rerun 侧实报:所有条目都没视频):
    交付内视频被懒镜像跳过、本机没有源目录时,按交付记录的源路径 + origin
    桶映射走 tos://,文件经 dsfs 列举、播放走预签名 https。"""
    import json as _json

    from curation.ingest import dsfs
    from curation.ui import manifest as M

    droot = tmp_path / "cache" / "droid-50"
    run = droot / "20260825-015215"
    run.mkdir(parents=True)
    (run / "passed.json").write_text(_json.dumps(
        {"episodes": {}, "源数据集路径": "/mnt/tos/datasets/no-such-src"}),
        encoding="utf-8")
    (droot / "latest").write_text("20260825-015215", encoding="utf-8")
    (droot / ".tos-origin.json").write_text(_json.dumps(
        {"delivery_url": "tos://curation/deliveries/droid-50-guide"}),
        encoding="utf-8")
    monkeypatch.setenv("CURATION_TOS_MOUNT", "/mnt/tos")
    monkeypatch.setattr(dsfs, "exists", lambda p: True)
    monkeypatch.setattr(dsfs, "read_json",
                        lambda p: {"codebase_version": "v2.0"})
    monkeypatch.setattr(dsfs, "glob", lambda pat: [
        "tos://curation/datasets/no-such-src/videos/chunk-000/cam_a/episode_000003.mp4"])
    m = {"path": str(run)}
    paths = M.source_video_paths(m, "ep000003", data_root=None)
    assert paths == ["tos://curation/datasets/no-such-src/videos/chunk-000/"
                     "cam_a/episode_000003.mp4"]
    # _lane:远端免探测,按能播摆槽位
    lane = M._lane(paths[0], "ep000003", M.VIDEO_SOURCE_SOURCE)
    assert lane["playable"] is True and lane["camera"]
    # _file_url:tos:// → 预签名 https 直连桶
    monkeypatch.setattr(dsfs, "browser_media_source",
                        lambda p: "https://signed.example/" + p.split("/")[-1])
    assert M._file_url(paths[0]).startswith("https://signed.example/")


def test_delivery_records_portable_source_path(monkeypatch):
    """写端根治(2026-08-27):挂载实例产交付,「源数据集路径」记 tos:// 规范
    坐标而不是产地挂载路径;非挂载路径与 tos:// 输入原样。"""
    from curation.pipeline.run import _portable_source_path

    monkeypatch.setenv("CURATION_TOS_MOUNT", "/mnt/tos")
    monkeypatch.setenv("TOS_BUCKET", "curation")
    assert (_portable_source_path("/mnt/tos/datasets/droid_lerobot")
            == "tos://curation/datasets/droid_lerobot")
    assert _portable_source_path("tos://b/x") == "tos://b/x"
    assert _portable_source_path("/data/other") == "/data/other"
    monkeypatch.delenv("TOS_BUCKET")
    assert (_portable_source_path("/mnt/tos/datasets/x")
            == "/mnt/tos/datasets/x")   # 不知道桶名就不硬猜


def test_droid_100_semantics_profile_matches_by_name():
    """元数据残缺数据集按目录名认领语义档案(2026-08-27):官方 droid_100 的
    robot_type=unknown、names=motor_*,若不按名认领会被当关节角误杀运动学。"""
    from curation.ingest.dataset_semantics import resolve_semantics

    info = {"codebase_version": "v3.0", "robot_type": "unknown",
            "features": {"action": {"shape": [7], "names": {
                "motors": [f"motor_{i}" for i in range(7)]}}}}
    sem = resolve_semantics(info, None, "droid_100")
    assert sem.source == "profile" and sem.profile_name == "droid_100.yaml"
    assert sem.action_space == "ee" and sem.proprio_space == "ee"
    # 不带名字 / 别的名字:不误配
    assert resolve_semantics(info, None).source == "inferred"
    assert resolve_semantics(info, None, "droid_999").source == "inferred"


def test_v3_time_window_lane(tmp_path, monkeypatch):
    """v3 时间窗直播(2026-08-27 用户定'立即修'):合并 mp4 免审片站预切,
    源档/交付档给 `路径#t=from,to`,播放 URL 保片段,拖动钳制属性上标签。"""
    pd = pytest.importorskip("pandas")
    from curation.ingest import dsfs, lerobot_reader
    from curation.ui import manifest as M

    meta = pd.DataFrame([{
        "episode_index": 3,
        "videos/observation.images.cam_a/chunk_index": 0,
        "videos/observation.images.cam_a/file_index": 0,
        "videos/observation.images.cam_a/from_timestamp": 12.5,
        "videos/observation.images.cam_a/to_timestamp": 18.25,
    }])
    monkeypatch.setattr(lerobot_reader, "_load_episodes_meta", lambda d: meta)
    monkeypatch.setattr(dsfs, "read_json", lambda p: {
        "codebase_version": "v3.0",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
        "features": {"observation.images.cam_a": {"dtype": "video"}}})
    M._V3_META_CACHE.clear()
    root = "tos://somebucket/datasets/x"
    wins = M._v3_episode_windows(root, 3)
    assert wins == [root + "/videos/observation.images.cam_a/chunk-000/"
                    "file-000.mp4#t=12.500,18.250"]
    assert M._v3_episode_windows(root, 99) == []      # 查无此条不猜

    # _lane:v3 路径的相机名取 videos/<cam>/ 段;远端免探;path 保片段
    lane = M._lane(wins[0], "ep000003", M.VIDEO_SOURCE_SOURCE)
    assert lane["playable"] is True and "cam a" in lane["camera"].lower().replace("_", " ")
    assert lane["path"].endswith("#t=12.500,18.250")

    # _file_url:远端 → 预签名 + 片段;本地 → gradio 文件 URL + 片段
    monkeypatch.setattr(dsfs, "browser_media_source", lambda p: "https://signed/" + p.split("/")[-1])
    u = M._file_url(wins[0])
    assert u.startswith("https://signed/") and u.endswith("#t=12.500,18.250")
    local = tmp_path / "file-000.mp4"
    local.write_bytes(b"x")
    u2 = M._file_url(str(local) + "#t=1.000,2.000")
    assert u2.startswith("/gradio_api/file=") and u2.endswith("#t=1.000,2.000")
    assert "%23" not in u2, "片段号被 URL 转义吞掉了"

    # 视频标签:钳制属性 + 片段说明,& 字符禁入属性
    attrs = M._window_attrs(wins[0])
    assert 'data-t0="12.500"' in attrs and "onseeking=" in attrs and "&" not in attrs
    assert "12.5–18.2" in M._window_caption(wins[0]).replace("&#183;", "·") or \
           "片段" in M._window_caption(wins[0])


def test_embodiment_suggestion_engine(monkeypatch):
    """机器人型号推荐两层(2026-08-27):档案登记直接置顶;试穿层维度+值域,
    谁都不像给空表绝不硬猜。"""
    np = pytest.importorskip("numpy")
    from curation.ingest import dsfs
    from curation.ui import runner

    # 档案层:droid_100 档案登记 franka
    monkeypatch.setattr(dsfs, "read_json", lambda p: {
        "codebase_version": "v3.0", "robot_type": "unknown",
        "features": {"action": {"shape": [7], "names": {
            "motors": [f"motor_{i}" for i in range(7)]}}}})
    sug = runner.suggest_embodiments("tos://b/dataset", "droid_100")
    assert sug and sug[0]["id"] == "franka" and "档案" in sug[0]["reason"]

    # 试穿层:EE 位姿样本(xyz ~0.5m)→ franka 臂展命中;7 维筛掉 6/14 dof
    class _T:
        columns = ["observation.state"]
        def __getitem__(self, k):
            class _S:
                def head(self, n): return self
                def to_list(self):
                    return [[0.5, 0.1, 0.4, 3.0, 0.2, 0.3, 0.5]] * 50
            return _S()
    monkeypatch.setattr(dsfs, "glob", lambda p: ["x.parquet"])
    monkeypatch.setattr(dsfs, "read_parquet", lambda p: _T())
    got = runner._tryon_embodiments("whatever")
    assert any(x["id"] == "franka" for x in got), got
    assert all(x["id"] not in ("so100", "aloha") for x in got)   # 维度不符不入围

    # 谁都不像:3 维怪数据 → 空表
    class _T3(_T):
        def __getitem__(self, k):
            class _S:
                def head(self, n): return self
                def to_list(self): return [[9.9, 9.9, 9.9]] * 50
            return _S()
    monkeypatch.setattr(dsfs, "read_parquet", lambda p: _T3())
    assert runner._tryon_embodiments("whatever") == []


def test_embodiment_ask_md_and_robot_type(monkeypatch):
    from curation.ingest import dsfs
    from curation.ui import runner

    md = runner.embodiment_ask_md("droid_100", [{"id": "franka", "reason": "试穿"}])
    assert "droid_100" in md and "franka" in md and "跳过运动学" in md
    assert "没有登记机器人型号" in runner.embodiment_ask_md("x", [])
    monkeypatch.setattr(dsfs, "read_json", lambda p: {"robot_type": "unknown"})
    assert runner.dataset_robot_type("tos://b/d", "x") == "unknown"
    monkeypatch.setattr(dsfs, "read_json",
                        lambda p: (_ for _ in ()).throw(OSError("x")))
    assert runner.dataset_robot_type("tos://b/d", "x") == ""


def test_preflight_pops_embodiment_ask_for_unknown_robot(tmp_path, monkeypatch):
    """robot_type 没登记且表单没填型号 → 开跑前弹「机器人型号」模态,
    疑似型号预选;不是十条 traceback 灌日志(2026-08-27 用户实报)。"""
    import json as _json

    pytest.importorskip("gradio")
    from curation.ui.app import build_app

    deliv = tmp_path / "deliveries"
    (deliv / "d1" / "20260827-000001").mkdir(parents=True)
    (deliv / "d1" / "20260827-000001" / "passed.json").write_text(
        _json.dumps({"数据集": "d1", "episodes": {}}), encoding="utf-8")
    ds = tmp_path / "data" / "mystery"
    (ds / "meta").mkdir(parents=True)
    (ds / "meta" / "info.json").write_text(
        _json.dumps({"robot_type": "unknown"}), encoding="utf-8")
    app = build_app(str(deliv), data_root=str(tmp_path / "data"))
    fns = [f.fn for f in app.fns.values()
           if getattr(f.fn, "__name__", "") == "_run_preflight"]
    out = fns[0](str(tmp_path / "data"), "", str(deliv), "", ["mystery"],
                 "newname", "", [], "", None, "", None,
                 "", "", None, None, None, None, "", False, False)
    import json as _j
    flat = _j.dumps([str(x) for x in out], ensure_ascii=False)
    assert "没有登记机器人型号" in flat, "该弹型号追问没弹"
    assert "'visible': True" in flat or '"visible": true' in flat.lower()
    # 弹型号框的同一响应里,切片框(没开着)只许空更新,见 bad-name 用例的注释
    assert "'visible': False" not in flat, "切片框没开着,不许发 visible=False"


def test_cli_interactive_preflight(tmp_path, monkeypatch):
    """CLI 的两问(2026-08-27 用户定):TTY 下问型号与 v3 切分;非 TTY 绝不问
    (UI 任务台子进程停下等键盘=任务吊死)。"""
    import json as _json
    import sys as _sys
    import types

    from curation import cli

    ds = tmp_path / "mystery"
    (ds / "meta").mkdir(parents=True)
    (ds / "meta" / "info.json").write_text(_json.dumps(
        {"robot_type": "unknown", "codebase_version": "v3.0"}), encoding="utf-8")
    args = types.SimpleNamespace(input=str(ds), output=str(tmp_path / "out"),
                                 embodiment_id=None, only=None, skip=None,
                                 batch=False)
    # 非 TTY:一个问题都不问,args 原样
    monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
    assert cli._interactive_run_preflight(args) is None
    assert args.embodiment_id is None and args.skip is None
    # TTY:回车吃疑似型号(monkeypatch 推荐器),y 切分
    monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(_sys.stdout, "isatty", lambda: True)
    from curation.ui import runner as _r
    monkeypatch.setattr(_r, "suggest_embodiments",
                        lambda root, name: [{"id": "franka", "reason": "试穿"}])
    answers = iter(["", "y"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    clip_out = cli._interactive_run_preflight(args)
    assert args.embodiment_id == "franka"
    assert clip_out and clip_out.endswith("/review/mystery")
    # TTY + skip:跳过运动学
    args2 = types.SimpleNamespace(input=str(ds), output="tos://b/deliveries/x",
                                  embodiment_id=None, only=None, skip=None,
                                  batch=False)
    answers2 = iter(["skip", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers2))
    assert cli._interactive_run_preflight(args2) is None
    assert args2.skip == "kinematic_limits" and args2.embodiment_id is None


def test_preflight_rejects_bad_name_before_any_modal(tmp_path, monkeypatch):
    """交付名非法 → 弹任何模态之前拦下(2026-08-27 用户实见:名字错却先弹
    切片框,答完才报错,人被锁在框里);两个模态都有「取消」退路。"""
    import json as _json

    pytest.importorskip("gradio")
    import gradio as gr
    from curation.ui.app import build_app

    deliv = tmp_path / "deliveries"
    (deliv / "d1" / "20260827-000001").mkdir(parents=True)
    (deliv / "d1" / "20260827-000001" / "passed.json").write_text(
        _json.dumps({"数据集": "d1", "episodes": {}}), encoding="utf-8")
    ds = tmp_path / "data" / "mystery"
    (ds / "meta").mkdir(parents=True)
    (ds / "meta" / "info.json").write_text(_json.dumps(
        {"robot_type": "unknown", "codebase_version": "v3.0"}), encoding="utf-8")
    app = build_app(str(deliv), data_root=str(tmp_path / "data"))
    fns = [f.fn for f in app.fns.values()
           if getattr(f.fn, "__name__", "") == "_run_preflight"]
    out = fns[0](str(tmp_path / "data"), "", str(deliv), "", ["mystery"],
                 "test=_0827", "", [], "", None, "", None,
                 "", "", None, None, None, None, "", False, False)
    import json as _j
    flat = _j.dumps([str(x) for x in out], ensure_ascii=False)
    assert "⚠️" in flat and "交付名只能用" in flat, \
        "报错第一个词必须点名「交付名」(2026-08-27 用户:只说'名字'不知改哪个框)"
    assert "'visible': True" not in flat, "非法名不许弹任何模态"
    # gradio 6.9:给从未挂载的隐藏容器发过 visible=False,它下一次 visible=True
    # 会挂载成隐藏态(2026-08-28 用户实见"第一次点没反应") —— 没开着的模态
    # 只许发 gr.update() 空更新
    assert "'visible': False" not in flat, "没开着的模态不许发 visible=False"
    # 报错要贴在「交付名」框下方的说明位(红字),不能只落远处的任务区
    assert "note-err" in str(out[-1]) and "交付名" in str(out[-1]), \
        "字段下方要出现红字报错(任务区太远,用户注意不到)"
    # 取消按钮存在
    labels = [b.value for b in app.blocks.values() if isinstance(b, gr.Button)]
    assert labels.count("返回") >= 2, "两个模态都要有返回退路"


def test_module_pick_change_does_not_rerender_itself(tmp_path):
    """自选模块里每勾一个,勾选框闪一下(2026-08-27 用户实见):勾选自身的
    change 回调不许把 visible= 回写给勾选框/只跑跳过——可见性由模式单选独占。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app

    (tmp_path / "data").mkdir()
    app = build_app(str(tmp_path / "deliveries"),
                    data_root=str(tmp_path / "data"))
    picks_fns = [f for f in app.fns.values()
                 if getattr(f.fn, "__name__", "") == "_tk_picks"]
    assert len(picks_fns) == 2, "勾选与只跑跳过都该走 _tk_picks"
    for f in picks_fns:
        out_labels = [getattr(c, "label", "") for c in f.outputs]
        assert "要跑的模块" not in out_labels, \
            "勾选回调把勾选框列为输出=每勾一下重渲染闪一次"
        for got in f.fn("自选模块", ["visual_quality"], "只跑选中"):
            assert "visible" not in str(got), "勾选变化不许动可见性"
    mode_fns = [f for f in app.fns.values()
                if getattr(f.fn, "__name__", "") == "_tk_mode"]
    assert mode_fns, "模式单选的回调还在"
    assert "要跑的模块" in [getattr(c, "label", "")
                           for c in mode_fns[0].outputs], \
        "模式切换仍要负责勾选框的显隐"


def test_mirror_cache_files_are_servable(tmp_path, monkeypatch):
    """直连交付的懒镜像缓存必须在 gradio 的文件放行名单里(2026-08-28 用户实见:
    同步曲线图镜像到了本地却 403 不显示;裁决卡证据帧同族)。"""
    pytest.importorskip("gradio")
    from starlette.testclient import TestClient

    monkeypatch.delenv("CURATION_UI_USER", raising=False)
    monkeypatch.delenv("CURATION_UI_PASSWORD", raising=False)
    monkeypatch.setenv("CURATION_TOS_CACHE", str(tmp_path / "cache"))
    plot = (tmp_path / "cache" / "reports" / "b" / "deliveries" / "d" / "r"
            / "details" / "plots" / "ep000001_sync.png")
    plot.parent.mkdir(parents=True)
    plot.write_bytes(b"\x89PNG\r\n\x1a\n0000")
    from curation.ui.app import create_asgi_app
    root = tmp_path / "deliv"
    root.mkdir()
    _new_delivery(root, "d1")
    app = create_asgi_app(str(root), data_root=str(tmp_path / "data"))
    with TestClient(app) as c:
        r = c.get(f"/gradio_api/file={plot}")
        assert r.status_code == 200, "镜像缓存里的证据文件被 403,同步图显示不出来"


def test_empty_adjudication_deck_collapses_card_block(tmp_path):
    """裁决队列为空时整个卡片区收起(2026-08-28 用户点名:机位框/翻页按钮空摆着
    像"有东西没显示出来"),只留一句说明;有条目时反向。"""
    import json as _json

    pytest.importorskip("gradio")
    from curation.ui.app import build_app

    deliv = tmp_path / "deliveries"
    run = deliv / "d1" / "20260828-000001"
    run.mkdir(parents=True)
    (run / "passed.json").write_text(_json.dumps({"数据集": "d1", "episodes": {}}),
                                     encoding="utf-8")
    app = build_app(str(deliv), data_root=str(tmp_path / "data"))
    fns = [f.fn for f in app.fns.values()
           if getattr(f.fn, "__name__", "") == "<lambda>"
           and "_mg_render" in (f.fn.__code__.co_freevars or ())]
    assert fns, "翻页/筛选回调该闭包引用 _mg_render"
    out = fns[0]({"episodes": {}, "audit_queue": [], "path": str(run)}, "全部")
    blk, note = out[-2], out[-1]
    assert blk.get("visible") is False, "空队列必须整块收起"
    assert "没有待你裁决" in str(note), "收起后要顶一句说明"


def test_max_episodes_field_is_blank_textbox(tmp_path):
    """「只跑前 N 条(留空=全部)」必须真空白:gr.Number 的 None 会被 gradio 6.9
    前端画成 0(2026-08-28 用户实见,像要跑 0 条)——钉住它是 Textbox 且无默认值。"""
    pytest.importorskip("gradio")
    import gradio as gr
    from curation.ui.app import build_app

    (tmp_path / "data").mkdir()
    app = build_app(str(tmp_path / "deliveries"),
                    data_root=str(tmp_path / "data"))
    boxes = [b for b in app.blocks.values()
             if getattr(b, "label", "") == "只跑前 N 条(留空=全部)"]
    assert boxes and isinstance(boxes[0], gr.Textbox), "必须是 Textbox 不是 Number"
    assert not (boxes[0].value or ""), "默认必须空白"
