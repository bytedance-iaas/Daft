"""读端会说 tos://(2026-08-21):dsfs 用假桶(本地目录当对象存储)离线验证。

关键判据:同一份数据集,tos:// 读出来与本地读出来**逐字节/逐值一致**;视频不下载
(media_source 给预签名 URL);v2 导出从 tos:// 源拷出的 mp4 与源字节相同。
"""
from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import numpy as np
import pytest

from curation.ingest import dsfs


class _FakeClient:
    """把本地目录当成桶:key = 相对路径。list 分页、get_object、pre_signed_url 三个动作。"""

    def __init__(self, root: str, page: int = 3):
        self.root, self.page = root, page
        self.gets: list[str] = []
        self.presigned: list[str] = []

    def _keys(self):
        out = []
        for dp, _dn, fns in os.walk(self.root):
            for fn in fns:
                p = os.path.join(dp, fn)
                out.append(os.path.relpath(p, self.root).replace(os.sep, "/"))
        return sorted(out)

    def list_objects_type2(self, bucket, prefix="", delimiter=None,
                           continuation_token=None, max_keys=1000):
        keys = [k for k in self._keys() if k.startswith(prefix)]
        start = int(continuation_token or 0)
        chunk = keys[start:start + self.page]
        contents, cps = [], []
        if delimiter:
            seen = set()
            for k in keys:
                rest = k[len(prefix):]
                if delimiter in rest:
                    cp = prefix + rest.split(delimiter, 1)[0] + delimiter
                    if cp not in seen:
                        seen.add(cp)
                        cps.append(SimpleNamespace(prefix=cp))
                else:
                    contents.append(self._obj(k))
            return SimpleNamespace(contents=contents, common_prefixes=cps, is_truncated=False)
        contents = [self._obj(k) for k in chunk]
        more = start + self.page < len(keys)
        return SimpleNamespace(contents=contents, common_prefixes=[], is_truncated=more,
                               next_continuation_token=str(start + self.page) if more else None)

    def _obj(self, k):
        p = os.path.join(self.root, k)
        data = open(p, "rb").read()
        return SimpleNamespace(key=k, size=len(data), etag=hashlib.md5(data).hexdigest())

    def get_object(self, bucket, key):
        self.gets.append(key)
        data = open(os.path.join(self.root, key), "rb").read()
        return SimpleNamespace(read=lambda: data)

    def get_object_to_file(self, bucket, key, local_path):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(open(os.path.join(self.root, key), "rb").read())

    def pre_signed_url(self, method, bucket, key, expires=3600):
        self.presigned.append(key)
        return SimpleNamespace(signed_url=f"https://fake/{bucket}/{key}?sig=1")


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    """tmp_path/bkt 当桶 'bkt';dsfs 的 store 换成假客户端。"""
    from curation import tos_store
    root = tmp_path / "bkt"
    root.mkdir()
    client = _FakeClient(str(root))
    st = tos_store.TosStore("ep", "cn-beijing", client=client)
    monkeypatch.setattr(dsfs, "_store", lambda bucket=None: st)   # _store 按桶挑客户端(2026-08-21)
    dsfs.forget()
    yield root, client
    dsfs.forget()


def _v2(root, n=3):
    from curation.tests.test_lerobot_v2_export import _write_v2_dataset
    return _write_v2_dataset(str(root / "datasets" / "arm"), n_episodes=n,
                             episodes_stats=True)


# ── 基础语义 ──────────────────────────────────────────────────────────────────

def test_join_and_basename_keep_local_behavior_and_scheme():
    assert dsfs.join("tos://b/x/", "meta", "info.json") == "tos://b/x/meta/info.json"
    assert dsfs.join("/data/x", "meta", "info.json") == os.path.join("/data/x", "meta", "info.json")
    assert dsfs.basename("tos://b/x/y/") == "y" and dsfs.basename("/a/b") == "b"
    assert dsfs.is_remote("tos://b/x") and not dsfs.is_remote("/mnt/tos/x")


def test_prefetch_then_exists_isdir_listdir_glob_without_more_network(bucket):
    root, client = bucket
    _v2(root)
    url = "tos://bkt/datasets/arm"
    n = dsfs.prefetch(url)
    assert n > 0
    before = len(client.gets)
    assert dsfs.exists(url + "/meta/info.json")
    assert dsfs.isdir(url + "/meta") and dsfs.isdir(url)
    assert not dsfs.exists(url + "/meta/nope.json")
    assert not dsfs.isdir(url + "/meta/info.json")
    assert "meta" in dsfs.listdir(url) and "info.json" in dsfs.listdir(url + "/meta")
    assert dsfs.glob(url + "/*.rrd") == []
    mp4s = dsfs.glob(url + "/videos/chunk-*/*/episode_*.mp4")
    assert mp4s and all(m.startswith("tos://bkt/datasets/arm/videos/") for m in mp4s)
    assert len(client.gets) == before, "预取后的 exists/glob 不许再出网取对象"


