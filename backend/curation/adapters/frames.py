"""抽帧适配器:video 指针 → 帧数组(daft 内置 video_frames;spike2 验证的写法)。

⚠️ v3 合并 mp4 的 end_time 是闭区间 → 边界取 [from_ts, to_ts)(spike2 learning,
否则把下一条 episode 首帧抽进来)。
"""
from __future__ import annotations

import numpy as np


def extract_frames(
    video: dict,
    sample_interval_seconds: float | None = None,
    width: int | None = None,
    height: int | None = None,
) -> tuple[list[np.ndarray], np.ndarray]:
    """video: {path, from_ts, to_ts}(M1 的指针 struct)→ (帧列表, 帧时间数组,episode 内轴)。"""
    import daft
    from daft import col
    from daft.functions import video_file, video_frames

    df = daft.from_pydict({"path": [video["path"]]})
    expr = video_frames(
        video_file(col("path")),
        start_time=video["from_ts"],
        end_time=video["to_ts"],
        width=width, height=height,
        sample_interval_seconds=sample_interval_seconds,
    )
    out = df.select(expr.alias("f")).to_pydict()["f"][0]
    out = [x for x in out if float(x["frame_time"]) < video["to_ts"] - 1e-6]  # 闭区间修正
    frames = [np.asarray(x["data"]) for x in out]
    ts = np.asarray([float(x["frame_time"]) for x in out]) - video["from_ts"]
    return frames, ts
