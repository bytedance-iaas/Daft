"""The visualizer's reader of a raw UMI / TRUMI session (design doc 24 §6).

A session is ``dataset_plan.pkl`` beside ``demos/``: an episode is a plan entry, its cameras the demos' own
``raw_video.mp4`` files cut to the plan's frame range, its curves each hand's TCP position and opening from
the plan. Nothing is converted: the cameras point at the raw files with ``from_ts`` / ``to_ts`` (GoPro HEVC at
2.7K, which the platform transcodes for the browser like any other codec it cannot play), the curves are read
from the plan. Same surface as :class:`.lerobot.LeRobotReader` - the service routes camera bytes and transcodes
through :meth:`camera_file`.
"""
from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass, field

import numpy as np

from curation.streams.rangefile import RangeFile
from curation.viz import lerobot_info as L
from curation.viz.groups import Group, Line
from curation.viz.series import json_values, thin, window

from ..errors import ApiError
from .source import Access, VizSource

PLAN = "dataset_plan.pkl"


@dataclass
class _Episode:
    index: int
    times: np.ndarray                         # episode time of each plan sample, from 0
    tcp: list[np.ndarray]                     # per hand (n, 6): position + rotation vector
    width: list[np.ndarray]                   # per hand (n,)
    videos: list[tuple[str, float, float]]    # per camera: (file, from_ts, to_ts) in the raw video


@dataclass
class _Meta:
    episodes: list[_Episode]
    cameras: list[dict]
    fps: float | None
    hands: int
    by_index: dict[int, _Episode] = field(default_factory=dict)


