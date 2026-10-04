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
        from .mcap import McapReader

        self.lerobot = LeRobotReader(self)
        self.mcap = McapReader(self)

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
        # mcap and LeRobot whether or not the check reader takes them (status.viz_format)
        kind, version = src.format()
        if kind == "lerobot" and version in ("v2", "v3"):
            return "lerobot"
        if kind == "mcap":
            return "mcap"
        return None

    def _reader(self, src: VizSource):
        reader = self.reader_of(src)
        if reader == "lerobot":
            return self.lerobot
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

    def _transcode_answer(self, job, request_headers):
        from starlette.responses import JSONResponse

        if job.state == "done":
            return file_response(job.out, "video/mp4", request_headers)
        if job.state == "failed":
            raise ApiError("internal", job.message, details={"reason": "transcode_failed"})
        return JSONResponse(pending_body(job), status_code=202, headers={"Cache-Control": "no-store"})

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
        if self.reader_of(src) != "mcap":
            raise ApiError("not_found", "只有 mcap 的 JPEG / PNG 相机有帧包", details={"reason": "not_frames"})
        return file_response(self.mcap.frames_file(src, index, camera), "application/octet-stream", request_headers)

    def camera_frame_index(self, src: VizSource, index: int, camera: str) -> dict:
        self._reader(src)
        if self.reader_of(src) != "mcap":
            raise ApiError("not_found", "只有 mcap 的 JPEG / PNG 相机有帧包", details={"reason": "not_frames"})
        return self.mcap.frame_index(src, index, camera)

    def camera_video(self, src: VizSource, index: int, camera: str, transcode: bool, request_headers):
        """The ``.mp4`` route: a local LeRobot file, a remuxed mcap camera, a transcode (202 while
        it runs); a JPEG mcap camera without ``transcode`` is muxed as MJPEG (the check reader's mp4)."""
        if self.reader_of(src) == "mcap":
            self._reader(src)
            if transcode:
                return self._transcode_answer(self.mcap_transcode(src, index, camera), request_headers)
            video = self.mcap.video_file(src, index, camera)
            if video is None:
                video = self.mcap.mjpeg_file(src, index, camera)
            return file_response(video, "video/mp4", request_headers)
        if self.reader_of(src) != "lerobot":
            raise ApiError("not_found", "这个数据集的相机不经这条路由", details={"reason": "not_lerobot"})
        cam, rel, frm, to = self.lerobot.camera_file(src, index, camera)
        if transcode or cam["access"] in ("transcode", "unsupported"):
            return self._transcode_answer(self.lerobot_transcode(src, index, camera), request_headers)
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
        if tp.kind == "camera" and tp.codec not in ("jpeg", "png", "h264", "h265"):
            notes.append(f"编码 {tp.codec} 本期不支持")
        if tp.kind == "camera" and tp.codec == "h265":
            notes.append("H.265：浏览器放不了时由平台转码")
        topics.append({"topic": t, "schema": tp.schema, "schema_encoding": tp.schema_encoding,
                       "message_encoding": tp.message_encoding, "count": tp.count, "rate_hz": rate,
                       "start_s": round((tp.first_ns - pr.start_ns) / 1e9, 3) if tp.first_ns and pr.start_ns else None,
                       "end_s": round((pr.end_ns - pr.start_ns) / 1e9, 3) if pr.end_ns and pr.start_ns else None,
                       "image": {"codec": tp.codec, "width": tp.width, "height": tp.height} if tp.kind == "camera" else None,
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
