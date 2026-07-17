"""LeRobot v3 导出(文档中的 M8 的 schema 映射层)。

幸存 episode 重组为**合法 v3 数据集**,验收=官方 lerobot loader 无警告加载(关键回环)。
策略(最大限度继承源数据,不重建):
- data:从源 data parquet 按边界切行拼接(所有列天然齐全),只改写编号列
  (episode_index 重排/index 全局重排/task_index 按新任务表);
- meta/episodes:切源 episodes 元数据行(stats/* 列免费继承),覆写边界/编号列;
- videos:各相机把幸存 episode 的时间窗重编码拼接成新合并 mp4(av,libx264),记录新边界;
- info.json 改计数;stats.json 原样拷贝(全局近似统计,加载够用)。
"""
from __future__ import annotations

import json
import os
import shutil

import numpy as np
import pandas as pd


def _reencode_concat(src_windows: list[dict], out_path: str, fps: float) -> list[tuple]:
    """把多段 [from_ts,to_ts) 窗口按序重编码进一个 mp4;返回每段新 (from_ts, to_ts)。"""
    import av

    from ..adapters.decode import decode_window

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    bounds = []
    n_written = 0
    with av.open(out_path, "w") as out:
        stream = None
        for _wi, w in enumerate(src_windows):
            if _wi % 10 == 0 or _wi == len(src_windows) - 1:
                print(f"[curation]   视频切割 {os.path.basename(out_path)}: "
                      f"{_wi + 1}/{len(src_windows)} 段", flush=True)
            frames, _ = decode_window(w["path"], w["from_ts"], w["to_ts"])
            start = n_written / fps
            for img in frames:
                if stream is None:
                    h, wd = img.shape[:2]
                    stream = out.add_stream("libx264", rate=round(fps))
                    stream.width, stream.height = wd, h
                    stream.pix_fmt = "yuv420p"
                frame = av.VideoFrame.from_ndarray(img, format="rgb24")
                for pkt in stream.encode(frame):
                    out.mux(pkt)
                n_written += 1
            bounds.append((start, n_written / fps))
        if stream is not None:
            for pkt in stream.encode():
                out.mux(pkt)
    return bounds


