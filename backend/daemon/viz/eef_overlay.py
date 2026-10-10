"""The EEF marks of one episode, computed on request (design docs 20, 22 §3; C4 ``EefOverlay``).

The model's marked clip is encoded in memory and never saved, so the mini player plays the camera's own
video (the task's ``VizEpisode`` camera) and paints these layers on a canvas above it. They come from the
task's trajectory bundle - no frame is decoded: the copy made at start (``inputs/uploads.json``, brought
back from the delivery when the work directory was cleaned), else the upload itself - and, when the
module measured the episode (a gripper reference was given), from its observation rows in the run
directory. A task given no trajectory.json on a handheld gripper's mcap reads the episode's trajectory the checks
derived from the recording (``checks/eef_video_consistency/trajectory/``, design doc 22 §5.4); ``not_derived``
until they have. Each bundle camera is matched to the ``VizEpisode`` camera playing the same file (LeRobot) or
topic (mcap); ``viz_camera`` is null when none does.

``times_s`` places every sample frame where the player shows its paired video frame, from the same data
the player plays (design doc 22 §3.4): frame k of a LeRobot clip at k / fps, message k of an mcap topic
at its time in the episode's scan (null before the first keyframe, which is never shown). The bundle's
own timeline plays no part: its zero and clock need not be the player's.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from curation.contracts import modules as registry
from curation.pipeline.records import module_dir

from ..errors import ApiError
from ..results.files import LRU

log = logging.getLogger("daemon.viz")

MODULE = "eef_video_consistency"
PARAM = "trajectory_json"
#: a gripper reference: the module measured the episode, and the model saw its review windows
REFERENCES = ("observation_seeds", "gripper_template")
_CACHE = LRU(max_items=32, max_bytes=64 << 20)


def _bundle(rt, task, owner: str, index: int) -> tuple[Path, dict]:
    """The trajectory bundle the task's EEF module read for episode ``index``, and the module's parameters."""
    from curation.extensions.eef_consistency import derive, derive_mcap

    from ..orchestr.service import orchestrator_of
    from ..results.store import store_of

    row = next((m for m in rt.repo.get_task_modules(task.id) if m.module_id == MODULE), None)
    params = (row.params or {}) if row is not None else {}
    handle = params.get(PARAM)
    if row is None or not (row.selected or handle):
        raise ApiError("not_found", "这个任务没有勾选「EEF–视频一致性」，没有可叠加的投影",
                       details={"reason": "no_eef_module"})
    store = store_of(rt)
    run_dir = store.task_dir(task.id)
    if not handle:              # no upload: the copy generated from the dataset (design doc 24), else the episode's own
        generated = run_dir / derive.RUN_COPY                      # derived from a handheld gripper's mcap (22 §5.4)
        derived = derive_mcap.bundle_path(module_dir(str(run_dir), MODULE), index)
        if not (generated.is_file() or derived.is_file()) and int(task.result_rev or 0) > 0:
            try:                                    # a cleaned work directory: restore it first
                store.revision(task)
            except ApiError:
                pass
        for path in (generated, derived):
            if path.is_file():
                return path, params
        raise ApiError("not_found", f"episode {index} 的轨迹还没有生成（EEF 模块还没判到这一条，或这一条推不出来）",
                       details={"reason": "not_derived"})
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
            return run_dir / entry["path"], params
    kind = registry.upload_params(MODULE)[PARAM]
    path, _ = orchestrator_of(rt).uploads.resolve(owner, handle, kind=kind, field=f"modules.{MODULE}.params.{PARAM}")
    if not path.is_file():
        raise ApiError("not_found", "这个任务的 trajectory.json 已不在（运行目录和上传件都找不到）",
                       details={"reason": "no_trajectory"})
    return path, params


def _observation_files(rt, task_id: str, index: int) -> list[Path]:
    """The episode's observation rows, one file a camera (``<camera>.jsonl``); none in the opinion mode."""
    from ..results.store import store_of

    d = Path(module_dir(str(store_of(rt).task_dir(task_id)), MODULE)) / "observations" / f"{int(index):06d}"
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


def _rows(path: Path) -> list[dict]:
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def camera_maps(svc, src, index: int):
    """(file -> VizEpisode camera key, topic -> key, the episode times of a camera's frames) of ``src`` (a task's
    input or a registration); empty maps and no times when the input cannot be read."""
    try:
        reader = svc.reader_of(src)
        if reader == "lerobot":
            m, row = svc.lerobot.row(src, index)
            fps = float(m.fps or 0)

            def lerobot_times(key: str, frames: list) -> list:
                return [round(k / fps, 6) if k is not None and fps > 0 else None for k in frames]

            return ({row.videos[f]: key for key, f in m.camera_features.items() if f in row.videos}, {},
                    lerobot_times)
        if reader == "umi_session":             # design doc 24 §6: the demos' raw videos, timed from the episode start
            m, ep = svc.umi_session.row(src, index)
            fps = float(m.fps or 0)

            def session_times(key: str, frames: list) -> list:
                return [round(k / fps, 6) if k is not None and fps > 0 else None for k in frames]

            return ({rel: cam["key"] for cam, (rel, _, _) in zip(m.cameras, ep.videos)}, {}, session_times)
        if reader == "mcap":
            cams = svc.dataset(src).get("cameras") or []
            scanned = svc.mcap.frame_times(src, index)

            def mcap_times(key: str, frames: list) -> list:
                ts, first = scanned.get(key, ([], 0.0))
                return [ts[k] if k is not None and 0 <= k < len(ts) and ts[k] >= first - 1e-6 else None
                        for k in frames]

            return {}, {c["source"]: c["key"] for c in cams if c.get("source")}, mcap_times
    except Exception:  # noqa: BLE001 - the layers still come back; the player just has nothing to put them on
        log.warning("eef overlay: the cameras of %s %s episode %s could not be matched", src.scope, src.id, index,
                    exc_info=True)
    return {}, {}, None