def test_read_json_parquet_and_identity(bucket):
    root, client = bucket
    d = _v2(root)
    url = "tos://bkt/datasets/arm"
    assert dsfs.read_json(url + "/meta/info.json") == dsfs.read_json(os.path.join(d, "meta", "info.json"))
    local_pq = dsfs.glob(os.path.join(d, "data", "chunk-*", "episode_*.parquet"))[0]
    remote_pq = url + "/" + os.path.relpath(local_pq, d).replace(os.sep, "/")
    a, b = dsfs.read_parquet(local_pq), dsfs.read_parquet(remote_pq)
    assert a.equals(b)
    ident = dsfs.content_identity(remote_pq)
    assert ident.startswith("tos-etag:") and str(os.path.getsize(local_pq)) in ident
    assert isinstance(dsfs.mtime_key(remote_pq), str)
    with pytest.raises(FileNotFoundError):
        dsfs.content_identity(url + "/nope.mp4")


def test_media_source_presigns_remote_and_passes_local_through(bucket):
    root, client = bucket
    _v2(root)
    url = "tos://bkt/datasets/arm/videos/x.mp4"
    src = dsfs.media_source(url)
    assert src.startswith("https://fake/bkt/datasets/arm/videos/x.mp4")
    assert client.presigned == ["datasets/arm/videos/x.mp4"]
    assert dsfs.media_source("/mnt/tos/a.mp4") == "/mnt/tos/a.mp4"
    assert client.gets == [], "视频绝不整段取回内存"


def test_unprefetched_path_probes_once(bucket):
    root, client = bucket
    _v2(root)
    assert dsfs.exists("tos://bkt/datasets/arm/meta/info.json")      # 没 prefetch 也能答
    assert dsfs.isdir("tos://bkt/datasets/arm/meta")
    assert not dsfs.exists("tos://bkt/nothing/here")
    assert "arm" in dsfs.listdir("tos://bkt/datasets")


# ── 读端等价 ──────────────────────────────────────────────────────────────────

def test_lerobot_rows_and_meta_from_tos_equal_local(bucket):
    from curation.ingest.lerobot_reader import read_lerobot_meta, read_lerobot_rows
    root, client = bucket
    d = _v2(root)
    url = "tos://bkt/datasets/arm"
    loc = read_lerobot_rows(d, validate=True)
    rem = read_lerobot_rows(url, validate=True)        # validate 会 exists 每个视频指针
    assert len(loc) == len(rem) == 3
    for a, b in zip(loc, rem):
        assert a["episode_id"] == b["episode_id"] and a["instruction"] == b["instruction"]
        np.testing.assert_array_equal(a["action"], b["action"])
        np.testing.assert_array_equal(a["timestamps"], b["timestamps"])
        assert set(a["video"]) == set(b["video"])
        for cam in a["video"]:
            assert b["video"][cam]["path"].startswith(url + "/videos/")
            assert a["video"][cam]["from_ts"] == b["video"][cam]["from_ts"]
    metas = read_lerobot_meta(url)
    assert [m["episode_id"] for m in metas] == [r["episode_id"] for r in loc]
    assert not any(k.endswith(".mp4") for k in client.gets), "读行/读 meta 不碰视频字节"


def test_parent_dir_error_lists_datasets_for_tos(bucket):
    from curation.ingest.lerobot_reader import NotADatasetError, _load_info
    root, _ = bucket
    _v2(root)
    with pytest.raises(NotADatasetError) as ei:
        _load_info("tos://bkt/datasets")
    assert "--input tos://bkt/datasets/arm" in str(ei.value)


def test_stats_prior_and_rrd_sniff_accept_tos(bucket):
    from curation.ingest.rrd_reader import is_rrd_dataset
    from curation.ingest.validate import stats_prior_warnings
    root, _ = bucket
    _v2(root)
    assert is_rrd_dataset("tos://bkt/datasets/arm") is False
    assert isinstance(stats_prior_warnings("tos://bkt/datasets/arm"), list)


