"""LeRobot 导出(交付层的 schema 映射)。幸存 episode 重组为**合法 LeRobot 数据集**,
验收=官方 lerobot loader 无警告加载 + 自家 reader 读得回(关键回环)。

源是 v2 还是 v3,导出代价差一个数量级,因此两个导出器分开写:

**v3**(多条 episode 合并进同一 parquet/mp4)= `export_lerobot_v3`:
- data:从源 data parquet 按边界切行拼接(所有列天然齐全),只改写编号列
  (episode_index 重排/index 全局重排/task_index 按新任务表);
- meta/episodes:切源 episodes 元数据行(stats/* 列免费继承),覆写边界/编号列;
- videos:各相机把幸存 episode 的时间窗重编码拼接成新合并 mp4(av,libx264),记录新边界;
- info.json 改计数;stats.json 原样拷贝(全局近似统计,加载够用)。

**v2**(每条 episode 独立 parquet + 独立 mp4,如 DROID/Bridge)= `export_lerobot_v2`:
文件边界天然就是 episode 边界 → **选中文件拷过去 + 重编号 + 重建 meta 即可,零视频重编码**。
mp4 直接 `shutil.copyfile`(不解码不转码,几百 GB 也只是顺序拷贝);parquet 逐个读入只改
编号列。同样一批幸存者,v2 导出比 v3 便宜一个数量级——这不是偷懒,是源布局本来就允许。
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


def _refuse_nonempty(out_dir: str) -> None:
    """输出目录非空一律拒绝:parquet/mp4 写入是"补文件"语义,和上次的残留混在一起
    会产出一个看着完整、实际编号错位的假数据集——宁可报错让人换目录。"""
    if os.path.exists(out_dir) and os.listdir(out_dir):
        raise FileExistsError(f"输出目录非空: {out_dir}(写入追加语义危险,拒绝)")


def _task_table(tasks_per_episode: list[list[str]]) -> tuple[list[str], dict]:
    """各 episode 的任务文本(覆写已生效)→ (保序去重的任务表, 文本→新 task_index)。

    保序而非排序:任务表顺序即 task_index,按出现顺序排能让新旧编号尽量接近,
    人眼对账 meta/tasks 时不至于整体错位。"""
    task_strings: list[str] = []
    for tasks in tasks_per_episode:
        for t in tasks:
            if t not in task_strings:
                task_strings.append(t)
    return task_strings, {t: i for i, t in enumerate(task_strings)}


def _to_parquet_fsx_safe(df, dst: str, **kw) -> None:
    """pandas.to_parquet 的 FSX 安全版:pyarrow 直写 TOS 挂载会 EPERM
    (随机写被拒,2026-08-06 v2 导出端到端实锤,与 faststart/aria2 同族)。
    本地临时文件写好再整文件顺序拷贝,交付目录只见顺序写。
    kw 原样透传 to_parquet(index 语义由调用方定,勿在此处强加)。"""
    import shutil
    import tempfile
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        df.to_parquet(tmp_path, **kw)
        shutil.copyfile(tmp_path, dst)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def export_lerobot_v3(dataset_dir: str, keep_episode_indices: list[int], out_dir: str,
                      task_overrides: dict | None = None) -> dict:
    """源 v3 数据集 + 幸存 episode 序号 → out_dir 下的合法 v3 数据集。返回导出统计。

    task_overrides: {源 episode_index: 新任务文本}——两类来源(2026-08-06 出数据闭环):
    ①无标注条目的自产 caption 补标;②人工裁决采纳的改标(rejudge 重导出)。
    覆写同时作用于任务表、逐帧 task_index 与 meta/episodes 的 tasks 列。"""
    from ..ingest.lerobot_reader import _load_episodes_meta, _load_info

    _refuse_nonempty(out_dir)
    info = _load_info(dataset_dir)
    assert info["codebase_version"].startswith("v3"), "本导出器只收 v3 源(v2 源走 export_lerobot_v2)"
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

    task_overrides = task_overrides or {}

    def _tasks_of(ep):
        ov = task_overrides.get(int(ep["episode_index"]))
        return [ov] if ov else (list(ep["tasks"]) or [""])

    # 新任务表(保序去重;覆写生效后的文本)
    task_strings, task_index_of = _task_table([_tasks_of(ep) for _, ep in sel.iterrows()])

    new_from, cursor = [], 0
    for new_idx, (piece, (_, ep)) in enumerate(zip(pieces, sel.iterrows())):
        piece["episode_index"] = new_idx
        primary_task = _tasks_of(ep)[0]
        piece["task_index"] = task_index_of[primary_task]
        piece["index"] = np.arange(cursor, cursor + len(piece), dtype=piece["index"].dtype)
        new_from.append(cursor)
        cursor += len(piece)
    data = pd.concat(pieces, ignore_index=True)
    data_path = os.path.join(out_dir, info["data_path"].format(chunk_index=0, file_index=0))
    os.makedirs(os.path.dirname(data_path), exist_ok=True)
    _to_parquet_fsx_safe(data, data_path, index=False)

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
    if task_overrides:
        new_meta["tasks"] = [_tasks_of(ep) for _, ep in sel.iterrows()]
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
    _to_parquet_fsx_safe(new_meta, ep_path, index=False)

    # ---------- meta/tasks + info + stats ----------
    tasks_df = pd.DataFrame({"task_index": range(len(task_strings))},
                            index=pd.Index(task_strings))
    _to_parquet_fsx_safe(tasks_df,          # 保留 task 文本索引(v3 任务表语义)
                         os.path.join(out_dir, "meta", "tasks.parquet"))

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


def _write_jsonl(path: str, records: list[dict]) -> None:
    """一行一条 JSON 写 meta/*.jsonl。ensure_ascii=False:任务文本常是中文,
    转义成 \\uXXXX 客户打开 meta 一片乱码,没法人工核对。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _copy_v2_stats(dataset_dir: str, out_dir: str, keep: list[int]) -> None:
    """统计文件跟着走:v2.0 是全局 meta/stats.json(与子集无关,原样拷);
    v2.1 是逐条 meta/episodes_stats.jsonl(按选中条目过滤并重编号,否则新旧编号错位)。
    两个都没有也不报错——统计缺失不影响加载,交付照常。"""
    os.makedirs(os.path.join(out_dir, "meta"), exist_ok=True)
    src_stats = os.path.join(dataset_dir, "meta", "stats.json")
    if os.path.exists(src_stats):
        shutil.copyfile(src_stats, os.path.join(out_dir, "meta", "stats.json"))
    src_ep_stats = os.path.join(dataset_dir, "meta", "episodes_stats.jsonl")
    if os.path.exists(src_ep_stats):
        by_index = {}
        with open(src_ep_stats, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    by_index[int(rec["episode_index"])] = rec
        rows = []
        for new_idx, src_idx in enumerate(keep):
            rec = by_index.get(src_idx)
            if rec is None:
                continue                     # 源就缺这条统计:不伪造
            rec = dict(rec)
            rec["episode_index"] = new_idx
            rows.append(rec)
        _write_jsonl(os.path.join(out_dir, "meta", "episodes_stats.jsonl"), rows)


def export_lerobot_v2(dataset_dir: str, keep_episode_indices: list[int], out_dir: str,
                      task_overrides: dict | None = None) -> dict:
    """源 v2 数据集 + 幸存 episode 序号 → out_dir 下的合法 v2 数据集。返回导出统计。

    v2 每条 episode 一个 parquet + 每路相机一个 mp4,**文件边界即 episode 边界**:
    选中的 parquet 逐个读入只改编号列后另存,mp4 直接字节拷贝(不解码不转码)。
    因此这里没有 v3 导出器里那段最贵的视频切割重编码。

    task_overrides: {源 episode_index: 新任务文本}——语义与 v3 版一致(该条任务整体
    替换成这一条文本),同时作用于 meta/episodes.jsonl 的 tasks、meta/tasks.jsonl 任务表
    与 parquet 的 task_index 三处,三者必须一致否则下游按 task_index 取文本会错。
    """
    from ..ingest.lerobot_reader import _load_info, _v2_episode_list, _v2_episode_paths

    _refuse_nonempty(out_dir)
    info = _load_info(dataset_dir)
    assert info["codebase_version"].startswith("v2"), "本导出器只收 v2 源(v3 源走 export_lerobot_v3)"
    chunks_size = int(info["chunks_size"])
    video_keys = [k for k, v in info["features"].items() if v["dtype"] == "video"]

    keep = [int(i) for i in keep_episode_indices]
    src_eps = {int(e["episode_index"]): e for e in _v2_episode_list(dataset_dir)}
    unknown = [i for i in keep if i not in src_eps]
    if unknown:
        raise KeyError(f"这些 episode_index 不在源 meta/episodes.jsonl 里: {unknown[:8]}")

    task_overrides = {int(k): v for k, v in (task_overrides or {}).items()}

    def _tasks_of(ep) -> list[str]:
        ov = task_overrides.get(int(ep["episode_index"]))
        return [ov] if ov else (list(ep.get("tasks") or []) or [""])

    sel = [src_eps[i] for i in keep]
    task_strings, task_index_of = _task_table([_tasks_of(ep) for ep in sel])

    print(f"[curation] v2 导出:{len(sel)} 条 episode × {len(video_keys)} 路相机 "
          f"(每条独立文件 → 视频直接拷贝,无需重编码)", flush=True)

    # ---------- 逐条:parquet 改编号另存 + mp4 拷贝 ----------
    n_frames, n_videos = 0, 0
    missing_videos: list[str] = []
    new_eps: list[dict] = []
    for new_idx, ep in enumerate(sel):
        src_idx = int(ep["episode_index"])
        src_data, src_videos = _v2_episode_paths(dataset_dir, info, ep)
        out_chunk = new_idx // chunks_size

        df = pd.read_parquet(src_data)
        tasks = _tasks_of(ep)
        df["episode_index"] = new_idx
        if "task_index" in df.columns:
            df["task_index"] = task_index_of[tasks[0]]
        if "index" in df.columns:
            # index 是**全局**帧序号(跨 episode 连续),抽走一部分 episode 后必须整体重排,
            # 否则新数据集的帧号有洞,按 index 定位的下游会取到错帧
            df["index"] = np.arange(n_frames, n_frames + len(df), dtype=df["index"].dtype)
        out_data = os.path.join(out_dir, info["data_path"].format(
            episode_chunk=out_chunk, episode_index=new_idx))
        os.makedirs(os.path.dirname(out_data), exist_ok=True)
        _to_parquet_fsx_safe(df, out_data, index=False)
        n_frames += len(df)

        for vk in video_keys:
            src_mp4 = src_videos[vk]["path"]
            if not os.path.exists(src_mp4):
                # 缺某路视频是 v2 社区转换集的常态(如 DROID 部分条目缺腕部相机):
                # 有多少拷多少,末尾如实汇报,不因一路缺失丢掉整条 episode
                missing_videos.append(f"ep{src_idx:06d}/{vk}")
                continue
            out_mp4 = os.path.join(out_dir, info["video_path"].format(
                episode_chunk=out_chunk, video_key=vk, episode_index=new_idx))
            os.makedirs(os.path.dirname(out_mp4), exist_ok=True)
            shutil.copyfile(src_mp4, out_mp4)   # 顺序整文件拷贝:挂载盘上比随机写安全
            n_videos += 1

        # 源行的其它列(如社区转换集自带的额外字段)原样继承,只覆写编号/任务/长度
        meta_row = dict(ep)
        meta_row["episode_index"] = new_idx
        meta_row["tasks"] = tasks
        meta_row["length"] = len(df)
        new_eps.append(meta_row)

        # 条数多时逐条打印会淹没日志:每 50 条或每写满一个 chunk 报一次进度
        if (new_idx + 1) % 50 == 0 or (new_idx + 1) % chunks_size == 0 \
                or new_idx == len(sel) - 1:
            print(f"[curation] v2 导出 {new_idx + 1}/{len(sel)} 条"
                  f"(chunk-{out_chunk:03d},累计 {n_frames} 帧 / {n_videos} 个视频)",
                  flush=True)

    if missing_videos:
        print(f"[curation] ⚠️ 源缺 {len(missing_videos)} 个视频文件,已跳过"
              f"(前几个: {missing_videos[:5]})", flush=True)

    # ---------- meta 三件套 + 统计 ----------
    _write_jsonl(os.path.join(out_dir, "meta", "episodes.jsonl"), new_eps)
    _write_jsonl(os.path.join(out_dir, "meta", "tasks.jsonl"),
                 [{"task_index": i, "task": t} for i, t in enumerate(task_strings)])

    new_info = dict(info)
    new_info["total_episodes"] = len(sel)
    new_info["total_frames"] = int(n_frames)
    new_info["total_tasks"] = len(task_strings)
    new_info["total_videos"] = n_videos          # 实拷贝数(缺路时如实少,不按理论值虚报)
    new_info["total_chunks"] = (len(sel) + chunks_size - 1) // chunks_size
    new_info["splits"] = {"train": f"0:{len(sel)}"}
    # codebase_version 原样保留:导出没有升级格式,声称 v2.1 却按 v2.0 写 meta 会骗到 loader
    with open(os.path.join(out_dir, "meta", "info.json"), "w", encoding="utf-8") as f:
        json.dump(new_info, f, indent=2, ensure_ascii=False)
    _copy_v2_stats(dataset_dir, out_dir, keep)

    return {"episodes": len(sel), "frames": int(n_frames), "tasks": len(task_strings),
            "videos": n_videos, "video_keys": video_keys, "out_dir": out_dir}
