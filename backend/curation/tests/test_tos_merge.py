"""TOS 融合的纯函数层(2026-08-20,融合公开 PR#65 与我们的桶对表)。

快慢分流的两条铁律在这里钉死:
  · URL 对上配置桶 → 挂载零预下载直读(200GB 级数据集只有这条路能跑);
  · 对不上 → stage_in 整树暂存,绝不硬凑到默认桶(同名不同库跑错数据
    是最坏的一类事故)。
"""
from __future__ import annotations

import pytest

from curation.ui import runner


# ── split_dataset_url:深链拆「根前缀 + 数据集名」────────────────────────

def test_split_dataset_url_last_segment_rule():
    assert runner.split_dataset_url("tos://curation/datasets/droid_lerobot") \
        == ("tos://curation/datasets", "droid_lerobot")
    # 多层前缀:只有最后一段是数据集名
    assert runner.split_dataset_url("tos://bkt/a/b/c") == ("tos://bkt/a/b", "c")
    # 只有桶+一段:根前缀就是桶本身
    assert runner.split_dataset_url("tos://bkt/pusht") == ("tos://bkt", "pusht")


def test_split_dataset_url_bucket_only_means_no_preselect():
    """连数据集段都没有 → 数据集名为空,调用方只填根、不预选(不硬猜)。"""
    assert runner.split_dataset_url("tos://bkt") == ("tos://bkt", "")


def test_split_dataset_url_rejects_bad_syntax():
    with pytest.raises(ValueError):
        runner.split_dataset_url("s3://bkt/x")
    with pytest.raises(ValueError):
        runner.split_dataset_url("tos://bkt/../etc")


# ── mount_root_for_url:快慢分流的唯一判据 ───────────────────────────────

_BUCKETS = [
    {"name": "默认", "bucket": "curation", "tos_prefix": "datasets",
     "datasets_path": "/mnt/tos/datasets"},
    {"name": "合成", "bucket": None, "tos_prefix": None,
     "datasets_path": "/data/local"},
]


def test_mount_root_known_bucket_goes_to_mount():
    assert runner.mount_root_for_url("tos://curation/datasets", _BUCKETS) \
        == "/mnt/tos/datasets"
    # 前缀首尾斜杠不敏感
    assert runner.mount_root_for_url("tos://curation/datasets/", _BUCKETS) \
        == "/mnt/tos/datasets"


def test_mount_root_unknown_bucket_is_none_never_guessed():
    """陌生桶/陌生前缀/合成桶(bucket 未知)一律 None —— 走 stage_in,
    绝不回落到默认桶找同名的。"""
    assert runner.mount_root_for_url("tos://elsewhere/datasets", _BUCKETS) is None
    assert runner.mount_root_for_url("tos://curation/raw", _BUCKETS) is None
    assert runner.mount_root_for_url("bad-url", _BUCKETS) is None
    assert runner.mount_root_for_url("tos://curation/datasets", []) is None


# ── tos_list_datasets:一层 common prefix,不深验 ─────────────────────────

class _FakeStore:
    def __init__(self, names):
        self.names = names
        self.calls = []

    def iter_common_prefixes(self, bucket, prefix):
        self.calls.append((bucket, prefix))
        yield from self.names


def test_tos_list_datasets_sorted_unique():
    st = _FakeStore(["b_ds", "a_ds", "b_ds"])
    out = runner.tos_list_datasets("tos://bkt/datasets", store=st)
    assert out == ["a_ds", "b_ds"]
    assert st.calls == [("bkt", "datasets")]


# ── iter_common_prefixes:delimiter 一层,分页翻到底 ──────────────────────

class _Page:
    def __init__(self, prefixes, truncated, token=None):
        self.common_prefixes = [type("CP", (), {"prefix": p})() for p in prefixes]
        self.contents = []
        self.is_truncated = truncated
        self.next_continuation_token = token


