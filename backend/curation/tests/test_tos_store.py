"""tos_store(TOS 直连存储层)单测:纯逻辑 + 假客户端的上传下载与预签名。

全部离线:SDK 从不导入(TosStore 收注入的假客户端),网络零依赖。
"""
from __future__ import annotations

import os

import pytest

from curation import tos_store
from curation.tos_store import TosStore, TosUrlError, endpoint_for_region, parse_tos_url, region_from_endpoint


# ── URL 解析 ─────────────────────────────────────────────────────────────


def test_parse_url_basic():
    assert parse_tos_url("tos://my-bucket/datasets/pusht") == ("my-bucket", "datasets/pusht")
    assert parse_tos_url("tos://my-bucket/datasets/pusht/") == ("my-bucket", "datasets/pusht")
    assert parse_tos_url("tos://my-bucket") == ("my-bucket", "")
    assert parse_tos_url("tos://my-bucket/") == ("my-bucket", "")


@pytest.mark.parametrize("bad", [
    "", "s3://b/k", "http://b/k", "/mnt/tos/datasets",       # 不是 tos://
    "tos://UPPER/k", "tos://x/k", "tos://-bad-/k",            # 桶名不合法
    "tos://bucket/a/../b", "tos://bucket/a\\b",               # 前缀危险写法
])
def test_parse_url_rejects(bad):
    with pytest.raises(TosUrlError):
        parse_tos_url(bad)


# ── 地区/端点推导(规则与 rerun client.rs 对齐)──────────────────────────


def test_region_from_endpoint():
    assert region_from_endpoint("https://tos-s3-cn-beijing.ivolces.com") == "cn-beijing"
    assert region_from_endpoint("https://tos-s3-cn-beijing.volces.com") == "cn-beijing"
    assert region_from_endpoint("tos-cn-shanghai.volces.com") == "cn-shanghai"
    assert region_from_endpoint("https://tos-s3-ap-southeast-1.volces.com") == "ap-southeast-1"
    assert region_from_endpoint("") is None
    assert region_from_endpoint("https://example.com") is None            # 非 volces 域
    assert region_from_endpoint("https://oss-cn-beijing.volces.com") is None  # 认不出的标签


def test_endpoint_for_region_native_protocol_and_internal_preference():
    """端点必须是原生协议形式 tos-<region>(SDK 用),不是 rerun 的 s3 网关
    形式 tos-s3-<region>;部署端点只贡献「地区 + 内外网」两个事实。"""
    dep = "https://tos-s3-cn-beijing.ivolces.com"        # helm 里的 s3 形式
    assert endpoint_for_region("cn-beijing", dep) == "https://tos-cn-beijing.ivolces.com"
    assert endpoint_for_region("cn-shanghai", dep) == "https://tos-cn-shanghai.volces.com"
    assert endpoint_for_region("cn-beijing", None) == "https://tos-cn-beijing.volces.com"
    dep_native = "https://tos-cn-beijing.ivolces.com"    # 原生形式的部署端点同样认
    assert endpoint_for_region("cn-beijing", dep_native) == dep_native


def test_make_store_env(monkeypatch):
    monkeypatch.setenv("TOS_ACCESS_KEY", "ak")
    monkeypatch.setenv("TOS_SECRET_KEY", "sk")
    monkeypatch.setenv("TOS_ENDPOINT", "https://tos-s3-cn-beijing.ivolces.com")
    monkeypatch.delenv("TOS_REGION", raising=False)
    st = tos_store.make_store(None, client=object())
    assert st.region == "cn-beijing" and st.endpoint.endswith("ivolces.com")
    st2 = tos_store.make_store("ap-southeast-1", client=object())
    assert st2.endpoint == "https://tos-ap-southeast-1.volces.com"


def test_make_store_missing_creds(monkeypatch):
    monkeypatch.delenv("TOS_ACCESS_KEY", raising=False)
    monkeypatch.delenv("TOS_SECRET_KEY", raising=False)
    with pytest.raises(tos_store.TosConfigError, match="TOS_ACCESS_KEY"):
        tos_store.make_store("cn-beijing")



