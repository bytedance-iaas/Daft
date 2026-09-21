"""v3 导出的滚动分文件(2026-08-21 方案 1):data / 视频按阈值在轨迹边界封口,文件封口即交发布器。

合成一个最小 v3 数据集(两路相机、6 条轨迹、真实 mp4),不依赖外部数据。
"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

pytest.importorskip("pandas")
pytest.importorskip("av")
pytest.importorskip("pyarrow")

FPS = 10
LENGTHS = [60, 70, 50, 80, 60, 40]   # 每条几十帧:x264 有约 40 帧前瞻缓冲,太短的轨迹编完还没吐 packet,封口判断无从谈起
CAMS = ["observation.images.top", "observation.images.wrist"]


def _write_mp4(path: str, n_frames: int, seed: int, size: int = 32) -> None:
    import av
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with av.open(path, "w") as out:
        st = out.add_stream("libx264", rate=FPS)
        st.width = st.height = size
        st.pix_fmt = "yuv420p"
        for i in range(n_frames):
            img = np.full((size, size, 3), (seed * 31 + i * 7) % 255, dtype=np.uint8)
            for pkt in st.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                out.mux(pkt)
        for pkt in st.encode():
            out.mux(pkt)


def write_v3_dataset(root: str, lengths=LENGTHS, cams=CAMS) -> str:
    """最小 v3 源:所有轨迹挤在 data/chunk-000/file-000 与每路相机一个 mp4 里(和真实 v3 一样)。"""
    import pandas as pd
    os.makedirs(os.path.join(root, "meta", "episodes", "chunk-000"), exist_ok=True)
    n_total = sum(lengths)
    info = {
        "codebase_version": "v3.0", "robot_type": "so101", "fps": FPS,
        "total_episodes": len(lengths), "total_frames": n_total, "total_tasks": 2,
        "total_videos": len(cams), "chunks_size": 1000,
        "data_files_size_in_mb": 100, "video_files_size_in_mb": 200,
        "splits": {"train": f"0:{len(lengths)}"},
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
        "features": {
            "action": {"dtype": "float32", "shape": [6], "names": [f"j{i}" for i in range(6)]},
            "observation.state": {"dtype": "float32", "shape": [6], "names": [f"j{i}" for i in range(6)]},
            "timestamp": {"dtype": "float32", "shape": [1]},
            "frame_index": {"dtype": "int64", "shape": [1]},
            "episode_index": {"dtype": "int64", "shape": [1]},
            "index": {"dtype": "int64", "shape": [1]},
            "task_index": {"dtype": "int64", "shape": [1]},
            **{c: {"dtype": "video", "shape": [32, 32, 3], "names": ["h", "w", "c"],
                   "info": {"video.fps": FPS}} for c in cams},
        },
    }
    with open(os.path.join(root, "meta", "info.json"), "w") as f:
        json.dump(info, f)
    tasks = ["pick", "place"]
    pd.DataFrame({"task_index": [0, 1]}, index=pd.Index(tasks)).to_parquet(
        os.path.join(root, "meta", "tasks.parquet"))
    rows, meta, cursor, t_cursor = [], [], 0, 0.0
    for ei, n in enumerate(lengths):
        for fi in range(n):
            rows.append({"index": cursor + fi, "episode_index": ei, "frame_index": fi,
                         "timestamp": np.float32(fi / FPS), "task_index": ei % 2,
                         "action": np.full(6, ei + fi * 0.1, dtype=np.float32),
                         "observation.state": np.full(6, ei, dtype=np.float32)})
        m = {"episode_index": ei, "tasks": [tasks[ei % 2]], "length": n,
             "data/chunk_index": 0, "data/file_index": 0,
             "dataset_from_index": cursor, "dataset_to_index": cursor + n,
             "meta/episodes/chunk_index": 0, "meta/episodes/file_index": 0}
        for c in cams:
            m[f"videos/{c}/chunk_index"] = 0
            m[f"videos/{c}/file_index"] = 0
            m[f"videos/{c}/from_timestamp"] = t_cursor
            m[f"videos/{c}/to_timestamp"] = t_cursor + n / FPS
        meta.append(m)
        cursor += n
        t_cursor += n / FPS
    os.makedirs(os.path.join(root, "data", "chunk-000"), exist_ok=True)
    pd.DataFrame(rows).to_parquet(os.path.join(root, "data", "chunk-000", "file-000.parquet"), index=False)
    pd.DataFrame(meta).to_parquet(os.path.join(root, "meta", "episodes", "chunk-000", "file-000.parquet"), index=False)
    for ci, c in enumerate(cams):
        _write_mp4(os.path.join(root, "videos", c, "chunk-000", "file-000.mp4"), n_total, seed=ci)
    return root


def _out_layout(out: str):
    import pandas as pd
    meta = pd.read_parquet(os.path.join(out, "meta", "episodes", "chunk-000", "file-000.parquet"))
    data_files = sorted(p for dp, _, fs in os.walk(os.path.join(out, "data")) for p in
                        [os.path.join(dp, f) for f in fs] if p.endswith(".parquet"))
    vid_files = {c: sorted(p for dp, _, fs in os.walk(os.path.join(out, "videos", c)) for p in
                           [os.path.join(dp, f) for f in fs] if p.endswith(".mp4")) for c in CAMS}
    return meta, data_files, vid_files


def _frames_in(path: str, from_ts: float, to_ts: float) -> int:
    from curation.adapters.decode import decode_window
    frames, _ = decode_window(path, from_ts, to_ts)
    return len(frames)


def test_rolling_export_splits_files_at_episode_boundaries(tmp_path):
    from curation.export.lerobot_writer import export_lerobot_v3
    src = write_v3_dataset(str(tmp_path / "src"))
    out = str(tmp_path / "out")
    keep = [0, 1, 3, 4, 5]                    # 判废第 2 条:编号重排,不留洞
    stats = export_lerobot_v3(src, keep, out, video_file_mb=0.0005, data_file_mb=0.0005)
    meta, data_files, vid_files = _out_layout(out)
    assert stats["episodes"] == 5 and list(meta["episode_index"]) == [0, 1, 2, 3, 4]
    assert stats["data_files"] == len(data_files) >= 2, "阈值极小:data 必须滚动成多个文件"
    for c in CAMS:
        assert len(vid_files[c]) >= 2, f"{c} 视频必须滚动成多个文件"
        assert stats["video_files"][c] == len(vid_files[c])
    # 每条轨迹的视频位置:文件索引单调不减;时间戳相对所在文件;按窗口解码的帧数 = length
    for c in CAMS:
        fidx = list(meta[f"videos/{c}/file_index"])
        assert fidx == sorted(fidx)
        for _, ep in meta.iterrows():
            path = os.path.join(out, "videos", c, f"chunk-{int(ep[f'videos/{c}/chunk_index']):03d}",
                                f"file-{int(ep[f'videos/{c}/file_index']):03d}.mp4")
            assert os.path.exists(path)
            assert ep[f"videos/{c}/from_timestamp"] >= 0
            n = _frames_in(path, float(ep[f"videos/{c}/from_timestamp"]),
                           float(ep[f"videos/{c}/to_timestamp"]))
            assert n == int(ep["length"]), (c, int(ep["episode_index"]), n, int(ep["length"]))
        # 文件里第一条轨迹的起点必须是 0(时间戳相对文件,不是相对数据集)
        first_in_file = meta.groupby(f"videos/{c}/file_index")[f"videos/{c}/from_timestamp"].min()
        assert all(abs(v) < 1e-6 for v in first_in_file)
    # data:全局 index 连续、每条轨迹的 from/to 对得上、文件索引与行所在文件一致
    import pandas as pd
    frames = pd.concat([pd.read_parquet(p) for p in data_files], ignore_index=True)
    assert list(frames["index"]) == list(range(sum(LENGTHS[i] for i in keep)))
    assert list(frames["episode_index"].drop_duplicates()) == [0, 1, 2, 3, 4]
    for _, ep in meta.iterrows():
        path = os.path.join(out, "data", f"chunk-{int(ep['data/chunk_index']):03d}",
                            f"file-{int(ep['data/file_index']):03d}.parquet")
        part = pd.read_parquet(path)
        assert int(ep["episode_index"]) in set(part["episode_index"])
        assert int(ep["dataset_to_index"]) - int(ep["dataset_from_index"]) == int(ep["length"])
    info = json.load(open(os.path.join(out, "meta", "info.json")))
    assert info["total_episodes"] == 5 and info["total_frames"] == len(frames)


def test_large_threshold_keeps_single_file_and_reader_loads_it(tmp_path):
    """阈值够大 = 老布局(一路相机一个文件);自家 reader 读得回(关键回环)。"""
    from curation.export.lerobot_writer import export_lerobot_v3
    from curation.ingest.lerobot_reader import _load_episodes_meta, _load_info
    src = write_v3_dataset(str(tmp_path / "src"))
    out = str(tmp_path / "out")
    export_lerobot_v3(src, [1, 2], out, task_overrides={2: "corrected"})
    meta, data_files, vid_files = _out_layout(out)
    assert len(data_files) == 1 and all(len(v) == 1 for v in vid_files.values())
    assert _load_info(out)["total_episodes"] == 2
    m2 = _load_episodes_meta(out)
    assert list(m2["length"]) == [LENGTHS[1], LENGTHS[2]]
    assert list(m2["tasks"].iloc[1]) == ["corrected"]
    c = CAMS[0]
    assert _frames_in(vid_files[c][0], float(m2[f"videos/{c}/from_timestamp"].iloc[1]),
                      float(m2[f"videos/{c}/to_timestamp"].iloc[1])) == LENGTHS[2]


def test_sealed_files_go_to_publisher_markers_stay(tmp_path):
    """封口即交发布器:data / 视频文件上传并删本地;meta/info.json 留给最后。"""
    from curation.export import publish
    from curation.export.lerobot_writer import export_lerobot_v3

    class _St:
        def __init__(self): self.keys = []
        def upload(self, local, bucket, key): self.keys.append(key)

    src = write_v3_dataset(str(tmp_path / "src"))
    root = tmp_path / "deliv"
    out = str(root / "lerobot_curated")
    st = _St()
    with publish.activate(publish.Publisher(str(root), "tos://bkt/d", store=st)) as pub:
        export_lerobot_v3(src, [0, 1, 2], out, video_file_mb=0.0005, data_file_mb=0.0005)
        n = pub.finish()
    assert n >= 4 and all(k.startswith("d/lerobot_curated/") for k in st.keys)
    assert any("/videos/" in k for k in st.keys) and any("/data/" in k for k in st.keys)
    assert "lerobot_curated/meta/info.json" in pub.deferred
    assert not [p for dp, _, fs in os.walk(os.path.join(out, "videos")) for p in fs], "视频传完即删"
    assert os.path.exists(os.path.join(out, "meta", "info.json")), "标志文件留在本地"