class _FakeClient:
    """两页翻页;记录 delimiter 真的传了(不传就退化成全量枚举)。"""

    def __init__(self):
        self.calls = []

    def list_objects_type2(self, bucket, prefix="", delimiter=None,
                           continuation_token=None, max_keys=None):
        self.calls.append({"delimiter": delimiter, "token": continuation_token})
        if continuation_token is None:
            return _Page([prefix + "ds1/", prefix + "ds2/"], True, "T")
        return _Page([prefix + "ds3/"], False)


def test_iter_common_prefixes_uses_delimiter_and_pages():
    from curation.tos_store import TosStore
    c = _FakeClient()
    st = TosStore("ep", "cn-beijing", client=c)
    assert list(st.iter_common_prefixes("bkt", "datasets")) \
        == ["ds1", "ds2", "ds3"]
    assert all(x["delimiter"] == "/" for x in c.calls), \
        "没传 delimiter —— 一次下拉会变成对几十万对象的全量枚举"
    assert [x["token"] for x in c.calls] == [None, "T"]


# ── 阶段4:报告页读端懒镜像(2026-08-20)────────────────────────────────────

class _MirrorStore:
    """假 store:objects = {key: bytes};记录下载/上传的 key。"""

    def __init__(self, objects):
        self.objects = dict(objects)
        self.downloaded = []
        self.uploaded = []

    def iter_objects(self, bucket, prefix):
        for k in sorted(self.objects):
            if k.startswith(prefix):
                yield k, len(self.objects[k])

    def iter_common_prefixes(self, bucket, prefix):
        p = prefix.strip("/") + "/" if prefix.strip("/") else ""
        seen = set()
        for k in sorted(self.objects):
            if k.startswith(p):
                rest = k[len(p):]
                if "/" in rest:
                    seen.add(rest.split("/")[0])
        yield from sorted(seen)

    def download(self, bucket, key, local_path, size=None):
        import os
        self.downloaded.append(key)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(self.objects[key])

    def upload(self, local_path, bucket, key):
        self.uploaded.append(key)


def _mirror_objects():
    run = "deliv/d1/20260820-000001"
    return {
        f"{run}/passed.json": b'{"a":1}',
        f"{run}/report.md": b"# r",
        f"{run}/details/audit_clips/c.mp4": b"mp4",
        # 大件:绝不下载(几百 MB~GB 级,报告/裁决用不到字节)
        f"{run}/episodes_parquet/part-0.parquet": b"x" * 50,
        f"{run}/lerobot_curated/videos/e0.mp4": b"x" * 50,
        "deliv/d1/human-decisions/label_decisions.csv": b"h\n",
        "deliv/d1/latest": b"20260820-000001\n",
    }


def test_mirror_run_skips_dataset_dirs_and_writes_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("CURATION_TOS_CACHE", str(tmp_path))
    st = _MirrorStore(_mirror_objects())
    local = runner.mirror_run("tos://bkt/deliv/d1/20260820-000001",
                              "cn-beijing", store=st)
    import os
    assert os.path.isfile(os.path.join(local, "passed.json"))
    assert os.path.isfile(os.path.join(local, "details", "audit_clips", "c.mp4"))
    assert not any("episodes_parquet" in k or "lerobot_curated" in k
                   for k in st.downloaded), "大件目录被下载了——懒镜像名存实亡"
    # human-decisions 与 latest 一并镜像(跨批次状态,裁决队列靠它)
    deliv_local = os.path.dirname(local)
    assert os.path.isfile(os.path.join(deliv_local, "human-decisions",
                                       "label_decisions.csv"))
    origin = runner.tos_origin_of(local)
    assert origin and origin["delivery_url"] == "tos://bkt/deliv/d1"
    assert origin["region"] == "cn-beijing"


