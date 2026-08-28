"""tos_store(TOS 直连存储层)单测:纯逻辑 + 假客户端的 stage in/out + CLI 接线。

全部离线:SDK 从不导入(TosStore 收注入的假客户端),网络零依赖。
"""
from __future__ import annotations

import os

import pytest

from curation import tos_store
from curation.tos_store import (
    TosStageError,
    TosStore,
    TosUrlError,
    endpoint_for_region,
    is_marker,
    parse_tos_url,
    region_from_endpoint,
    stage_in,
    stage_out,
    upload_plan,
)


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


# ── 上传顺序:marker 最后 ────────────────────────────────────────────────


def test_upload_plan_markers_last():
    rels = ["latest", "20260819-1/passed.json", "20260819-1/report.md",
            "20260819-1/lerobot_curated/meta/info.json",
            "20260819-1/lerobot_curated/data/chunk-000/file-000.parquet",
            "20260819-1/run.json"]
    plan = upload_plan(rels)
    assert plan[-1] == "latest"                                   # latest 绝对最后
    assert plan[-2] == "20260819-1/passed.json"                   # 跑批 marker 次后
    assert plan[-3] == "20260819-1/lerobot_curated/meta/info.json"  # 数据集哨兵再前
    assert set(plan[:3]) == {"20260819-1/report.md", "20260819-1/run.json",
                             "20260819-1/lerobot_curated/data/chunk-000/file-000.parquet"}


def test_is_marker():
    assert is_marker("latest") and is_marker("a/b/passed.json")
    assert is_marker("x/meta/info.json") and is_marker("meta/info.json")
    assert not is_marker("run.json") and not is_marker("meta/episodes.jsonl")


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
        self.uploaded: list[str] = []          # 记录上传顺序(marker-last 断言用)

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
        self.uploaded.append(key)


def _store(objects):
    return TosStore("https://tos-s3-cn-beijing.volces.com", "cn-beijing",
                    client=FakeClient(objects))


# ── stage_in ─────────────────────────────────────────────────────────────


def _dataset_objects(bucket="bkt", prefix="datasets/pusht"):
    return {
        (bucket, f"{prefix}/meta/info.json"): b'{"codebase_version": "v2.0"}',
        (bucket, f"{prefix}/data/ep0.parquet"): b"P" * 100,
        (bucket, f"{prefix}/videos/ep0.mp4"): b"V" * 300,
    }


def test_stage_in_downloads_all(tmp_path, monkeypatch):
    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    st = _store(_dataset_objects())
    dest = stage_in("tos://bkt/datasets/pusht", store=st, budget_bytes=10**9)
    assert os.path.getsize(os.path.join(dest, "data/ep0.parquet")) == 100
    assert os.path.getsize(os.path.join(dest, "videos/ep0.mp4")) == 300
    assert open(os.path.join(dest, "meta/info.json")).read().startswith("{")


