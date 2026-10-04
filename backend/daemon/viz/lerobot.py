"""The LeRobot v2 / v3 reader of the visualizer (design doc 18 §4, D61).

Everything the dataset model needs comes from ``meta/``: ``info.json`` (features, cameras and their
codecs, fps), the episode table (``meta/episodes.jsonl`` or ``meta/episodes/*.parquet``: length,
task text, the files of every episode and, in v3, each episode's row window and video window) and
the small lookup tables of the annotations. One episode's frame columns - the curves and the
per-frame annotation columns, never pictures or depth - are read once from its data parquet (v3:
only its row groups) and kept in a bounded cache, so the episode answer and its curve requests share
one read.
"""
from __future__ import annotations

import io
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from curation.streams.rangefile import RangeFile
from curation.viz import annotations as A
from curation.viz import lerobot_info as L
from curation.viz.groups import Group, curve_groups
from curation.viz.series import clock_problem, episode_times, json_values, read_episode_columns, thin, window

from ..errors import ApiError
from .source import Access, VizSource

#: the widest per-frame numeric column read for curves (a flattened picture is not a curve)
MAX_CURVE_WIDTH = 512
META_FILES_PREVIEW = 256 * 1024


def _v(x):
    """A JSON-safe scalar from a pandas / numpy value."""
    if hasattr(x, "tolist"):
        x = x.tolist()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


@dataclass
class EpisodeRow:
    index: int
    length: int
    task: str
    data_key: str
    videos: dict[str, str]                       # feature key -> file
    windows: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    from_index: int | None = None                # v3: global frame window [from, to)
    to_index: int | None = None
    fields: dict[str, Any] = field(default_factory=dict)   # the rest of the episode row
    video_files: dict[str, tuple[int, int]] = field(default_factory=dict)   # v3: feature key -> (chunk, file)


@dataclass
class LeRobotMeta:
    info: dict
    version: str                                 # v2 | v3
    fps: float | None
    episodes: list[EpisodeRow]
    by_index: dict[int, EpisodeRow]
    cameras: list[dict]                          # C4 VizCamera
    camera_features: dict[str, str]              # camera key -> feature key
    groups: list[Group]
    sources: list[A.Source]
    meta_files: list[str]
    tasks: dict[int, str]
    lookups: dict[str, dict[int, str]]
    curve_columns: list[str]
    annotation_columns: list[str]
    episode_fields: set[str]
    made_at: float = field(default_factory=time.monotonic)