def test_mirror_run_resumes_by_size(tmp_path, monkeypatch):
    """文件级续传按「本地大小==远端大小」跳过;CSV 变长(追加式)自然重下。"""
    monkeypatch.setenv("CURATION_TOS_CACHE", str(tmp_path))
    st = _MirrorStore(_mirror_objects())
    runner.mirror_run("tos://bkt/deliv/d1/20260820-000001", None, store=st)
    n1 = len(st.downloaded)
    st.downloaded.clear()
    runner.mirror_run("tos://bkt/deliv/d1/20260820-000001", None, store=st)
    # latest **刻意**每次重下:内容是定宽时间戳,按大小对账永远"没变",
    # 靠大小续传它就永远陈旧(同 stage_in 对 meta/info.json 一律重下的纪律)
    assert st.downloaded == ["deliv/d1/latest"], \
        "除 latest 外同大小文件被重下——续传对账没生效"
    st.objects["deliv/d1/human-decisions/label_decisions.csv"] = b"h\nrow2\n"
    st.downloaded.clear()
    runner.mirror_run("tos://bkt/deliv/d1/20260820-000001", None, store=st)
    assert st.downloaded == ["deliv/d1/human-decisions/label_decisions.csv",
                             "deliv/d1/latest"]
    assert n1 > 0


def test_tos_run_choices_excludes_human_decisions_and_sorts_new_first():
    st = _MirrorStore(_mirror_objects())
    st.objects["deliv/d1/20260819-000009/report.md"] = b"# old"
    rc = runner.tos_run_choices("tos://bkt/deliv/d1", store=st)
    assert [lab for lab, _v in rc] == ["20260820-000001", "20260819-000009"]
    assert rc[0][1] == "tos://bkt/deliv/d1/20260820-000001"
    assert all("human-decisions" not in lab for lab, _v in rc)


def test_push_decisions_uploads_csvs_back_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("CURATION_TOS_CACHE", str(tmp_path))
    st = _MirrorStore(_mirror_objects())
    local = runner.mirror_run("tos://bkt/deliv/d1/20260820-000001",
                              None, store=st)
    note = runner.push_decisions(local, store=st)
    assert st.uploaded == ["deliv/d1/human-decisions/label_decisions.csv"]
    assert "已同步回 tos://bkt/deliv/d1" in note


def test_push_decisions_is_silent_noop_for_local_deliveries(tmp_path):
    """挂载交付(没有 .tos-origin)→ 空串:裁决提示语一个字不多。"""
    assert runner.push_decisions(str(tmp_path / "deliv" / "run1")) == ""


# ── tos_dataset_listing:下拉只列真数据集(issue #98)─────────────────────

class _DsStore:
    """假 store:head 认 meta/info.json 的键;list_dir/iter_common_prefixes 按表回。"""

    def __init__(self, children, dataset_prefixes):
        self.children = children              # 一层子目录名
        self.ds = set(dataset_prefixes)       # 哪些前缀算数据集(有 meta/info.json)

    def iter_common_prefixes(self, bucket, prefix):
        yield from self.children

    def head(self, bucket, key):
        assert key.endswith("meta/info.json")
        pfx = key[: -len("/meta/info.json")]
        if pfx in self.ds:
            return (10, "etag")
        raise KeyError(key)

    def list_dir(self, bucket, prefix):
        return ([], [])


def test_tos_dataset_listing_filters_non_datasets():
    """混杂目录:deliveries/review 之类不进下拉;junk 留作空清单诊断词料。"""
    st = _DsStore(["droid_100", "deliveries", "review", "aloha"],
                  {"datasets/droid_100", "datasets/aloha"})
    out = runner.tos_dataset_listing("tos://bkt/datasets", store=st)
    assert out["kind"] == "list"
    assert out["names"] == ["aloha", "droid_100"]
    assert set(out["junk"]) == {"deliveries", "review"}


def test_tos_dataset_listing_recognizes_dataset_itself():
    """用户把数据集本身填进目录框 → 退层预选,不列 meta/data/videos 当数据集。"""
    st = _DsStore(["meta", "data", "videos"], {"dataset/droid_100"})
    out = runner.tos_dataset_listing("tos://bkt/dataset/droid_100", store=st)
    assert out == {"kind": "dataset", "name": "droid_100",
                   "parent": "tos://bkt/dataset"}


def test_tos_dataset_listing_all_junk_keeps_diagnostic_names():
    st = _DsStore(["misc", "logs"], set())
    out = runner.tos_dataset_listing("tos://bkt/stuff", store=st)
    assert out["kind"] == "list" and out["names"] == []
    assert out["junk"] == ["logs", "misc"]
