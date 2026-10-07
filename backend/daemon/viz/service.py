"""The visualizer service: sources, readers, caches and the transcoder of one Daemon (design doc 18).

Routes ask it for a source (a registration, or a task's frozen input) and then for the parts of the
presentation model (C4 2.4.0 ``VizDataset``, ``VizEpisode``, ``VizSeries``); it picks the reader by
the source's format and keeps what readers build in bounded in-memory caches keyed by the source's
fingerprint. Camera bytes it serves (local files, transcodes) go through :mod:`.media`.
"""
from __future__ import annotations

import contextlib
import pathlib
import threading
from typing import Any

from ..errors import ApiError
from ..results.files import LRU
from .media import Builder, DiskCache, Transcoder, digest, file_response, pending_body, ranged_response
from .source import URL_TTL_S, Access, VizSource, dataset_source, task_source

_CREATE = threading.Lock()


class EpisodeUrls:
    """The ``cameras[]`` of an episode answer: what the browser loads each camera from."""

    def __init__(self, svc: "VizService"):
        self.svc = svc

    def daemon(self, src: VizSource, index: int, key: str, suffix: str, transcode: bool = False) -> str:
        scope = "datasets" if src.scope == "dataset" else "tasks"
        base = self.svc.rt.links.base_path
        url = f"{base}/api/v1/{scope}/{src.id}/episodes/{int(index)}/cameras/{key}.{suffix}"
        return url + ("?transcode=1" if transcode else "")

    def stream(self, src: VizSource, index: int, key: str, kind: str, reason: str | None = None,
               offset_s: float = 0.0) -> dict:
        """C4 ``VizEpisodeStream``: where an episode's depth pack is read from (design doc 21 §5)."""
        scope = "datasets" if src.scope == "dataset" else "tasks"
        base = f"{self.svc.rt.links.base_path}/api/v1/{scope}/{src.id}/episodes/{int(index)}/streams/{key}"
        ok = reason is None
        return {"key": key, "kind": kind, "url": f"{base}.frames" if ok else None,
                "index_url": f"{base}.json" if ok else None, "offset_s": round(float(offset_s), 6), "reason": reason}

    def camera(self, src: VizSource, index: int, cam: dict, rel: str | None, frm, to) -> dict:
        out = {"key": cam["key"], "kind": cam["kind"], "access": cam["access"], "url": None, "index_url": None,
               "samples_url": None, "transcode_url": None, "from_ts": frm, "to_ts": to, "offset_s": 0.0,
               "transcoded": cam["access"] == "transcode", "expires_at": None, "reason": cam.get("reason")}
        if rel is None:
            out["access"], out["reason"] = "unsupported", "这条 episode 没有这一路相机的视频文件"
            return out
        access = cam["access"]
        if access == "direct":
            out["url"], out["expires_at"] = self.svc.presigned_camera_url(src, rel)
            if out["url"] is None:                                 # a local source after all
                out["access"], out["url"] = "local", self.daemon(src, index, cam["key"], "mp4")
        elif access in ("local", "transcode", "remux", "blob"):
            out["url"] = self.daemon(src, index, cam["key"], "mp4")
        elif access == "frames":
            out["url"] = self.daemon(src, index, cam["key"], "frames")
            out["index_url"] = self.daemon(src, index, cam["key"], "json")
        if self.svc.transcode_enabled and access in ("direct", "local", "remux", "blob"):
            out["transcode_url"] = self.daemon(src, index, cam["key"], "mp4", transcode=True)
        if access == "transcode" and frm is not None:
            # from_ts / to_ts are times in what `url` serves (design doc 21 §4.5): a transcode's 0 is
            # the episode's start (curation.viz.transcode --from), not the shared file's from_ts
            out["from_ts"], out["to_ts"] = 0.0, (round(float(to) - float(frm), 6) if to is not None else None)
        if access in ("direct", "local", "blob") and self.svc.segments.enabled:
            # CURATOR_VIZ_SEGMENT (design doc 21 §4.3-§4.4): the Daemon serves the episode's slice of a
            # shared file, or a copy of a moov-at-end file with it in front
            place = self.svc.segment_place(src, index, cam["key"], frm)
            if place is not None:
                out["access"], out["expires_at"] = "remux", None
                out["url"] = self.daemon(src, index, cam["key"], "mp4") + "?segment=1"
                if not place["whole"] and frm is not None:
                    out["from_ts"] = round(float(frm) - place["start_s"], 6)
                    out["to_ts"] = round(float(to) - place["start_s"], 6) if to is not None else None
        if access == "transcode":                       # the copy starts now, before the player asks
            with contextlib.suppress(Exception):
                if self.svc.reader_of(src) == "lance":
                    self.svc.lance_transcode(src, index, cam["key"])
                else:
                    self.svc.lerobot_transcode(src, index, cam["key"])
        return out


