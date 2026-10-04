"""The mcap reader of the visualizer (design doc 18 §4, §6; D61, D62).

The dataset's confirmed mapping (C7; a task's frozen one) says which topics are cameras and curves.
The dataset model comes from the mapping plus a probe of the first episode file (summary + first
messages, a few ranged reads). An episode is read once, in one pass over its mapped topics
(:mod:`curation.viz.mcap_episode`), when it is first opened: JPEG / PNG cameras become frame packs,
H.264 / H.265 cameras fragmented mp4s, curves an ``npz`` - all in the Daemon's disk cache, named by
the source's fingerprint and the mapping's version, so a new mapping version reads the file again.
"""
from __future__ import annotations

import json
import pathlib
import threading
from typing import Any

import numpy as np

from curation.streams.rangefile import RangeFile
from curation.viz import mcap_messages as MM
from curation.viz import mcap_probe as MP
from curation.viz.mcap_episode import camera_keys, scan
from curation.viz.series import json_values, thin, window

from ..errors import ApiError
from .media import digest
from .source import Access, VizSource

#: files whose topic sets are compared with the probed one
MAX_PROBE_FILES = 3
#: files tried in order when the first one cannot be probed (no summary, unreadable)
MAX_PROBE_CANDIDATES = 5


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^0-9A-Za-z_.-]+", "_", text.lstrip("/")).strip("_.")[:120] or "x"


