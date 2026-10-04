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
from .media import DiskCache, Transcoder, digest, file_response, pending_body
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

    def camera(self, src: VizSource, index: int, cam: dict, rel: str | None, frm, to) -> dict:
        out = {"key": cam["key"], "kind": cam["kind"], "access": cam["access"], "url": None, "index_url": None,
               "transcode_url": None, "from_ts": frm, "to_ts": to, "offset_s": 0.0,
               "transcoded": cam["access"] == "transcode", "expires_at": None, "reason": cam.get("reason")}
        if rel is None:
            out["access"], out["reason"] = "unsupported", "这条 episode 没有这一路相机的视频文件"
            return out
        access = cam["access"]
        if access == "direct":
            out["url"] = Access(self.svc.rt, src).browser_url(rel, URL_TTL_S)
            out["expires_at"] = self.svc.rt.clock() + URL_TTL_S * 1000
            if out["url"] is None:                                 # a local source after all
                out["access"], out["url"] = "local", self.daemon(src, index, cam["key"], "mp4")
        elif access in ("local", "transcode", "remux"):
            out["url"] = self.daemon(src, index, cam["key"], "mp4")
        elif access == "frames":
            out["url"] = self.daemon(src, index, cam["key"], "frames")
            out["index_url"] = self.daemon(src, index, cam["key"], "json")
        if self.svc.transcode_enabled and access in ("direct", "local", "remux"):
            out["transcode_url"] = self.daemon(src, index, cam["key"], "mp4", transcode=True)
        if access == "transcode":                       # the copy starts now, before the player asks
            with contextlib.suppress(Exception):
                self.svc.lerobot_transcode(src, index, cam["key"])
        return out


class VizService:
    META_TTL_S = 600.0

    def __init__(self, rt):
        self.rt = rt
        s = rt.settings
        self.transcode_enabled = bool(getattr(s, "viz_transcode", True))
        self.meta_cache = LRU(max_items=32)
        self.frames_cache = LRU(max_items=64, max_bytes=256 << 20)
        self.label_cache = LRU(max_items=8)
        self.disk = DiskCache(getattr(s, "viz_cache_dir", None) or pathlib.Path(s.scratch_dir) / "viz-cache",
                              int(float(getattr(s, "viz_cache_gb", 20.0)) * (1 << 30)))
        self.transcoder = Transcoder(self.disk, int(getattr(s, "viz_transcode_workers", 2)))
        self.urls = EpisodeUrls(self)
        from .lerobot import LeRobotReader

        self.lerobot = LeRobotReader(self)

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

    def reader_of(self, src: VizSource) -> str | None:
        kind, version = src.format()
        fmt = src.preflight.get("format") if isinstance(src.preflight, dict) else None
        supported = bool((fmt or {}).get("supported", True))
        if kind == "lerobot" and supported and version in ("v2", "v3"):
            return "lerobot"
        if kind == "mcap" and supported:
            return "mcap"
        return None

    def _reader(self, src: VizSource):
        reader = self.reader_of(src)
        if reader == "lerobot":
            return self.lerobot
        if reader == "mcap":
            mcap = getattr(self, "mcap", None)
            if mcap is None:
                raise ApiError("not_found", "mcap 数据集的可视化在 F13.3 落地", details={"reason": "mcap_pending"})
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
                       "version": version if version in ("v2", "v3") else None, "reader": reader},
            "fps": None, "episode_count": 0, "episode_indices": None, "total_frames": None,
            "robot_type": None, "bytes": sum(o.size for o in listing.values()) if listing else None,
            "cameras": [], "streams": [], "annotation_sources": [], "field_tree": [],
            "mapping": self._mapping_state(src, kind), "transcode": {"enabled": self.transcode_enabled},
            "warnings": [], "fingerprint": src.fingerprint}
        if reader is None:
            out["warnings"].append({"code": "unsupported", "message": "Lance 数据集的可视化读取器在第二期" if kind == "lance"
                                    else f"这个数据集的格式（{kind}）没有可视化读取器"})
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

        def prepare() -> pathlib.Path:
            if src.is_local:
                return access.local_file(rel)
            if self.disk.get(source_copy) is None:
                tmp = source_copy.with_name(source_copy.name + ".part")
                with access.storage() as st:
                    st.download(rel, str(tmp))
                tmp.replace(source_copy)
                self.disk.added(source_copy)
            return source_copy

        return self.transcoder.ensure(key, out, prepare, start=frm, end=to, keep=(source_copy,))

    def camera_video(self, src: VizSource, index: int, camera: str, transcode: bool, request_headers):
        """The ``.mp4`` route of a LeRobot camera: a local file, or the transcode (202 while it runs)."""
        from starlette.responses import JSONResponse

        if self.reader_of(src) != "lerobot":
            raise ApiError("not_found", "这个数据集的相机不经这条路由", details={"reason": "not_lerobot"})
        cam, rel, frm, to = self.lerobot.camera_file(src, index, camera)
        if transcode or cam["access"] in ("transcode", "unsupported"):
            job = self.lerobot_transcode(src, index, camera)
            if job.state == "done":
                return file_response(job.out, "video/mp4", request_headers)
            if job.state == "failed":
                raise ApiError("internal", job.message, details={"reason": "transcode_failed"})
            return JSONResponse(pending_body(job), status_code=202, headers={"Cache-Control": "no-store"})
        if src.is_local:
            return file_response(Access(self.rt, src).local_file(rel), "video/mp4", request_headers)
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
    return svc
