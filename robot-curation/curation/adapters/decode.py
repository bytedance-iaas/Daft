"""视频窗口解码(PyAV,不经 daft)——供 UDF 内部使用。

为什么不用 daft 内置 video_frames:它的 start/end_time 是标量参数,不能按行变化;
漏斗里每行的时间窗不同(v3 合并 mp4),UDF 内直接用 av 解码即用即弃(流式指针模式)。
边界语义 [from_ts, to_ts)(spike2 learning:v3 闭区间会漏进下一条 episode 首帧)。
"""
from __future__ import annotations

import numpy as np


def decode_window(
    path: str,
    from_ts: float,
    to_ts: float,
    *,
    sample_interval_s: float | None = None,
    max_side: int | None = None,
) -> tuple[list[np.ndarray], np.ndarray]:
    """解码 [from_ts, to_ts) 内的帧;返回 (帧列表 RGB, episode 内相对时间)。"""
    import av
    import cv2

    from ..ingest import dsfs

    frames: list[np.ndarray] = []
    ts: list[float] = []
    next_keep = from_ts
    # tos:// 指针 → 预签名 URL,PyAV 按 HTTP Range 顺序读(2026-08-21 实测可行);
    # 本地路径原样。所有视频解码都从这儿进,8 个调用方因此一个字不用改。
    with av.open(dsfs.media_source(path)) as container:
        stream = container.streams.video[0]
        # seek 到窗口前的最近关键帧,再向后解码丢弃到 from_ts
        container.seek(int(from_ts / stream.time_base), stream=stream, any_frame=False)
        for frame in container.decode(stream):
            t = float(frame.pts * stream.time_base)
            if t < from_ts - 1e-6:
                continue
            if t >= to_ts - 1e-6:
                break
            if sample_interval_s is not None and t < next_keep - 1e-6:
                continue
            img = frame.to_ndarray(format="rgb24")
            if max_side is not None and max(img.shape[:2]) > max_side:
                scale = max_side / max(img.shape[:2])
                img = cv2.resize(img, (int(img.shape[1] * scale), int(img.shape[0] * scale)),
                                 interpolation=cv2.INTER_AREA)
            frames.append(img)
            ts.append(t - from_ts)
            if sample_interval_s is not None:
                next_keep = max(next_keep + sample_interval_s, t)
    return frames, np.asarray(ts)


# 占位/黑帧通道识别曾经在这里(probe_live_cameras):报告阶段逐条 episode、逐相机
# **再解一遍帧**,只为看一帧的亮度/方差。2026-08-14 并进视觉质量那一遍(判据搬去
# core/checks/visual_quality.is_live_channel,数据由 funnel 顺手产出),整整一遍解码
# 就此省掉,故连同函数一起删掉不留死代码。