class VizService:
    META_TTL_S = 600.0
    #: presigned camera URL lifetime, and the bucket a signature is reused within so the frontend's
    #: pre-expiry refresh (design doc 18 §5.8) signs a byte-identical URL and the browser keeps cache
    PRESIGN_TTL_S = URL_TTL_S
    PRESIGN_BUCKET_S = 600

    def __init__(self, rt):
        self.rt = rt
        s = rt.settings
        self.transcode_enabled = bool(getattr(s, "viz_transcode", True))
        self.client_decode = bool(getattr(s, "viz_client_decode", True))
        self.meta_cache = LRU(max_items=32)
        self.presign_cache = LRU(max_items=512)
        self.frames_cache = LRU(max_items=64, max_bytes=256 << 20)
        self.label_cache = LRU(max_items=8)
        self.disk = DiskCache(getattr(s, "viz_cache_dir", None) or pathlib.Path(s.scratch_dir) / "viz-cache",
                              int(float(getattr(s, "viz_cache_gb", 20.0)) * (1 << 30)))
        self.transcoder = Transcoder(self.disk, int(getattr(s, "viz_transcode_workers", 2)))
        self.builder = Builder(self.disk, int(getattr(s, "viz_transcode_workers", 2)))
        self.urls = EpisodeUrls(self)
        from .segments import Segments

        self.segments = Segments(self)
        self._copy_locks: dict[str, threading.Lock] = {}
        self._copy_lock = threading.Lock()
        from .lance import LanceReader
        from .lerobot import LeRobotReader
        from .mcap import McapReader

        self.lerobot = LeRobotReader(self)
        self.mcap = McapReader(self)
        self.lance = LanceReader(self)
        self._lance_roots: dict[tuple, bool] = {}

    def shutdown(self) -> None:
        """Daemon shutdown: running transcodes are killed (their copies are made again when asked)."""
        self.transcoder.shutdown()
        self.builder.shutdown()

    def copy_lock(self, path: pathlib.Path) -> threading.Lock:
        """One writer per source copy: two transcodes of episodes in one shared file would otherwise
        download it into the same ``.part`` at once."""
        with self._copy_lock:
            return self._copy_locks.setdefault(str(path), threading.Lock())

    def forget(self, ds) -> int:
        """A deleted registration's products (its current fingerprint; older ones age out); bytes freed."""
        src = dataset_source(self.rt, ds, ds.owner_id)
        fp = digest(src.scope, src.id, src.fingerprint)
        dirs = [self.disk.root / kind / fp for kind in ("transcode", "source", "lance", "depth", "segment")]
        dirs += self.mcap.dirs_of(src)
        return self.disk.drop(*dirs)

    # ------------------------------------------------------------ sources
    def dataset_source(self, dataset_id: str, owner: str) -> VizSource:
        return dataset_source(self.rt, self.rt.repo.get_dataset(dataset_id, owner=owner), owner)

    def task_source(self, task_id: str, owner: str) -> VizSource:
        task = self.rt.repo.get_task(task_id, owner=owner)
        run_dir = None
        if task.run_id:
            from ..results.store import store_of

            cand = store_of(self.rt).task_dir(task.id)
            run_dir = cand if cand.is_dir() else None
        return task_source(self.rt, task, owner, run_dir)

    def presigned_camera_url(self, src: VizSource, rel: str):
        """A browser URL for a TOS camera object, or ``(None, None)`` for a local source. The object
        is immutable, so the URL is signed with an immutable ``Cache-Control`` (TOS echoes it) and is
        reused within a :data:`PRESIGN_BUCKET_S` time bucket: the frontend refreshes a camera URL just
        before it expires (design doc 18 §5.8), and within a bucket that refresh signs a byte-identical
        URL, so the browser keeps the bytes it cached instead of re-fetching under a new signature.
        ``expires_at`` is reported from the bucket start, so it never outlasts the signature it names."""
        now_ms = self.rt.clock()
        bucket = int(now_ms // 1000 // self.PRESIGN_BUCKET_S) * self.PRESIGN_BUCKET_S
        k = (src.scope, src.id, src.fingerprint, rel, bucket)
        hit = self.presign_cache.get(k)
        if hit is not None:
            return hit
        cc = f"private, max-age={self.PRESIGN_TTL_S}, immutable"
        url = Access(self.rt, src).browser_url(rel, self.PRESIGN_TTL_S, cache_control=cc)
        if url is None:
            return None, None
        out = (url, (bucket + self.PRESIGN_TTL_S) * 1000)
        self.presign_cache.put(k, out)
        return out

    def reader_of(self, src: VizSource) -> str | None:
        # mcap and LeRobot whether or not the check reader takes them (status.viz_format); Lance
        # tables are lerobot-lancedb's whether the preflight saw them as Lance (0.3) or as LeRobot
        # missing its data files (0.1-0.2, whose root holds meta/ beside <name>.lance)
        kind, version = src.format()
        if kind == "lance":
            return "lance"
        if kind == "lerobot" and version in ("v2", "v3"):
            return "lance" if self._has_lance_tables(src) else "lerobot"
        if kind == "mcap":
            return "mcap"
        return None

    def _has_lance_tables(self, src: VizSource) -> bool:
        key = (src.scope, src.id, src.fingerprint)
        hit = self._lance_roots.get(key)
        if hit is None:
            from curation.viz.lance_layout import tables_of

            listing = src.listing()
            if listing is None or not any(k.startswith("data/") for k in listing):
                # the kept listing of a LeRobot registration holds only meta/, data/ and videos/: a
                # root without data/ is looked at itself (0.1-0.2 Lance keeps <name>.lance there)
                try:
                    if src.is_local:
                        listing = {p.name + "/": None for p in Access(self.rt, src).local_root().iterdir() if p.is_dir()}
                    else:
                        with Access(self.rt, src).storage() as st:
                            listing = st.list("")
                except Exception:  # noqa: BLE001 - unreadable now: taken for LeRobot, which says why
                    listing = None
            hit = bool(listing) and bool(tables_of(listing))
            if len(self._lance_roots) > 512:
                self._lance_roots.clear()
            self._lance_roots[key] = hit
        return hit

    def _reader(self, src: VizSource):
        reader = self.reader_of(src)
        if reader == "lerobot":
            return self.lerobot
        if reader == "lance":
            return self.lance
        if reader == "mcap":
            mcap = self.mcap
            if not src.mapping:
                raise ApiError("validation_failed", "mcap 数据集还没有确认字段映射：到「mcap 配置」确认后才能看",
                               details={"reason": "mapping_pending"})
            return mcap
        kind, _ = src.format()
        raise ApiError("validation_failed", f"这个数据集的格式（{kind}）没有可视化读取器",
                       details={"reason": "unsupported"})

    # ------------------------------------------------------------ the dataset model
    def dataset(self, src: VizSource) -> dict:
        kind, version = src.format()
        reader = self.reader_of(src)
        listing = src.listing()
        out: dict[str, Any] = {
            "scope": src.scope, "id": src.id, "dataset_id": src.dataset_id, "name": src.name,
            "format": {"kind": kind if kind in ("lerobot", "mcap", "lance", "lancedb", "rrd") else "unknown",
                       "version": version if version in ("v2", "v3") else None, "reader": reader, "layout": None},
            "fps": None, "episode_count": 0, "episode_indices": None, "total_frames": None,
            "robot_type": None, "bytes": sum(o.size for o in listing.values()) if listing else None,
            "cameras": [], "streams": [], "annotation_sources": [], "field_tree": [],
            "mapping": self._mapping_state(src, kind), "transcode": {"enabled": self.transcode_enabled},
            "warnings": [], "fingerprint": src.fingerprint}
        if reader is None:
            out["warnings"].append({"code": "unsupported", "message": "这份 Lance 数据没有 LeRobot 的元数据（不是 lerobot-lancedb 转出来的），可视化读不了"
                                    if kind == "lancedb" else f"这个数据集的格式（{kind}）没有可视化读取器"})
            return out
        if reader == "mcap" and not src.mapping:
            out["warnings"].append({"code": "mapping_pending",
                                    "message": "mcap 数据集还没有确认字段映射：到「mcap 配置」确认后才能看相机与曲线"})
            ds = src.preflight.get("dataset") if isinstance(src.preflight.get("dataset"), dict) else {}
            out["episode_count"] = int(ds.get("episode_count") or 0)
            return out
        out.update(self._reader(src).dataset_model(src))
        return out

    @staticmethod
    def _mapping_state(src: VizSource, kind: str) -> dict:
        if kind != "mcap":
            return {"state": "not_needed", "version": None, "name": None}
        if not src.mapping:
            return {"state": "none", "version": None, "name": None}
        name = src.mapping.get("name") if isinstance(src.mapping.get("name"), str) else None
        return {"state": "frozen" if src.scope == "task" else "confirmed", "version": src.mapping_version, "name": name}

    def episode_items(self, src: VizSource) -> list[dict]:
        return self._reader(src).episode_items(src)

    def episode(self, src: VizSource, index: int) -> dict:
        body = self._reader(src).episode_model(src, index, self.urls)
        return {"scope": src.scope, "id": src.id, "index": int(index), **body}

    def series(self, src: VizSource, index: int, stream: str, start, end, points: int) -> dict:
        return self._reader(src).series(src, index, stream, start, end, points)

    def meta_file(self, src: VizSource, path: str) -> dict:
        return self._reader(src).meta_file(src, path)

    # ------------------------------------------------------------ external annotations
    def external_annotations(self, src: VizSource, index: int):
        from curation.viz.annotations import AnnotationFileError, argus_annotations, read_label_file

        upload_id = src.annotations_upload
        if not upload_id:
            return None
        docs = self.label_cache.get(upload_id)
        if docs is None:
            from ..orchestr.service import orchestrator_of

            store = orchestrator_of(self.rt).uploads
            try:
                meta = store.get(src.owner, upload_id)
                data = store.path(src.owner, upload_id).read_bytes()
                docs = read_label_file(data, str(meta.get("name") or ""))
            except (ApiError, OSError, AnnotationFileError):
                docs = {}
            self.label_cache.put(upload_id, docs)
        doc = docs.get(int(index))
        return argus_annotations(doc) if doc is not None else None

    # ------------------------------------------------------------ slices (design doc 21 §4)
    def camera_source(self, src: VizSource, index: int, camera: str):
        """(VizCamera, the source file's identity, how to open it, from_ts, to_ts) of a LeRobot or Lance
        camera of an episode - what a slice is cut from."""
        if self.reader_of(src) == "lance":
            cam, blob, frm, to = self.lance.blob(src, index, camera)
            m, row = self.lance.row(src, index)
            feature = m.camera_features[camera]
            chunk, file = row.video_files[feature]
            ident = f"lance:{feature}:{chunk}:{file}"
            return cam, ident, self.segments.blob_opener(blob, ident), frm, to
        cam, rel, frm, to = self.lerobot.camera_file(src, index, camera)
        return cam, rel, self.segments.opener(src, rel), frm, to

    def segment_place(self, src: VizSource, index: int, camera: str, frm) -> dict | None:
        """Where the episode sits in the slice the Daemon serves for a camera, or None (the browser reads
        the file itself; also when the file cannot be looked at now - it says why when it is played)."""
        try:
            _, ident, open_, frm, _ = self.camera_source(src, index, camera)
            return self.segments.place(src, ident, open_, frm)
        except Exception:  # noqa: BLE001 - not sliced: the direct file plays as before
            return None

    def segment_file(self, src: VizSource, index: int, camera: str):
        if not self.segments.enabled:
            raise ApiError("not_found", "切片播放没有打开（CURATOR_VIZ_SEGMENT=0）", details={"reason": "segment_disabled"})
        cam, ident, open_, frm, to = self.camera_source(src, index, camera)
        if self.segments.place(src, ident, open_, frm) is None:
            raise ApiError("not_found", f"相机 {camera} 不需要切片（直接读原文件）", details={"reason": "not_segmented"})
        try:
            return self.segments.file(src, index, camera, open_, frm, to)[0]
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - the <video> says it cannot play, with why
            raise ApiError("internal", f"切片失败：{str(exc)[:200]}", details={"reason": "segment_failed"}) from None

    # ------------------------------------------------------------ camera bytes (LeRobot)
    def lerobot_transcode(self, src: VizSource, index: int, camera: str):
        """The transcode job of one camera of an episode (started if it is not running)."""
        if not self.transcode_enabled:
            raise ApiError("not_found", "平台转码已关闭（CURATOR_VIZ_TRANSCODE=0）",
                           details={"reason": "transcode_disabled"})
        cam, rel, frm, to = self.lerobot.camera_file(src, index, camera)
        fp = digest(src.scope, src.id, src.fingerprint)
        out = self.disk.path("transcode", fp, f"ep{int(index):06d}", f"{camera}.mp4")
        key = f"{fp}:{index}:{camera}"
        access = Access(self.rt, src)
        source_copy = self.disk.path("source", fp, *rel.split("/"))

        def prepare():
            if src.is_local:
                return access.local_file(rel)
            if frm is not None:
                # an episode of a shared file: its slice, read by range - not the whole file (design doc 21 §4.3)
                path, seg = self.segments.file(src, index, camera, self.segments.opener(src, rel), frm, to)
                return path, float(frm) - seg["start_s"], (float(to) - seg["start_s"]) if to is not None else None
            with self.copy_lock(source_copy):
                if self.disk.get(source_copy) is None:
                    tmp = source_copy.with_name(source_copy.name + ".part")
                    with access.storage() as st:
                        st.download(rel, str(tmp))
                    tmp.replace(source_copy)
                    self.disk.added(source_copy)
            return source_copy

        return self.transcoder.ensure(key, out, prepare, start=frm, end=to, keep=(source_copy,))

    def _cam_cache(self, src: VizSource, index: int, camera: str, suffix: str, transcode: bool = False):
        """ETag and whether the bytes are immutable, for one camera byte response. The bytes a URL
        serves are fixed by the source fingerprint and (mcap) the confirmed mapping version, so the
        ETag folds both in; a changed fingerprint / mapping yields a new ETag. They are immutable at
        a stable URL except for an mcap *dataset* whose mapping can be re-confirmed in place - there
        the URL is unchanged while the bytes are not, so ``immutable`` is dropped and the browser
        revalidates with the ETag instead (304 while the mapping stands)."""
        etag = '"' + digest("viz-cam", src.scope, src.id, src.fingerprint, src.mapping_version or 0,
                            int(index), camera, suffix, bool(transcode)) + '"'
        immutable = src.scope == "task" or self.reader_of(src) != "mcap"
        return etag, immutable

    def _transcode_answer(self, job, request_headers, etag: str | None = None, immutable: bool = True):
        from starlette.responses import JSONResponse

        if job.state == "done":
            return file_response(job.out, "video/mp4", request_headers, etag=etag, immutable=immutable)
        if job.state == "failed":
            raise ApiError("internal", job.message, details={"reason": "transcode_failed"})
        return JSONResponse(pending_body(job), status_code=202, headers={"Cache-Control": "no-store"})

    def lance_transcode(self, src: VizSource, index: int, camera: str):
        """The transcode of a Lance camera the browser cannot play: the mp4 is copied out of the blob
        once (the whole shared file, kept beside the copies), then transcoded like a LeRobot one."""
        if not self.transcode_enabled:
            raise ApiError("not_found", "平台转码已关闭（CURATOR_VIZ_TRANSCODE=0）",
                           details={"reason": "transcode_disabled"})
        cam, blob, frm, to = self.lance.blob(src, index, camera)
        m, row = self.lance.row(src, index)
        feature = m.camera_features[camera]
        chunk, file = row.video_files[feature]
        fp = digest(src.scope, src.id, src.fingerprint)
        out = self.disk.path("transcode", fp, f"ep{int(index):06d}", f"{camera}.mp4")
        source_copy = self.disk.path("source", fp, "lance", f"{feature}-c{chunk}-f{file}.mp4")

        def prepare():
            if frm is not None:
                # the episode's slice of the shared mp4 in the blob, read by range (design doc 21 §4.3)
                opener = self.segments.blob_opener(blob, f"lance:{feature}:{chunk}:{file}")
                path, seg = self.segments.file(src, index, camera, opener, frm, to)
                return path, float(frm) - seg["start_s"], (float(to) - seg["start_s"]) if to is not None else None
            with self.copy_lock(source_copy):
                if self.disk.get(source_copy) is None:
                    tmp = source_copy.with_name(source_copy.name + ".part")
                    size, pos = blob.size(), 0
                    with open(tmp, "wb") as fh:
                        while pos < size:
                            data = blob.read_range(pos, min(1 << 22, size - pos))
                            if not data:
                                break
                            fh.write(data)
                            pos += len(data)
                    tmp.replace(source_copy)
                    self.disk.added(source_copy)
            return source_copy

        return self.transcoder.ensure(f"{fp}:{index}:{camera}", out, prepare, start=frm, end=to, keep=(source_copy,))

    def mcap_transcode(self, src: VizSource, index: int, camera: str):
        if not self.transcode_enabled:
            raise ApiError("not_found", "平台转码已关闭（CURATOR_VIZ_TRANSCODE=0）",
                           details={"reason": "transcode_disabled"})
        self._reader(src)                                         # the mapping must be there
        cd, d = self.mcap.camera(src, index, camera)
        out = d / f"{camera}.h264.mp4"
        key = f"mcap:{out}"

        def prepare() -> pathlib.Path:
            video = self.mcap.video_file(src, index, camera)
            return video if video is not None else self.mcap.mjpeg_file(src, index, camera)

        return self.transcoder.ensure(key, out, prepare)

    def camera_frames(self, src: VizSource, index: int, camera: str, request_headers):
        self._reader(src)
        reader = self.reader_of(src)
        etag, immutable = self._cam_cache(src, index, camera, "frames")
        if reader == "lance":
            return file_response(self.lance.frame_pack(src, index, camera)[0], "application/octet-stream",
                                 request_headers, etag=etag, immutable=immutable)
        if reader != "mcap":
            raise ApiError("not_found", "只有 mcap 与 Lance 逐帧图片的相机有帧包", details={"reason": "not_frames"})
        return file_response(self.mcap.frames_file(src, index, camera), "application/octet-stream",
                             request_headers, etag=etag, immutable=immutable)

    def camera_frame_index(self, src: VizSource, index: int, camera: str) -> dict:
        self._reader(src)
        reader = self.reader_of(src)
        if reader == "lance":
            return self.lance.frame_pack(src, index, camera)[1]
        if reader != "mcap":
            raise ApiError("not_found", "只有 mcap 与 Lance 逐帧图片的相机有帧包", details={"reason": "not_frames"})
        return self.mcap.frame_index(src, index, camera)

    # ------------------------------------------------------------ depth streams (design doc 21 §5)
    def _depth(self, src: VizSource, index: int, key: str):
        reader = self._reader(src)
        if not hasattr(reader, "depth_pack"):
            raise ApiError("not_found", f"没有深度流 {key}", details={"reason": "unknown_stream"})
        return reader.depth_pack(src, index, key)

    def stream_frame_index(self, src: VizSource, index: int, key: str):
        """C4 ``VizFrameIndex`` of a depth pack, or 202 + ``VizMediaPending`` while it is made."""
        import json

        from starlette.responses import JSONResponse

        job, _, doc = self._depth(src, index, key)
        if job.state == "done" and doc.is_file():
            return json.loads(doc.read_text(encoding="utf-8"))
        if job.state == "failed":
            raise ApiError("internal", job.message, details={"reason": "depth_failed"})
        return JSONResponse(pending_body(job), status_code=202, headers={"Cache-Control": "no-store"})

    def stream_frames(self, src: VizSource, index: int, key: str, request_headers):
        from starlette.responses import JSONResponse

        job, pack, _ = self._depth(src, index, key)
        if job.state == "done" and pack.is_file():
            etag, immutable = self._cam_cache(src, index, key, "depth")
            return file_response(pack, "application/octet-stream", request_headers, etag=etag, immutable=immutable)
        if job.state == "failed":
            raise ApiError("internal", job.message, details={"reason": "depth_failed"})
        return JSONResponse(pending_body(job), status_code=202, headers={"Cache-Control": "no-store"})

    def camera_video(self, src: VizSource, index: int, camera: str, transcode: bool, request_headers,
                     segment: bool = False):
        """The ``.mp4`` route: a local LeRobot file, a remuxed mcap camera, a transcode (202 while
        it runs); a JPEG mcap camera without ``transcode`` is muxed as MJPEG (the check reader's mp4);
        with ``segment`` the episode's slice of a LeRobot / Lance camera (design doc 21 §4.3)."""
        if segment and not transcode and self.reader_of(src) in ("lerobot", "lance"):
            self._reader(src)
            etag, immutable = self._cam_cache(src, index, camera, "mp4:segment")
            return file_response(self.segment_file(src, index, camera), "video/mp4", request_headers,
                                 etag=etag, immutable=immutable)
        if self.reader_of(src) == "mcap":
            self._reader(src)
            if transcode:
                etag, immutable = self._cam_cache(src, index, camera, "mp4", transcode=True)
                return self._transcode_answer(self.mcap_transcode(src, index, camera), request_headers,
                                              etag=etag, immutable=immutable)
            video = self.mcap.video_file(src, index, camera)
            if video is None:
                video = self.mcap.mjpeg_file(src, index, camera)
            etag, immutable = self._cam_cache(src, index, camera, "mp4")
            return file_response(video, "video/mp4", request_headers, etag=etag, immutable=immutable)
        if self.reader_of(src) == "lance":
            self._reader(src)
            cam, blob, _, _ = self.lance.blob(src, index, camera)
            if transcode or cam["access"] in ("transcode", "unsupported"):
                etag, immutable = self._cam_cache(src, index, camera, "mp4", transcode=True)
                return self._transcode_answer(self.lance_transcode(src, index, camera), request_headers,
                                              etag=etag, immutable=immutable)
            etag, immutable = self._cam_cache(src, index, camera, "mp4")
            return ranged_response(blob.read_range, int(blob.size()), "video/mp4", request_headers,
                                   etag=etag, immutable=immutable)
        if self.reader_of(src) != "lerobot":
            raise ApiError("not_found", "这个数据集的相机不经这条路由", details={"reason": "not_lerobot"})
        cam, rel, frm, to = self.lerobot.camera_file(src, index, camera)
        if transcode or cam["access"] in ("transcode", "unsupported"):
            etag, immutable = self._cam_cache(src, index, camera, "mp4", transcode=True)
            return self._transcode_answer(self.lerobot_transcode(src, index, camera), request_headers,
                                          etag=etag, immutable=immutable)
        if src.is_local:
            etag, immutable = self._cam_cache(src, index, camera, "mp4")
            return file_response(Access(self.rt, src).local_file(rel), "video/mp4", request_headers,
                                 etag=etag, immutable=immutable)
        raise ApiError("not_found", f"相机 {camera} 直连 TOS，不经 Daemon（用 episode 记录里的 url）",
                       details={"reason": "direct"})


def viz_of(rt) -> VizService:
    svc = getattr(rt, "viz", None)
    if svc is None:
        with _CREATE:
            svc = getattr(rt, "viz", None)
            if svc is None:
                svc = VizService(rt)
                rt.viz = svc
                hooks = getattr(rt, "on_shutdown", None)
                if isinstance(hooks, list):
                    hooks.append(lambda _rt: svc.shutdown())
    return svc


# ---------------------------------------------------------------- mcap mappings and templates (§6)

def _input_source(svc: VizService, spec: dict, owner: str) -> VizSource:
    """The source a probe reads: a registration (``{dataset_id}``), or an input given in full -
    the add drawer probes before registering."""
    if "dataset_id" in spec:
        return svc.dataset_source(spec["dataset_id"], owner)
    from ..taskspec import resolve_input

    fields = resolve_input(svc.rt.repo, svc.rt.settings, spec, owner)
    uri = fields["input_uri"]
    return VizSource(scope="dataset", id="probe", owner=owner, name=uri.rstrip("/").rsplit("/", 1)[-1],
                     source=fields["input_source"], uri=uri, region=fields.get("input_region"),
                     cred_id=fields.get("input_cred_id"),
                     preflight={"format": {"kind": "mcap", "version": None, "supported": True}},
                     fingerprint=digest("probe", fields["input_source"], uri, fields.get("input_region")))


def probe(svc: VizService, spec: dict, owner: str, *, file: str | None = None, template: str | None = None) -> dict:
    """C4 ``McapProbe``: the topics of one episode file and a mapping drafted for them."""
    from curation.viz import mcap_mapping as MMAP

    src = _input_source(svc, spec, owner)
    if svc.reader_of(src) not in ("mcap", None) and "dataset_id" in spec:
        raise ApiError("validation_failed", "这不是 mcap 数据集：没有 episode_N.mcap", details={"reason": "not_mcap"})
    pr, count, warnings = svc.mcap.probe_source(src, file)
    site = [{"id": t.id, "name": t.name, "mapping": t.mapping} for t in svc.rt.repo.list_viz_templates(owner=owner)]
    if template and not template.startswith("builtin:") and template not in {t["id"] for t in site}:
        raise ApiError("not_found", f"没有模版 {template}", details={"reason": "unknown_template"})
    mapping, matched = MMAP.draft(pr, template=template, site_templates=site)
    uses = MMAP.topic_uses(mapping, pr)
    topics = []
    for t in sorted(pr.topics):
        tp = pr.topics[t]
        use, role, name = uses.get(t, ("unmapped", None, ""))
        notes = []
        rate = tp.rate_hz(pr.end_ns)
        if rate is not None and rate > 150:
            notes.append("高频：作曲线时下采样到 ≤ 2000 点")
        if not tp.decodable:
            notes.append(f"{tp.message_encoding} 消息解不开")
        if tp.kind == "camera" and tp.codec == "raw":
            notes.append("原始图像（raw）本期不支持")
        elif tp.kind == "camera" and tp.codec not in ("jpeg", "png", "h264", "h265"):
            notes.append(f"编码 {tp.codec} 本期不支持")
        if tp.kind == "depth" and tp.codec == "rvl":
            notes.append("RVL 编码的压缩深度本期不支持")
        if tp.kind == "camera" and tp.codec == "h265":
            notes.append("H.265：浏览器放不了时由平台转码")
        topics.append({"topic": t, "schema": tp.schema, "schema_encoding": tp.schema_encoding,
                       "message_encoding": tp.message_encoding, "count": tp.count, "rate_hz": rate,
                       "start_s": round((tp.first_ns - pr.start_ns) / 1e9, 3) if tp.first_ns and pr.start_ns else None,
                       "end_s": round((pr.end_ns - pr.start_ns) / 1e9, 3) if pr.end_ns and pr.start_ns else None,
                       "image": {"codec": tp.codec, "width": tp.width, "height": tp.height} if tp.kind in ("camera", "depth") else None,
                       "fields": [{"path": f["path"], "size": f["size"]} for f in tp.fields] if tp.fields else None,
                       "use": use, "role": role if role in ("state", "action", "other") else None, "name": name,
                       "notes": notes})
    if not mapping["cameras"]:
        warnings.append({"code": "no_camera", "message": "没有相机：可视化只能看曲线"})
    if not mapping["series"]:
        warnings.append({"code": "no_series", "message": "没有曲线：运动质量、视频-动作同步等模块不可用"})
    warnings += MMAP.check_gaps(mapping, pr)
    return {"file": pr.file, "files": count, "topics": topics, "metadata": pr.metadata,
            "attachments": pr.attachments, "draft": mapping, "matched": matched, "warnings": warnings}


def mapping_doc(svc: VizService, ds, *, gaps: bool = True) -> dict:
    """C4 ``DatasetMapping``; ``gaps`` adds what the check reader will not read in it (a probe of the
    dataset, cached by its fingerprint)."""
    from curation.viz import mcap_mapping as MMAP

    from ..repo.extras import dataset_format
    from .status import is_mcap

    preflight = ds.preflight if isinstance(ds.preflight, dict) else {}
    if not is_mcap(preflight):
        raise ApiError("validation_failed", "只有 mcap 数据集有字段映射", details={"reason": "not_mcap"})
    m = ds.viz_mapping if isinstance(ds.viz_mapping, dict) else None
    warnings = []
    if dataset_format(preflight) == "unsupported":
        detail = str((preflight.get("format") or {}).get("detail") or "")
        warnings.append({"code": "checks_unreadable",
                         "message": ("质检读取器按这份映射仍读不了，可视化不受影响" if m else
                                     "质检读取器按站点缺省读不了，确认映射后重新预检") + (f"：{detail[:300]}" if detail else "")})
    if m and gaps:
        try:
            pr, _, _ = svc.mcap.probe_source(dataset_source(svc.rt, ds, ds.owner_id))
            warnings += MMAP.check_gaps(m, pr)
        except ApiError:
            pass                                             # the files cannot be probed now: no claim
    return {"dataset_id": ds.id, "state": "confirmed" if m else "none", "mapping": m,
            "version": int(ds.viz_mapping_version or 0), "updated_at": ds.viz_mapping_updated_at,
            "check_mapping": MMAP.check_mapping(m) if m else None, "warnings": warnings}


def validate_for(svc: VizService, src: VizSource, mapping: dict) -> None:
    """Raise validation_failed with every problem of ``mapping`` against the dataset's topics."""
    from curation.viz import mcap_mapping as MMAP

    problems = MMAP.validate(mapping)
    if not problems:
        pr, _, _ = svc.mcap.probe_source(src)
        problems = MMAP.validate(mapping, set(pr.topics))
    if problems:
        first = problems[0]
        raise ApiError("validation_failed", f"映射不合格：{first['field']} {first['problem']}"
                       + (f"（另有 {len(problems) - 1} 处）" if len(problems) > 1 else ""),
                       details={"errors": [{"field": f"mapping.{p['field']}", "problem": p["problem"]} for p in problems[:50]]})


def put_mapping(svc: VizService, dataset_id: str, owner: str, mapping: dict):
    ds = svc.rt.repo.get_dataset(dataset_id, owner=owner)
    mapping_doc(svc, ds, gaps=False)                         # mcap only
    validate_for(svc, dataset_source(svc.rt, ds, owner), mapping)
    return svc.rt.repo.set_dataset_viz_mapping(dataset_id, mapping, owner=owner)


def template_doc(t) -> dict:
    return {"id": t.id, "name": t.name, "description": t.description, "builtin": False, "mapping": t.mapping,
            "created_at": t.created_at, "updated_at": t.updated_at}


def list_templates(svc: VizService, owner: str) -> list[dict]:
    from curation.viz.mcap_mapping import BUILTINS

    out = [{"id": b["id"], "name": b["name"], "description": b["description"], "builtin": True, "mapping": None,
            "created_at": None, "updated_at": None} for b in BUILTINS]
    return out + [template_doc(t) for t in svc.rt.repo.list_viz_templates(owner=owner)]


def create_template(svc: VizService, owner: str, name: str, description: str, mapping: dict) -> dict:
    from curation.viz import mcap_mapping as MMAP

    from ..repo import protocol as P

    problems = MMAP.validate(mapping)
    if problems:
        raise ApiError("validation_failed", f"模版不合格：{problems[0]['field']} {problems[0]['problem']}",
                       details={"errors": [{"field": f"mapping.{p['field']}", "problem": p["problem"]} for p in problems[:50]]})
    if name in {b["name"] for b in MMAP.BUILTINS}:
        raise ApiError("name_taken", f"「{name}」是内置模版的名字，换一个")
    try:
        t = svc.rt.repo.create_viz_template(P.VizTemplate(id="", name=name, mapping=mapping,
                                                          description=description or "", owner_id=owner))
    except P.Conflict:
        raise ApiError("name_taken", f"已经有叫「{name}」的模版") from None
    return template_doc(t)
