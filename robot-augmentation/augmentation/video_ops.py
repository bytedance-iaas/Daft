"""视频切段 / 重采样 / 拼接 / 叠加 / 对照拼图。全部 PyAV + numpy + cv2,不依赖 ffmpeg 命令行。"""
from __future__ import annotations

import os
from fractions import Fraction
from typing import Iterator

import av
import cv2
import numpy as np


def probe(path: str) -> dict:
    c = av.open(path)
    v = c.streams.video[0]
    n = 0
    last_t = 0.0
    for f in c.decode(v):
        n += 1
        if f.pts is not None:
            last_t = float(f.pts * v.time_base)
    fps = float(v.average_rate) if v.average_rate else (n / last_t if last_t else 0.0)
    out = {
        "width": v.width, "height": v.height, "fps": fps, "frames": n,
        "duration_s": (c.duration or 0) / 1e6 if c.duration else (last_t + (1 / fps if fps else 0)),
        "codec": v.codec_context.name,
    }
    c.close()
    return out


def iter_frames(path: str) -> Iterator[tuple[float, np.ndarray]]:
    """按解码顺序吐 (时间秒, RGB 数组)。"""
    c = av.open(path)
    v = c.streams.video[0]
    v.thread_type = "AUTO"
    for f in c.decode(v):
        t = float(f.pts * v.time_base) if f.pts is not None else 0.0
        yield t, f.to_ndarray(format="rgb24")
    c.close()


_CODEC_BY_NAME = {"h264": "libx264", "avc": "libx264", "hevc": "libx265", "h265": "libx265", "av1": "libsvtav1"}
_CODEC_FALLBACK = {"libsvtav1": "libaom-av1"}


def encode_spec(video_info: dict | None) -> dict:
    """LeRobot info.json 里某路相机的 info(video.codec / video.crf / video.g / video.pix_fmt)→ 编码规格。
    目的:增广后的视频与源视频同编码规格(同 codec、同 crf、同关键帧间隔、同像素格式),别让画质规格本身
    成为分布差异(源 so101 是 h264 crf 30 g 2,我们原来写 crf 16 g 250,码率差好几倍)。缺项按 x264 缺省。"""
    vi = video_info or {}
    codec = _CODEC_BY_NAME.get(str(vi.get("video.codec", "h264")).lower(), "libx264")
    try:
        av.codec.Codec(codec, "w")
    except Exception:
        codec = _CODEC_FALLBACK.get(codec, "libx264")
    spec = {"codec": codec, "pix_fmt": vi.get("video.pix_fmt") or "yuv420p", "preset": vi.get("video.preset") or "medium"}
    if vi.get("video.crf") is not None:
        spec["crf"] = int(vi["video.crf"])
    if vi.get("video.g") is not None:
        spec["gop"] = int(vi["video.g"])
    return spec


class Writer:
    """mp4 写盘器,恒定帧率,pts 逐帧递增。缺省 libx264 crf 16(中间产物);封数据集时用 encode_spec 对齐源规格。"""

    def __init__(self, path: str, fps: int, width: int, height: int, crf: int | None = 16, *, codec: str = "libx264",
                 gop: int | None = None, preset: str | None = "medium", pix_fmt: str = "yuv420p"):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.c = av.open(path, "w")
        self.s = self.c.add_stream(codec, rate=fps)
        self.s.width, self.s.height = width, height
        self.s.pix_fmt = pix_fmt
        self.s.codec_context.time_base = Fraction(1, fps)
        opts = {}
        if crf is not None:
            opts["crf"] = str(crf)
        if preset and codec in ("libx264", "libx265", "libsvtav1"):
            opts["preset"] = str(preset)
        if gop is not None:
            opts["g"] = str(gop)
            if codec == "libx264":
                opts["keyint_min"] = str(gop)   # x264 否则会把最小关键帧间隔抬到 g/10 以上,g=2 就不准了
        self.s.options = opts
        self.spec = {"codec": codec, "crf": crf, "gop": gop, "preset": preset, "pix_fmt": pix_fmt}
        self.fps = fps
        self.i = 0

    @classmethod
    def from_spec(cls, path: str, fps: int, width: int, height: int, spec: dict) -> "Writer":
        return cls(path, fps, width, height, spec.get("crf"), codec=spec.get("codec", "libx264"), gop=spec.get("gop"),
                   preset=spec.get("preset"), pix_fmt=spec.get("pix_fmt", "yuv420p"))

    def write(self, rgb: np.ndarray) -> None:
        f = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24")
        f.pts = self.i
        f.time_base = Fraction(1, self.fps)
        self.i += 1
        for p in self.s.encode(f):
            self.c.mux(p)

    def close(self) -> int:
        for p in self.s.encode():
            self.c.mux(p)
        self.c.close()
        return self.i


