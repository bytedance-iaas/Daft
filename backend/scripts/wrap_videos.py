#!/usr/bin/env python3
"""把一堆本地 mp4 包成平台能读的 LeRobot v2.1 数据集，然后就能当普通数据集质检。

平台读的是数据集（LeRobot / mcap / Lance），不是散装视频文件，所以先包一层：一条 episode
一个目录项，视频原样拷过去（不转码、不改字节），再按视频自身的帧率和帧数补出 LeRobot 需要的
元数据与一张最小的数据表（时间戳、帧号、条目号、全局序号、任务号）。

平台的元数据校验要求数据集必须有动作通道（`ingest/validate.py`，机器人数据的硬性约定），
纯视频没有，所以这里写一列**占位动作**（全零、单通道）并在输出里说明。占位的后果要记牢：
**不要勾选「视频-动作同步」**，它会拿这列假动作去跟画面比，结论没有意义；运动学极限缺本体、
运动质量缺状态列，预检会自动标成不支持。真有动作数据就别用这个脚本，按 LeRobot 正规写。

能放心跑的是：数据完整性、时间戳检查、视觉质量、任务成败判定（含镜头画面缺陷）、技能画像、精确去重。

用法：

    # 每个 mp4 是一条 episode，单相机
    python3 scripts/wrap_videos.py --out ~/datasets/my-clips ~/clips/*.mp4

    # 多机位：按文件名里的相机名分组，同名的不同相机算同一条 episode
    python3 scripts/wrap_videos.py --out ~/datasets/my-clips \\
        --camera-from-dir ~/clips/front/*.mp4 ~/clips/wrist/*.mp4

    # 给任务文本（不给就留空，平台会用模型补一句描述）
    python3 scripts/wrap_videos.py --out ~/datasets/my-clips --task "把杯子放进箱子" ~/clips/*.mp4

包完把 `--out` 的路径填进控制台的「本地挂载路径」，或者传到 TOS 再按 tos:// 建任务。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import OrderedDict

CHUNK = 1000
DATA_PATH = "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
VIDEO_PATH = "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"


def probe(path: str) -> dict:
    """帧数、帧率、宽高、编码——从视频自己读，不猜。"""
    import av

    with av.open(path) as container:
        if not container.streams.video:
            raise SystemExit(f"{path}: 里面没有视频流")
        stream = container.streams.video[0]
        fps = float(stream.average_rate or 0) or None
        frames = stream.frames or 0
        duration = float(container.duration or 0) / 1e6 or None
        codec = stream.codec_context.name
        width, height = stream.width, stream.height
        if not frames:                      # 有些文件头里没写帧数，只能数一遍
            frames = sum(1 for _ in container.decode(stream))
    if not fps and duration and frames:
        fps = frames / duration
    if not fps:
        raise SystemExit(f"{path}: 读不出帧率，请用 --fps 指定")
    return {"frames": int(frames), "fps": float(fps), "width": width, "height": height,
            "codec": codec, "duration_s": (frames / fps) if fps else duration}


def group(paths: list[str], camera_from_dir: bool, camera: str) -> "OrderedDict[str, dict]":
    """episode 名 -> {相机名: 文件路径}。"""
    out: OrderedDict[str, dict] = OrderedDict()
    for path in paths:
        stem = os.path.splitext(os.path.basename(path))[0]
        cam = os.path.basename(os.path.dirname(path)) if camera_from_dir else camera
        out.setdefault(stem, {})[cam] = path
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("videos", nargs="+", help="mp4 文件")
    p.add_argument("--out", required=True, help="要写出的数据集目录")
    p.add_argument("--task", default="", help="任务文本；不给就留空，平台会补描述")
    p.add_argument("--camera", default="main", help="单相机时的相机名（默认 main）")
    p.add_argument("--camera-from-dir", action="store_true",
                   help="多机位：相机名取文件所在目录名，同名文件算同一条 episode")
    p.add_argument("--robot-type", default=None, help="本体型号；不给就留空")
    p.add_argument("--fps", type=float, default=None, help="覆盖帧率（视频里读不出时用）")
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args(argv)

    import pandas as pd

    episodes = group(args.videos, args.camera_from_dir, args.camera)
    if not episodes:
        raise SystemExit("没有输入文件")
    cameras = sorted({cam for cams in episodes.values() for cam in cams})
    missing = [(name, cam) for name, cams in episodes.items() for cam in cameras
               if cam not in cams]
    if missing:
        raise SystemExit(f"这些 episode 缺相机：{missing[:5]}（每条必须每路相机都有）")

    root = os.path.abspath(args.out)
    if os.path.exists(root):
        if not args.overwrite:
            raise SystemExit(f"{root} 已存在，加 --overwrite 覆盖")
        shutil.rmtree(root)
    os.makedirs(os.path.join(root, "meta"))

    rows, cursor, total_frames = [], 0, 0
    fps_seen: set[float] = set()
    shape = None
    for index, (name, cams) in enumerate(episodes.items()):
        probes = {cam: probe(path) for cam, path in cams.items()}
        frames = min(pr["frames"] for pr in probes.values())
        fps = args.fps or probes[cameras[0]]["fps"]
        fps_seen.add(round(fps, 3))
        shape = shape or [probes[cameras[0]]["height"], probes[cameras[0]]["width"], 3]
        chunk = index // CHUNK
        table = pd.DataFrame({
            # 占位动作：平台要求这一列存在，视频数据没有真动作，全零并在 info 里标明
            "action": [[0.0] for _ in range(frames)],
            "timestamp": [i / fps for i in range(frames)],
            "frame_index": list(range(frames)),
            "episode_index": [index] * frames,
            "index": list(range(cursor, cursor + frames)),
            "task_index": [0] * frames,
        })
        data_file = os.path.join(root, DATA_PATH.format(episode_chunk=chunk, episode_index=index))
        os.makedirs(os.path.dirname(data_file), exist_ok=True)
        table.to_parquet(data_file, index=False)
        for cam, path in cams.items():
            dst = os.path.join(root, VIDEO_PATH.format(episode_chunk=chunk, video_key=_key(cam),
                                                       episode_index=index))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(path, dst)                    # 原样拷贝，不转码
        rows.append({"episode_index": index, "tasks": [args.task], "length": frames})
        cursor += frames
        total_frames += frames
        codecs = ", ".join(f"{cam}:{probes[cam]['codec']}" for cam in cams)
        print(f"  ep{index:04d}  {name}  {frames} 帧 · {fps:.3f} fps · {codecs}")

    if len(fps_seen) > 1:
        print(f"注意：这批视频帧率不一致 {sorted(fps_seen)}，元数据写的是第一条的；"
              f"要统一请用 --fps", file=sys.stderr)
    fps = args.fps or sorted(fps_seen)[0]
    info = {
        "codebase_version": "v2.1",
        "robot_type": args.robot_type,
        "total_episodes": len(rows),
        "total_frames": total_frames,
        "total_tasks": 1,
        "total_videos": len(rows) * len(cameras),
        "total_chunks": (len(rows) - 1) // CHUNK + 1,
        "chunks_size": CHUNK,
        "fps": fps,
        "splits": {"train": f"0:{len(rows)}"},
        "data_path": DATA_PATH,
        "video_path": VIDEO_PATH,
        "features": {
            "action": {"dtype": "float32", "shape": [1], "names": ["placeholder"],
                       "info": {"placeholder": True,
                                "note": "wrap_videos.py: 视频数据没有动作，这一列是占位"}},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
            **{_key(cam): {"dtype": "video", "shape": shape,
                           "names": ["height", "width", "channel"],
                           "info": {"video.fps": fps}} for cam in cameras},
        },
    }
    with open(os.path.join(root, "meta", "info.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(root, "meta", "episodes.jsonl"), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(os.path.join(root, "meta", "tasks.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"task_index": 0, "task": args.task}, ensure_ascii=False) + "\n")

    print(f"\n写好了：{root}")
    print(f"  {len(rows)} 条 episode · {total_frames} 帧 · {fps:.3f} fps · 相机 {cameras}")
    print("  能跑的模块：数据完整性、时间戳检查、视觉质量、任务成败判定（含镜头画面缺陷）、"
          "技能画像、精确去重")
    print("  ⚠ 动作列是占位的全零：建任务时别勾「视频-动作同步」，它比的是画面与动作，"
          "对占位数据没有意义")
    print("  运动学极限（缺本体）、运动质量（缺状态列）、EEF（缺轨迹文件）预检会自动标成不支持")
    return 0


def _key(camera: str) -> str:
    """LeRobot 的相机特征名约定。"""
    return camera if camera.startswith("observation.images.") else f"observation.images.{camera}"


if __name__ == "__main__":
    sys.exit(main())
