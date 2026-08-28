"""公共数据集(2026-08-21):匿名桶客户端 + 清单过滤 + 界面「数据来源」+ CLI `public`。

全部离线:假 TOS 客户端演镜像桶(清单里混着非机器人数据集),不出网、不要凭证。
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from curation import tos_store
from curation.ingest import public_catalog as pc

BKT = "mirror-bkt"
MANIFEST = {
    "563w_baidubaike": ["dataset/563w_baidubaike/data.json"],
    "libero": ["dataset/libero/.metadata.json", "dataset/libero/meta/info.json",
               "dataset/libero/data/chunk-000/file-000.parquet",
               "dataset/libero/videos/cam/chunk-000/file-000.mp4"],
    "aloha_sim": ["dataset/aloha_sim/meta/info.json", "dataset/aloha_sim/.metadata.json",
                  "dataset/aloha_sim/videos/top/chunk-000/file-000.mp4"],
    "broken_info": ["dataset/broken_info/meta/info.json"],
    "wiki-index": ["dataset/wiki-index/index.bin", "dataset/wiki-index/meta/readme.md"],
}
OBJECTS = {
    "dataset_files.json": json.dumps(MANIFEST).encode(),
    "dataset/libero/meta/info.json": json.dumps(
        {"codebase_version": "v3.0", "total_episodes": 1693}).encode(),
    "dataset/libero/.metadata.json": json.dumps(
        {"id": "HuggingFaceVLA/libero", "siblings": []}).encode(),
    "dataset/aloha_sim/meta/info.json": json.dumps(
        {"codebase_version": "v2.1", "total_episodes": 50}).encode(),
    "dataset/aloha_sim/.metadata.json": b'{"id": "lerobot/aloha_sim"}',
    "dataset/broken_info/meta/info.json": b"{not json",
    "dataset/libero/videos/cam/chunk-000/file-000.mp4": b"\x00" * 64,
    "dataset/aloha_sim/videos/top/chunk-000/file-000.mp4": b"",     # 镜像没同步完:0 字节
}


class _Err(Exception):
    def __init__(self, status, code="X"):
        super().__init__(f"http {status}")
        self.status_code, self.code = status, code


class _FakeClient:
    def __init__(self, etag="e1"):
        self.etag = etag
        self.gets, self.heads, self.puts = [], [], []

    def get_object(self, bucket, key):
        assert bucket == BKT
        self.gets.append(key)
        if key not in OBJECTS:
            raise _Err(404, "NoSuchKey")
        return io.BytesIO(OBJECTS[key])

    def head_object(self, bucket, key):
        self.heads.append(key)
        if key not in OBJECTS:
            raise _Err(404, "NoSuchKey")
        return SimpleNamespace(content_length=len(OBJECTS[key]), etag=f'"{self.etag}"')

    def list_objects_type2(self, bucket, prefix="", **kw):
        return SimpleNamespace(contents=[], common_prefixes=[], is_truncated=False)

    def put_object(self, bucket, key, content=b""):
        self.puts.append(key)
        raise _Err(403, "AccessDenied")      # 公共桶:能读不能写

    def delete_object(self, bucket, key):
        raise AssertionError("没写成功不该删")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pc.reset()
    tos_store.clear_anonymous_buckets()
    monkeypatch.delenv("CURATION_CONFIG", raising=False)
    monkeypatch.delenv("TOS_ACCESS_KEY", raising=False)
    monkeypatch.delenv("TOS_SECRET_KEY", raising=False)
    yield
    pc.reset()
    tos_store.clear_anonymous_buckets()


def _cfg(**over):
    d = {"bucket": BKT, "region": "cn-beijing"}
    d.update(over)
    return {"public_datasets": d}


def _store(client=None):
    return tos_store.TosStore("https://tos-cn-beijing.ivolces.com", "cn-beijing",
                              client=client or _FakeClient(), anonymous=True)


# ── 配置与匿名客户端 ────────────────────────────────────────────────────────

def test_apply_config_registers_anonymous_bucket_or_disables():
    assert pc.apply_config({}) is None and pc.configured() is None
    assert pc.apply_config({"public_datasets": {"bucket": None}}) is None
    assert not tos_store.is_anonymous_bucket(BKT)
    c = pc.apply_config(_cfg())
    assert c["prefix"] == "dataset" and c["manifest"] == "dataset_files.json"
    assert tos_store.is_anonymous_bucket(BKT)
    assert tos_store.anonymous_region(BKT) == "cn-beijing"
    assert pc.root_url() == f"tos://{BKT}/dataset"
    assert pc.is_public_root(f"tos://{BKT}/dataset/") and not pc.is_public_root("tos://other/dataset")
    assert pc.dataset_url("libero") == f"tos://{BKT}/dataset/libero"


def test_apply_config_rejects_bad_bucket_name():
    with pytest.raises(tos_store.TosUrlError):
        pc.apply_config(_cfg(bucket="Bad_Bucket"))
    assert pc.configured() is None


def test_lazy_load_from_env_config(tmp_path, monkeypatch):
    f = tmp_path / "site.yaml"
    f.write_text(f"public_datasets:\n  bucket: {BKT}\n  region: cn-shanghai\n", encoding="utf-8")
    monkeypatch.setenv("CURATION_CONFIG", str(f))
    assert pc.configured()["region"] == "cn-shanghai", "CLI 子进程没人显式 apply 也要认站点配置"
    assert tos_store.is_anonymous_bucket(BKT)


def test_anonymous_store_needs_no_credentials_and_signed_one_still_does():
    st = tos_store.make_store("cn-beijing", anonymous=True, client=_FakeClient())
    assert st.anonymous
    with pytest.raises(tos_store.TosConfigError):
        tos_store.make_store_for("private-bkt", "cn-beijing")   # 未登记的桶仍要凭证
    tos_store.register_anonymous_bucket(BKT, "cn-beijing")
    st2 = tos_store.make_store_for(BKT, "ap-southeast-1", client=_FakeClient())
    assert st2.anonymous and st2.region == "cn-beijing", "登记的地区优先于调用方给的"


def test_anonymous_presign_is_plain_public_url():
    st = _store()
    url = st.presign(BKT, "dataset/libero/videos/a b.mp4")
    assert url == f"https://{BKT}.tos-cn-beijing.ivolces.com/dataset/libero/videos/a%20b.mp4"
    st_http = tos_store.TosStore("http://tos-cn-beijing.volces.com", "cn-beijing",
                                 client=_FakeClient(), anonymous=True)
    assert st_http.public_url(BKT, "k").startswith("http://")


def test_dsfs_picks_anonymous_client_per_bucket(monkeypatch):
    from curation.ingest import dsfs
    calls = []
    monkeypatch.setattr(tos_store, "make_store_for",
                        lambda b, r=None, client=None: calls.append(("anon", b)) or object())
    monkeypatch.setattr(tos_store, "make_store",
                        lambda r=None, client=None, **kw: calls.append(("signed", r)) or object())
    dsfs.configure("cn-guangzhou")
    tos_store.register_anonymous_bucket(BKT, "cn-beijing")
    dsfs._store(BKT)
    dsfs._store("private-bkt")
    dsfs._store(None)
    assert calls == [("anon", BKT), ("signed", "cn-guangzhou")], "签名客户端按地区缓存复用"
    dsfs.configure(None)


def test_probe_on_public_bucket_goes_anonymous_and_reads_as_readonly(monkeypatch):
    pc.apply_config(_cfg())
    seen = []
    fake = _FakeClient()
    orig = tos_store.make_store_for

    def spy(bucket, region=None, client=None):
        seen.append(bucket)
        return orig(bucket, region, client=fake)
    monkeypatch.setattr(tos_store, "make_store_for", spy)
    r = tos_store.probe_writable(f"tos://{BKT}/deliveries", "cn-beijing")
    assert seen == [BKT] and r["kind"] == "readonly", "公共桶的真相是只读,不是'密钥没权限'"


# ── 清单 ────────────────────────────────────────────────────────────────────

def test_catalog_filters_lerobot_and_enriches():
    pc.apply_config(_cfg())
    fake = _FakeClient()
    ents = pc.catalog(store=_store(fake))
    assert [e["name"] for e in ents] == ["aloha_sim", "broken_info", "libero"], \
        "只留清单里带 meta/info.json 的;百科/wiki 不进来"
    lib = next(e for e in ents if e["name"] == "libero")
    assert lib == {"name": "libero", "id": "HuggingFaceVLA/libero", "version": "v3.0",
                   "episodes": 1693, "files": 4, "url": f"tos://{BKT}/dataset/libero"}
    bad = next(e for e in ents if e["name"] == "broken_info")
    assert bad["id"] == "broken_info" and bad["version"] == "" and bad["episodes"] is None, \
        "单个数据集的 info 坏了不拖累整份清单"
    assert bad["warning"] == "没有视频文件"
    aloha = next(e for e in ents if e["name"] == "aloha_sim")
    assert aloha["warning"] == "视频文件为空(镜像不完整)", "0 字节 mp4 要在清单里就标出来"
    assert pc.label(lib) == "HuggingFaceVLA/libero · v3.0 · 1693 条"
    assert pc.label(bad) == "broken_info · ⚠️ 没有视频文件"
    assert pc.label(aloha).endswith("⚠️ 视频文件为空(镜像不完整)")
    assert "dataset/libero/videos/cam/chunk-000/file-000.mp4" not in fake.gets, "视频只 HEAD 不 GET"
    assert ("HuggingFaceVLA/libero · v3.0 · 1693 条", "libero") in pc.choices(store=_store(fake))
    assert "dataset/563w_baidubaike/data.json" not in fake.gets, "没过滤上的数据集一个字节都不取"
    assert "3 个 LeRobot 数据集" in pc.summary_line(3) and BKT in pc.summary_line(3)


def test_catalog_caches_by_etag_and_recheck_window():
    pc.apply_config(_cfg())
    fake = _FakeClient(etag="e1")
    st = _store(fake)
    pc.catalog(store=st, now=1000.0)
    n_get = len(fake.gets)
    mh = lambda: fake.heads.count("dataset_files.json")  # noqa: E731 只数清单的 HEAD(视频探空另算)
    pc.catalog(store=st, now=1010.0)
    assert len(fake.gets) == n_get and mh() == 1, "复查窗口内连 HEAD 都不发"
    pc.catalog(store=st, now=1000.0 + pc.RECHECK_S + 1)
    assert mh() == 2 and len(fake.gets) == n_get, "ETag 没变不重读清单"
    fake.etag = "e2"
    pc.catalog(store=st, now=1000.0 + 2 * pc.RECHECK_S + 2)
    assert len(fake.gets) > n_get, "ETag 变了才重读"
    n_get = len(fake.gets)
    pc.catalog(store=st, force=True, now=1000.0 + 2 * pc.RECHECK_S + 3)
    assert len(fake.gets) > n_get


def test_catalog_errors_are_one_sentence_and_unconfigured_is_empty():
    assert pc.catalog() == []
    pc.apply_config(_cfg(manifest="missing.json"))
    with pytest.raises(pc.PublicCatalogError) as ei:
        pc.catalog(store=_store())
    assert "missing.json" in str(ei.value) and BKT in str(ei.value)


def test_resolve_by_name_id_url_or_tail():
    pc.apply_config(_cfg())
    st = _store()
    assert pc.resolve("libero", store=st)["id"] == "HuggingFaceVLA/libero"
    assert pc.resolve("HuggingFaceVLA/libero", store=st)["name"] == "libero"
    assert pc.resolve(f"tos://{BKT}/dataset/libero/", store=st)["name"] == "libero"
    assert pc.resolve("someone-else/libero", store=st)["name"] == "libero"
    assert pc.resolve("tos://other/dataset/libero", store=st) is None
    assert pc.resolve("nope", store=st) is None and pc.resolve("", store=st) is None


# ── runner 侧规则 ───────────────────────────────────────────────────────────

def test_runner_never_borrows_public_bucket_and_public_output_default(tmp_path, monkeypatch):
    from curation.ui import runner
    pc.apply_config(_cfg())
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    monkeypatch.delenv("TOS_BUCKET", raising=False)
    local = str(tmp_path / "data" / "deliveries")
    assert runner.borrowed_output_url(f"tos://{BKT}/dataset/libero", local) == ""
    assert runner.borrowed_output_url("tos://their-bucket/x", local) == "tos://their-bucket/deliveries"
    assert runner.public_output_default(local) == ("", runner.PUBLIC_READONLY_NOTE)
    monkeypatch.setenv("TOS_BUCKET", "mine")
    assert runner.public_output_default(local) == ("tos://mine/deliveries", "")


def test_runner_tos_list_datasets_on_public_root_uses_catalog():
    from curation.ui import runner
    pc.apply_config(_cfg())
    assert runner.tos_list_datasets(f"tos://{BKT}/dataset", "cn-beijing", store=_store()) \
        == ["aloha_sim", "broken_info", "libero"]


# ── 界面 ────────────────────────────────────────────────────────────────────

def _src_radio(app):
    import gradio as gr
    return next(b for b in app.blocks.values() if isinstance(b, gr.Radio) and b.elem_id == "rn-pub")


def _fn_on(app, block, event="input"):
    for fn in app.fns.values():
        for tgt in (getattr(fn, "targets", None) or []):
            cid, ev = (tgt if isinstance(tgt, tuple) else (tgt.block._id, tgt.event_name))
            if cid == block._id and ev == event:
                return fn
    raise AssertionError(f"{block} 没接 {event}")


@pytest.fixture
def site(tmp_path):
    f = tmp_path / "site.yaml"
    f.write_text(f"public_datasets:\n  bucket: {BKT}\n  region: cn-beijing\n", encoding="utf-8")
    return str(f)


def _build(tmp_path, config_path=None):
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    root = tmp_path / "deliveries"
    root.mkdir(exist_ok=True)
    return build_app(str(root), config_path=config_path,
                     data_root=str(tmp_path / "datasets"))


def test_source_radio_hidden_without_config_and_visible_with(tmp_path, site):
    from curation.ui import app as ui_app
    app = _build(tmp_path)
    assert _src_radio(app).visible is False
    assert pc.configured() is None
    app2 = _build(tmp_path, site)
    box = _src_radio(app2)
    assert box.visible is True and box.value == ui_app.SRC_PRIVATE == "私有"
    assert [c[1] for c in box.choices] == ["私有", "HuggingFace 缓存桶"]
    assert pc.source_label() == "HuggingFace 缓存桶"
    assert tos_store.is_anonymous_bucket(BKT), "建界面时就把公共桶登记成匿名读"
    import gradio as gr
    tin = next(b for b in app2.blocks.values() if isinstance(b, gr.Textbox) and b.label == "数据集目录")
    assert tin.show_label is False, "标题行自己画(标签 + 二选一),原生标签藏起来"


def test_choosing_mirror_greys_root_and_region_lists_catalog_and_leaves_output_alone(
        tmp_path, site, monkeypatch):
    from curation.ui import app as ui_app
    monkeypatch.setattr(pc, "catalog", lambda **kw: [
        {"name": "libero", "id": "HuggingFaceVLA/libero", "version": "v3.0",
         "episodes": 1693, "files": 3, "url": f"tos://{BKT}/dataset/libero"}])
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    monkeypatch.delenv("TOS_BUCKET", raising=False)
    app = _build(tmp_path, site)
    ev = _fn_on(app, _src_radio(app))
    labels = [getattr(o, "label", None) for o in ev.outputs]
    assert labels[:3] == ["数据集目录", "地区", "数据集"] and len(labels) == 5
    assert "交付目录" not in labels, "来源切来切去都不许碰交付目录(2026-08-21 用户)"
    tin, rg, ds, note, ds_note = ev.fn(pc.source_label())
    assert tin["value"] == f"tos://{BKT}/dataset" and tin["interactive"] is False, \
        "桶名照样显示,只是置灰不用填"
    assert rg["value"] == "cn-beijing" and rg["interactive"] is False
    assert ds["choices"] == [("HuggingFaceVLA/libero · v3.0 · 1693 条", "libero")] and ds["value"] == []
    assert note == "" and ds_note == ""
    back = ev.fn(ui_app.SRC_PRIVATE)
    assert back[0]["interactive"] is True and back[1]["interactive"] is True
    assert back[0]["value"] != f"tos://{BKT}/dataset"


def test_public_deeplink_preselects_and_switches_source(tmp_path, site, monkeypatch):
    from curation.ui import app as ui_app
    monkeypatch.setattr(pc, "catalog", lambda **kw: [
        {"name": "libero", "id": "HuggingFaceVLA/libero", "version": "v3.0",
         "episodes": 1693, "files": 3, "url": f"tos://{BKT}/dataset/libero"}])
    app = _build(tmp_path, site)
    fn = next(f.fn for f in app.fns.values()
              if getattr(f.fn, "__name__", "") == "_prefill_from_query")
    out = fn(SimpleNamespace(query_params={"source": "public", "dataset": "libero"}))
    assert len(out) == 6
    assert out[0]["value"] == f"tos://{BKT}/dataset" and out[0]["interactive"] is False
    assert out[1]["value"] == ["libero"]
    assert out[2]["value"] == "cn-beijing"
    assert out[5]["value"] == pc.source_label(), "第 6 个输出 = 来源二选一"
    # 完整 tos:// 地址的老契约指到公共桶,同样识别
    out = fn(SimpleNamespace(query_params={"dataset": f"tos://{BKT}/dataset/libero"}))
    assert out[1]["value"] == ["libero"] and out[5]["value"] == pc.source_label()
    # 只带 source=public:切到镜像、不预选
    out = fn(SimpleNamespace(query_params={"source": "public"}))
    assert out[1]["value"] == [] and out[5]["value"] == pc.source_label()
    # 普通深链把来源钉回「私有」(深链总在页面刚开时到,值本来就是它)
    out = fn(SimpleNamespace(query_params={"dataset": "tos://other-bkt/datasets/x"}))
    assert out[5]["value"] == ui_app.SRC_PRIVATE and out[0]["interactive"] is True


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_public_lists_json_or_says_unconfigured(tmp_path, site, capsys, monkeypatch):
    from curation import cli
    monkeypatch.setattr(pc, "catalog", lambda **kw: [
        {"name": "libero", "id": "HuggingFaceVLA/libero", "version": "v3.0",
         "episodes": 1693, "files": 3, "url": f"tos://{BKT}/dataset/libero"}])
    assert cli.main(["public", "--config", site, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 1 and data["datasets"][0]["name"] == "libero"
    assert data["root"] == f"tos://{BKT}/dataset" and data["region"] == "cn-beijing"
    assert cli.main(["public", "--config", site]) == 0
    txt = capsys.readouterr().out
    assert "libero" in txt and "--input-region cn-beijing" in txt
    empty = tmp_path / "empty.yaml"
    empty.write_text("{}\n", encoding="utf-8")
    pc.reset()
    assert cli.main(["public", "--config", str(empty)]) == 2
