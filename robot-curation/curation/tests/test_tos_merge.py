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
