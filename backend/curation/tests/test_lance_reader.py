"""lance(lerobot-lance-convert 三表布局,V1)reader 验收(P2,2026-09-21)。

合成小三表数据集现场写、现场读:钉住布局探测与开关、storage_format 标记强制、
v3 语义直通(fps/robot_type/任务全来自 meta/)、frames 按 index 切片对账、
videos blob 落盘幂等与共享、source-column-name-map 列名对账、meta.lance 物化兜底、
三表不一致响亮失败。真实数据(lance-format/pusht-lance)的验收走手动 E2E,不进单测。
"""
from __future__ import annotations

import io
import json
import os

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("lance", reason="未装 pylance(lance 是可选格式)")

from curation.ingest.lerobot_reader import NotADatasetError  # noqa: E402
from curation.ingest.lance_reader import (  # noqa: E402
    is_lance_dataset,
    read_lance_meta,
    read_lance_rows,
)

FPS = 5.0
N = 8            # 每 episode 帧数
N_EP = 2
VKEY = "observation.image"


def _merged_mp4(n_frames: int, size: int = 32) -> bytes:
    """一个合并 mp4(两条 episode 的帧首尾相接,v3 布局的本性)。"""
    import av

    buf = io.BytesIO()
    with av.open(buf, "w", format="mp4") as out:
        s = out.add_stream("libx264", rate=round(FPS))
        s.width = s.height = size
        s.pix_fmt = "yuv420p"
        for i in range(n_frames):
            img = np.full((size, size, 3), (i * 9) % 255, dtype=np.uint8)
            for pkt in s.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                out.mux(pkt)
        for pkt in s.encode():
            out.mux(pkt)
    return buf.getvalue()


def _synth_action(ep: int, n: int = N, dim: int = 3) -> np.ndarray:
    t = np.linspace(0.0, 1.0, n)
    return np.stack([t * (d + 1) + ep * 10 for d in range(dim)], axis=1).astype(np.float32)