def test_stage_in_resume_skips_but_refreshes_sentinel(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    objs = _dataset_objects()
    st = _store(objs)
    stage_in("tos://bkt/datasets/pusht", store=st, budget_bytes=10**9)
    # 第二次:大小一致的跳过,哨兵仍重下(缓存新鲜度以远端为准)
    st2 = _store(objs)
    stage_in("tos://bkt/datasets/pusht", store=st2, budget_bytes=10**9)
    out = capsys.readouterr().out
    assert "跳过重下" in out
    assert "1 个文件" in out                    # 只剩哨兵要下


def test_stage_in_prefix_no_sibling_bleed(tmp_path, monkeypatch):
    """前缀按目录语义补斜杠:datasets/pusht 不许把 datasets/pusht-v2 捞进来。"""
    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    objs = _dataset_objects()
    objs[("bkt", "datasets/pusht-v2/meta/info.json")] = b"{}"
    dest = stage_in("tos://bkt/datasets/pusht", store=_store(objs), budget_bytes=10**9)
    assert not os.path.exists(os.path.join(dest, "-v2"))
    assert sorted(os.listdir(dest)) == ["data", "meta", "videos"]


def test_stage_in_budget_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    with pytest.raises(TosStageError, match="预算"):
        stage_in("tos://bkt/datasets/pusht", store=_store(_dataset_objects()),
                 budget_bytes=10)


def test_stage_in_empty_prefix_errors(tmp_path, monkeypatch):
    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    with pytest.raises(TosStageError, match="没有对象"):
        stage_in("tos://bkt/datasets/nope", store=_store(_dataset_objects()),
                 budget_bytes=10**9)


def test_stage_in_pagination(tmp_path, monkeypatch):
    """对象数超过一页也列得全(翻页游标真在翻)。"""
    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    monkeypatch.setattr(tos_store, "_LIST_PAGE", 2)
    objs = {("bkt", f"ds/f{i:02d}.bin"): b"x" * (i + 1) for i in range(7)}
    dest = stage_in("tos://bkt/ds", store=_store(objs), budget_bytes=10**9)
    assert len(os.listdir(dest)) == 7


# ── stage_out ────────────────────────────────────────────────────────────


def _make_delivery(root):
    """本地造一份最小交付树(跑批目录 + 数据集哨兵 + latest)。"""
    run = root / "20260819-070000"
    (run / "lerobot_curated" / "meta").mkdir(parents=True)
    (run / "lerobot_curated" / "meta" / "info.json").write_text("{}")
    (run / "lerobot_curated" / "data.parquet").write_text("PQ")
    (run / "report.md").write_text("# 报告")
    (run / "passed.json").write_text("{}")
    (root / "latest").write_text("20260819-070000\n")
    return root


def test_stage_out_uploads_markers_last(tmp_path):
    local = _make_delivery(tmp_path / "out")
    st = _store({})
    n = stage_out(str(local), "tos://dst/deliveries/pusht", store=st)
    fake = st._c
    assert n == 5 == len(fake.uploaded)
    assert fake.uploaded[-1] == "deliveries/pusht/latest"
    assert fake.uploaded[-2] == "deliveries/pusht/20260819-070000/passed.json"
    assert fake.uploaded[-3].endswith("meta/info.json")
    # 内容真到位
    assert fake.objects[("dst", "deliveries/pusht/latest")] == b"20260819-070000\n"


def test_stage_out_resume_skips_equal_but_not_markers(tmp_path, capsys):
    local = _make_delivery(tmp_path / "out")
    st = _store({})
    stage_out(str(local), "tos://dst/d", store=st)
    st._c.uploaded.clear()
    stage_out(str(local), "tos://dst/d", store=st)          # 重跑:普通文件跳过
    again = st._c.uploaded
    assert set(again) == {"d/latest", "d/20260819-070000/passed.json",
                          "d/20260819-070000/lerobot_curated/meta/info.json"}
    assert "跳过重传" in capsys.readouterr().out


def test_stage_out_skips_safe_write_temps(tmp_path):
    local = _make_delivery(tmp_path / "out")
    (local / "20260819-070000" / ".curation-pub-xyz").write_text("垃圾")
    st = _store({})
    stage_out(str(local), "tos://dst/d", store=st)
    assert not any(".curation-" in k for k in st._c.uploaded)


def test_stage_out_empty_dir_errors(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(TosStageError, match="空的"):
        stage_out(str(tmp_path / "empty"), "tos://dst/d", store=_store({}))


# ── CLI 接线:tos:// 输入输出走 stage_in/stage_out ───────────────────────


def test_cli_run_tos_roundtrip(tmp_path, monkeypatch, capsys):
    """--input/--output 都给 tos:// 时:输入不是 LeRobot(没 meta/info.json,如 RRD)
    → 先 stage_in,跑本地,成功后 stage_out。"""
    from curation import cli
    from curation.ingest import dsfs
    monkeypatch.setattr(dsfs, "exists", lambda p: False)      # 桶里没有 meta/info.json

    staged = tmp_path / "staged-ds"
    staged.mkdir()
    calls = {}

    def fake_stage_in(url, region=None, **kw):
        calls["in"] = (url, region)
        return str(staged)

    def fake_stage_out(local_root, url, region=None, **kw):
        calls["out"] = (local_root, url, region)
        assert os.path.isdir(local_root)        # 本地产出根真被建出来了
        return 3

    def fake_run_pipeline(config, inp, outp, **kw):
        assert inp == str(staged)               # 管道拿到的是本地缓存目录
        assert not str(outp).startswith("tos://")
        os.makedirs(outp, exist_ok=True)
        calls["run"] = (inp, outp)
        return {"stats": {"input": 1}, "run_dir": outp, "n_delivered": 1,
                "deliverables": {}, "robot": {}}

    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    monkeypatch.setattr(tos_store, "stage_in", fake_stage_in)
    monkeypatch.setattr(tos_store, "stage_out", fake_stage_out)
    import curation.pipeline.run as pr
    monkeypatch.setattr(pr, "run_pipeline", fake_run_pipeline)

    rc = cli.main(["run", "--input", "tos://src/datasets/pusht",
                   "--output", "tos://dst/deliveries/pusht",
                   "--input-region", "cn-beijing",
                   "--output-region", "ap-southeast-1", "--lite"])
    assert rc == 0
    assert calls["in"] == ("tos://src/datasets/pusht", "cn-beijing")
    assert calls["out"][1:] == ("tos://dst/deliveries/pusht", "ap-southeast-1")
    assert "交付已上传" in capsys.readouterr().out
    # 输出根固定在缓存下的 桶/前缀(重跑可续传)
    assert calls["out"][0] == os.path.join(str(tmp_path / "cache"), "out",
                                           "dst", "deliveries/pusht")


def test_cli_run_tos_direct_read_for_lerobot(tmp_path, monkeypatch, capsys):
    """2026-08-21 读端会说 tos://:输入桶里有 meta/info.json → **不 stage_in**,
    管道直接拿 tos:// URL;地区通过 dsfs.configure 传进去;输出照旧 stage_out。"""
    from curation import cli
    from curation.ingest import dsfs
    calls = {}
    monkeypatch.setattr(dsfs, "exists",
                        lambda p: p == "tos://src/datasets/pusht/meta/info.json")
    monkeypatch.setattr(dsfs, "configure", lambda region=None: calls.setdefault("rg", region))
    monkeypatch.setattr(tos_store, "stage_in",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("LeRobot 桶不该暂存")))
    monkeypatch.setattr(tos_store, "stage_out", lambda local_root, url, region=None, **kw: 1)

    def fake_run_pipeline(config, inp, outp, **kw):
        assert inp == "tos://src/datasets/pusht"      # 管道直接拿桶地址
        os.makedirs(outp, exist_ok=True)
        calls["run"] = inp
        return {"stats": {"input": 1}, "run_dir": outp, "n_delivered": 1,
                "deliverables": {}, "robot": {}}

    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    import curation.pipeline.run as pr
    monkeypatch.setattr(pr, "run_pipeline", fake_run_pipeline)
    rc = cli.main(["run", "--input", "tos://src/datasets/pusht",
                   "--output", "tos://dst/deliveries/pusht",
                   "--input-region", "cn-beijing", "--lite"])
    assert rc == 0 and calls["run"] == "tos://src/datasets/pusht"
    assert calls["rg"] == "cn-beijing"
    assert "TOS 直读" in capsys.readouterr().out


def test_cli_run_bad_tos_output_fails_before_run(tmp_path, monkeypatch, capsys):
    """输出 URL 不合法必须跑批前就退出,而不是几小时后上传时才炸。"""
    from curation import cli

    def boom(*a, **kw):
        raise AssertionError("URL 不合法时不该起跑批")

    import curation.pipeline.run as pr
    monkeypatch.setattr(pr, "run_pipeline", boom)
    rc = cli.main(["run", "--input", str(tmp_path),
                   "--output", "tos://BAD_BUCKET/x", "--lite"])
    assert rc == 2
    assert "桶名不合法" in capsys.readouterr().err


def test_cli_run_batch_with_tos_input_refused(tmp_path, monkeypatch, capsys):
    from curation import cli

    rc = cli.main(["run", "--batch", "--input", "tos://src/parent",
                   "--output", str(tmp_path / "out"), "--lite"])
    assert rc == 2
    assert "--batch 暂不支持 tos:// 输入" in capsys.readouterr().err


# ── UI 侧(runner)的 TOS 直连校验与 argv 装配 ───────────────────────────


def test_runner_tos_url_and_region_errors():
    from curation.ui import runner

    assert runner.tos_url_error("tos://bkt/datasets/pusht", "数据源") == ""
    assert "数据源" in runner.tos_url_error("s3://bkt/x", "数据源")
    assert "桶名不合法" in runner.tos_url_error("tos://BAD/x", "输出")
    assert runner.tos_region_error("", "数据源") == ""          # 空 = 按部署推导
    assert runner.tos_region_error("cn-beijing", "数据源") == ""
    assert "写法不对" in runner.tos_region_error("北京", "数据源")
    assert "写法不对" in runner.tos_region_error("CN_BEIJING", "输出")


def test_runner_build_argv_with_tos_regions():
    from curation.ui import runner

    argv = runner.build_argv("run", input="tos://src-bkt/datasets/pusht",
                             output="tos://dst-bkt/deliveries/pusht",
                             input_region="cn-beijing",
                             output_region="ap-southeast-1")
    s = " ".join(argv)
    assert "--input tos://src-bkt/datasets/pusht" in s
    assert "--output tos://dst-bkt/deliveries/pusht" in s
    assert "--input-region cn-beijing" in s
    assert "--output-region ap-southeast-1" in s
    # 不给地区就不发旗标(CLI 侧按 TOS_REGION/端点推导)
    argv2 = runner.build_argv("run", input="tos://src-bkt/d", output="/tmp/out")
    assert "--input-region" not in argv2 and "--output-region" not in argv2


def test_runner_tos_regions_aligned_with_rerun():
    """地区下拉与 rerun OpenTosModal 的 TOS_REGIONS 同值同序(两产品一份清单)。"""
    from curation.ui import runner

    assert runner.TOS_REGIONS == ("cn-beijing", "ap-southeast-1",
                                  "ap-southeast-3", "cn-guangzhou",
                                  "cn-hongkong", "cn-shanghai")


def test_runner_default_tos_region(monkeypatch):
    from curation.ui import runner

    monkeypatch.setenv("TOS_REGION", "ap-southeast-1")
    assert runner.default_tos_region() == "ap-southeast-1"
    monkeypatch.delenv("TOS_REGION", raising=False)
    monkeypatch.setenv("TOS_ENDPOINT", "https://tos-s3-cn-shanghai.ivolces.com")
    assert runner.default_tos_region() == "cn-shanghai"
    monkeypatch.delenv("TOS_ENDPOINT", raising=False)
    assert runner.default_tos_region() == "cn-beijing"


# ── 大文件分片断点续传 + 整文件重试(2026-08-19 办公网 Read timed out 实测)──


class FlakyClient(FakeClient):
    """前 fail_times 次下载抛连接错误,之后正常 —— 模拟链路抖动。"""

    def __init__(self, objects, fail_times=1):
        super().__init__(objects)
        self.fails = fail_times
        self.calls = 0

    def get_object_to_file(self, bucket, key, path):
        self.calls += 1
        if self.calls <= self.fails:
            raise ConnectionError("Read timed out")
        return super().get_object_to_file(bucket, key, path)


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


def test_stage_in_retries_transient_failures(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    st = TosStore("https://tos-cn-beijing.volces.com", "cn-beijing",
                  client=FlakyClient(_dataset_objects(), fail_times=1))
    dest = stage_in("tos://bkt/datasets/pusht", store=st, budget_bytes=10**9)
    assert os.path.getsize(os.path.join(dest, "videos/ep0.mp4")) == 300
    assert "重试" in capsys.readouterr().out


def test_stage_in_gives_up_after_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    st = TosStore("https://tos-cn-beijing.volces.com", "cn-beijing",
                  client=FlakyClient(_dataset_objects(), fail_times=99))
    with pytest.raises(TosStageError, match="下载失败"):
        stage_in("tos://bkt/datasets/pusht", store=st, budget_bytes=10**9)


def test_big_files_use_multipart_small_files_do_not(tmp_path, monkeypatch):
    """≥门槛的对象走分片断点续传(download_file/upload_file),小文件走单流;
    checkpoint 必须启用且指到独立文件(断言在假客户端里)。"""
    monkeypatch.setenv(tos_store.CACHE_ENV, str(tmp_path / "cache"))
    monkeypatch.setattr(tos_store, "_MULTIPART_THRESHOLD", 200)
    st = TosStore("https://tos-cn-beijing.volces.com", "cn-beijing",
                  client=MultipartClient(_dataset_objects()))   # mp4=300B, pq=100B
    stage_in("tos://bkt/datasets/pusht", store=st, budget_bytes=10**9)
    assert ("dl", "datasets/pusht/videos/ep0.mp4") in st._c.multipart
    assert all(k != "datasets/pusht/data/ep0.parquet" for _d, k in st._c.multipart)
    # 上传方向:门槛压到 1,交付树全部走分片,checkpoint 不进交付树
    monkeypatch.setattr(tos_store, "_MULTIPART_THRESHOLD", 1)
    local = _make_delivery(tmp_path / "out")
    st2 = TosStore("https://tos-cn-beijing.volces.com", "cn-beijing",
                   client=MultipartClient({}))
    stage_out(str(local), "tos://dst/d", store=st2)
    assert any(d == "ul" for d, _k in st2._c.multipart)
    assert all("ckpt" not in k for _d, k in st2._c.multipart)


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