def overlay_cameras(path: Path, index: int, *, observations: list[Path] = (), judged: bool = False,
                    max_gap_ms: float | None = None, cache=None, scope: tuple = (),
                    kept: bool = False) -> tuple[list[dict], dict | None]:
    """The layers of every camera of the bundle at ``path`` for episode ``index`` (each with its media uri and
    topic, to be matched to a player camera) and the poses bridged for a UMI hand; kept in ``cache``. ``kept``:
    the platform's own bundle in the viz disk cache: its frames are not checked against their schema again, and
    the cache's reads touch the file's times - one made again is a new file (written, renamed), told by its inode."""
    from curation.extensions.eef_consistency import load, overlay, umi

    stat = path.stat()
    seen = tuple((f.name, f.stat().st_size, f.stat().st_mtime_ns) for f in observations)
    stamp = (stat.st_ino, stat.st_size) if kept else (stat.st_size, stat.st_mtime_ns)
    key = (*scope, str(path), *stamp, int(index), judged, seen, max_gap_ms)
    cached = (cache or _CACHE).get(key)
    if cached is not None:
        return json.loads(json.dumps(cached[0])), cached[1]
    result = load.load_bundle(path, check_media=False, episodes=[int(index)], trusted=kept)
    if not result.ok:
        raise ApiError("not_found", "轨迹包读不了，画不出投影", details={"reason": "trajectory_invalid"})
    sample = result.samples.get(int(index))
    if sample is None:
        raise ApiError("not_found", f"轨迹包里没有 episode {index}", details={"reason": "no_episode"})
    observed = {f.stem: overlay.observed_tracks(_rows(f), sample.n_frames) for f in observations
                if f.stem in sample.cameras}
    bridged = None
    if sample.hand_poses:              # a UMI hand's short pose gaps (design doc 22 §5.2): the checks' default or the viewer's
        default = umi.default_gap_s(sample)
        done = umi.fill_gaps(sample, max_gap_ms / 1000 if max_gap_ms is not None else None)
        bridged = {"max_gap_s": done["max_gap_s"], "default_s": default,
                   "step_s": round(default / umi.GAP_STEPS, 6) if default else None,
                   "range_steps": list(umi.GAP_RANGE_STEPS), "frames": done["frames"]}
    cameras = overlay.episode_overlay(sample, observed, judged)
    for c in cameras:
        media = sample.cameras[c["camera_id"]].media
        c["media_uri"], c["topic"] = media.get("uri"), media.get("topic")
    (cache or _CACHE).put(key, (cameras, bridged), len(json.dumps(cameras)))
    return json.loads(json.dumps(cameras)), bridged


def episode_overlay(rt, svc, task_id: str, owner: str, index: int, *, max_gap_ms: float | None = None) -> dict:
    task = rt.repo.get_task(task_id, owner=owner)
    path, params = _bundle(rt, task, owner, int(index))
    judged = any(params.get(p) for p in REFERENCES)
    observations = _observation_files(rt, task.id, index) if judged else []
    try:
        cameras, bridged = overlay_cameras(path, int(index), observations=observations, judged=judged,
                                           max_gap_ms=max_gap_ms, scope=("task", task.id))
    except ApiError as err:
        if (err.details or {}).get("reason") == "trajectory_invalid":
            raise ApiError("not_found", "这个任务的 trajectory.json 读不了，画不出投影",
                           details={"reason": "trajectory_invalid"}) from None
        raise
    src = svc.task_source(task.id, owner)
    record = {}
    if params.get(PARAM):           # an upload overrides what the declaration generates: the record's own, dashed (§5.3)
        from .eef_dataset import record_layers

        record = record_layers(rt, svc, task, src, int(index))
    files, topics, times = camera_maps(svc, src, int(index))
    out = []
    for c in cameras:
        viz = topics.get(c["topic"]) if c["topic"] else files.get(c["media_uri"])
        frames = c["media_frames"]
        extra = record.get(c["topic"] or c["media_uri"]) or []
        out.append({k: v for k, v in c.items() if k not in ("media_uri", "topic")}
                   | {"viz_camera": viz, "times_s": times(viz, frames) if viz and times else [None] * len(frames),
                      "layers": c["layers"] + extra})
    return {"task_id": task.id, "dataset_id": task.dataset_id, "episode_index": int(index), "cameras": out,
            "interpolation": bridged}