# ── 假客户端(模拟 TosClientV2 被用到的三个方法 + 翻页)───────────────────


class _Obj:
    def __init__(self, key, size):
        self.key, self.size = key, size


class _Page:
    def __init__(self, contents, truncated, token):
        self.contents = contents
        self.is_truncated = truncated
        self.next_continuation_token = token


class FakeClient:
    """内存桶:{(bucket, key): bytes}。翻页按 max_keys 真切,翻页逻辑不是摆设。"""

    def __init__(self, objects: dict):
        self.objects = dict(objects)

    def list_objects_type2(self, bucket, prefix="", delimiter=None,
                           continuation_token=None, max_keys=1000, **kw):
        keys = sorted(k for b, k in self.objects if b == bucket and k.startswith(prefix))
        start = int(continuation_token or 0)
        page = keys[start:start + max_keys]
        nxt = start + len(page)
        return _Page([_Obj(k, len(self.objects[(bucket, k)])) for k in page],
                     truncated=nxt < len(keys), token=str(nxt))

    def get_object_to_file(self, bucket, key, path):
        with open(path, "wb") as f:
            f.write(self.objects[(bucket, key)])

    def put_object_from_file(self, bucket, key, file_path):
        with open(file_path, "rb") as f:
            self.objects[(bucket, key)] = f.read()


# ── 大文件分片断点续传(2026-08-19 办公网 Read timed out 实测)──


class MultipartClient(FakeClient):
    """带 download_file/upload_file 的假客户端,记录哪些对象走了分片通道。"""

    def __init__(self, objects):
        super().__init__(objects)
        self.multipart: list = []

    def download_file(self, bucket, key, file_path, **kw):
        assert kw.get("enable_checkpoint") and kw.get("checkpoint_file")
        self.multipart.append(("dl", key))
        return super().get_object_to_file(bucket, key, file_path)

    def upload_file(self, bucket, key, file_path, **kw):
        assert kw.get("enable_checkpoint") and kw.get("checkpoint_file")
        self.multipart.append(("ul", key))
        return super().put_object_from_file(bucket, key, file_path)


def test_big_files_use_multipart_small_files_do_not(tmp_path, monkeypatch):
    """≥门槛的对象走分片断点续传(download_file/upload_file),小文件走单流;
    checkpoint 必须启用且指到独立文件(断言在假客户端里),不落在源文件旁边。"""
    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    monkeypatch.setattr(tos_store, "_MULTIPART_THRESHOLD", 200)
    st = TosStore("https://tos-cn-beijing.volces.com", "cn-beijing",
                  client=MultipartClient({("bkt", "big.mp4"): b"V" * 300, ("bkt", "small.parquet"): b"P" * 100}))
    st.download("bkt", "big.mp4", str(tmp_path / "in" / "big.mp4"), size=300)
    st.download("bkt", "small.parquet", str(tmp_path / "in" / "small.parquet"), size=100)
    assert st._c.multipart == [("dl", "big.mp4")]
    assert (tmp_path / "in" / "big.mp4").read_bytes() == b"V" * 300
    st.upload(str(tmp_path / "in" / "big.mp4"), "dst", "out/big.mp4")
    st.upload(str(tmp_path / "in" / "small.parquet"), "dst", "out/small.parquet")
    assert st._c.multipart[-1] == ("ul", "out/big.mp4")
    assert st._c.objects[("dst", "out/small.parquet")] == b"P" * 100
    assert sorted(os.listdir(tmp_path / "in")) == ["big.mp4", "small.parquet"], "no checkpoint beside the data"


# ── browser_url:给浏览器的预签名必须公网端点(2026-08-28 dataverse 实见)──