class UmiSessionReader:
    def __init__(self, service):
        self.svc = service
        self._lock = threading.Lock()
        self._metas: dict[tuple, _Meta] = {}

    # ------------------------------------------------------------ the session
    def meta(self, src: VizSource) -> _Meta:
        key = src.cache_key
        with self._lock:
            hit = self._metas.get(key)
        if hit is not None:
            return hit
        m = self._build(src)
        with self._lock:
            if len(self._metas) > 64:
                self._metas.clear()
            self._metas[key] = m
        return m

    def _video(self, st, rel: str, size: int | None) -> tuple[float, int, int, str, str | None]:
        """(frame rate, width, height, codec, pix_fmt) from the video's header."""
        import av

        if st.remote:
            size = size if size is not None else st.stat(rel).size
            fh = RangeFile(lambda s, n: st.read_range(rel, s, n), int(size), name=rel)
        else:
            fh = open(os.path.join(st.root, rel), "rb")
        with fh, av.open(fh) as inp:
            s = inp.streams.video[0]
            return float(s.average_rate), s.width, s.height, s.codec_context.name, s.codec_context.pix_fmt

    def _build(self, src: VizSource) -> _Meta:
        from curation.extensions.eef_consistency.adapters.umi import read_plan

        with Access(self.svc.rt, src).storage() as st:
            data = st.read_bytes(PLAN) if st.remote else open(os.path.join(st.root, PLAN), "rb").read()
            fd, tmp = tempfile.mkstemp(prefix="umi-plan-", suffix=".pkl")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                plans = read_plan(tmp)
            finally:
                os.unlink(tmp)
            headers: dict[str, tuple] = {}
            episodes = []
            for ep, plan in enumerate(plans):
                t = np.asarray(plan["episode_timestamps"], float)
                t = t - t[0]
                videos = []
                for cam in plan["cameras"]:
                    rel = f"demos/{cam['video_path']}"
                    if rel not in headers:
                        headers[rel] = self._video(st, rel, src.size_of(rel))
                    rate = headers[rel][0]
                    first, end = cam["video_start_end"]
                    videos.append((rel, first / rate, end / rate))
                episodes.append(_Episode(ep, t, [np.asarray(g["tcp_pose"], float) for g in plan["grippers"]],
                                         [np.asarray(g["gripper_width"], float).reshape(-1) for g in plan["grippers"]],
                                         videos))
        first = episodes[0]
        cameras = []
        for j, (rel, _, _) in enumerate(first.videos):
            rate, w, h, codec, pix = headers[rel]
            playable = codec == "h264"
            access = ("local" if src.is_local else "direct") if playable else (
                "transcode" if self.svc.transcode_enabled else "unsupported")
            reason = None if playable else (
                f"原始编码 {codec}，浏览器不能直接播放，由平台转为 H.264" if self.svc.transcode_enabled
                else f"原始编码 {codec}，浏览器不能直接播放；平台转码已关闭（CURATOR_VIZ_TRANSCODE=0）")
            cameras.append({"key": f"camera{j}", "name": f"camera{j}", "source": f"camera{j}", "kind": "video",
                            "access": access, "codec": codec, "codec_string": L.codec_string(codec, pix),
                            "width": w, "height": h, "fps": rate, "pix_fmt": pix,
                            "transcoded": access == "transcode", "reason": reason})
        dt = np.median(np.diff(first.times)) if len(first.times) > 1 else 0.0
        m = _Meta(episodes, cameras, round(1.0 / float(dt), 6) if dt > 0 else None, len(first.tcp))
        m.by_index = {e.index: e for e in episodes}
        return m

    def _groups(self, m: _Meta) -> list[Group]:
        out = []
        for j in range(m.hands):
            hand = f"robot{j}"
            out.append(Group(key=f"{hand}_tcp_position", name=f"{hand} TCP 位置", smart=False, unit="m",
                             lines=[Line(name=a, role="state", source=f"{hand}.tcp", dim=k) for k, a in enumerate("xyz")],
                             sources=[f"{hand}.tcp"]))
            out.append(Group(key=f"{hand}_gripper_width", name=f"{hand} 开口", smart=False, unit="m",
                             lines=[Line(name="width", role="state", source=f"{hand}.width", dim=0)],
                             sources=[f"{hand}.width"]))
        return out

    # ------------------------------------------------------------ the service's surface
    def dataset_model(self, src: VizSource) -> dict:
        m = self.meta(src)
        return {"cameras": m.cameras, "streams": [g.as_stream() for g in self._groups(m)], "annotation_sources": [],
                "field_tree": [], "fps": m.fps, "episode_count": len(m.episodes),
                "total_frames": int(sum(len(e.times) for e in m.episodes)),
                "robot_type": "umi_dual_handheld_gripper" if m.hands == 2 else "umi_handheld_gripper",
                "episode_indices": None, "warnings": []}

    def episode_items(self, src: VizSource) -> list[dict]:
        m = self.meta(src)
        return [{"index": e.index, "duration_s": round(float(e.times[-1]) + (1 / m.fps if m.fps else 0), 3),
                 "frames": len(e.times), "task": None, "steps": None} for e in m.episodes]

    def row(self, src: VizSource, index: int) -> tuple[_Meta, _Episode]:
        m = self.meta(src)
        e = m.by_index.get(int(index))
        if e is None:
            raise ApiError("not_found", f"数据集里没有 episode {index}", details={"reason": "no_episode"})
        return m, e

    def episode_model(self, src: VizSource, index: int, urls) -> dict:
        m, e = self.row(src, index)
        duration = float(e.times[-1]) + (1.0 / m.fps if m.fps else 0.0)
        cameras = [urls.camera(src, index, cam, rel, frm, to) for cam, (rel, frm, to) in zip(m.cameras, e.videos)]
        return {"duration_s": round(duration, 3), "frames": len(e.times), "fps": m.fps, "task": None,
                "timeline": {"kind": "frame", "fps": m.fps, "frame_reference": None, "frame_times": None},
                "cameras": cameras, "streams": [],
                "annotations": {"tracks": [], "events": [], "labels": [], "warnings": []}, "warnings": [],
                "check_clock": {"offset_s": 0.0, "fps": m.fps} if src.scope == "task" else None}

    def series(self, src: VizSource, index: int, stream: str, start, end, points: int) -> dict:
        m, e = self.row(src, index)
        group = next((g for g in self._groups(m) if g.key == stream), None)
        if group is None:
            raise ApiError("not_found", f"没有曲线组 {stream}", details={"reason": "unknown_stream"})
        t = e.times
        sl = window(t, start, end)
        hand = int(group.sources[0][len("robot"):].split(".")[0])
        lines = [(e.tcp[hand][:, ln.dim] if group.sources[0].endswith(".tcp") else e.width[hand])[sl]
                 for ln in group.lines]
        tt, ys, thinned = thin(t[sl], lines, points)
        return {"stream": stream, "unit": group.unit,
                "from_s": round(float(t[sl][0]), 4) if len(t[sl]) else float(start or 0.0),
                "to_s": round(float(t[sl][-1]), 4) if len(t[sl]) else float(end or 0.0),
                "t": json_values(tt, 4),
                "lines": [{"name": ln.name, "role": ln.role, "values": json_values(y)} for ln, y in zip(group.lines, ys)],
                "total_points": int(len(t[sl])), "downsampled": bool(thinned)}

    def camera_file(self, src: VizSource, index: int, camera: str) -> tuple[dict, str, float | None, float | None]:
        m, e = self.row(src, index)
        j = next((k for k, c in enumerate(m.cameras) if c["key"] == camera), None)
        if j is None:
            raise ApiError("not_found", f"没有相机 {camera}", details={"reason": "unknown_camera"})
        rel, frm, to = e.videos[j]
        return m.cameras[j], rel, frm, to

    def meta_file(self, src: VizSource, path: str) -> dict:
        raise ApiError("validation_failed", "UMI 原始会话没有可看的元数据文件",
                       details={"errors": [{"field": "path", "problem": "not a metadata file"}]})

    def depth_pack(self, src: VizSource, index: int, key: str):
        raise ApiError("not_found", f"没有深度流 {key}", details={"reason": "unknown_stream"})
