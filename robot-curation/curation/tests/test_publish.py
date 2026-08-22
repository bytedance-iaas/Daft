"""边出边传发布器(2026-08-21 方案 1):文件封口即传、传完即删、完整性标志留给最后。全部离线。"""
from __future__ import annotations

import os
import threading

import pytest

from curation import tos_store
from curation.export import publish


class _FakeStore:
    """记录 upload 调用;可按 key 注入失败。"""

    def __init__(self, fail_keys=()):
        self.uploads: list[tuple[str, str, int]] = []
        self.fail_keys = set(fail_keys)
        self.thread_ids: set[int] = set()

    def upload(self, local_path, bucket, key):
        self.thread_ids.add(threading.get_ident())
        if key in self.fail_keys:
            raise RuntimeError(f"boom {key}")
        self.uploads.append((bucket, key, os.path.getsize(local_path)))


def _mk(root, rel, content=b"x"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(content)
    return p


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


def test_file_done_uploads_in_background_and_deletes_local(tmp_path):
    root = tmp_path / "out"
    st = _FakeStore()
    pub = publish.Publisher(str(root), "tos://bkt/deliveries/x", store=st)
    a = _mk(root, "lerobot_curated/videos/cam/chunk-000/file-000.mp4", b"v" * 100)
    b = _mk(root, "episodes_parquet/part-0.parquet", b"p" * 10)
    assert pub.file_done(a) and pub.file_done(b)
    assert pub.file_done(a) is False, "同一文件不重复排队"
    assert pub.finish() == 2
    assert {k for _b, k, _s in st.uploads} == {
        "deliveries/x/lerobot_curated/videos/cam/chunk-000/file-000.mp4",
        "deliveries/x/episodes_parquet/part-0.parquet"}
    assert not os.path.exists(a) and not os.path.exists(b), "传成功就删本地"
    assert pub.bytes_uploaded == 110
    assert st.thread_ids and threading.get_ident() not in st.thread_ids, "上传在后台线程"
    assert "2 个文件" in pub.summary()


def test_markers_are_deferred_not_uploaded(tmp_path):
    """完整性标志(meta/info.json / passed.json / latest)只记不传 —— 留给 stage_out 最后传。"""
    root = tmp_path / "out"
    st = _FakeStore()
    pub = publish.Publisher(str(root), "tos://bkt/d", store=st)
    info = _mk(root, "lerobot_curated/meta/info.json", b"{}")
    passed = _mk(root, "passed.json", b"{}")
    latest = _mk(root, "latest", b"r")
    tasks = _mk(root, "lerobot_curated/meta/tasks.parquet", b"t")
    for f in (info, passed, latest, tasks):
        pub.file_done(f)
    pub.finish()
    assert [k for _b, k, _s in st.uploads] == ["d/lerobot_curated/meta/tasks.parquet"]
    assert sorted(pub.deferred) == ["latest", "lerobot_curated/meta/info.json", "passed.json"]
    for f in (info, passed, latest):
        assert os.path.exists(f), "标志文件必须留在本地给 stage_out"


def test_dir_done_walks_and_skips_staging_leftovers(tmp_path):
    root = tmp_path / "out"
    st = _FakeStore()
    pub = publish.Publisher(str(root), "tos://bkt/d", store=st)
    _mk(root, "episodes_parquet/a.parquet"); _mk(root, "episodes_parquet/b.parquet")
    _mk(root, "episodes_parquet/.curation-stage-zzz/x.parquet")
    assert pub.dir_done(str(root / "episodes_parquet")) == 2
    pub.finish()
    assert sorted(k for _b, k, _s in st.uploads) == ["d/episodes_parquet/a.parquet",
                                                     "d/episodes_parquet/b.parquet"]


def test_failure_keeps_local_and_raises_at_finish(tmp_path):
    root = tmp_path / "out"
    st = _FakeStore(fail_keys={"d/bad.bin"})
    pub = publish.Publisher(str(root), "tos://bkt/d", store=st)
    good = _mk(root, "good.bin"); bad = _mk(root, "bad.bin")
    pub.file_done(good); pub.file_done(bad)
    with pytest.raises(tos_store.TosStageError) as ei:
        pub.finish()
    assert "bad.bin" in str(ei.value) and "重跑即可续传" in str(ei.value)
    assert not os.path.exists(good) and os.path.exists(bad), "失败的文件留在本地续传"


def test_outside_root_and_after_finish_are_refused(tmp_path):
    root = tmp_path / "out"; root.mkdir()
    pub = publish.Publisher(str(root), "tos://bkt/d", store=_FakeStore())
    other = _mk(tmp_path / "elsewhere", "f.bin")
    with pytest.raises(ValueError):
        pub.file_done(other)
    pub.finish()
    with pytest.raises(RuntimeError):
        pub.file_done(_mk(root, "late.bin"))


def test_module_hooks_are_noop_without_activation(tmp_path):
    f = _mk(tmp_path, "a.bin")
    assert publish.file_done(f) is False and publish.dir_done(str(tmp_path)) == 0
    assert os.path.exists(f)
    st = _FakeStore()
    with publish.activate(publish.Publisher(str(tmp_path), "tos://bkt/d", store=st)) as pub:
        assert publish.active() is pub
        assert publish.file_done(f) is True
        pub.finish()
    assert publish.active() is None
    assert [k for _b, k, _s in st.uploads] == ["d/a.bin"]


def test_v2_export_hands_every_file_to_publisher(tmp_path):
    """v2 导出:每条 parquet / mp4 写完即交发布器(文件边界 = 轨迹边界,不用滚动)。"""
    pytest.importorskip("pandas")
    from curation.export.lerobot_writer import export_lerobot_v2
    from curation.tests.test_lerobot_v2_export import _write_v2_dataset
    src = tmp_path / "src"
    _write_v2_dataset(str(src))
    out = tmp_path / "out" / "lerobot_curated"
    st = _FakeStore()
    with publish.activate(publish.Publisher(str(tmp_path / "out"), "tos://bkt/d", store=st)) as pub:
        export_lerobot_v2(str(src), [0, 2], str(out))
        pub.finish()
    keys = sorted(k for _b, k, _s in st.uploads)
    assert any(k.endswith(".parquet") and "/data/" in k for k in keys), keys
    assert not any("meta/info.json" in k for k in keys), "info.json 是标志,不该由发布器传"
    assert "lerobot_curated/meta/info.json" in pub.deferred
    assert not any(p.endswith(".parquet") for p in
                   [os.path.join(dp, f) for dp, _, fs in os.walk(out / "data") for f in fs]), \
        "data 文件传完应已从本地删除"