def test_export_v2_from_tos_source_copies_video_bytes(bucket, tmp_path):
    from curation.export.lerobot_writer import export_lerobot_v2
    root, client = bucket
    d = _v2(root)
    out = tmp_path / "out"
    stats = export_lerobot_v2("tos://bkt/datasets/arm", [0, 2], str(out))
    assert stats["episodes"] == 2 and stats["videos"] > 0
    src_mp4 = sorted(p for p in dsfs.glob(os.path.join(d, "videos", "chunk-*", "*", "episode_*.mp4"))
                     if p.endswith("episode_000000.mp4"))[0]
    dst_mp4 = sorted(p for p in dsfs.glob(str(out / "videos" / "chunk-*" / "*" / "episode_*.mp4"))
                     if p.endswith("episode_000000.mp4"))[0]
    assert open(src_mp4, "rb").read() == open(dst_mp4, "rb").read()
    assert (out / "meta" / "stats.json").exists() or (out / "meta" / "episodes_stats.jsonl").exists()


def test_dedup_fingerprint_uses_etag_for_remote(bucket):
    from curation.dataset_level.dedup import episode_fingerprint
    from curation.ingest.lerobot_reader import read_lerobot_rows
    root, client = bucket
    d = _v2(root)
    loc = read_lerobot_rows(d, validate=False)
    rem = read_lerobot_rows("tos://bkt/datasets/arm", validate=False)
    fp_r = [episode_fingerprint(r) for r in rem]
    assert len(set(fp_r)) == len(fp_r), "不同 episode 指纹必须不同"
    assert fp_r == [episode_fingerprint(r) for r in rem], "指纹稳定"
    assert not any(k.endswith(".mp4") for k in client.gets), "远端指纹靠 etag,不拉视频"
    _ = loc


# ── 可靠性:瞬断重试 / 远端解码重开 ──────────────────────────────────────────

def test_remote_read_retries_transient_errors(bucket, monkeypatch):
    root, client = bucket
    _v2(root)
    monkeypatch.setattr(dsfs, "_RETRY_SLEEP_S", (0.0, 0.0))
    orig = client.get_object
    fails = {"n": 1}

    def flaky(bucket_, key):
        if fails["n"]:
            fails["n"] -= 1
            raise ConnectionError("reset by peer")
        return orig(bucket_, key)
    monkeypatch.setattr(client, "get_object", flaky)
    assert dsfs.read_json("tos://bkt/datasets/arm/meta/info.json")["codebase_version"] == "v2.0"
    # 连续失败 RETRY_ATTEMPTS 次才放弃
    fails["n"] = 99
    with pytest.raises(ConnectionError):
        dsfs.read_bytes("tos://bkt/datasets/arm/meta/info.json")
    assert fails["n"] == 99 - dsfs.RETRY_ATTEMPTS


def test_remote_decode_reopens_with_fresh_signature(monkeypatch):
    from curation.adapters import decode
    calls = {"sign": 0, "open": 0}
    monkeypatch.setattr(dsfs, "media_source",
                        lambda p: (calls.__setitem__("sign", calls["sign"] + 1) or f"https://u/{calls['sign']}"))

    def once(src, a, b, **kw):
        calls["open"] += 1
        assert kw.get("options") == decode.REMOTE_OPEN_OPTIONS, "远端必须带断线重连选项"
        if calls["open"] == 1:
            raise OSError("Connection reset")
        return ([np.zeros((2, 2, 3), np.uint8)], np.asarray([0.0]))
    monkeypatch.setattr(decode, "_decode_once", once)
    frames, ts = decode.decode_window("tos://bkt/v.mp4", 0.0, 1.0)
    assert len(frames) == 1 and calls["open"] == 2 and calls["sign"] == 2, "重开要重新签名"
    # 本地路径:不签名、不带远端选项、不重试
    calls.update(sign=0, open=0)

    def local_once(src, a, b, **kw):
        calls["open"] += 1
        assert kw.get("options") is None and src == "/x.mp4"
        return ([], np.asarray([]))
    monkeypatch.setattr(decode, "_decode_once", local_once)
    decode.decode_window("/x.mp4", 0.0, 1.0)
    assert calls == {"sign": 0, "open": 1}


def test_dataset_format_and_clip_need_on_tos(bucket):
    from curation.ui import runner
    root, _ = bucket
    _v2(root)
    assert runner.dataset_format("tos://bkt/datasets/arm") == {
        "kind": "lerobot", "version": "v2.0", "needs_clips": False}
    assert runner.dataset_format("tos://bkt/datasets/nope")["kind"] == "unknown"
    assert runner.datasets_needing_clips("tos://bkt/datasets", ["arm"]) == []