class _Signer:
    """只管签名的假客户端:记录调用,URL 带上自己的主机名以便断言归属。"""

    def __init__(self, host):
        self.host = host
        self.calls = []

    def pre_signed_url(self, method, bucket, key, expires=None):
        self.calls.append((bucket, key, expires))
        r = type("R", (), {})()
        r.signed_url = f"https://{bucket}.{self.host}/{key}?X-Tos-Signature=s"
        return r


def test_browser_url_resigns_on_public_endpoint_when_internal():
    """端点是内网(*.ivolces.com)时:浏览器 URL 换公网端点客户端重签;
    pod 面的 presign 不动,仍走内网 —— 两个受众各签各的。"""
    st = TosStore("https://tos-cn-beijing.ivolces.com", "cn-beijing",
                  client=_Signer("tos-cn-beijing.ivolces.com"),
                  browser_client=_Signer("tos-cn-beijing.volces.com"))
    url = st.browser_url("bkt", "videos/x.mp4")
    assert "ivolces" not in url and ".tos-cn-beijing.volces.com/" in url
    assert st._cb.calls == [("bkt", "videos/x.mp4", 3600)]
    pod = st.presign("bkt", "videos/x.mp4")
    assert "ivolces" in pod and st._c.calls
    assert st.browser_endpoint() == "https://tos-cn-beijing.volces.com"


def test_browser_url_public_endpoint_reuses_main_client():
    main = _Signer("tos-cn-beijing.volces.com")
    st = TosStore("https://tos-cn-beijing.volces.com", "cn-beijing", client=main)
    url = st.browser_url("bkt", "k.mp4", expires=60)
    assert ".tos-cn-beijing.volces.com/" in url
    assert main.calls == [("bkt", "k.mp4", 60)] and st._cb is None, \
        "公网端点没必要建第二个客户端"


def test_browser_url_anonymous_forces_public_host():
    st = TosStore("https://tos-cn-beijing.ivolces.com", "cn-beijing",
                  client=object(), anonymous=True)
    url = st.browser_url("pub", "a b.mp4")
    assert url == "https://pub.tos-cn-beijing.volces.com/a%20b.mp4"
    # 匿名桶 pod 面的裸 URL 仍按自己的端点走(内网读得更快)
    assert "ivolces" in st.public_url("pub", "a.mp4")


# ── 桶→地区登记表(2026-08-28:上海交付桶,轨迹页视频全黑)──────────────


def test_make_store_for_never_learns_from_params(monkeypatch):
    """🔴 毒化回归(2026-08-28 dataverse 实见):调用方传的 (桶, 地区) 只当次
    生效,**绝不写进登记表** —— 用户在界面把地区切到上海的一瞬间,交付目录还是
    北京桶,那个瞬时错误组合曾被登记,毒翻后续全部源数据集读取。
    登记只发生在操作成功之后(mirror/prefetch/resolve)或 locate_bucket 实探。"""
    monkeypatch.setenv("TOS_ACCESS_KEY", "ak")
    monkeypatch.setenv("TOS_SECRET_KEY", "sk")
    tos_store.clear_bucket_regions()
    st = tos_store.make_store_for("bj-bkt", "cn-shanghai", client=object())
    assert st.region == "cn-shanghai", "参数当次照常生效"
    assert tos_store.bucket_region("bj-bkt") is None, "但通讯录一个字不许写"
    # 登记表里已验证的条目,喂给无参调用方(dsfs 这类离参数最远的签名端)
    tos_store.register_bucket_region("sh-bkt", "cn-shanghai")
    st2 = tos_store.make_store_for("sh-bkt", client=object())
    assert st2.region == "cn-shanghai"
    assert "tos-cn-shanghai" in st2.browser_endpoint()
    # 明确参数仍优先于登记(用户当场选的算数),且同样不回写
    st3 = tos_store.make_store_for("sh-bkt", "cn-guangzhou", client=object())
    assert st3.region == "cn-guangzhou"
    assert tos_store.bucket_region("sh-bkt") == "cn-shanghai"
    tos_store.clear_bucket_regions()