class McapReader:
    def __init__(self, service):
        self.svc = service
        self._locks: dict[tuple, threading.Lock] = {}
        self._lock = threading.Lock()

    def _lock_for(self, key: tuple) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(key, threading.Lock())

    # ------------------------------------------------------------ files
    def files(self, src: VizSource) -> dict[int, tuple[str, int]]:
        """``{episode: (file, size)}`` by v1's numbering (``episode_<N>.mcap``)."""
        key = ("mcap-files", src.scope, src.id, src.fingerprint)
        hit = self.svc.meta_cache.get(key)
        if hit is not None:
            return hit
        from curation.cli.containers import mcap_episodes

        listing = src.listing()
        if listing is None:
            with Access(self.svc.rt, src).storage() as st:
                listing = st.list()
        numbering = mcap_episodes(listing)
        out = {int(i): (k, int(listing[k].size)) for i, k in numbering.items()}
        self.svc.meta_cache.put(key, out)
        return out

    def _open(self, st, src: VizSource, name: str, size: int):
        if st.remote:
            return RangeFile(lambda s, n: st.read_range(name, s, n), size, name=name)
        return open(pathlib.Path(st.root) / name, "rb")

    # ------------------------------------------------------------ probing
    def probe_source(self, src: VizSource, file: str | None = None) -> tuple[MP.FileProbe, int, list[dict]]:
        """(probe of one episode file, file count, warnings about the files disagreeing). Without
        ``file`` the first file that probes is used: a truncated first episode (no summary) does not
        take the whole dataset with it - its own episode fails when it is opened."""
        files = self.files(src)
        if not files:
            raise ApiError("validation_failed", "这里没有 episode_N.mcap：不是 mcap 数据集", details={"reason": "not_mcap"})
        ordered = [files[i] for i in sorted(files)]
        if file:
            chosen = next(((k, s) for k, s in ordered if k == file or k.rsplit("/", 1)[-1] == file), None)
            if chosen is None:
                raise ApiError("validation_failed", f"数据集里没有 {file}",
                               details={"errors": [{"field": "file", "problem": "not an episode file"}]})
            candidates = [chosen]
        else:
            candidates = ordered[:MAX_PROBE_CANDIDATES]
        key = ("mcap-probe", src.scope, src.id, src.fingerprint, candidates[0][0])
        hit = self.svc.meta_cache.get(key)
        if hit is not None:
            return hit
        warnings = []
        with Access(self.svc.rt, src).storage() as st:
            failed: list[tuple[str, str]] = []
            probe = None
            for name, size in candidates:
                fh = self._open(st, src, name, size)
                try:
                    probe = MP.probe(fh, name)
                finally:
                    getattr(fh, "close", lambda: None)()
                if not probe.error:
                    chosen = (name, size)
                    break
                failed.append((name, probe.error))
            if probe is None or probe.error:
                raise ApiError("validation_failed", f"{failed[0][0]}：{failed[0][1]}"
                               + (f"（后面 {len(failed) - 1} 个文件也探测不了）" if len(failed) > 1 else ""))
            for name, err in failed:
                warnings.append({"code": "file_unreadable", "message": f"{name} 探测不了（{err}），按 {chosen[0]} 探测"})
            bad = {name for name, _ in failed}
            others = [x for x in ordered if x[0] != chosen[0] and x[0] not in bad][:MAX_PROBE_FILES - 1]
            for name, size in others:
                fh = self._open(st, src, name, size)
                try:
                    from curation.cli.containers import read_summary

                    other = read_summary(fh, name)
                finally:
                    getattr(fh, "close", lambda: None)()
                if other.indexed and set(other.topics) != set(probe.topics):
                    missing = sorted(set(probe.topics) - set(other.topics))[:5]
                    extra = sorted(set(other.topics) - set(probe.topics))[:5]
                    warnings.append({"code": "topics_differ",
                                     "message": f"{name} 的 topic 与 {chosen[0]} 不一样"
                                                + (f"：少 {'、'.join(missing)}" if missing else "")
                                                + (f"；多 {'、'.join(extra)}" if extra else "")})
        out = (probe, len(files), warnings)
        self.svc.meta_cache.put(key, out)
        return out

    # ------------------------------------------------------------ the dataset model
    def dataset_model(self, src: VizSource) -> dict:
        mapping = src.mapping or {}
        probe, count, warnings = self.probe_source(src)
        keys = camera_keys(mapping)
        cameras = []
        for c in mapping.get("cameras") or []:
            tp = probe.topics.get(c["topic"])
            cameras.append(self._camera(keys[c["topic"]], c, tp, probe))
        streams = self._streams(mapping, probe)
        sources = []
        seg = mapping.get("segments")
        if isinstance(seg, dict):
            where = f"topic {seg['topic']}" if seg.get("topic") else f"附件 {seg.get('attachment')}"
            sources.append({"key": "segments", "kind": "segments", "name": "分段标注",
                            "format": "mcap_topic" if seg.get("topic") else "mcap_attachment", "source": where,
                            "supported": True, "reason": None, "primary": True})
        if src.annotations_upload:
            sources.append({"key": "external", "kind": "segments", "name": "外部标注", "format": "argus",
                            "source": "外部标注文件（上传）", "supported": True, "reason": None, "primary": not sources})
        for t, tp in probe.topics.items():
            if not tp.decodable:
                warnings.append({"code": "topic_undecodable", "message": f"topic {t}（{tp.message_encoding}）解不开"})
        files = self.files(src)
        idx = sorted(files)
        from .lerobot import _compact

        return {"cameras": cameras, "streams": streams, "annotation_sources": sources,
                "field_tree": self._field_tree(mapping, probe, keys), "fps": None, "episode_count": count,
                "total_frames": None, "robot_type": None, "episode_indices": _compact(idx), "warnings": warnings}

    def _camera(self, key: str, c: dict, tp: MP.TopicProbe | None, probe: MP.FileProbe) -> dict:
        codec = tp.codec if tp is not None else None
        if codec in ("jpeg", "png"):
            kind, access, reason = "frames", "frames", None
        elif codec in ("h264", "h265"):
            kind, access, reason = "video", "remux", None
        elif tp is None:
            kind, access, reason = "video", "unsupported", f"第一条 episode 里没有 topic {c['topic']}"
        else:
            kind, access, reason = "video", "unsupported", f"编码 {codec or '未知'} 本期不支持（RawImage / 未知编码）"
        from curation.viz.lerobot_info import codec_string

        return {"key": key, "name": c.get("name") or key, "source": c["topic"], "kind": kind, "access": access,
                "codec": {"h265": "hevc"}.get(codec, codec) if codec else None,
                "codec_string": codec_string({"h265": "hevc"}.get(codec, codec)) if codec in ("h264", "h265") else None,
                "width": tp.width if tp else None, "height": tp.height if tp else None,
                "fps": tp.rate_hz(probe.end_ns) if tp else None, "pix_fmt": None, "transcoded": False,
                "reason": reason}

    def _lines_of(self, entry: dict, tp: MP.TopicProbe | None, role: str) -> list[dict]:
        sizes = {f["path"]: f["size"] for f in (tp.fields if tp and tp.fields else [])}
        labels: list[str] = []
        fields = entry.get("fields") or []
        transforms = entry.get("transforms") or {}
        if not fields:
            total = sum(sizes.values()) or 1
            names = tp.names if tp and tp.names and len(tp.names) == total else None
            labels = names or [f"dim_{i}" for i in range(total)]
        else:
            for f in fields:
                n = sizes.get(f) or sum(v for k, v in sizes.items() if k.startswith(f + ".")) or 1
                labels += MM.transform_labels(f, n, transforms.get(f)) if transforms.get(f) else (
                    [f] if n == 1 else [f"{f}.{i}" for i in range(n)])
        given = entry.get("labels")
        if given and len(given) == len(labels):
            labels = list(given)
        elif tp and tp.names and entry.get("names_field") and len(tp.names) == len(labels):
            labels = list(tp.names)
        return [{"name": lb, "role": role, "source": entry["topic"], "dim": i, "unit": entry.get("unit")}
                for i, lb in enumerate(labels)]

    def _streams(self, mapping: dict, probe: MP.FileProbe) -> list[dict]:
        series = mapping.get("series") or []
        by = {s["topic"]: s for s in series}
        done: set[str] = set()
        out = []
        for s in series:
            if s["topic"] in done:
                continue
            group = [s]
            other = by.get(s.get("pair_with") or "")
            if other is not None and other["topic"] not in done:
                group = sorted([s, other], key=lambda e: 0 if e.get("role") == "state" else 1)
            done.update(e["topic"] for e in group)
            lines = []
            for e in group:
                role = e.get("role") if e.get("role") in ("state", "action") else "other"
                lines += self._lines_of(e, probe.topics.get(e["topic"]), role)
            tp = probe.topics.get(group[0]["topic"])
            out.append({"key": _slug(group[0]["topic"]), "kind": "series", "name": group[0].get("name") or group[0]["topic"],
                        "unit": group[0].get("unit"), "lines": lines, "smart": all(e.get("smart", True) for e in group),
                        "available": True, "reason": None, "sources": [e["topic"] for e in group],
                        "rate_hz": tp.rate_hz(probe.end_ns) if tp else None})
        return [x for x in out if x["smart"]] + [x for x in out if not x["smart"]]

    def _field_tree(self, mapping: dict, probe: MP.FileProbe, keys: dict[str, str]) -> list[dict]:
        from curation.viz.mcap_mapping import topic_uses

        uses = topic_uses(mapping, probe)
        names = {"camera": "相机", "series": "曲线", "task": "任务描述", "segments": "分段标注", "ignore": "忽略",
                 "unmapped": "未映射"}
        streams = {t: _slug(s["topic"]) for s in mapping.get("series") or [] for t in (s["topic"],)}
        topic_nodes = []
        for t in sorted(probe.topics):
            tp = probe.topics[t]
            use = uses.get(t, ("unmapped", None, ""))[0]
            node: dict[str, Any] = {"id": f"topic:{t}", "name": t, "kind": "topic",
                                    "detail": {"schema": tp.schema, "编码": tp.message_encoding, "消息数": tp.count,
                                               "频率": f"{tp.rate_hz(probe.end_ns)} Hz" if tp.rate_hz(probe.end_ns) else None,
                                               "用途": names.get(use, use),
                                               "画面": f"{tp.codec} {tp.width}×{tp.height}" if tp.kind == "camera" else None,
                                               "字段": "、".join(f"{f['path']}[{f['size']}]" for f in tp.fields or [])[:300] or None}}
            if t in keys:
                node["camera"] = keys[t]
            if t in streams:
                node["stream"] = streams[t]
            topic_nodes.append(node)
        meta_nodes = [{"id": f"metadata:{name}", "name": name, "kind": "metadata", "detail": dict(list(rec.items())[:40])}
                      for name, rec in probe.metadata.items()]
        att_nodes = [{"id": f"attachment:{a['name']}", "name": a["name"], "kind": "attachment",
                      "detail": {"类型": a["media_type"], "字节": a["size"]}} for a in probe.attachments]
        return [{"id": "topics", "name": "Topic", "kind": "group", "children": topic_nodes},
                {"id": "metadata", "name": "Metadata", "kind": "group", "children": meta_nodes},
                {"id": "attachments", "name": "Attachments", "kind": "group", "children": att_nodes}]

    def meta_file(self, src: VizSource, path: str) -> dict:
        # the field tree of an mcap dataset names topics, metadata records and attachments, never files
        raise ApiError("validation_failed", "mcap 数据集没有可预览的元数据文件", details={"reason": "no_meta_files"})

    # ------------------------------------------------------------ the episode list
    def episode_items(self, src: VizSource) -> list[dict]:
        files = self.files(src)
        out = []
        for i in sorted(files):
            doc = self._cached_doc(src, i)
            out.append({"index": i, "duration_s": doc["duration_s"] if doc else None,
                        "frames": len(doc["frame_times"]) if doc else None, "task": (doc or {}).get("task") or "",
                        "steps": [{"start_s": s["start_s"], "end_s": s["end_s"]} for s in doc["segments"]]
                        if doc and doc.get("segments") else None})
        return out

    # ------------------------------------------------------------ one episode
    def _dir(self, src: VizSource, index: int) -> pathlib.Path:
        fp = digest(src.scope, src.id, src.fingerprint, src.mapping_version or 0,
                    json.dumps(src.mapping or {}, sort_keys=True))
        return self.svc.disk.root / "mcap" / fp / f"ep{int(index):06d}"

    def _cached_doc(self, src: VizSource, index: int) -> dict | None:
        path = self._dir(src, index) / "episode.json"
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        return None

    def episode_doc(self, src: VizSource, index: int) -> tuple[dict, pathlib.Path]:
        files = self.files(src)
        if int(index) not in files:
            raise ApiError("not_found", f"数据集里没有 episode {index}", details={"reason": "no_episode"})
        d = self._dir(src, index)
        doc = self._cached_doc(src, index)
        if doc is not None:
            return doc, d
        with self._lock_for(("mcap-scan", str(d))):
            doc = self._cached_doc(src, index)
            if doc is not None:
                return doc, d
            name, size = files[int(index)]
            with Access(self.svc.rt, src).storage() as st:
                fh = self._open(st, src, name, size)
                try:
                    doc = scan(fh, src.mapping or {}, d)
                except ValueError as err:
                    raise ApiError("validation_failed", f"{name}：{err}") from None
                finally:
                    getattr(fh, "close", lambda: None)()
            for f in d.iterdir():
                self.svc.disk.added(f)
            return doc, d

    def episode_model(self, src: VizSource, index: int, urls) -> dict:
        doc, d = self.episode_doc(src, index)
        model = self.dataset_model(src)
        cameras = []
        for c in model["cameras"]:
            cd = doc["cameras"].get(c["key"]) or {}
            cam = dict(c)
            if cd.get("error"):
                cam["access"], cam["reason"] = "unsupported", cd["error"]
            elif not cd.get("count"):
                cam["access"], cam["reason"] = "unsupported", "这条 episode 里没有这一路相机的画面"
            entry = urls.camera(src, index, cam, "mcap", None, None)
            entry["offset_s"] = float(cd.get("offset_s") or 0.0)
            cameras.append(entry)
        ann = {"tracks": [], "events": [], "labels": [], "warnings": []}
        if doc.get("segments"):
            ann["tracks"].append({"key": "segments", "name": "分段标注", "source": doc.get("segments_source") or "",
                                  "primary": True, "segments": doc["segments"]})
        if src.annotations_upload:
            ext = self.svc.external_annotations(src, index)
            if ext is not None:
                if ann["tracks"]:
                    for t in ext.tracks:
                        t["primary"] = False
                elif ext.tracks:
                    ext.tracks[0]["primary"] = True
                ann["tracks"] += ext.tracks
                ann["events"] += ext.events
                ann["labels"] += ext.labels
        task = {"text": doc["task"], "source": "原始标注"} if doc.get("task") else None
        return {"duration_s": round(float(doc["duration_s"]), 3), "frames": len(doc["frame_times"]), "fps": None,
                "task": task, "timeline": {"kind": "timestamp", "fps": None, "frame_reference": doc.get("frame_reference"),
                                           "frame_times": doc["frame_times"]},
                "cameras": cameras, "annotations": ann, "warnings": doc.get("warnings") or [],
                "check_clock": doc["check_clock"] if src.scope == "task" else None}

    # ------------------------------------------------------------ curves
    def _arrays(self, src: VizSource, index: int) -> tuple[dict, dict]:
        doc, d = self.episode_doc(src, index)
        key = ("mcap-series", str(d))
        hit = self.svc.frames_cache.get(key)
        if hit is None:
            with np.load(d / "series.npz") as z:
                hit = {k: z[k] for k in z.files}
            self.svc.frames_cache.put(key, hit, size=sum(v.nbytes for v in hit.values()))
        return doc, hit

    def series(self, src: VizSource, index: int, stream: str, start, end, points: int) -> dict:
        model = self.dataset_model(src)
        st = next((s for s in model["streams"] if s["key"] == stream), None)
        if st is None:
            raise ApiError("not_found", f"没有曲线组 {stream}", details={"reason": "unknown_stream"})
        doc, arrays = self._arrays(src, index)
        topics = st["sources"]
        ref = doc["series"].get(topics[0])
        if ref is None:
            raise ApiError("not_found", f"这条 episode 里没有 {topics[0]} 的消息", details={"reason": "no_messages"})
        t = arrays[f"{ref['key']}__t"]
        sl = window(t, start, end)
        lines = []
        for line in st["lines"]:
            info = doc["series"].get(line["source"])
            if info is None:
                lines.append(np.full(len(t[sl]), np.nan))
                continue
            vt, vv = arrays[f"{info['key']}__t"], arrays[f"{info['key']}__v"]
            col = vv[:, line["dim"]] if line["dim"] < vv.shape[1] else np.full(len(vt), np.nan)
            if line["source"] == topics[0]:
                lines.append(col[sl])
            else:                                   # the partner topic, sampled at the reference's times
                idx = np.searchsorted(vt, t[sl], side="right") - 1
                lines.append(np.where(idx >= 0, col[np.clip(idx, 0, len(col) - 1)], np.nan))
        tt, ys, thinned = thin(t[sl], lines, points)
        return {"stream": stream, "unit": st.get("unit"),
                "from_s": round(float(t[sl][0]), 4) if len(t[sl]) else float(start or 0.0),
                "to_s": round(float(t[sl][-1]), 4) if len(t[sl]) else float(end or 0.0),
                "t": json_values(tt, 4),
                "lines": [{"name": ln["name"], "role": ln["role"], "values": json_values(y)} for ln, y in zip(st["lines"], ys)],
                "total_points": int(len(t[sl])), "downsampled": bool(thinned)}

    # ------------------------------------------------------------ media
    def camera(self, src: VizSource, index: int, key: str) -> tuple[dict, pathlib.Path]:
        doc, d = self.episode_doc(src, index)
        cd = doc["cameras"].get(key)
        if cd is None:
            raise ApiError("not_found", f"没有相机 {key}", details={"reason": "unknown_camera"})
        return cd, d

    def frame_index(self, src: VizSource, index: int, key: str) -> dict:
        cd, _ = self.camera(src, index, key)
        if cd.get("codec") not in ("jpeg", "png") or "t" not in cd:
            raise ApiError("not_found", f"相机 {key} 不是 JPEG / PNG 帧包", details={"reason": "not_frames"})
        return {"camera": key, "codec": cd["codec"], "width": cd.get("width"), "height": cd.get("height"),
                "count": len(cd["t"]), "t": cd["t"], "offset": cd["offset"], "size": cd["size"],
                "bytes": int(cd.get("bytes") or 0)}

    def frames_file(self, src: VizSource, index: int, key: str) -> pathlib.Path:
        cd, d = self.camera(src, index, key)
        path = d / f"{key}.frames"
        if cd.get("codec") not in ("jpeg", "png") or not path.is_file():
            raise ApiError("not_found", f"相机 {key} 不是 JPEG / PNG 帧包", details={"reason": "not_frames"})
        return path

    def video_file(self, src: VizSource, index: int, key: str) -> pathlib.Path | None:
        """The remuxed mp4 of an H.264 / H.265 camera (None for a frame-pack camera)."""
        cd, d = self.camera(src, index, key)
        path = d / f"{key}.mp4"
        return path if cd.get("mp4") and path.is_file() else None

    def mjpeg_file(self, src: VizSource, index: int, key: str) -> pathlib.Path:
        """A frame-pack camera as an MJPEG mp4 (the check reader's muxing): what the transcoder reads,
        and what the retiring 各机位视频 asked for."""
        cd, d = self.camera(src, index, key)
        out = d / f"{key}.mjpeg.mp4"
        if out.is_file():
            return out
        from curation.ingest import mcap_reader as MR

        pack = self.frames_file(src, index, key).read_bytes()
        blobs = [pack[o:o + s] for o, s in zip(cd["offset"], cd["size"])]
        times = [t - cd["t"][0] for t in cd["t"]]
        rate = (len(times) - 1) / times[-1] if len(times) > 1 and times[-1] > 0 else 30.0
        tmp = out.with_name(out.name + ".part")
        with open(tmp, "wb") as fh:
            MR.mux_jpeg_frames(blobs, times, fh, rate)
        tmp.replace(out)
        self.svc.disk.added(out)
        return out