class LeRobotReader:
    def __init__(self, service):
        self.svc = service
        self._locks: dict[tuple, threading.Lock] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ metadata
    def meta(self, src: VizSource) -> LeRobotMeta:
        key = ("lerobot-meta", *src.cache_key)
        hit = self.svc.meta_cache.get(key)
        if hit is not None and (src.scope == "task" or time.monotonic() - hit.made_at < self.svc.META_TTL_S):
            return hit
        with self._lock:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            hit = self.svc.meta_cache.get(key)
            if hit is not None and (src.scope == "task" or time.monotonic() - hit.made_at < self.svc.META_TTL_S):
                return hit
            built = self._build_meta(src)
            self.svc.meta_cache.put(key, built)
            return built

    def _build_meta(self, src: VizSource) -> LeRobotMeta:
        access = Access(self.svc.rt, src)
        with access.storage() as st:
            return self._meta_from(src, st, lambda: _meta_listing(src, st))

    def _meta_from(self, src: VizSource, st, meta_listing) -> LeRobotMeta:
        """The model of a dataset whose ``meta/`` files ``st`` reads (``read_bytes`` / ``read_range``);
        ``meta_listing()`` names them. The Lance reader hands in its ``meta.lance`` table here."""
        try:
            info = json.loads(st.read_bytes("meta/info.json").decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - missing, unreadable: the page says so
            raise ApiError("not_found", f"读不到 meta/info.json：{exc}") from None
        version = "v3" if str(info.get("codebase_version") or "").startswith("v3") else "v2"
        meta_files = meta_listing()
        cams = L.camera_info_of(info)
        camera_features = {}
        cameras = []
        used = set()
        for c in cams:
            key = _camera_key(c["name"], used)
            camera_features[key] = c["key"]
            cameras.append(self._camera(src, key, c))
        episodes = self._episodes(st, info, version, meta_files, [c["key"] for c in cams])
        ep_fields = set()
        for e in episodes[:50]:
            ep_fields.update(k for k, v in e.fields.items() if v is not None)
        peeks: dict[str, dict | None] = {}

        def peek(rel: str) -> dict | None:
            if rel not in peeks:
                try:
                    rows = A.parse_jsonl(st.read_range(rel, 0, 16384))
                    peeks[rel] = rows[0] if rows else None
                except Exception:  # noqa: BLE001
                    peeks[rel] = None
            return peeks[rel]

        sources = A.detect_sources(info, meta_files, peek=peek, episode_fields=ep_fields)
        if src.annotations_upload:
            sources.append(A.Source("external", "segments", "外部标注", "argus", "外部标注文件（上传）"))
            A._mark_primary(sources)
        tasks = self._tasks(st, meta_files)
        lookups = {}
        for s in sources:
            if s.table and s.table not in lookups and s.format in ("subtask_index", "index_table"):
                lookups[s.table] = self._lookup(st, s.table)
        groups = curve_groups(info)
        curve_cols = sorted({ln.source for g in groups for ln in g.lines})
        curve_cols = [c for c in curve_cols if L.width_of((info.get("features") or {}).get(c) or {}) <= MAX_CURVE_WIDTH]
        ann_cols = sorted({c for s in sources for c in s.columns} | {"quality_index"} & set(info.get("features") or {}))
        fps = info.get("fps") if isinstance(info.get("fps"), (int, float)) else None
        return LeRobotMeta(info=info, version=version, fps=float(fps) if fps else None, episodes=episodes,
                           by_index={e.index: e for e in episodes}, cameras=cameras,
                           camera_features=camera_features, groups=groups, sources=sources,
                           meta_files=meta_files, tasks=tasks, lookups=lookups, curve_columns=curve_cols,
                           annotation_columns=ann_cols, episode_fields=ep_fields)

    def _camera(self, src: VizSource, key: str, c: dict) -> dict:
        transcode = bool(c["needs_transcode"])
        if transcode:
            access = "transcode" if self.svc.transcode_enabled else "unsupported"
        else:
            access = "local" if src.is_local else "direct"
        reason = None
        if transcode:
            reason = (f"原始编码 {c['codec']}，浏览器不能直接播放，由平台转为 H.264" if self.svc.transcode_enabled
                      else f"原始编码 {c['codec']}，浏览器不能直接播放；平台转码已关闭（CURATOR_VIZ_TRANSCODE=0）")
        return {"key": key, "name": c["name"], "source": c["key"], "kind": "video", "access": access,
                "codec": c["codec"], "codec_string": L.codec_string(c["codec"], c.get("pix_fmt")),
                "width": c["width"], "height": c["height"], "fps": c["fps"], "pix_fmt": c.get("pix_fmt"),
                "transcoded": access == "transcode", "reason": reason}

    def _episodes(self, st, info: dict, version: str, meta_files: list[str], cam_keys: list[str]) -> list[EpisodeRow]:
        out = []
        if version == "v2":
            raw = st.read_bytes("meta/episodes.jsonl")
            chunks = int(info.get("chunks_size") or 1000) or 1000
            data_tpl, video_tpl = str(info.get("data_path") or ""), str(info.get("video_path") or "")
            for rec in A.parse_jsonl(raw):
                try:
                    idx = int(rec["episode_index"])
                except (KeyError, TypeError, ValueError):
                    continue
                tasks = rec.get("tasks")
                task = str(tasks[0]) if isinstance(tasks, list) and tasks else (tasks if isinstance(tasks, str) else "")
                chunk = idx // chunks
                videos = {}
                for vk in cam_keys:
                    try:
                        videos[vk] = video_tpl.format(episode_chunk=chunk, video_key=vk, episode_index=idx)
                    except (KeyError, IndexError, ValueError):
                        continue
                try:
                    data_key = data_tpl.format(episode_chunk=chunk, episode_index=idx)
                except (KeyError, IndexError, ValueError):
                    data_key = ""
                out.append(EpisodeRow(idx, int(rec.get("length") or 0), task, data_key, videos,
                                      fields={k: v for k, v in rec.items() if k not in ("episode_index", "tasks", "length")}))
        else:
            import pandas as pd

            keys = [k for k in meta_files if k.startswith("meta/episodes/") and k.endswith(".parquet")]
            frames = [pd.read_parquet(io.BytesIO(st.read_bytes(k))) for k in sorted(keys)]
            if not frames:
                raise ApiError("not_found", "meta/episodes/ 下没有 parquet（LeRobot v3 的 episode 表）")
            table = pd.concat(frames, ignore_index=True)
            data_tpl, video_tpl = str(info.get("data_path") or ""), str(info.get("video_path") or "")
            skip = {"episode_index", "length", "tasks", "dataset_from_index", "dataset_to_index"}
            for rec in table.to_dict("records"):
                try:
                    idx = int(rec["episode_index"])
                except (KeyError, TypeError, ValueError):
                    continue
                tasks = rec.get("tasks")
                tasks = list(tasks) if tasks is not None and not isinstance(tasks, str) else tasks
                task = str(tasks[0]) if isinstance(tasks, list) and tasks else (tasks or "")
                try:
                    data_key = data_tpl.format(chunk_index=int(rec["data/chunk_index"]),
                                               file_index=int(rec["data/file_index"]))
                except (KeyError, TypeError, ValueError):
                    data_key = ""
                videos, windows, files = {}, {}, {}
                for vk in cam_keys:
                    try:
                        chunk, file = int(rec[f"videos/{vk}/chunk_index"]), int(rec[f"videos/{vk}/file_index"])
                        videos[vk] = video_tpl.format(video_key=vk, chunk_index=chunk, file_index=file)
                        windows[vk] = (_num(rec.get(f"videos/{vk}/from_timestamp")),
                                       _num(rec.get(f"videos/{vk}/to_timestamp")))
                        files[vk] = (chunk, file)
                    except (KeyError, TypeError, ValueError):
                        continue
                fields = {k: _v(v) for k, v in rec.items() if k not in skip and not k.startswith(("data/", "videos/", "stats/", "meta/"))}
                out.append(EpisodeRow(idx, int(rec.get("length") or 0), task, data_key, videos, windows,
                                      from_index=_int(rec.get("dataset_from_index")),
                                      to_index=_int(rec.get("dataset_to_index")), fields=fields, video_files=files))
        out.sort(key=lambda e: e.index)
        return out

    def _tasks(self, st, meta_files: list[str]) -> dict[int, str]:
        try:
            if "meta/tasks.jsonl" in meta_files:
                return A.lookup_from_records(A.parse_jsonl(st.read_bytes("meta/tasks.jsonl")))
            if "meta/tasks.parquet" in meta_files:
                return A.parse_lookup_parquet(st.read_bytes("meta/tasks.parquet"))
        except Exception:  # noqa: BLE001 - task texts are a nicety here
            return {}
        return {}

    def _lookup(self, st, rel: str) -> dict[int, str]:
        try:
            data = st.read_bytes(rel)
        except Exception:  # noqa: BLE001
            return {}
        if rel.endswith(".parquet"):
            return A.parse_lookup_parquet(data)
        return A.lookup_from_records(A.parse_jsonl(data))

    # ------------------------------------------------------------ the dataset model
    def dataset_model(self, src: VizSource) -> dict:
        m = self.meta(src)
        info = m.info
        streams = [g.as_stream() for g in m.groups]
        for d in L.depth_features(info):
            streams.append({"key": _slug(d), "kind": "depth", "name": d, "unit": None, "lines": [], "smart": False,
                            "available": False, "reason": "深度图在第二期渲染（设计 18 §10）", "sources": [d],
                            "rate_hz": None})
        warnings = []
        for s in m.sources:
            if not s.supported:
                warnings.append({"code": "annotation_unsupported", "message": s.reason or "标注格式不支持"})
        for c in m.cameras:
            if c["access"] == "unsupported":
                warnings.append({"code": "camera_unsupported", "message": f"相机 {c['name']}：{c['reason']}"})
        frames = info.get("total_frames") if isinstance(info.get("total_frames"), int) else None
        return {"cameras": m.cameras, "streams": streams,
                "annotation_sources": [s.as_dict() for s in m.sources],
                "field_tree": self._field_tree(src, m), "fps": m.fps,
                "episode_count": len(m.episodes), "total_frames": frames if frames is not None else sum(e.length for e in m.episodes),
                "robot_type": info.get("robot_type") if isinstance(info.get("robot_type"), str) else None,
                "episode_indices": _compact([e.index for e in m.episodes]), "warnings": warnings}

    def _field_tree(self, src: VizSource, m: LeRobotMeta) -> list[dict]:
        feats = m.info.get("features") or {}
        cam_nodes = []
        for c in m.cameras:
            detail = {"分辨率": f"{c['width']}×{c['height']}" if c["width"] else None, "编码": c["codec"],
                      "帧率": c["fps"], "读取方式": {"direct": "直连", "local": "本地", "transcode": "平台转码",
                                                    "unsupported": "播不了"}.get(c["access"], c["access"]),
                      "字段": c["source"]}
            cam_nodes.append({"id": f"camera:{c['key']}", "name": c["name"], "kind": "camera", "camera": c["key"],
                              "dtype": "video", "shape": L.shape_of(feats.get(c["source"]) or {}), "detail": detail})
        stream_nodes = []
        for g in m.groups:
            names = [ln.name for ln in g.lines if ln.role != "action"] or [ln.name for ln in g.lines]
            stream_nodes.append({"id": f"stream:{g.key}", "name": g.name, "kind": "series", "stream": g.key,
                                 "names": names, "detail": {"来源": "、".join(g.sources), "条数": len(g.lines),
                                                            "智能布局": g.smart}})
        for d in L.depth_features(m.info):
            stream_nodes.append({"id": f"depth:{d}", "name": d, "kind": "depth", "dtype": str((feats.get(d) or {}).get("dtype")),
                                 "shape": L.shape_of(feats.get(d) or {}), "detail": {"说明": "深度图在第二期渲染"}})
        for d in L.image_features(m.info):
            stream_nodes.append({"id": f"image:{d}", "name": d, "kind": "other", "dtype": "image",
                                 "shape": L.shape_of(feats.get(d) or {}), "detail": {"说明": "图片帧序列本期不显示"}})
        ann_nodes = [{"id": f"annotation:{s.key}", "name": s.name, "kind": "table",
                      "detail": {"来源": s.source, "格式": s.format, "支持": s.supported, "原因": s.reason}}
                     for s in m.sources]
        tasks_file = next((f for f in ("meta/tasks.jsonl", "meta/tasks.parquet") if f in m.meta_files), None)
        if tasks_file:
            ann_nodes.insert(0, {"id": "tasks", "name": tasks_file.rsplit("/", 1)[-1], "kind": "table",
                                 "file": tasks_file if tasks_file.endswith(".jsonl") else None,
                                 "detail": {"任务条数": len(m.tasks)}})
        meta_nodes = []
        for f in m.meta_files:
            if f.startswith("meta/episodes/"):           # the episode table's chunks: listed as one
                continue
            meta_nodes.append({"id": f"file:{f}", "name": f[len("meta/"):], "kind": "file",
                               "file": f if f.endswith((".json", ".jsonl", ".md")) else None,
                               "detail": {"大小": src.size_of(f)}})
        other = [{"id": f"feature:{k}", "name": k, "kind": "other", "dtype": str(f.get("dtype")),
                  "shape": L.shape_of(f), "names": L.flat_names(f.get("names")), "detail": {}}
                 for k, f in feats.items() if isinstance(f, dict) and k not in m.camera_features.values()
                 and str(f.get("dtype")) not in ("float16", "float32", "float64", "video")]
        files = []
        listing = src.listing()
        if listing:
            for top in ("data", "videos", "meta"):
                objs = [o for k, o in listing.items() if k.startswith(top + "/")]
                if objs:
                    files.append({"id": f"dir:{top}", "name": f"{top}/", "kind": "group",
                                  "detail": {"文件数": len(objs), "字节": sum(o.size for o in objs)}})
        tree = [{"id": "cameras", "name": "相机", "kind": "group", "children": cam_nodes},
                {"id": "streams", "name": "状态与动作", "kind": "group", "children": stream_nodes},
                {"id": "annotations", "name": "任务与标注", "kind": "group", "children": ann_nodes},
                {"id": "meta", "name": "元数据", "kind": "group", "children": meta_nodes}]
        if other:
            tree.append({"id": "other", "name": "其他字段", "kind": "group", "children": other})
        if files:
            tree.append({"id": "files", "name": "文件", "kind": "group", "children": files})
        return _clean_tree(tree)

    # ------------------------------------------------------------ the episode list
    def episode_items(self, src: VizSource) -> list[dict]:
        m = self.meta(src)
        out = []
        for e in m.episodes:
            out.append({"index": e.index, "duration_s": round(e.length / m.fps, 3) if m.fps else None,
                        "frames": e.length, "task": e.task, "steps": None})
        return out

    # ------------------------------------------------------------ one episode
    def row(self, src: VizSource, index: int) -> tuple[LeRobotMeta, EpisodeRow]:
        m = self.meta(src)
        row = m.by_index.get(int(index))
        if row is None:
            raise ApiError("not_found", f"数据集里没有 episode {index}", details={"reason": "no_episode"})
        return m, row

    def frames(self, src: VizSource, index: int) -> dict[str, Any]:
        """The episode's frame columns (curves + annotation columns) and ``__t__`` (episode time)."""
        m, row = self.row(src, index)
        key = ("lerobot-frames", *src.cache_key, int(index))
        hit = self.svc.frames_cache.get(key)
        if hit is not None:
            return hit
        cols = list(dict.fromkeys(["timestamp", "frame_index"] + m.curve_columns + m.annotation_columns))
        access = Access(self.svc.rt, src)
        with access.storage() as st:
            if not row.data_key:
                raise ApiError("not_found", f"episode {index} 没有数据文件")
            size = src.size_of(row.data_key)
            if size is None:
                info = st.stat(row.data_key)
                if info is None:
                    raise ApiError("not_found", f"数据集里没有 {row.data_key}")
                size = info.size
            if st.remote:
                fileobj = RangeFile(lambda s, n: st.read_range(row.data_key, s, n), size, name=row.data_key)
                data = read_episode_columns(fileobj, cols, from_index=row.from_index, to_index=row.to_index,
                                            episode_index=row.index if m.version == "v3" else None)
            else:
                with open(os.path.join(st.root, row.data_key), "rb") as fh:
                    data = read_episode_columns(fh, cols, from_index=row.from_index, to_index=row.to_index,
                                                episode_index=row.index if m.version == "v3" else None)
        n = max((len(v) for v in data.values()), default=0)
        data["__t__"] = episode_times(data, m.fps, n)
        size = sum(v.nbytes if isinstance(v, np.ndarray) else 64 * len(v) for v in data.values())
        self.svc.frames_cache.put(key, data, size=size)
        return data

    def episode_model(self, src: VizSource, index: int, urls) -> dict:
        m, row = self.row(src, index)
        data = self.frames(src, index)
        t = data["__t__"]
        warnings = []
        if row.length and len(t) and len(t) != row.length:
            warnings.append({"code": "episode_rows_mismatch",
                             "message": f"episode 表写 {row.length} 帧，数据里 episode_index = {row.index} 的行有 {len(t)} 行：按数据本身读"})
        ts = data.get("timestamp")
        if isinstance(ts, np.ndarray) and ts.ndim == 1 and len(ts) == len(t) and len(t):
            problem = clock_problem(ts)
            if problem:
                warnings.append({"code": "timestamp_unusable",
                                 "message": f"timestamp 列{problem}，播放器按帧号 / fps 计时"})
        frames = len(t) or row.length
        duration = float(t[-1] + (1.0 / m.fps if m.fps else 0.0)) if len(t) else (row.length / m.fps if m.fps else 0.0)
        cameras = []
        for c in m.cameras:
            rel, frm, to = self._camera_place(m, row, c)
            cameras.append(urls.camera(src, index, c, rel, frm, to))
        ann = self._annotations(src, m, row, data)
        task = {"text": row.task, "source": "原始标注"} if row.task else None
        return {"duration_s": round(duration, 3), "frames": int(frames), "fps": m.fps, "task": task,
                "timeline": {"kind": "frame", "fps": m.fps, "frame_reference": None, "frame_times": None},
                "cameras": cameras, "annotations": ann, "warnings": warnings,
                "check_clock": {"offset_s": 0.0, "fps": m.fps} if src.scope == "task" else None}

    def _camera_place(self, m: LeRobotMeta, row: EpisodeRow, cam: dict) -> tuple[str | None, float | None, float | None]:
        """(file, from_ts, to_ts) of a camera in an episode; no file: the episode has no video of it."""
        feature = m.camera_features[cam["key"]]
        frm, to = row.windows.get(feature, (None, None))
        return row.videos.get(feature), frm, to

    def _annotations(self, src: VizSource, m: LeRobotMeta, row: EpisodeRow, data: dict) -> dict:
        times = [float(x) for x in data["__t__"]]
        cols = {c: (v.tolist() if isinstance(v, np.ndarray) else v) for c, v in data.items()
                if c in m.annotation_columns}
        primary = (src.display_config or {}).get("track") if isinstance(src.display_config, dict) else None
        ann = A.episode_annotations([s for s in m.sources if s.format != "argus"], cols, times,
                                    lookups=m.lookups, episode_row=row.fields, tasks=m.tasks, primary=primary)
        if src.annotations_upload:
            ext = self.svc.external_annotations(src, row.index)
            if ext is not None:
                ann.tracks += ext.tracks
                ann.events += ext.events
                ann.labels += ext.labels
                if not any(t["primary"] for t in ann.tracks) and ann.tracks:
                    ann.tracks[0]["primary"] = True
        return ann.as_dict()

    # ------------------------------------------------------------ curves
    def series(self, src: VizSource, index: int, stream: str, start: float | None, end: float | None,
               points: int) -> dict:
        m, _ = self.row(src, index)
        group = next((g for g in m.groups if g.key == stream), None)
        if group is None:
            raise ApiError("not_found", f"没有曲线组 {stream}", details={"reason": "unknown_stream"})
        data = self.frames(src, index)
        t = np.asarray(data["__t__"], dtype=np.float64)
        sl = window(t, start, end)
        lines = []
        for ln in group.lines:
            col = data.get(ln.source)
            if isinstance(col, np.ndarray) and col.ndim == 2 and ln.dim < col.shape[1]:
                lines.append(col[:, ln.dim][sl])
            elif isinstance(col, np.ndarray) and col.ndim == 1 and ln.dim == 0:
                lines.append(col[sl])
            else:
                lines.append(np.full(len(t[sl]), np.nan))
        tt, ys, thinned = thin(t[sl], lines, points)
        return {"stream": stream, "unit": None,
                "from_s": round(float(t[sl][0]), 4) if len(t[sl]) else float(start or 0.0),
                "to_s": round(float(t[sl][-1]), 4) if len(t[sl]) else float(end or 0.0),
                "t": json_values(tt, 4),
                "lines": [{"name": ln.name, "role": ln.role, "values": json_values(y)} for ln, y in zip(group.lines, ys)],
                "total_points": int(len(t[sl])), "downsampled": bool(thinned)}

    # ------------------------------------------------------------ media
    def camera_file(self, src: VizSource, index: int, camera: str) -> tuple[dict, str, float | None, float | None]:
        """(VizCamera, file, from_ts, to_ts) of one camera of an episode."""
        m, row = self.row(src, index)
        cam = next((c for c in m.cameras if c["key"] == camera), None)
        if cam is None:
            raise ApiError("not_found", f"没有相机 {camera}", details={"reason": "unknown_camera"})
        feature = m.camera_features[camera]
        rel = row.videos.get(feature)
        if not rel:
            raise ApiError("not_found", f"episode {index} 没有相机 {camera} 的视频")
        frm, to = row.windows.get(feature, (None, None))
        return cam, rel, frm, to

    def meta_file(self, src: VizSource, path: str) -> dict:
        m = self.meta(src)
        allowed = set(m.meta_files) | {"README.md"}
        if path not in allowed or not path.endswith((".json", ".jsonl", ".md", ".txt")):
            raise ApiError("validation_failed", f"只能看数据集信息树里的元数据文件，不能看 {path}",
                           details={"errors": [{"field": "path", "problem": "not a metadata file"}]})
        access = Access(self.svc.rt, src)
        with access.storage() as st:
            size = src.size_of(path)
            if size is None:
                info = st.stat(path)
                if info is None:
                    raise ApiError("not_found", f"数据集里没有 {path}")
                size = info.size
            data = st.read_range(path, 0, min(size, META_FILES_PREVIEW)) if size else b""
        kind = "json" if path.endswith(".json") else "jsonl" if path.endswith(".jsonl") else (
            "markdown" if path.endswith(".md") else "text")
        return {"path": path, "size": int(size), "truncated": size > META_FILES_PREVIEW,
                "kind": kind, "text": data.decode("utf-8", "replace")}


def _meta_listing(src: VizSource, st) -> list[str]:
    listing = src.listing()
    if listing is None:
        return sorted(st.list("meta/"))
    return sorted(k for k in listing if k.startswith("meta/"))


def _clean_tree(nodes: list[dict]) -> list[dict]:
    """Optional references (``file``, ``camera``, ``stream``) are left out rather than null."""
    out = []
    for n in nodes:
        n = {k: v for k, v in n.items() if not (k in ("file", "camera", "stream") and v is None)}
        if "children" in n:
            n["children"] = _clean_tree(n["children"])
        out.append(n)
    return out


def _camera_key(name: str, used: set[str]) -> str:
    base = _slug(name)[:90] or "camera"
    key, n = base, 2
    while key in used:
        key, n = f"{base}_{n}", n + 1
    used.add(key)
    return key


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^0-9A-Za-z_-]+", "_", text).strip("_")


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _compact(indices: list[int]) -> str | None:
    if indices == list(range(len(indices))):
        return None
    from curation.cli.episodes import compact

    return compact(indices)
