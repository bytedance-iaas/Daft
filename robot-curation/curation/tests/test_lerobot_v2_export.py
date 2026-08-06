"""v2 源(每 episode 独立 parquet/mp4,如 DROID/Bridge)的交付导出验收。

v2 导出不重编码视频,所以假 mp4 只要几个字节就够测——导出全程只拷贝不解码。
数据集就地合成(不依赖任何真实数据集路径),本机也能跑。
"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

pytest.importorskip("pandas", reason="本机无 pandas(导出器依赖)")
pytest.importorskip("pyarrow", reason="本机无 pyarrow(parquet 读写依赖)")

import pandas as pd  # noqa: E402

CHUNKS_SIZE = 2                     # 故意设小:4 条就跨 chunk,能验证新布局的分块
FPS = 10.0
VIDEO_KEYS = ("observation.images.cam_high", "observation.images.cam_wrist")
TASKS = ["pick up the red cube", "place the cube in the box"]
LENGTHS = [6, 5, 7, 4]              # 各条帧数不同:index 重排错了立刻暴露
EP_TASK = [0, 1, 0, 1]              # episode → TASKS 下标


def _fake_mp4(ep: int, cam: str) -> bytes:
    """几个字节的假 mp4:导出只拷贝字节,内容可辨认即可(用于逐字节比对落位)。"""
    return b"\x00\x00\x00\x18ftypmp42" + f"|ep{ep}|{cam}".encode()


def _write_v2_dataset(root: str, n_episodes: int = 4,
                      episodes_stats: bool = False) -> str:
    """就地合成一个最小 v2.0 数据集,返回目录路径。"""
    dim = 6
    info = {
        "codebase_version": "v2.0",
        "robot_type": "testarm",
        "total_episodes": n_episodes,
        "total_frames": sum(LENGTHS[:n_episodes]),
        "total_tasks": len(TASKS),
        "total_videos": n_episodes * len(VIDEO_KEYS),
        "total_chunks": (n_episodes + CHUNKS_SIZE - 1) // CHUNKS_SIZE,
        "chunks_size": CHUNKS_SIZE,
        "fps": FPS,
        "splits": {"train": f"0:{n_episodes}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": ("videos/chunk-{episode_chunk:03d}/{video_key}/"
                       "episode_{episode_index:06d}.mp4"),
        "features": {
            "action": {"dtype": "float32", "shape": [dim],
                       "names": [f"joint_{i}" for i in range(dim)]},
            "observation.state": {"dtype": "float32", "shape": [dim],
                                  "names": [f"joint_{i}" for i in range(dim)]},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
            **{vk: {"dtype": "video", "shape": [64, 64, 3],
                    "names": ["height", "width", "channel"],
                    "info": {"video.fps": FPS}} for vk in VIDEO_KEYS},
        },
    }
    os.makedirs(os.path.join(root, "meta"), exist_ok=True)
    with open(os.path.join(root, "meta", "info.json"), "w") as f:
        json.dump(info, f)

    eps, stats_rows, cursor = [], [], 0
    for ep in range(n_episodes):
        length = LENGTHS[ep]
        chunk = ep // CHUNKS_SIZE
        base = np.arange(length, dtype=np.float32)[:, None] * 0.01 + ep
        df = pd.DataFrame({
            "action": list((base + np.arange(dim, dtype=np.float32)).astype(np.float32)),
            "observation.state": list((base * 0.5).astype(np.float32)),
            "timestamp": (np.arange(length) / FPS).astype(np.float32),
            "frame_index": np.arange(length, dtype=np.int64),
            "episode_index": np.full(length, ep, dtype=np.int64),
            "index": np.arange(cursor, cursor + length, dtype=np.int64),
            "task_index": np.full(length, EP_TASK[ep], dtype=np.int64),
        })
        cursor += length
        p = os.path.join(root, info["data_path"].format(
            episode_chunk=chunk, episode_index=ep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        df.to_parquet(p, index=False)

        for vk in VIDEO_KEYS:
            v = os.path.join(root, info["video_path"].format(
                episode_chunk=chunk, video_key=vk, episode_index=ep))
            os.makedirs(os.path.dirname(v), exist_ok=True)
            with open(v, "wb") as f:
                f.write(_fake_mp4(ep, vk))

        eps.append({"episode_index": ep, "tasks": [TASKS[EP_TASK[ep]]], "length": length})
        stats_rows.append({"episode_index": ep, "stats": {"action": {"mean": [float(ep)]}}})

    with open(os.path.join(root, "meta", "episodes.jsonl"), "w") as f:
        for e in eps:
            f.write(json.dumps(e) + "\n")
    with open(os.path.join(root, "meta", "tasks.jsonl"), "w") as f:
        for i, t in enumerate(TASKS):
            f.write(json.dumps({"task_index": i, "task": t}) + "\n")
    if episodes_stats:                                     # v2.1 风格:逐条统计
        with open(os.path.join(root, "meta", "episodes_stats.jsonl"), "w") as f:
            for s in stats_rows:
                f.write(json.dumps(s) + "\n")
    else:                                                  # v2.0 风格:全局统计
        with open(os.path.join(root, "meta", "stats.json"), "w") as f:
            json.dump({"action": {"mean": [0.0] * dim}}, f)
    return root


def _read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


KEEP = [0, 2, 3]                    # 跳过 ep1:重编号/index 重排必须补上缺口


@pytest.fixture(scope="module")
def src_dataset(tmp_path_factory):
    return _write_v2_dataset(str(tmp_path_factory.mktemp("v2src") / "fake_v2"))


@pytest.fixture(scope="module")
def exported(src_dataset, tmp_path_factory):
    from curation.export.lerobot_writer import export_lerobot_v2

    out = str(tmp_path_factory.mktemp("v2out") / "curated")
    stats = export_lerobot_v2(src_dataset, KEEP, out)
    return out, stats


def test_export_stats(exported):
    _, stats = exported
    assert stats["episodes"] == 3
    assert stats["frames"] == sum(LENGTHS[i] for i in KEEP)
    assert stats["tasks"] == 2
    assert stats["videos"] == 3 * len(VIDEO_KEYS)


def test_data_files_renumbered(exported):
    """选中 episode 落到新编号 0..n-1 的新 chunk 布局;编号列与全局 index 连续。"""
    out, _ = exported
    cursor = 0
    for new_idx, src_idx in enumerate(KEEP):
        p = os.path.join(out, "data", f"chunk-{new_idx // CHUNKS_SIZE:03d}",
                         f"episode_{new_idx:06d}.parquet")
        assert os.path.exists(p), f"新编号 {new_idx} 的 parquet 没落位"
        df = pd.read_parquet(p)
        assert len(df) == LENGTHS[src_idx]
        assert (df["episode_index"] == new_idx).all()
        assert df["index"].tolist() == list(range(cursor, cursor + len(df)))
        assert df["frame_index"].tolist() == list(range(len(df)))   # 条内序号不动
        cursor += len(df)
    # 未选中的源条目不得混进交付:parquet 总数就等于选中条数
    import glob
    assert len(glob.glob(os.path.join(out, "data", "chunk-*", "*.parquet"))) == len(KEEP)


def test_meta_rebuilt(exported):
    out, _ = exported
    info = json.load(open(os.path.join(out, "meta", "info.json")))
    assert info["codebase_version"] == "v2.0"        # 版本原样保留,不擅自升级格式
    assert info["total_episodes"] == 3
    assert info["total_frames"] == sum(LENGTHS[i] for i in KEEP)
    assert info["total_tasks"] == 2
    assert info["total_videos"] == 3 * len(VIDEO_KEYS)
    assert info["total_chunks"] == 2                 # 3 条 / chunks_size 2
    assert info["splits"] == {"train": "0:3"}
    assert info["chunks_size"] == CHUNKS_SIZE

    eps = _read_jsonl(os.path.join(out, "meta", "episodes.jsonl"))
    assert [e["episode_index"] for e in eps] == [0, 1, 2]
    assert [e["length"] for e in eps] == [LENGTHS[i] for i in KEEP]
    assert [e["tasks"] for e in eps] == [[TASKS[EP_TASK[i]]] for i in KEEP]

    tasks = _read_jsonl(os.path.join(out, "meta", "tasks.jsonl"))
    assert [t["task_index"] for t in tasks] == [0, 1]
    assert sorted(t["task"] for t in tasks) == sorted(TASKS)
    # parquet 的 task_index 必须能在新任务表里查到本条的任务文本
    text_of = {t["task_index"]: t["task"] for t in tasks}
    for new_idx, src_idx in enumerate(KEEP):
        df = pd.read_parquet(os.path.join(out, "data", f"chunk-{new_idx // CHUNKS_SIZE:03d}",
                                          f"episode_{new_idx:06d}.parquet"))
        assert text_of[int(df["task_index"].iloc[0])] == TASKS[EP_TASK[src_idx]]

    assert os.path.exists(os.path.join(out, "meta", "stats.json"))


def test_videos_copied_byte_identical(exported):
    """视频只拷不转:新路径下的字节必须与源逐字节相同(且落在新编号上)。"""
    out, _ = exported
    for new_idx, src_idx in enumerate(KEEP):
        for vk in VIDEO_KEYS:
            p = os.path.join(out, "videos", f"chunk-{new_idx // CHUNKS_SIZE:03d}", vk,
                             f"episode_{new_idx:06d}.mp4")
            assert os.path.exists(p), f"{vk} 新编号 {new_idx} 的视频没落位"
            assert open(p, "rb").read() == _fake_mp4(src_idx, vk)


def test_roundtrip_with_our_reader(exported, src_dataset):
    """最强一条:交付包用自家 reader 读得回,条数/标注/帧数与预期一致。"""
    from curation.ingest.lerobot_reader import read_lerobot_rows

    out, _ = exported
    back = read_lerobot_rows(out)
    src = read_lerobot_rows(src_dataset)
    assert len(back) == 3
    for new_i, old_i in enumerate(KEEP):
        assert back[new_i]["instruction"] == src[old_i]["instruction"]
        assert len(back[new_i]["action"]) == LENGTHS[old_i]
        assert np.allclose(back[new_i]["action"], src[old_i]["action"])
        assert all(os.path.exists(v["path"]) for v in back[new_i]["video"].values())


def test_task_overrides_applied_everywhere(src_dataset, tmp_path):
    """补标/改标:episodes.jsonl + tasks.jsonl + parquet 的 task_index 三处一致换新。"""
    from curation.export.lerobot_writer import export_lerobot_v2
    from curation.ingest.lerobot_reader import read_lerobot_rows

    new_text = "wipe the table with a cloth"
    out = str(tmp_path / "curated_ov")
    export_lerobot_v2(src_dataset, KEEP, out, task_overrides={2: new_text})

    eps = _read_jsonl(os.path.join(out, "meta", "episodes.jsonl"))
    assert eps[1]["tasks"] == [new_text]                     # 源 ep2 → 新编号 1
    assert eps[0]["tasks"] == [TASKS[EP_TASK[0]]]            # 其它条不受影响

    tasks = _read_jsonl(os.path.join(out, "meta", "tasks.jsonl"))
    text_of = {t["task_index"]: t["task"] for t in tasks}
    assert new_text in text_of.values()
    df = pd.read_parquet(os.path.join(out, "data", "chunk-000", "episode_000001.parquet"))
    assert text_of[int(df["task_index"].iloc[0])] == new_text

    assert read_lerobot_rows(out)[1]["instruction"] == new_text


def test_missing_video_tolerated(src_dataset, tmp_path):
    """源缺某路视频是 v2 社区转换集常态:有多少拷多少,计数如实,不丢整条。"""
    import shutil

    from curation.export.lerobot_writer import export_lerobot_v2

    holed = str(tmp_path / "holed_src")
    shutil.copytree(src_dataset, holed)
    os.remove(os.path.join(holed, "videos", "chunk-000", VIDEO_KEYS[1],
                           "episode_000000.mp4"))
    out = str(tmp_path / "curated_holed")
    stats = export_lerobot_v2(holed, KEEP, out)
    assert stats["episodes"] == 3
    assert stats["videos"] == 3 * len(VIDEO_KEYS) - 1
    assert json.load(open(os.path.join(out, "meta", "info.json")))["total_videos"] \
        == stats["videos"]


def test_episodes_stats_filtered_and_renumbered(tmp_path):
    """v2.1 的逐条统计:按选中条目过滤 + 重编号(不重编号新旧统计就整体错位)。"""
    from curation.export.lerobot_writer import export_lerobot_v2

    src = _write_v2_dataset(str(tmp_path / "v21_src"), episodes_stats=True)
    out = str(tmp_path / "v21_curated")
    export_lerobot_v2(src, KEEP, out)
    rows = _read_jsonl(os.path.join(out, "meta", "episodes_stats.jsonl"))
    assert [r["episode_index"] for r in rows] == [0, 1, 2]
    # 统计内容跟着源条目走(fixture 把源 episode 序号写进了 mean)
    assert [r["stats"]["action"]["mean"][0] for r in rows] == [float(i) for i in KEEP]
    assert not os.path.exists(os.path.join(out, "meta", "stats.json"))


def test_refuse_nonempty_output(src_dataset, tmp_path):
    from curation.export.lerobot_writer import export_lerobot_v2

    d = tmp_path / "occupied"
    d.mkdir()
    (d / "junk.txt").write_text("x")
    with pytest.raises(FileExistsError):
        export_lerobot_v2(src_dataset, [0], str(d))


def test_unknown_episode_index_rejected(src_dataset, tmp_path):
    from curation.export.lerobot_writer import export_lerobot_v2

    with pytest.raises(KeyError):
        export_lerobot_v2(src_dataset, [0, 999], str(tmp_path / "curated_bad"))


def test_v3_source_rejected_by_v2_exporter(tmp_path):
    """走错导出器要当场炸,而不是产出一个格式混血的假数据集。"""
    from curation.export.lerobot_writer import export_lerobot_v2

    root = tmp_path / "v3ish"
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text(json.dumps({
        "codebase_version": "v3.0", "fps": FPS, "chunks_size": 1000,
        "data_path": "", "video_path": "", "features": {}}))
    with pytest.raises(AssertionError):
        export_lerobot_v2(str(root), [0], str(tmp_path / "out_v3ish"))
