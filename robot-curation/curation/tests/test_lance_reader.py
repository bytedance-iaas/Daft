"""lance/lancedb reader + 交付导出验收(P1,2026-09-18)。

合成小 lance 表现场写、现场读:钉住列名自动识别、mapping 覆盖、表/库目录两种输入、
多表要指名、时间轴三级降级(timestamp 列 → metadata fps → 配置 fps → 响亮失败)、
JPEG 帧封装 mp4 的可解码性与幂等、非 JPEG 响亮拒绝、开关默认关、
lance_curated 导出(过滤/改标/溯源列/index.json)。
"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

pytest.importorskip("lance", reason="未装 pylance(lance 是可选格式)")

from curation.ingest.lerobot_reader import NotADatasetError  # noqa: E402
from curation.ingest.lance_reader import (  # noqa: E402
    is_lance_dataset,
    read_lance_meta,
    read_lance_rows,
)

FPS = 5.0
N = 8


def _jpeg(i: int, size: int = 32) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".jpg", np.full((size, size, 3), (i * 30) % 255, np.uint8))
    assert ok
    return buf.tobytes()


def _synth_action(ep: int, n: int = N, dim: int = 3) -> np.ndarray:
    t = np.linspace(0.0, 1.0, n)
    return np.stack([t * (d + 1) + ep * 10 for d in range(dim)], axis=1).astype(np.float32)


def _write_table(path: str, n_ep: int = 2, *, with_ts: bool = True,
                 metadata: dict | None = None, names: dict | None = None,
                 video_kind: str = "frames", blob_override: bytes | None = None):
    """写一张帧级 lance 表,返回 {episode: action}(供 roundtrip 对账)。

    names 可改列名(测 mapping 覆盖与"认不出"的报错);
    video_kind: "frames"=逐帧 JPEG 字节列 / "path"=每 episode 一个 mp4 的路径列。
    """
    import lance
    import pyarrow as pa

    col = {"episode": "episode_index", "timestamp": "timestamp", "action": "action",
           "state": "observation_state", "task": "task",
           "cam": "observation_images_cam0", **(names or {})}
    acts = {e: _synth_action(e) for e in range(n_ep)}
    data: dict = {col["episode"]: [], col["action"]: [], col["state"]: [],
                  col["task"]: [], col["cam"]: []}
    if with_ts:
        data[col["timestamp"]] = []
    for e in range(n_ep):
        for i in range(N):
            data[col["episode"]].append(e)
            if with_ts:
                data[col["timestamp"]].append(100.0 + i / FPS)   # 绝对时间:读端要归零
            data[col["action"]].append(acts[e][i].tolist())
            data[col["state"]].append((acts[e][i] + 0.5).tolist())
            data[col["task"]].append("put spoon in tray")
            data[col["cam"]].append(blob_override if blob_override is not None
                                    else _jpeg(i))
    arrays = {
        col["episode"]: pa.array(data[col["episode"]], pa.int64()),
        col["action"]: pa.array(data[col["action"]], pa.list_(pa.float32())),
        col["state"]: pa.array(data[col["state"]], pa.list_(pa.float32())),
        col["task"]: pa.array(data[col["task"]], pa.string()),
        col["cam"]: (pa.array(data[col["cam"]], pa.string())
                     if video_kind == "path"
                     else pa.array(data[col["cam"]], pa.large_binary())),
    }
    if with_ts:
        arrays[col["timestamp"]] = pa.array(data[col["timestamp"]], pa.float64())
    tbl = pa.table(arrays)
    if metadata:
        tbl = tbl.replace_schema_metadata({k: str(v) for k, v in metadata.items()})
    lance.write_dataset(tbl, path)
    return acts


@pytest.fixture(autouse=True, scope="module")
def _clean_video_cache():
    """本模块跑完清掉 reader 落的临时 mp4(与 test_rrd_reader 同一道纪律)。"""
    yield
    from curation.ingest.lance_reader import cleanup_video_cache
    cleanup_video_cache()


@pytest.fixture()
def table_dir(tmp_path):
    """两条 episode 的合成 lance 表(帧级行,JPEG 帧,绝对 timestamp)。"""
    d = str(tmp_path / "synth.lance")
    acts = _write_table(d, metadata={"robot_type": "so101_follower"})
    return d, acts


def test_sniff_table_and_db_dir(table_dir, tmp_path):
    d, _ = table_dir
    assert is_lance_dataset(d)                        # 表本体
    assert is_lance_dataset(str(tmp_path))            # 含 *.lance 的库目录
    assert not is_lance_dataset(str(tmp_path / "nope"))
    # 开关关着 = 当它不存在(管线嗅探入口)
    from curation.ingest import lance_reader
    lance_reader.set_enabled(False)
    try:
        assert not is_lance_dataset(d)
    finally:
        lance_reader.set_enabled(True)


def test_auto_mapping_roundtrip(table_dir):
    """默认列名自动识别;action 数值逐值 roundtrip;timestamp 归零;视频可解码。"""
    d, acts = table_dir
    rows = read_lance_rows(d)
    assert [r["episode_id"] for r in rows] == ["ep000000", "ep000001"]
    r = rows[0]
    assert np.allclose(r["action"], acts[0], atol=1e-6)
    assert np.allclose(r["proprio_state"], acts[0] + 0.5, atol=1e-6)
    assert r["action"].dtype == np.float32 and r["action"].ndim == 2
    assert r["instruction"] == "put spoon in tray"
    assert r["embodiment_id"] == "so101_follower"     # schema metadata 内嵌
    assert r["fps"] == pytest.approx(FPS)
    assert np.allclose(r["timestamps"], np.arange(N) / FPS, atol=1e-6)  # 绝对→相对
    v = r["video"]["observation_images_cam0"]
    assert (v["from_ts"], v["to_ts"]) == (0.0, pytest.approx(N / FPS))
    assert os.path.exists(v["path"])
    import av
    with av.open(v["path"]) as c:
        n = sum(1 for _ in c.decode(c.streams.video[0]))
    assert n == N
    assert json.loads(r["semantics_extras"])["source_format"] == "lance"


def test_db_dir_single_table_auto(table_dir, tmp_path):
    """库目录单表自动选:--input 指父目录与指表本体读出同一批行。"""
    d, acts = table_dir
    rows = read_lance_rows(str(tmp_path))
    assert np.allclose(rows[0]["action"], acts[0], atol=1e-6)


def test_db_dir_multi_table_requires_name(tmp_path):
    """库目录多表不指名要响亮报错并列出实有表;指名后能读。"""
    _write_table(str(tmp_path / "alpha.lance"))
    acts = _write_table(str(tmp_path / "beta.lance"), n_ep=1)
    with pytest.raises(NotADatasetError) as e:
        read_lance_rows(str(tmp_path))
    assert "alpha" in str(e.value) and "beta" in str(e.value)
    assert "lance_table" in str(e.value)
    rows = read_lance_rows(str(tmp_path), table="beta")
    assert len(rows) == 1
    assert np.allclose(rows[0]["action"], acts[0], atol=1e-6)


def test_mapping_override_and_unknown_column_error(tmp_path):
    """客户自定义列名:默认认不出要列出实见列名;mapping 覆盖后能读。"""
    d = str(tmp_path / "custom.lance")
    acts = _write_table(d, names={"action": "cmd", "state": "prop",
                                  "task": "instr", "cam": "cams_rgb"})
    with pytest.raises(NotADatasetError) as e:
        read_lance_rows(d)
    msg = str(e.value)
    assert "cmd" in msg and "cams_rgb" in msg        # 实际的列要点名
    assert "action" in msg and "mapping" in msg      # 期望的约定 + 出路
    rows = read_lance_rows(d, mapping={"action": "cmd", "state": "prop",
                                       "task": "instr", "video_prefix": "cams_"})
    assert np.allclose(rows[0]["action"], acts[0], atol=1e-6)
    assert set(rows[0]["video"]) == {"cams_rgb"}


def test_missing_time_info_fallbacks(tmp_path):
    """无 timestamp 列:metadata fps → 配置 fps → 都没有响亮失败给可照抄的命令。"""
    d1 = str(tmp_path / "md.lance")
    _write_table(d1, with_ts=False, metadata={"fps": "10"})
    r = read_lance_rows(d1, max_episodes=1)[0]
    assert r["fps"] == 10.0
    assert np.allclose(r["timestamps"], np.arange(N) / 10.0)

    d2 = str(tmp_path / "notime.lance")
    _write_table(d2, with_ts=False)
    with pytest.raises(NotADatasetError) as e:
        read_lance_rows(d2)
    assert "ingest.lance_fps=30" in str(e.value)
    r = read_lance_rows(d2, fps=4.0, max_episodes=1)[0]
    assert r["fps"] == 4.0


def test_video_path_column(tmp_path):
    """string 相机列 = 每 episode 一个视频文件:相对路径按表目录解析,原样当指针。"""
    import av

    vid = tmp_path / "ep0.mp4"
    with av.open(str(vid), "w", format="mp4") as out:
        s = out.add_stream("libx264", rate=5)
        s.width = s.height = 32
        s.pix_fmt = "yuv420p"
        for i in range(N):
            for pkt in s.encode(av.VideoFrame.from_ndarray(
                    np.full((32, 32, 3), i * 7 % 255, np.uint8), format="rgb24")):
                out.mux(pkt)
        for pkt in s.encode():
            out.mux(pkt)
    d = str(tmp_path / "byref.lance")
    import lance
    import pyarrow as pa
    acts = _synth_action(0)
    tbl = pa.table({
        "episode_index": pa.array([0] * N, pa.int64()),
        "timestamp": pa.array([i / FPS for i in range(N)], pa.float64()),
        "action": pa.array([a.tolist() for a in acts], pa.list_(pa.float32())),
        "task": pa.array(["x"] * N, pa.string()),
        "observation_images_cam0": pa.array(["ep0.mp4"] * N, pa.string()),
    })
    lance.write_dataset(tbl, d)
    r = read_lance_rows(d)[0]
    v = r["video"]["observation_images_cam0"]
    assert v["path"] == str(vid)                     # 相对表目录的父目录解析
    assert r["proprio_state"] is None


def test_non_jpeg_blob_rejected(tmp_path):
    """非 JPEG 的帧字节响亮拒绝(转码尚未实现),不产出解不出帧的 mp4。"""
    d = str(tmp_path / "png.lance")
    _write_table(d, n_ep=1, blob_override=b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    with pytest.raises(NotADatasetError) as e:
        read_lance_rows(d)
    assert "JPEG" in str(e.value)


def test_episode_selection_and_meta(table_dir):
    d, acts = table_dir
    rows = read_lance_rows(d, episode_indices={1})
    assert [r["episode_id"] for r in rows] == ["ep000001"]
    assert np.allclose(rows[0]["action"], acts[1], atol=1e-6)
    metas = read_lance_meta(d, max_episodes=1)
    assert metas[0]["episode_id"] == "ep000000"
    assert metas[0]["length"] == N
    assert os.path.exists(metas[0]["video"]["observation_images_cam0"]["path"])


def test_video_materialization_idempotent(table_dir):
    """同一 episode 重复读:复用已落盘的 mp4,不重写;落盘是原子的。"""
    d, _ = table_dir
    p1 = read_lance_rows(d, max_episodes=1)[0]["video"]["observation_images_cam0"]["path"]
    st1 = os.stat(p1)
    p2 = read_lance_meta(d, max_episodes=1)[0]["video"]["observation_images_cam0"]["path"]
    st2 = os.stat(p2)
    assert p1 == p2
    assert (st1.st_mtime_ns, st1.st_size) == (st2.st_mtime_ns, st2.st_size)
    assert not os.path.exists(p1 + ".part")


def test_export_lance_curated(table_dir, tmp_path):
    """交付导出:过滤(剔除的 episode 的行不在表中)、改标落 task 列、溯源列、清单。"""
    import lance

    from curation.export.lance_writer import export_lance_curated

    d, acts = table_dir
    delivery = str(tmp_path / "delivery")
    os.makedirs(delivery)
    stats = export_lance_curated(
        delivery, d, ["ep000001"], relabels={"ep000001": "stack the cups"},
        episodes={"ep000001": {"verdict": "通过", "instruction": "stack the cups",
                               "instruction_source": "人工裁决改标"}},
        generated_at="2026-09-18 00:00:00")
    assert stats["episodes"] == 1 and stats["relabeled"] == 1 and stats["removed"] == 1
    out = lance.dataset(os.path.join(stats["out_dir"], "episodes.lance")).to_table()
    assert out.num_rows == N                                     # 只剩 ep1 的行
    assert set(out["episode_index"].to_pylist()) == {1}
    assert set(out["task"].to_pylist()) == {"stack the cups"}    # 改标落列
    assert set(out["curation_verdict"].to_pylist()) == {"通过"}
    assert set(out["curation_instruction_source"].to_pylist()) == {"human-adjudicated"}
    assert np.allclose(np.asarray(out["action"].to_pylist(), dtype=np.float32),
                       acts[1], atol=1e-6)                       # 其余列逐值一致
    with open(os.path.join(stats["out_dir"], "index.json"), encoding="utf-8") as f:
        index = json.load(f)
    assert index["n_kept"] == 1 and index["n_removed"] == 1
    assert index["episodes"][0]["relabeled"] is True