def transcode(src: str, dst: str, spec: dict, *, fps: int | None = None) -> dict:
    """逐帧解码再按 spec 重编码(不改分辨率、帧数、帧率)。返回 probe + 关键帧间隔实测。"""
    info = probe(src)
    fps = fps or int(round(info["fps"]))
    w: Writer | None = None
    for _, rgb in iter_frames(src):
        if w is None:
            w = Writer.from_spec(dst, fps, rgb.shape[1], rgb.shape[0], spec)
        w.write(rgb)
    n = w.close() if w else 0
    out = probe(dst)
    out.update({"spec": spec, "frames": n, "keyframe_interval": keyframe_interval(dst), "bytes": os.path.getsize(dst)})
    return out


def keyframe_interval(path: str) -> int | None:
    """实测关键帧间隔(帧),核对 g 有没有生效。"""
    c = av.open(path)
    v = c.streams.video[0]
    keys = [i for i, p in enumerate(c.demux(v)) if p.is_keyframe]
    c.close()
    if len(keys) < 2:
        return None
    return int(round(float(np.median(np.diff(keys)))))


def resize(rgb: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    w, h = size
    if rgb.shape[1] == w and rgb.shape[0] == h:
        return rgb
    interp = cv2.INTER_CUBIC if w > rgb.shape[1] else cv2.INTER_AREA
    return cv2.resize(rgb, (w, h), interpolation=interp)


def cut_segment(src: str, t0: float, t1: float, out: str, *, size: tuple[int, int] | None = None, fps: int | None = None) -> int:
    """取 [t0, t1) 的帧(左闭右开),可选放大到 size,写成恒定帧率 mp4。返回帧数。"""
    info = probe(src)
    fps = fps or int(round(info["fps"]))
    size = size or (info["width"], info["height"])
    w: Writer | None = None
    n = 0
    for t, rgb in iter_frames(src):
        if t < t0 - 1e-6:
            continue
        if t >= t1 - 1e-6:
            break
        if w is None:
            w = Writer(out, fps, *size)
        w.write(resize(rgb, size))
        n += 1
    if w:
        w.close()
    return n


def resample_to(src: str, out: str, *, fps: int, n_frames: int, size: tuple[int, int], time_scale: str = "normalized", interp: str = "nearest") -> int:
    """把任意帧率的生成视频按时间最近邻重采样成恰好 n_frames 帧、fps 恒定、size 尺寸。
    time_scale="normalized":生成视频的整段时长对应原段整段时长(模型会把内容匀速压缩 1-2%,
    按归一化时间映射才不会累积漂移);"absolute":按绝对秒数映射,生成略短时末尾用最后一帧补齐。
    返回补齐的帧数(normalized 下恒为 0)。"""
    frames = [(t, rgb) for t, rgb in iter_frames(src)]
    if not frames:
        raise RuntimeError(f"no frames: {src}")
    ts = np.array([t for t, _ in frames])
    gen_dur = ts[-1] + (ts[-1] - ts[0]) / max(len(ts) - 1, 1)  # 末帧再加一帧间隔
    src_dur = n_frames / fps
    w = Writer(out, fps, *size)
    pad = 0
    for i in range(n_frames):
        t = i / fps
        if time_scale == "normalized":
            t = t * gen_dur / src_dur
        if t > ts[-1] + 0.5 / fps:
            pad += 1
        if interp == "blend":
            # 可选 interp="blend":按时间权重混合相邻两帧,消 24→30fps 复制帧的顿挫;代价是运动处拖影,用户 2026-09-09 定不用,默认最近邻
            k = int(np.searchsorted(ts, t, side="right")) - 1
            k = max(0, min(k, len(ts) - 1))
            if k + 1 < len(ts) and ts[k + 1] > ts[k]:
                a = float(np.clip((t - ts[k]) / (ts[k + 1] - ts[k]), 0.0, 1.0))
                f = cv2.addWeighted(frames[k][1], 1 - a, frames[k + 1][1], a, 0) if 0 < a < 1 else (frames[k][1] if a <= 0 else frames[k + 1][1])
            else:
                f = frames[k][1]
            w.write(resize(f, size))
        else:
            j = int(np.argmin(np.abs(ts - t)))
            w.write(resize(frames[j][1], size))
    w.close()
    return pad


def concat(parts: list[str], out: str, *, fps: int, size: tuple[int, int]) -> int:
    w = Writer(out, fps, *size)
    for p in parts:
        for _, rgb in iter_frames(p):
            w.write(resize(rgb, size))
    return w.close()


def _tone_gain(ref_frames: list[np.ndarray], cur_frames: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """用同一时间窗(重叠区,画面内容相同)的 Lab 均值/标准差算一组逐通道线性映射,把 cur 的色调对齐到 ref。"""
    def stats(frames):
        lab = np.concatenate([cv2.cvtColor(f, cv2.COLOR_RGB2LAB).astype(np.float32).reshape(-1, 3) for f in frames])
        return lab.mean(0), lab.std(0) + 1e-3
    mr, sr = stats(ref_frames); mc, sc = stats(cur_frames)
    gain = sr / sc
    gain = np.clip(gain, 0.8, 1.25)           # 只纠小偏差,别把纹理对比度硬压
    offset = mr - gain * mc
    return gain, offset


def _apply_tone(rgb: np.ndarray, gain: np.ndarray, offset: np.ndarray, strength: float) -> np.ndarray:
    if strength <= 0:
        return rgb
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    g = 1 + (gain - 1) * strength
    o = offset * strength
    lab = lab * g + o
    return cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)


def assemble(parts: list[str], overlaps: list[int], out: str, *, fps: int, size: tuple[int, int], tone_ramp: int = 90) -> dict:
    """带重叠的拼接:parts[i](i≥1)开头 overlaps[i] 帧与前一段末尾同一时间窗重合。
    做两件事:①色调对齐——按重叠区统计把本段映射到前一段的 Lab 分布,强度从段首 1 线性降到 tone_ramp 帧后 0(只压段边界的台阶,不改本段后面的自身走向);
    ②交叉淡入淡出——重叠区内前一段权重 1→0、本段 0→1,把重画带来的纹理差异揉开。
    段边界不重叠(overlap=0)时退化为直接拼接。返回帧数与各边界的色调修正量,供留痕。"""
    w = Writer(out, fps, *size)
    prev: list[np.ndarray] = []           # 上一段尚未写出的尾巴(重叠区)
    n = 0
    log = []
    for i, p in enumerate(parts):
        cur = [resize(rgb, size) for _, rgb in iter_frames(p)]
        k = overlaps[i] if i < len(overlaps) else 0
        k = min(k, len(prev), len(cur))
        if i > 0 and k > 0:
            gain, offset = _tone_gain(prev[-k:], cur[:k])
            log.append({"boundary": i, "overlap_frames": k, "gain": [round(float(x), 3) for x in gain], "offset": [round(float(x), 2) for x in offset]})
            cur = [_apply_tone(f, gain, offset, max(0.0, 1 - j / tone_ramp)) for j, f in enumerate(cur)]
            head = prev[:-k]
            for f in head:
                w.write(f); n += 1
            for j in range(k):
                a = (j + 1) / (k + 1)
                w.write(cv2.addWeighted(prev[-k + j], 1 - a, cur[j], a, 0)); n += 1
            prev = cur[k:]
        else:
            for f in prev:
                w.write(f); n += 1
            prev = cur
    for f in prev:
        w.write(f); n += 1
    w.close()
    return {"frames": n, "boundaries": log}


def triptych(orig: str, gen: str, out: str, *, fps: int, alpha: float = 0.5) -> int:
    """[原 | 生成 | 半透明叠加] 三格并排,逐帧对齐(按索引)。"""
    a = iter_frames(orig)
    b = iter_frames(gen)
    w: Writer | None = None
    n = 0
    for (_, fa), (_, fb) in zip(a, b):
        fb = resize(fb, (fa.shape[1], fa.shape[0]))
        mix = cv2.addWeighted(fa, 1 - alpha, fb, alpha, 0)
        row = np.concatenate([fa, fb, mix], axis=1)
        if w is None:
            w = Writer(out, fps, row.shape[1], row.shape[0])
        w.write(row)
        n += 1
    if w:
        w.close()
    return n


def side_by_side(items: list[tuple[str, str]], out: str, *, fps: int, panel_w: int | None = None) -> int:
    """多路视频并排(逐帧按索引对齐),每格顶上写标签。items=[(标签, 路径)]。"""
    its = [iter_frames(p) for _, p in items]
    w: Writer | None = None
    n = 0
    for frames in zip(*its):
        tiles = []
        for (label, _), (_, f) in zip(items, frames):
            if panel_w and f.shape[1] != panel_w:
                f = resize(f, (panel_w, int(f.shape[0] * panel_w / f.shape[1])))
            f = f.copy()
            cv2.rectangle(f, (0, 0), (f.shape[1], 26), (0, 0, 0), -1)
            cv2.putText(f, label, (8, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2, cv2.LINE_AA)
            tiles.append(f)
        h = min(t.shape[0] for t in tiles)
        row = np.concatenate([t[:h] for t in tiles], axis=1)
        if w is None:
            w = Writer(out, fps, row.shape[1], row.shape[0], crf=18)
        w.write(row)
        n += 1
    if w:
        w.close()
    return n


def frames_at(path: str, times: list[float]) -> list[np.ndarray]:
    """取最接近各时间点的帧。"""
    want = sorted(times)
    got: dict[float, np.ndarray] = {}
    prev_t, prev = None, None
    for t, rgb in iter_frames(path):
        for tt in want:
            if tt not in got and t >= tt:
                got[tt] = rgb if prev is None or abs(t - tt) <= abs(prev_t - tt) else prev
        if len(got) == len(want):
            break
        prev_t, prev = t, rgb
    for tt in want:
        got.setdefault(tt, prev)
    return [got[t] for t in times]


def contact_sheet(rows: list[tuple[str, str]], times: list[float], out: str, *, thumb_w: int = 320) -> None:
    """rows=[(标签, 视频路径)];每行一个视频,每列一个时刻。"""
    tiles = []
    for label, path in rows:
        imgs = []
        for img in frames_at(path, times):
            h = int(img.shape[0] * thumb_w / img.shape[1])
            im = cv2.resize(img, (thumb_w, h), interpolation=cv2.INTER_AREA)
            imgs.append(im)
        row = np.concatenate(imgs, axis=1)
        bar = np.full((28, row.shape[1], 3), 255, np.uint8)
        cv2.putText(bar, label, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1, cv2.LINE_AA)
        tiles.append(np.concatenate([bar, row], axis=0))
    top = np.full((24, tiles[0].shape[1], 3), 255, np.uint8)
    for k, t in enumerate(times):
        cv2.putText(top, f"t={t:.1f}s", (k * thumb_w + 6, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 60, 60), 1, cv2.LINE_AA)
    sheet = np.concatenate([top] + tiles, axis=0)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    cv2.imwrite(out, cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR))


def splice_prefix(gen_part: str, src: str, t0: float, t1: float, out: str, *, prefix_frames: int, fps: int, size: tuple[int, int]) -> int:
    """接力段参考视频:前段生成结果的最后 prefix_frames 帧 + 源视频 [t0, t1) 的帧,统一到 size、恒定 fps。返回总帧数。"""
    gen = [rgb for _, rgb in iter_frames(gen_part)]
    head = gen[-prefix_frames:] if prefix_frames > 0 else []
    w = Writer(out, fps, *size)
    for f in head:
        w.write(resize(f, size))
    n = len(head)
    for t, rgb in iter_frames(src):
        if t < t0 - 1e-6:
            continue
        if t >= t1 - 1e-6:
            break
        w.write(resize(rgb, size)); n += 1
    w.close()
    return n