def export_lerobot_v3(dataset_dir: str, keep_episode_indices: list[int], out_dir: str) -> dict:
    """源 v3 数据集 + 幸存 episode 序号 → out_dir 下的合法 v3 数据集。返回导出统计。"""
    from ..ingest.lerobot_reader import _load_episodes_meta, _load_info

    if os.path.exists(out_dir) and os.listdir(out_dir):
        raise FileExistsError(f"输出目录非空: {out_dir}(写入追加语义危险,拒绝)")
    info = _load_info(dataset_dir)
    assert info["codebase_version"].startswith("v3"), "导出器当前只支持 v3 源(v2 源先过 M1)"
    fps = float(info["fps"])
    video_keys = [k for k, v in info["features"].items() if v["dtype"] == "video"]

    ep_meta = _load_episodes_meta(dataset_dir).set_index("episode_index", drop=False)
    keep = list(keep_episode_indices)
    sel = ep_meta.loc[keep].reset_index(drop=True)

    # ---------- data:按边界切源 parquet ----------
    pieces = []
    for _, ep in sel.iterrows():
        src = os.path.join(dataset_dir, info["data_path"].format(
            chunk_index=int(ep["data/chunk_index"]), file_index=int(ep["data/file_index"])))
        df = pd.read_parquet(src)
        base = int(df["index"].iloc[0])
        lo, hi = int(ep["dataset_from_index"]), int(ep["dataset_to_index"])
        pieces.append(df.iloc[lo - base: hi - base].copy())

    # 新任务表(保序去重)
    task_strings: list[str] = []
    for _, ep in sel.iterrows():
        for t in (list(ep["tasks"]) or [""]):
            if t not in task_strings:
                task_strings.append(t)
    task_index_of = {t: i for i, t in enumerate(task_strings)}

    new_from, cursor = [], 0
    for new_idx, (piece, (_, ep)) in enumerate(zip(pieces, sel.iterrows())):
        piece["episode_index"] = new_idx
        primary_task = (list(ep["tasks"]) or [""])[0]
        piece["task_index"] = task_index_of[primary_task]
        piece["index"] = np.arange(cursor, cursor + len(piece), dtype=piece["index"].dtype)
        new_from.append(cursor)
        cursor += len(piece)
    data = pd.concat(pieces, ignore_index=True)
    data_path = os.path.join(out_dir, info["data_path"].format(chunk_index=0, file_index=0))
    os.makedirs(os.path.dirname(data_path), exist_ok=True)
    data.to_parquet(data_path, index=False)

    # ---------- videos:重编码拼接(最耗时环节:逐帧解码+重编码;打进度防误判卡死) ----------
    print(f"[curation] 开始导出交付数据集:{len(sel)} 条 × {len(video_keys)} 路相机的视频"
          f"切割重编码(耗时较长;只要质检结果可用 --report-only 跳过此步)", flush=True)
    video_bounds: dict[str, list[tuple]] = {}
    for vk in video_keys:
        windows = [{
            "path": os.path.join(dataset_dir, info["video_path"].format(
                video_key=vk, chunk_index=int(ep[f"videos/{vk}/chunk_index"]),
                file_index=int(ep[f"videos/{vk}/file_index"]))),
            "from_ts": float(ep[f"videos/{vk}/from_timestamp"]),
            "to_ts": float(ep[f"videos/{vk}/to_timestamp"]),
        } for _, ep in sel.iterrows()]
        out_mp4 = os.path.join(out_dir, info["video_path"].format(
            video_key=vk, chunk_index=0, file_index=0))
        video_bounds[vk] = _reencode_concat(windows, out_mp4, fps)

    # ---------- meta/episodes:切源行继承 stats,覆写边界/编号 ----------
    new_meta = sel.copy()
    new_meta["episode_index"] = np.arange(len(sel))
    new_meta["data/chunk_index"] = 0
    new_meta["data/file_index"] = 0
    new_meta["dataset_from_index"] = new_from
    new_meta["dataset_to_index"] = [f + int(l) for f, l in zip(new_from, sel["length"])]
    new_meta["meta/episodes/chunk_index"] = 0
    new_meta["meta/episodes/file_index"] = 0
    for vk in video_keys:
        new_meta[f"videos/{vk}/chunk_index"] = 0
        new_meta[f"videos/{vk}/file_index"] = 0
        new_meta[f"videos/{vk}/from_timestamp"] = [b[0] for b in video_bounds[vk]]
        new_meta[f"videos/{vk}/to_timestamp"] = [b[1] for b in video_bounds[vk]]
    ep_path = os.path.join(out_dir, "meta", "episodes", "chunk-000", "file-000.parquet")
    os.makedirs(os.path.dirname(ep_path), exist_ok=True)
    new_meta.to_parquet(ep_path, index=False)

    # ---------- meta/tasks + info + stats ----------
    tasks_df = pd.DataFrame({"task_index": range(len(task_strings))},
                            index=pd.Index(task_strings))
    tasks_df.to_parquet(os.path.join(out_dir, "meta", "tasks.parquet"))

    new_info = dict(info)
    new_info["total_episodes"] = len(sel)
    new_info["total_frames"] = int(cursor)
    new_info["total_tasks"] = len(task_strings)
    new_info["splits"] = {"train": f"0:{len(sel)}"}
    if "total_videos" in new_info:
        new_info["total_videos"] = len(video_keys)
    with open(os.path.join(out_dir, "meta", "info.json"), "w") as f:
        json.dump(new_info, f, indent=2)
    shutil.copy(os.path.join(dataset_dir, "meta", "stats.json"),
                os.path.join(out_dir, "meta", "stats.json"))

    return {"episodes": len(sel), "frames": int(cursor), "tasks": len(task_strings),
            "video_keys": video_keys, "out_dir": out_dir}
