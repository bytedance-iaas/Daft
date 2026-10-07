#!/usr/bin/env python3
"""把一份 LeRobot v2.x 数据集转成 lerobot-lance-convert 的三表布局，供 lance 读取路径测试。

平台认的 lance 布局是「LeRobot v3 语义 + lance 字节后端」：

    meta/             LeRobot v3.0 的元数据（info.json 带 storage_format="lance"、
                      episodes 的 parquet、tasks.parquet）
    frames.lance      一行一帧，列名把特征名里的点换成下划线
    videos.lance      一行一个源 mp4，字节放 blob 列
    meta.lance        meta/ 下文件的 (path, bytes) 镜像

本脚本读 v2.x（一集一 parquet、一集一机位一 mp4），把帧表拼成一张、视频字节原样搬进 blob 列，
并按 v3 的字段补出 episodes 表（含 `dataset_from_index` / `dataset_to_index` 与每路相机的
chunk / file / from_timestamp / to_timestamp）。视频不转码，逐字节搬运。

    python3 scripts/lerobot_to_lance.py --src ~/datasets/anchor-3ep --out ~/datasets/anchor-3ep-lance
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", required=True, help="LeRobot v2.x 数据集目录")
    p.add_argument("--out", required=True, help="要写出的 lance 数据集目录")
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args(argv)

    import lance
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    src, out = os.path.abspath(args.src), os.path.abspath(args.out)
    if os.path.exists(out):
        if not args.overwrite:
            raise SystemExit(f"{out} 已存在，加 --overwrite")
        shutil.rmtree(out)
    os.makedirs(out)

    info = json.load(open(os.path.join(src, "meta", "info.json"), encoding="utf-8"))
    episodes_in = [json.loads(l) for l in
                   open(os.path.join(src, "meta", "episodes.jsonl"), encoding="utf-8") if l.strip()]
    cameras = [k for k, v in info["features"].items() if v.get("dtype") == "video"]
    fps = float(info["fps"])
    chunk_size = int(info.get("chunks_size") or 1000)
    data_tpl = info["data_path"]
    video_tpl = info["video_path"]

    tasks: list[str] = []
    rows, frames_tables, video_rows = [], [], {
        "video_key": [], "chunk_index": [], "file_index": [], "video_bytes": []}
    cursor = 0
    for ep in episodes_in:
        index = int(ep["episode_index"])
        chunk = index // chunk_size
        table = pq.read_table(os.path.join(src, data_tpl.format(
            episode_chunk=chunk, episode_index=index))).to_pandas()
        length = len(table)
        task = (ep.get("tasks") or [""])[0]
        if task not in tasks:
            tasks.append(task)
        table = table.rename(columns={c: c.replace(".", "_") for c in table.columns})
        table["index"] = range(cursor, cursor + length)
        table["episode_index"] = index
        table["task_index"] = tasks.index(task)
        frames_tables.append(table)
        row = {"episode_index": index, "length": length, "tasks": [task],
               "dataset_from_index": cursor, "dataset_to_index": cursor + length,
               "data/chunk_index": 0, "data/file_index": 0}
        for camera in cameras:
            path = os.path.join(src, video_tpl.format(episode_chunk=chunk, video_key=camera,
                                                      episode_index=index))
            with open(path, "rb") as fh:
                blob = fh.read()                           # 逐字节搬运，不转码
            video_rows["video_key"].append(camera)
            video_rows["chunk_index"].append(0)
            video_rows["file_index"].append(index)
            video_rows["video_bytes"].append(blob)
            row.update({f"videos/{camera}/chunk_index": 0,
                        f"videos/{camera}/file_index": index,
                        f"videos/{camera}/from_timestamp": 0.0,
                        f"videos/{camera}/to_timestamp": length / fps})
        rows.append(row)
        cursor += length
        print(f"  ep{index}  {length} 帧 · {len(cameras)} 路相机 · "
              f"{sum(len(b) for b in video_rows['video_bytes'][-len(cameras):])/1e6:.1f} MB 视频")

    frames = pd.concat(frames_tables, ignore_index=True).sort_values("index")
    info_out = dict(info)
    info_out.update(codebase_version="v3.0", storage_format="lance",
                    total_episodes=len(rows), total_frames=int(cursor), total_tasks=len(tasks),
                    total_videos=len(video_rows["video_key"]),
                    data_path="data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
                    video_path="videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4")
    info_out.pop("total_chunks", None)

    def parquet_bytes(df) -> bytes:
        buf = io.BytesIO()
        df.to_parquet(buf)
        return buf.getvalue()

    meta_files = {
        "meta/info.json": json.dumps(info_out, ensure_ascii=False, indent=1).encode(),
        "meta/episodes/chunk-000/file-000.parquet": parquet_bytes(pd.DataFrame(rows)),
        "meta/tasks.parquet": parquet_bytes(pd.DataFrame(
            {"task_index": list(range(len(tasks)))}, index=pd.Index(tasks, name="task"))),
    }
    for rel, blob in meta_files.items():
        path = os.path.join(out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(blob)

    lance.write_dataset(pa.Table.from_pandas(frames, preserve_index=False),
                        os.path.join(out, "frames.lance"))
    schema = pa.schema([pa.field("video_key", pa.string()), pa.field("chunk_index", pa.int64()),
                        pa.field("file_index", pa.int64()),
                        pa.field("video_bytes", pa.large_binary(),
                                 metadata={"lance-encoding:blob": "true"})])
    lance.write_dataset(pa.table(video_rows, schema=schema), os.path.join(out, "videos.lance"))
    lance.write_dataset(pa.table({"path": list(meta_files), "data": list(meta_files.values())}),
                        os.path.join(out, "meta.lance"))

    size = sum(os.path.getsize(os.path.join(b, f))
               for b, _d, fs in os.walk(out) for f in fs)
    print(f"\n写好了 {out}")
    print(f"  {len(rows)} 条 episode · {cursor} 帧 · 相机 {cameras}")
    print(f"  三表 + meta/ 共 {size/1e6:.1f} MB，info.json 标了 storage_format=lance")
    return 0


if __name__ == "__main__":
    sys.exit(main())
