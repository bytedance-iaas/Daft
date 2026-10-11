"""可写探针 + 交付目录默认借桶 + 只读锁(2026-08-21 用户定)。

全部离线:假 TOS 客户端按状态码演各种桶,不出网。
"""
from __future__ import annotations

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


# ── 读侧探针 + 地区找桶(2026-08-21 用户问"桶地址和地区对不上会不会跳出来提示")────────


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


