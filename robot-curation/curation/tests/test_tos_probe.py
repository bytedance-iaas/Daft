"""可写探针 + 交付目录默认借桶 + 只读锁(2026-08-21 用户定)。

全部离线:假 TOS 客户端按状态码演各种桶,不出网。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from curation import tos_store


class _Err(Exception):
    def __init__(self, status, code="X"):
        super().__init__(f"http {status}")
        self.status_code = status
        self.code = code


class _FakeClient:
    """list / put / delete 各自可设一个要抛的状态码;记下放过的探针 key。"""

    def __init__(self, list_status=None, put_status=None, del_status=None):
        self.list_status, self.put_status, self.del_status = list_status, put_status, del_status
        self.put_keys, self.deleted = [], []

    def list_objects_type2(self, bucket, prefix="", **kw):
        if self.list_status:
            raise _Err(self.list_status)
        return SimpleNamespace(contents=[], common_prefixes=[], is_truncated=False)

    def put_object(self, bucket, key, content=b""):
        if self.put_status:
            raise _Err(self.put_status)
        self.put_keys.append(key)

    def delete_object(self, bucket, key):
        if self.del_status:
            raise _Err(self.del_status)
        self.deleted.append(key)


def _store(**kw):
    return tos_store.TosStore("ep", "cn-beijing", client=_FakeClient(**kw))


def test_probe_ok_puts_and_deletes_marker_under_prefix():
    st = _store()
    r = tos_store.probe_writable("tos://bkt/deliveries", store=st)
    assert r["kind"] == "ok"
    assert st._c.put_keys == ["deliveries/" + tos_store.PROBE_NAME]
    assert st._c.deleted == st._c.put_keys, "探针必须写完立刻删,桶里不能留垃圾"


def test_probe_bucket_root_key_has_no_leading_slash():
    st = _store()
    tos_store.probe_writable("tos://bkt", store=st)
    assert st._c.put_keys == [tos_store.PROBE_NAME]


@pytest.mark.parametrize("kw, kind", [
    ({"list_status": 404}, "missing"),     # NoSuchBucket
    ({"list_status": 301}, "missing"),     # 桶在别的地区
    ({"list_status": 403}, "forbidden"),   # 连列都不让
    ({"put_status": 403}, "readonly"),     # 能列不能写 = 公共只读桶
    ({"put_status": 500}, "error"),
    ({"del_status": 403}, "leftover"),     # 能写不能删:要说出来
])
def test_probe_classifies_by_status(kw, kind):
    assert tos_store.probe_writable("tos://bkt/p", store=_store(**kw))["kind"] == kind


def test_writable_verdict_wording_distinguishes_three_causes():
    from curation.ui import runner
    texts = {}
    for kind in ("missing", "forbidden", "readonly", "leftover", "ok", "error"):
        ok, why = runner.writable_verdict(
            "tos://ai-infra/deliveries", "cn-beijing",
            probe=lambda u, r, k=kind: {"kind": k, "detail": "d"})
        texts[kind] = (ok, why)
    assert texts["ok"] == (True, "")
    assert texts["leftover"][0] is True and "0 字节" in texts["leftover"][1]
    assert texts["missing"][0] is False and "不存在" in texts["missing"][1]
    assert texts["forbidden"][0] is False and "权限" in texts["forbidden"][1]
    assert texts["readonly"][0] is False and "只读" in texts["readonly"][1]
    assert texts["error"][0] is False
    for ok, why in texts.values():
        if not ok:
            assert "ai-infra" in why, "原因里必须点名是哪个桶"


def test_writable_verdict_swallows_probe_exceptions_into_text():
    from curation.ui import runner

    def boom(u, r):
        raise RuntimeError("no creds")
    ok, why = runner.writable_verdict("tos://bkt/p", probe=boom)
    assert ok is False and "no creds" in why


def test_borrowed_output_url(tmp_path, monkeypatch):
    from curation.ui import runner
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    local = str(tmp_path / "data" / "deliveries")
    # 没桶的实例:借数据集所在的桶
    monkeypatch.delenv("TOS_BUCKET", raising=False)
    assert runner.borrowed_output_url("tos://their-bucket/dataset-1", local) \
        == "tos://their-bucket/deliveries"
    assert runner.borrowed_output_url("/data/datasets", local) == ""      # 数据集不是 tos
    assert runner.borrowed_output_url("", local) == ""
    assert runner.borrowed_output_url("tos://", local) == ""              # 解析不了
    # 有自己桶的实例:永远用自己的
    monkeypatch.setenv("TOS_BUCKET", "herbucket")
    assert runner.borrowed_output_url("tos://their-bucket/dataset-1", local) \
        == "tos://herbucket/deliveries"


def test_readonly_lock_messages():
    from curation.ui import app as ui_app
    assert ui_app.readonly_block_msg({}) == ""
    assert ui_app.readonly_block_msg(None) == ""
    assert ui_app.readonly_banner_md({"path": "x"}) == ""
    m = {"tos_readonly": "桶 ai-infra 对本实例只读"}
    assert "未记录" in ui_app.readonly_block_msg(m) and "ai-infra" in ui_app.readonly_block_msg(m)
    assert ui_app.readonly_banner_md(m).startswith("🔒")


def test_no_bucket_instance_leaves_output_boxes_empty_and_has_modal(tmp_path, monkeypatch):
    """同事的纯直连部署(没挂载、没 TOS_BUCKET):两页的「交付目录」默认留空等借桶,
    绝不显示 /data/deliveries;写不进去的对话框与按钮居中样式都在。"""
    pytest.importorskip("gradio")
    from curation.ui import app as ui_app
    monkeypatch.setenv("CURATION_TOS_MOUNT", str(tmp_path / "mnt"))
    monkeypatch.delenv("TOS_BUCKET", raising=False)
    monkeypatch.delenv("CURATION_CONFIG", raising=False)
    root = tmp_path / "data" / "deliveries"
    root.mkdir(parents=True)
    app = ui_app.build_app(str(root), data_root=str(tmp_path / "data" / "datasets"))
    cfg = json.loads(json.dumps(app.get_config_file(), default=str))
    outs = [c["props"].get("value") for c in cfg["components"]
            if c["props"].get("label") == "交付目录"]
    assert len(outs) == 2 and all(v in ("", None) for v in outs), outs
    ids = {c["props"].get("elem_id") for c in cfg["components"]}
    assert {"out-ask", "out-ask-btns", "rn-tout-note"} <= ids
    assert "#out-ask-btns" in ui_app._ARCO_CSS


# ── 读侧探针 + 地区找桶(2026-08-21 用户问"桶地址和地区对不上会不会跳出来提示")────────

@pytest.mark.parametrize("kw, kind", [
    ({}, "ok"), ({"list_status": 404}, "missing"), ({"list_status": 301}, "missing"),
    ({"list_status": 403}, "forbidden"), ({"list_status": 500}, "error"),
])
def test_probe_readable_classifies_list_only(kw, kind):
    st = _store(**kw)
    assert tos_store.probe_readable("tos://bkt/p", store=st)["kind"] == kind
    assert st._c.put_keys == [], "读侧探针绝不写"


def test_locate_bucket_tries_other_regions_and_skips_current():
    seen = []

    def make(bucket, rg):
        seen.append(rg)
        return tos_store.TosStore("ep", rg, client=_FakeClient(
            list_status=None if rg == "cn-shanghai" else 404))
    assert tos_store.locate_bucket("bkt", ("cn-beijing", "cn-shanghai", "cn-guangzhou"),
                                   skip="cn-beijing", make=make) == "cn-shanghai"
    assert seen == ["cn-shanghai"], "当前地区跳过,找到就停"
    forb = lambda b, r: tos_store.TosStore("ep", r, client=_FakeClient(list_status=403))  # noqa: E731
    assert tos_store.locate_bucket("bkt", ("a", "b"), make=forb) == "a", "403 = 桶在这个地区(只是没权限)"
    assert tos_store.locate_bucket("bkt", ("a", "b"), make=lambda b, r: (_ for _ in ()).throw(RuntimeError("x"))) is None


def test_missing_bucket_text_names_the_region_it_found():
    from curation.ui import runner
    t = runner.missing_bucket_text("bkt", "cn-beijing", "NoSuchBucket",
                                   locate=lambda b, skip: "cn-shanghai")
    assert "不在 cn-beijing" in t and "它在 cn-shanghai" in t and "改成 cn-shanghai" in t
    t2 = runner.missing_bucket_text("bkt", "cn-beijing", locate=lambda b, skip: None)
    assert "不存在" in t2 and "不在 cn-beijing" in t2 and "其他地区也没找到" in t2
    t3 = runner.missing_bucket_text("bkt", "cn-beijing")          # 没给 locate:不出网,也不瞎说
    assert "不存在" in t3 and "其他地区" not in t3

    def boom(b, skip):
        raise RuntimeError("no creds")
    assert "不存在" in runner.missing_bucket_text("bkt", None, locate=boom)


def test_readable_and_writable_verdicts_share_region_hint():
    from curation.ui import runner
    loc = lambda b, skip: "ap-southeast-1"  # noqa: E731
    ok, why = runner.readable_verdict("tos://bkt/ds", "cn-beijing",
                                      probe=lambda u, r: {"kind": "missing", "detail": "d"}, locate=loc)
    assert ok is False and "它在 ap-southeast-1" in why
    ok, why = runner.writable_verdict("tos://bkt/out", "cn-beijing",
                                      probe=lambda u, r: {"kind": "missing", "detail": "d"}, locate=loc)
    assert ok is False and "它在 ap-southeast-1" in why
    ok, why = runner.readable_verdict("tos://bkt/ds", probe=lambda u, r: {"kind": "forbidden", "detail": "d"})
    assert ok is False and "读权限" in why
    assert runner.readable_verdict("tos://bkt/ds", probe=lambda u, r: {"kind": "ok", "detail": ""}) == (True, "")
    assert runner.readable_verdict("not-a-url")[0] is False


def test_run_page_region_red_notes_replace_fill_time_dialogs(tmp_path, monkeypatch):
    """2026-08-28 用户定版:填表阶段桶/地区问题一律红字贴在地区下拉正下方,
    读侧对话框(in-ask)退役;交付目录对话框(out-ask)保留但只在点「开始
    质检」时出场。三个红字位都在。"""
    pytest.importorskip("gradio")
    from curation.ui import app as ui_app
    monkeypatch.delenv("CURATION_CONFIG", raising=False)
    root = tmp_path / "data" / "deliveries"
    root.mkdir(parents=True)
    app = ui_app.build_app(str(root), data_root=str(tmp_path / "data" / "datasets"))
    cfg = json.loads(json.dumps(app.get_config_file(), default=str))
    ids = {c["props"].get("elem_id") for c in cfg["components"]}
    assert "in-ask" not in ids, "读侧填表对话框已退役"
    assert {"out-ask", "out-ask-ok"} <= ids, "开跑闸的对话框还在"
    assert {"rn-tin-rg-err", "rn-tout-rg-err", "rp-rg-err"} <= ids
    assert any(c["props"].get("label") == "交付名" and c["type"] == "dropdown"
               for c in cfg["components"]), "报告页的交付下拉叫「交付名」(与跑质检页同名)"


def test_mounted_bucket_region_and_mismatch_text(monkeypatch):
    from curation.ui import runner
    monkeypatch.delenv("TOS_REGION", raising=False)
    monkeypatch.delenv("TOS_ENDPOINT", raising=False)
    assert runner.mounted_bucket_region({"endpoint": "https://tos-s3-cn-beijing.ivolces.com"}) == "cn-beijing"
    assert runner.mounted_bucket_region({}) is None, "推不出就不硬猜"
    monkeypatch.setenv("TOS_REGION", "cn-shanghai")
    assert runner.mounted_bucket_region({}) == "cn-shanghai"
    t = runner.mount_region_mismatch("curation", "cn-beijing", "cn-guangzhou")
    assert "curation 在 cn-beijing" in t and "不在 cn-guangzhou" in t and "改回 cn-beijing" in t
    assert runner.mount_region_mismatch("curation", "cn-beijing", "cn-beijing") == ""
    assert runner.mount_region_mismatch("curation", None, "cn-guangzhou") == ""
    assert runner.mount_region_mismatch("curation", "cn-beijing", None) == ""
