"""地区找桶(2026-08-21 用户问"桶地址和地区对不上会不会跳出来提示")。

全部离线:假 TOS 客户端按状态码演各种桶,不出网。
"""
from __future__ import annotations

from types import SimpleNamespace


from curation import tos_store


class _Err(Exception):
    def __init__(self, status, code="X"):
        super().__init__(f"http {status}")
        self.status_code = status
        self.code = code


class _FakeClient:
    """list 可设一个要抛的状态码。"""

    def __init__(self, list_status=None):
        self.list_status = list_status

    def list_objects_type2(self, bucket, prefix="", **kw):
        if self.list_status:
            raise _Err(self.list_status)
        return SimpleNamespace(contents=[], common_prefixes=[], is_truncated=False)



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


