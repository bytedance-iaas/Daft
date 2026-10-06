"""The EEF opinion's marks for one episode, computed on request (design doc 20; C4 ``EefOverlay``).

The model's marked clip is encoded in memory and never saved, so the report plays the camera's own
video (the task's ``VizEpisode`` camera) and paints these layers on a canvas above it. They come from
the task's trajectory bundle alone - no frame is decoded: the copy made at start
(``inputs/uploads.json``, brought back from the delivery when the work directory was cleaned), else the
upload itself. Each bundle camera is matched to the ``VizEpisode`` camera playing the same file (LeRobot)
or topic (mcap); ``viz_camera`` is null when none does.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from curation.contracts import modules as registry

from ..errors import ApiError
from ..results.files import LRU

log = logging.getLogger("daemon.viz")

MODULE = "eef_video_consistency"
PARAM = "trajectory_json"
_CACHE = LRU(max_items=32, max_bytes=64 << 20)


def _bundle(rt, task, owner: str) -> Path:
    """The trajectory bundle the task's EEF module read."""
    from ..orchestr.service import orchestrator_of
    from ..results.store import store_of

    row = next((m for m in rt.repo.get_task_modules(task.id) if m.module_id == MODULE), None)
    handle = (row.params or {}).get(PARAM) if row is not None else None
    if not handle:
        raise ApiError("not_found", "这个任务没有勾选「EEF–视频一致性」，没有可叠加的投影",
                       details={"reason": "no_eef_module"})
    store = store_of(rt)
    run_dir = store.task_dir(task.id)
    table_path = run_dir / "inputs" / "uploads.json"
    if not table_path.is_file() and int(task.result_rev or 0) > 0:
        try:                                            # a cleaned work directory: restore it first
            store.revision(task)
        except ApiError:
            pass
    if table_path.is_file():
        try:
            entry = (json.loads(table_path.read_text()) or {}).get(handle)
        except (OSError, ValueError):
            entry = None
        if entry and (run_dir / entry["path"]).is_file():
            return run_dir / entry["path"]
    kind = registry.upload_params(MODULE)[PARAM]
    path, _ = orchestrator_of(rt).uploads.resolve(owner, handle, kind=kind, field=f"modules.{MODULE}.params.{PARAM}")
    if not path.is_file():
        raise ApiError("not_found", "这个任务的 trajectory.json 已不在（运行目录和上传件都找不到）",
                       details={"reason": "no_trajectory"})
    return path


def _viz_cameras(svc, task_id: str, owner: str, index: int) -> tuple[dict[str, str], dict[str, str]]:
    """(file -> VizEpisode camera key, topic -> key) of the task's input; empty when it cannot be read."""
    try:
        src = svc.task_source(task_id, owner)
        reader = svc.reader_of(src)
        if reader == "lerobot":
            m, row = svc.lerobot.row(src, index)
            return {row.videos[f]: key for key, f in m.camera_features.items() if f in row.videos}, {}
        if reader == "mcap":
            cams = svc.dataset(src).get("cameras") or []
            return {}, {c["source"]: c["key"] for c in cams if c.get("source")}
    except Exception:  # noqa: BLE001 - the layers still draw; the report just has no video to put them on
        log.warning("eef overlay: the cameras of task %s episode %s could not be matched", task_id, index,
                    exc_info=True)
    return {}, {}


def episode_overlay(rt, svc, task_id: str, owner: str, index: int) -> dict:
    from curation.extensions.eef_consistency import load, overlay

    task = rt.repo.get_task(task_id, owner=owner)
    path = _bundle(rt, task, owner)
    stat = path.stat()
    key = (task.id, str(path), stat.st_size, stat.st_mtime_ns, int(index))
    cameras = _CACHE.get(key)
    if cameras is None:
        result = load.load_bundle(path, check_media=False, episodes=[int(index)])
        if not result.ok:
            raise ApiError("not_found", "这个任务的 trajectory.json 读不了，画不出投影",
                           details={"reason": "trajectory_invalid"})
        sample = result.samples.get(int(index))
        if sample is None:
            raise ApiError("not_found", f"trajectory.json 里没有 episode {index}", details={"reason": "no_episode"})
        cameras = overlay.episode_overlay(sample)
        for c in cameras:
            media = sample.cameras[c["camera_id"]].media
            c["media_uri"], c["topic"] = media.get("uri"), media.get("topic")
        _CACHE.put(key, cameras, len(json.dumps(cameras)))
    files, topics = _viz_cameras(svc, task.id, owner, int(index))
    out = []
    for c in cameras:
        viz = topics.get(c["topic"]) if c["topic"] else files.get(c["media_uri"])
        out.append({k: v for k, v in c.items() if k not in ("media_uri", "topic")} | {"viz_camera": viz})
    return {"task_id": task.id, "episode_index": int(index), "cameras": out}
