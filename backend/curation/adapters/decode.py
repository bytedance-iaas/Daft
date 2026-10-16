"""视频窗口解码(PyAV,不经 daft)——供 UDF 内部使用。

为什么不用 daft 内置 video_frames:它的 start/end_time 是标量参数,不能按行变化;
漏斗里每行的时间窗不同(v3 合并 mp4),UDF 内直接用 av 解码即用即弃(流式指针模式)。
边界语义 [from_ts, to_ts)(spike2 learning:v3 闭区间会漏进下一条 episode 首帧)。
"""
from __future__ import annotations

import numpy as np


#: 远端视频(HTTP)交给 ffmpeg 的网络选项:断线自动重连、单次读超时 30s —— 没有
#: 这两条,TOS 侧一次抖动就是整条 episode 解码失败。
REMOTE_OPEN_OPTIONS = {"reconnect": "1", "reconnect_streamed": "1",
                       "reconnect_delay_max": "4", "rw_timeout": "30000000"}
REMOTE_ATTEMPTS = 3


def decode_window(
    path: str,
    from_ts: float,
    to_ts: float,
    *,
    sample_interval_s: float | None = None,
    max_side: int | None = None,
    notes: dict | None = None,
) -> tuple[list[np.ndarray], np.ndarray]:
    """解码 [from_ts, to_ts) 内的帧;返回 (帧列表 RGB, episode 内相对时间)。

    ``notes`` given: a video decoded from its start because its first packet is not a keyframe (D99, design
    doc 30 §2) whose first decodable frame comes later than its first packet gets
    ``undecodable_head: [from_s, to_s]`` there (window-relative seconds).

    tos:// 指针 → 预签名 URL,PyAV 按 HTTP Range 顺序读(2026-08-21 实测可行);本地
    路径原样。所有视频解码都从这儿进,8 个调用方因此一个字不用改。远端整段失败
    (连接被掐/签名过期)重开最多 REMOTE_ATTEMPTS 次,每次**重新签名**。
    """
    from ..ingest import dsfs

    if not dsfs.is_remote(path):
        return _decode_once(path, from_ts, to_ts, sample_interval_s=sample_interval_s,
                            max_side=max_side, notes=notes)
    last: Exception | None = None
    for attempt in range(1, REMOTE_ATTEMPTS + 1):
        try:
            return _decode_once(dsfs.media_source(path), from_ts, to_ts,
                                sample_interval_s=sample_interval_s, max_side=max_side,
                                options=REMOTE_OPEN_OPTIONS, notes=notes)
        except Exception as e:  # noqa: BLE001 网络/容器异常族杂
            last = e
            if attempt == REMOTE_ATTEMPTS:
                raise
            print(f"[curation] 远端视频解码失败(第 {attempt}/{REMOTE_ATTEMPTS} 次)"
                  f" {path}:{type(e).__name__}: {str(e)[:100]} —— 重新签名重开", flush=True)
    raise last  # pragma: no cover —— 循环里要么 return 要么 raise


def _decode_once(src: str, from_ts: float, to_ts: float, *,
                 sample_interval_s: float | None, max_side: int | None,
                 options: dict | None = None,
                 notes: dict | None = None) -> tuple[list[np.ndarray], np.ndarray]:
    import av
    import cv2

    frames: list[np.ndarray] = []
    ts: list[float] = []
    next_keep = from_ts
    container = av.open(src, options=options)
    first_packet: list[float] = []
    try:
        stream = container.streams.video[0]
        try:
            # seek 到窗口前的最近关键帧,再向后解码丢弃到 from_ts
            container.seek(int(from_ts / stream.time_base), stream=stream, any_frame=False)
            source = container.decode(stream)
        except PermissionError:
            # D99 (design doc 30 §2): the first packet is not a keyframe, so there is none before from_ts and FFmpeg's
            # seek answers -1 (PyAV's errno 1). Decode from the start, noting when the packets begin.
            container.close()
            container = av.open(src, options=options)
            stream = container.streams.video[0]
            source = _from_start(container, stream, first_packet)
        first_frame = None
        for frame in source:
            t = float(frame.pts * stream.time_base)
            if first_frame is None:
                first_frame = t
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
        if notes is not None and first_packet:
            # the packets before the first keyframe hold frames nobody can decode
            head_from = max(first_packet[0], from_ts)
            head_to = min(to_ts, first_frame if first_frame is not None else to_ts)
            if head_to - head_from > 1 / float(stream.average_rate or 30) + 1e-6:
                notes["undecodable_head"] = [round(head_from - from_ts, 3), round(head_to - from_ts, 3)]
    finally:
        container.close()
    return frames, np.asarray(ts)


def _from_start(container, stream, first_packet: list):
    """The stream's frames decoded from its first packet on; ``first_packet`` gets that packet's time."""
    for packet in container.demux(stream):
        if not first_packet and packet.pts is not None:
            first_packet.append(float(packet.pts * stream.time_base))
        yield from packet.decode()


# 占位/黑帧通道识别曾经在这里(probe_live_cameras):报告阶段逐条 episode、逐相机
# **再解一遍帧**,只为看一帧的亮度/方差。2026-08-14 并进视觉质量那一遍(判据搬去
# core/checks/visual_quality.is_live_channel,数据由帧段顺手产出),整整一遍解码
# 就此省掉,故连同函数一起删掉不留死代码。