def _write_v1(root: str, *, stamp: str = "lance", colmap: dict | None = None,
              action_col: str = "action", with_meta_dir: bool = True,
              truncate_frames: bool = False):
    """写一个最小 V1 三表数据集,返回 {episode: action}(供 roundtrip 对账)。

    colmap 写 source-column-name-map 进 frames 的 schema metadata(测列名对账);
    with_meta_dir=False 时 meta/ 不落目录、只进 meta.lance 镜像(测物化兜底);
    truncate_frames 砍掉 frames 尾部几行(测三表不一致的响亮失败)。
    """
    import lance
    import pyarrow as pa

    acts = {e: _synth_action(e) for e in range(N_EP)}
    total = N_EP * N

    info = {
        "codebase_version": "v3.0", "fps": FPS, "robot_type": "so101_follower",
        "total_episodes": N_EP, "total_frames": total, "chunks_size": 1000,
        "storage_format": stamp,
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
        "features": {
            VKEY: {"dtype": "video", "shape": [32, 32, 3]},
            "observation.state": {"dtype": "float32", "names": ["x", "y", "z"]},
            "action": {"dtype": "float32", "names": ["x", "y", "z"]},
            "timestamp": {"dtype": "float32"},
            "index": {"dtype": "int64"},
            "episode_index": {"dtype": "int64"},
            "task_index": {"dtype": "int64"},
        },
    }
    if stamp is None:
        info.pop("storage_format")
    ep_rows = []
    for e in range(N_EP):
        ep_rows.append({
            "episode_index": e, "length": N,
            "dataset_from_index": e * N, "dataset_to_index": (e + 1) * N,
            "tasks": ["push the T block"],
            f"videos/{VKEY}/chunk_index": 0, f"videos/{VKEY}/file_index": 0,
            f"videos/{VKEY}/from_timestamp": e * N / FPS,
            f"videos/{VKEY}/to_timestamp": (e + 1) * N / FPS,
        })
    meta_files = {
        "meta/info.json": json.dumps(info).encode(),
    }
    ep_parquet = io.BytesIO()
    pd.DataFrame(ep_rows).to_parquet(ep_parquet)
    meta_files["meta/episodes/chunk-000/file-000.parquet"] = ep_parquet.getvalue()
    if with_meta_dir:
        for path, data in meta_files.items():
            p = os.path.join(root, path)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)

    # frames.lance:列名 = 特征名点转下划线(或 colmap 指定的自定义名)
    def cname(feat):
        return (colmap or {}).get(feat) or feat.replace(".", "_")

    n_rows = total - (2 if truncate_frames else 0)
    idx = list(range(n_rows))
    fr = pa.table({
        cname("index"): pa.array(idx, pa.int64()),
        cname("episode_index"): pa.array([i // N for i in idx], pa.int64()),
        cname("timestamp"): pa.array([(i % N) / FPS for i in idx], pa.float32()),
        cname("task_index"): pa.array([0] * n_rows, pa.int64()),
        (colmap or {}).get("action") or action_col:
            pa.array([acts[i // N][i % N].tolist() for i in idx],
                     pa.list_(pa.float32(), 3)),
        cname("observation.state"):
            pa.array([(acts[i // N][i % N] + 0.5).tolist() for i in idx],
                     pa.list_(pa.float32(), 3)),
    })
    if colmap:
        fr = fr.replace_schema_metadata({
            "source-column-name-map": json.dumps(
                {v: k for k, v in colmap.items()})})
    lance.write_dataset(fr, os.path.join(root, "frames.lance"))

    vschema = pa.schema([
        pa.field("video_key", pa.string()),
        pa.field("chunk_index", pa.int64()),
        pa.field("file_index", pa.int64()),
        pa.field("video_bytes", pa.large_binary(),
                 metadata={"lance-encoding:blob": "true"}),
    ])
    vt = pa.table({"video_key": [VKEY], "chunk_index": [0], "file_index": [0],
                   "video_bytes": [_merged_mp4(total)]}, schema=vschema)
    lance.write_dataset(vt, os.path.join(root, "videos.lance"))

    mt = pa.table({"path": list(meta_files), "data": list(meta_files.values())})
    lance.write_dataset(mt, os.path.join(root, "meta.lance"))
    return acts


@pytest.fixture(autouse=True, scope="module")
def _clean_video_cache():
    """本模块跑完清掉 reader 落的临时 mp4/物化 meta(与 test_rrd_reader 同纪律)。"""
    yield
    from curation.ingest.lance_reader import cleanup_video_cache
    cleanup_video_cache()


@pytest.fixture()
def v1_root(tmp_path):
    d = str(tmp_path / "synth-lance")
    os.makedirs(d)
    acts = _write_v1(d)
    return d, acts


def test_sniff_and_switch(v1_root, tmp_path):
    d, _ = v1_root
    assert is_lance_dataset(d)                       # frames+videos 两表是身份证
    assert not is_lance_dataset(str(tmp_path))
    from curation.ingest import lance_reader
    lance_reader.set_enabled(False)
    try:
        assert not is_lance_dataset(d)               # 开关关着 = 当它不存在
        # 且 LeRobot 路径按 storage_format 标记给明确的"未开放"话,不误导
        from curation.ingest.lerobot_reader import _load_info
        with pytest.raises(NotADatasetError) as e:
            _load_info(d)
        assert "lance_enabled" in str(e.value)
    finally:
        lance_reader.set_enabled(True)


def test_v3_semantics_roundtrip(v1_root):
    """v3 语义直通:fps/robot_type/任务全来自 meta/;action 逐值 roundtrip;
    视频指针 = 共享合并 mp4 + episodes 元数据里的时间窗。"""
    d, acts = v1_root
    rows = read_lance_rows(d)
    assert [r["episode_id"] for r in rows] == ["ep000000", "ep000001"]
    r0, r1 = rows
    assert np.allclose(r0["action"], acts[0], atol=1e-6)
    assert np.allclose(r1["action"], acts[1], atol=1e-6)
    assert np.allclose(r0["proprio_state"], acts[0] + 0.5, atol=1e-6)
    assert r0["fps"] == FPS                          # info.json 的 fps,非推断
    assert r0["embodiment_id"] == "so101_follower"   # info.json 的 robot_type
    assert r0["instruction"] == "push the T block"   # episodes 表的 tasks
    v0, v1 = r0["video"][VKEY], r1["video"][VKEY]
    assert v0["path"] == v1["path"]                  # 两条共享同一个合并 mp4
    assert os.path.exists(v0["path"])
    assert (v0["from_ts"], v0["to_ts"]) == (0.0, pytest.approx(N / FPS))
    assert (v1["from_ts"], v1["to_ts"]) == (pytest.approx(N / FPS),
                                            pytest.approx(2 * N / FPS))
    import av
    with av.open(v0["path"]) as c:
        assert sum(1 for _ in c.decode(c.streams.video[0])) == N_EP * N
    assert json.loads(r0["semantics_extras"])["source_format"] == "lance"


def test_missing_stamp_rejected(tmp_path):
    """三表齐但 info.json 无 storage_format 标记 → 不是官方转换产出,响亮拒绝。"""
    d = str(tmp_path / "nostamp")
    os.makedirs(d)
    _write_v1(d, stamp=None)
    with pytest.raises(NotADatasetError) as e:
        read_lance_rows(d)
    assert "lerobot-lance-convert" in str(e.value)


def test_source_column_name_map(tmp_path):
    """转换器改过列名时按 source-column-name-map 对账(不靠下划线约定猜)。"""
    d = str(tmp_path / "mapped")
    os.makedirs(d)
    acts = _write_v1(d, colmap={"action": "act_v2", "index": "global_idx"})
    rows = read_lance_rows(d)
    assert np.allclose(rows[0]["action"], acts[0], atol=1e-6)


def test_three_table_mismatch_loud(tmp_path):
    """frames 行数与 meta 边界对不上(残缺拷贝/转换中断)→ 响亮失败,绝不静默。"""
    d = str(tmp_path / "trunc")
    os.makedirs(d)
    _write_v1(d, truncate_frames=True)
    with pytest.raises(NotADatasetError) as e:
        read_lance_rows(d)
    assert "三表不一致" in str(e.value)


def test_meta_lance_materialization(tmp_path, capsys):
    """meta/ 目录缺失(只拷了三张表)→ 从 meta.lance 镜像物化,读取结果一致。"""
    d = str(tmp_path / "tablesonly")
    os.makedirs(d)
    acts = _write_v1(d, with_meta_dir=False)
    rows = read_lance_rows(d)
    assert "物化" in capsys.readouterr().out
    assert rows[0]["embodiment_id"] == "so101_follower"
    assert np.allclose(rows[1]["action"], acts[1], atol=1e-6)


def test_episode_selection_and_meta(v1_root):
    d, acts = v1_root
    rows = read_lance_rows(d, episode_indices={1})
    assert [r["episode_id"] for r in rows] == ["ep000001"]
    assert np.allclose(rows[0]["action"], acts[1], atol=1e-6)
    metas = read_lance_meta(d, max_episodes=1)
    assert metas[0]["episode_id"] == "ep000000"
    assert metas[0]["length"] == N
    assert os.path.exists(metas[0]["video"][VKEY]["path"])


def test_cleanup_never_deletes_source(tmp_path):
    """P1 回归(2026-09-21 审查):源数据集本身在临时目录下 + 输入路径带尾斜杠,
    收尾清理**绝不能**碰源数据 —— 只删本模块自己 mkdtemp 的目录。"""
    from curation.ingest.lance_reader import cleanup_video_cache

    d = str(tmp_path / "src-in-tmp")        # pytest tmp_path 就在系统临时目录下
    os.makedirs(d)
    _write_v1(d)
    rows = read_lance_rows(d + "/")          # 尾斜杠:曾触发 m != k 的误判
    assert len(rows) == N_EP
    cleanup_video_cache(d + "/")
    cleanup_video_cache()                    # 全量收尾再来一遍
    assert os.path.isfile(os.path.join(d, "meta", "info.json"))   # 源数据完好
    assert os.path.isdir(os.path.join(d, "frames.lance"))
    # 但 reader 落的临时 mp4 确实被清了
    assert not os.path.exists(rows[0]["video"][VKEY]["path"])


def test_meta_lance_traversal_rejected(tmp_path):
    """P1 回归(2026-09-21 审查):meta.lance 镜像里的 ../ 越界路径响亮拒绝,
    不写任何盘 —— 镜像行是客户数据,必须当不可信输入。"""
    import lance
    import pyarrow as pa

    d = str(tmp_path / "evil")
    os.makedirs(d)
    _write_v1(d, with_meta_dir=False)
    lance.write_dataset(
        pa.table({"path": ["../../pwned.txt"], "data": [b"evil"]}),
        os.path.join(d, "meta.lance"), mode="append")
    with pytest.raises(NotADatasetError) as e:
        read_lance_rows(d)
    assert "越界" in str(e.value)


def test_persist_videos_survives_cleanup(v1_root, tmp_path):
    """P2 回归(2026-09-21 审查):交付前持久化视频并改写指针 —— 收尾清理后
    episodes_parquet 里的视频路径仍然有效;共享 mp4 只拷一份。"""
    from curation.ingest.lance_reader import cleanup_video_cache, persist_videos

    d, _ = v1_root
    rows = read_lance_rows(d)
    dest = str(tmp_path / "delivery_videos")
    n = persist_videos(rows, dest)
    assert n == 1                                        # 两条 episode 共享一个合并 mp4
    p0 = rows[0]["video"][VKEY]["path"]
    assert p0.startswith(dest) and os.path.isfile(p0)
    assert rows[1]["video"][VKEY]["path"] == p0          # 共享关系保持
    cleanup_video_cache(d)
    assert os.path.isfile(p0)                            # 清理临时缓存后交付内仍在


def test_blob_materialization_idempotent(v1_root):
    """同一合并 mp4 被多条 episode/多遍读取引用:只落盘一次,原子写。"""
    d, _ = v1_root
    p1 = read_lance_rows(d, max_episodes=1)[0]["video"][VKEY]["path"]
    st1 = os.stat(p1)
    p2 = read_lance_meta(d)[0]["video"][VKEY]["path"]
    st2 = os.stat(p2)
    assert p1 == p2
    assert (st1.st_mtime_ns, st1.st_size) == (st2.st_mtime_ns, st2.st_size)
    assert not os.path.exists(p1 + ".part")
