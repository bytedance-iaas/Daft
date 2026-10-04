"""The Lance reader of the visualizer (design doc 19 §4, D61): datasets lerobot-lancedb converted.

A Lance dataset is LeRobot v3 metadata (``meta/``, or the ``meta.lance`` table of a root that holds only
the tables) over Lance tables instead of parquet and mp4 files; the three layouts are in
:mod:`curation.viz.lance_layout`. So this is the LeRobot reader with two parts swapped:

* an episode's frame columns come from the frames table - its row window, only the columns asked for,
  into the same bounded cache the episode answer and its curve requests share;
* a camera's bytes come from the videos table's blob column. They have no object of their own to
  presign, so the Daemon serves them by Range (``access: blob``), windows of a shared mp4 as ever with
  ``from_ts`` / ``to_ts``; a codec the browser cannot play is copied out of the blob and transcoded. The
  0.1-0.2 frames layout keeps a JPEG per frame instead: an episode's pictures become a frame pack.

Tables open where they are - a local directory, or TOS through its S3-compatible endpoint
(:meth:`~daemon.viz.source.Access.lance_target`) - and are read by range, never copied whole.
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from curation.viz import lance_layout as LL
from curation.viz import lerobot_info as L
from curation.viz.series import episode_times

from ..errors import ApiError
from .lerobot import EpisodeRow, LeRobotMeta, LeRobotReader, _camera_key, _meta_listing
from .media import digest
from .source import Access, VizSource


@dataclass
class LanceMeta(LeRobotMeta):
    layout: LL.Layout | None = None
    pictures: dict[str, str] = field(default_factory=dict)       # camera key -> frames-table column (JPEG)
    tables: list[dict] = field(default_factory=list)             # the field tree's 「Lance 表」
    meta_table: bool = False                                     # meta/ came from meta.lance


class MetaFiles:
    """The ``meta/`` files of a ``meta.lance`` table, read like a storage."""

    remote = False

    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def read_bytes(self, key: str) -> bytes:
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def read_range(self, key: str, start: int, length: int) -> bytes:
        return self.read_bytes(key)[start:start + length]

    def list(self, prefix: str = "") -> dict[str, Any]:
        return {k: len(v) for k, v in self.files.items() if k.startswith(prefix)}


class LanceReader(LeRobotReader):
    def __init__(self, service):
        super().__init__(service)
        self._tables: dict[tuple, tuple[float, Any]] = {}
        self._tables_lock = threading.Lock()
        self._packs: dict[str, threading.Lock] = {}

    # ------------------------------------------------------------ tables
    def layout(self, src: VizSource) -> LL.Layout:
        keys = src.listing()
        if keys is None or not LL.tables_of(keys):
            # a 0.1-0.2 root registered as LeRobot keeps only meta/ in its listing: look at the root
            if src.is_local:
                keys = [p.name + "/" for p in Access(self.svc.rt, src).local_root().iterdir() if p.is_dir()]
            else:
                with Access(self.svc.rt, src).storage() as st:
                    keys = st.list("")
        lay = LL.detect(LL.tables_of(keys))
        if lay is None:
            raise ApiError("validation_failed", "认不出这份 Lance 数据的布局：不是 lerobot-lancedb 转出来的三种布局之一（设计 19 §4.2）",
                           details={"reason": "unsupported"})
        return lay

    def table(self, src: VizSource, name: str):
        """The Lance table ``name`` of the source, opened once per source state (dataset scope: for as
        long as its metadata is kept, a newer version is picked up after that)."""
        import lance

        key = (*src.cache_key, name)
        now = time.monotonic()
        with self._tables_lock:
            hit = self._tables.get(key)
            if hit is not None and (src.scope == "task" or now - hit[0] < self.svc.META_TTL_S):
                return hit[1]
        uri, opts = Access(self.svc.rt, src).lance_target(name)
        try:
            ds = lance.dataset(uri, storage_options=opts) if opts else lance.dataset(uri)
        except Exception as exc:  # noqa: BLE001 - missing, unreadable, no access: the page says so
            raise ApiError("not_found", f"打不开 Lance 表 {name}：{str(exc)[:200]}") from None
        with self._tables_lock:
            if len(self._tables) >= 64:
                self._tables.pop(next(iter(self._tables)))
            self._tables[key] = (now, ds)
        return ds

    # ------------------------------------------------------------ metadata
    def _build_meta(self, src: VizSource) -> LeRobotMeta:
        lay = self.layout(src)
        listing = src.listing()
        access = Access(self.svc.rt, src)
        with access.storage() as st:
            has_meta = ("meta/info.json" in listing) if listing is not None else st.stat("meta/info.json") is not None
            if has_meta:
                m = self._meta_from(src, st, lambda: _meta_listing(src, st))
            elif lay.meta:
                files = LL.meta_files(self.table(src, lay.meta))
                m = self._meta_from(src, MetaFiles(files), lambda: sorted(files))
            else:
                raise ApiError("not_found", "没有 meta/ 目录，也没有 meta.lance 表：读不到 LeRobot 的元数据")
        return self._lance_meta(src, lay, m, meta_table=not has_meta)

    def _lance_meta(self, src: VizSource, lay: LL.Layout, m: LeRobotMeta, *, meta_table: bool) -> LanceMeta:
        frames = self.table(src, lay.frames)
        cameras, camera_features, pictures = [], dict(m.camera_features), {}
        if lay.videos:
            for c in m.cameras:
                c = dict(c)
                if c["access"] in ("direct", "local"):
                    c["access"] = "blob"
                cameras.append(c)
        else:
            feats = m.info.get("features") or {}
            keys = [k for k, f in feats.items() if isinstance(f, dict) and f.get("dtype") in ("video", "image")]
            cols = LL.picture_columns(frames, keys)
            camera_features, used = {}, set()
            fps = m.fps
            for feature, col in cols.items():
                key = _camera_key(L.short_camera(feature), used)
                camera_features[key] = feature
                pictures[key] = col
                shape = L.shape_of(feats.get(feature) or {}) or []
                dims = [x for x in shape if x > 4] if shape and min(shape) <= 4 else shape[:2]
                cameras.append({"key": key, "name": L.short_camera(feature), "source": feature, "kind": "frames",
                                "access": "frames", "codec": "jpeg", "codec_string": None,
                                "width": int(dims[1]) if len(dims) >= 2 else None,
                                "height": int(dims[0]) if len(dims) >= 2 else None,
                                "fps": fps, "pix_fmt": None, "transcoded": False, "reason": None})
        tables = []
        for name in (lay.frames, lay.videos, lay.meta):
            if not name:
                continue
            try:
                ds = self.table(src, name)
                cols = [f.name for f in ds.schema]
                tables.append({"name": name, "rows": int(ds.count_rows()), "columns": cols})
            except ApiError:
                continue
        fields = dict(vars(m))
        fields.update(cameras=cameras, camera_features=camera_features, made_at=time.monotonic())
        return LanceMeta(**fields, layout=lay, pictures=pictures, tables=tables, meta_table=meta_table)

    def meta(self, src: VizSource) -> LanceMeta:
        key = ("lance-meta", *src.cache_key)
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

    def dataset_model(self, src: VizSource) -> dict:
        m = self.meta(src)
        out = super().dataset_model(src)
        out["format"] = {"kind": "lance", "version": m.version, "reader": "lance", "layout": m.layout.name}
        return out

    def _field_tree(self, src: VizSource, m: LeRobotMeta) -> list[dict]:
        tree = super()._field_tree(src, m)
        tables = getattr(m, "tables", [])
        if tables:
            tree.append({"id": "lance", "name": "Lance 表", "kind": "group", "children": [
                {"id": f"table:{t['name']}", "name": t["name"], "kind": "table",
                 "detail": {"行数": t["rows"], "列": "、".join(t["columns"][:24]) + ("…" if len(t["columns"]) > 24 else "")}}
                for t in tables]})
        return tree

    def meta_file(self, src: VizSource, path: str) -> dict:
        m = self.meta(src)
        if not m.meta_table:
            return super().meta_file(src, path)
        allowed = set(m.meta_files)
        if path not in allowed or not path.endswith((".json", ".jsonl", ".md", ".txt")):
            raise ApiError("validation_failed", f"只能看数据集信息树里的元数据文件，不能看 {path}",
                           details={"errors": [{"field": "path", "problem": "not a metadata file"}]})
        data = LL.meta_files(self.table(src, m.layout.meta)).get(path, b"")
        kind = "json" if path.endswith(".json") else "jsonl" if path.endswith(".jsonl") else (
            "markdown" if path.endswith(".md") else "text")
        cut = 256 * 1024
        return {"path": path, "size": len(data), "truncated": len(data) > cut, "kind": kind,
                "text": data[:cut].decode("utf-8", "replace")}

    # ------------------------------------------------------------ one episode
    def frames(self, src: VizSource, index: int) -> dict[str, Any]:
        m, row = self.row(src, index)
        key = ("lance-frames", *src.cache_key, int(index))
        hit = self.svc.frames_cache.get(key)
        if hit is not None:
            return hit
        cols = list(dict.fromkeys(["timestamp", "frame_index"] + m.curve_columns + m.annotation_columns))
        ds = self.table(src, m.layout.frames)
        try:
            data = LL.episode_columns(ds, cols, from_index=row.from_index, to_index=row.to_index, episode_index=row.index)
        except Exception as exc:  # noqa: BLE001 - a table that does not read: the page says so
            raise ApiError("not_found", f"读不出 episode {index} 的帧表：{str(exc)[:200]}") from None
        n = max((len(v) for v in data.values()), default=0)
        data["__t__"] = episode_times(data, m.fps, n)
        size = sum(v.nbytes if isinstance(v, np.ndarray) else 64 * len(v) for v in data.values())
        self.svc.frames_cache.put(key, data, size=size)
        return data

    def _camera_place(self, m: LeRobotMeta, row: EpisodeRow, cam: dict):
        if cam["access"] == "frames":
            return f"lance:{m.layout.frames}", None, None
        return super()._camera_place(m, row, cam)

    # ------------------------------------------------------------ camera bytes
    def blob(self, src: VizSource, index: int, camera: str):
        """(VizCamera, the blob of the mp4 that holds the camera's episode, from_ts, to_ts)."""
        m, row = self.row(src, index)
        cam = next((c for c in m.cameras if c["key"] == camera), None)
        if cam is None:
            raise ApiError("not_found", f"没有相机 {camera}", details={"reason": "unknown_camera"})
        if not m.layout.videos:
            raise ApiError("not_found", f"相机 {camera} 是逐帧图片，没有视频", details={"reason": "not_video"})
        feature = m.camera_features[camera]
        where = row.video_files.get(feature)
        if where is None:
            raise ApiError("not_found", f"episode {index} 没有相机 {camera} 的视频")
        rows = self._video_rows(src, m)
        rid = rows.get((feature, where[0], where[1]))
        if rid is None:
            raise ApiError("not_found", f"videos 表里没有 {feature} 的 chunk {where[0]} / file {where[1]}")
        blob = self.table(src, m.layout.videos).take_blobs(LL.BLOB_COLUMN, ids=[rid])[0]
        frm, to = row.windows.get(feature, (None, None))
        return cam, blob, frm, to

    def _video_rows(self, src: VizSource, m: LanceMeta) -> dict:
        key = ("lance-videos", *src.cache_key)
        hit = self.svc.meta_cache.get(key)
        if hit is None or (src.scope != "task" and time.monotonic() - hit[0] >= self.svc.META_TTL_S):
            hit = (time.monotonic(), LL.video_rows(self.table(src, m.layout.videos)))
            self.svc.meta_cache.put(key, hit)
        return hit[1]

    def frame_pack(self, src: VizSource, index: int, camera: str) -> tuple[pathlib.Path, dict]:
        """The JPEG frame pack of a 0.1-0.2 frames-layout camera and its C4 ``VizFrameIndex``, made once."""
        from curation.viz import mcap_messages as M

        m, row = self.row(src, index)
        col = m.pictures.get(camera)
        if col is None:
            raise ApiError("not_found", "只有逐帧图片的相机有帧包", details={"reason": "not_frames"})
        pack = self.svc.disk.path("lance", digest(src.scope, src.id, src.fingerprint), f"ep{int(index):06d}", f"{camera}.frames")
        doc_path = pack.with_suffix(".json")
        lock_key = str(pack)
        with self._lock:
            lock = self._packs.setdefault(lock_key, threading.Lock())
        with lock:
            if self.svc.disk.get(pack) is not None and doc_path.is_file():
                return pack, json.loads(doc_path.read_text(encoding="utf-8"))
            feature = m.camera_features[camera]
            pictures = LL.episode_columns(self.table(src, m.layout.frames), [feature], from_index=row.from_index,
                                          to_index=row.to_index, episode_index=row.index).get(feature) or []
            t = self.frames(src, index)["__t__"]
            offsets, sizes, pos = [], [], 0
            tmp = pack.with_name(pack.name + ".part")
            with open(tmp, "wb") as fh:
                for data in pictures:
                    data = bytes(data or b"")
                    offsets.append(pos)
                    sizes.append(len(data))
                    fh.write(data)
                    pos += len(data)
            width = height = None
            first = next((bytes(p) for p in pictures if p), b"")
            if first:
                width, height = M.picture_size("jpeg" if first[:2] == b"\xff\xd8" else "png", first)
            n = min(len(t), len(sizes))
            doc = {"camera": camera, "codec": "png" if first[:8] == b"\x89PNG\r\n\x1a\n" else "jpeg",
                   "width": width, "height": height, "count": n, "t": [round(float(x), 6) for x in t[:n]],
                   "offset": offsets[:n], "size": sizes[:n], "bytes": pos}
            os.replace(tmp, pack)
            doc_path.write_text(json.dumps(doc), encoding="utf-8")
            self.svc.disk.added(pack)
            return pack, doc
