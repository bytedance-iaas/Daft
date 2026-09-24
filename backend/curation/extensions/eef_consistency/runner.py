"""Streaming execution for one episode: decode, observe, measure, assess, write artifacts (design 12 §4.3).

The only module here that does I/O (decoding, observation files, curves, evidence). Per camera the
clip is decoded twice (tracking, then background with the observed gripper masked out); a pass keeps
only a short window of frames. Observations go to ``observations/<ep>/<cam>.jsonl``, per-frame curves to
``curves/<ep>/<cam>.parquet``, evidence images to ``evidence/<ep>/<cam>/``.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import time
from typing import Any

import numpy as np

from . import capability as CAP
from . import contracts as C
from . import metrics as MX
from . import motion as MO
from . import observations as O
from . import timeline as TL
from . import tracking as TR
from . import template as TP
from . import video as V
from .load import EefSample, declared_point_ids
from .profile import Profile


MAX_EVIDENCE_PER_CAMERA = 9


@dataclasses.dataclass
class RunConfig:
    lerobot_root: str
    seed_root: str | None
    profile: Profile
    out_dir: str | None = None
    evidence_mode: str = "flagged"                 # flagged | all | off
    allowed_mounts: tuple[str, ...] = CAP.DEFAULT_ALLOWED_MOUNTS
    lag_search_s: tuple[float, float] = (-1.0, 1.0)
    interpolation_gap_factor: float = 2.0
    tracker: TR.TrackerConfig = dataclasses.field(default_factory=TR.TrackerConfig)
    template: TP.GripperTemplate | None = None       # automatic anchors (F5.8); a camera with seeds keeps the seeds


@dataclasses.dataclass
class CameraMeasure:
    camera_id: str
    capability: dict
    t: np.ndarray                                  # main timeline of the sample
    fps: float
    batch: O.ObservationBatch | None = None
    obs: dict[str, np.ndarray] = dataclasses.field(default_factory=dict)          # point -> (N,2) on sample frames
    obs_vis: dict[str, np.ndarray] = dataclasses.field(default_factory=dict)
    positions: dict[str, dict] = dataclasses.field(default_factory=dict)
    orientations: dict[str, dict] = dataclasses.field(default_factory=dict)
    lag: dict | None = None
    local_lags: list[dict] = dataclasses.field(default_factory=list)
    hf: dict[str, dict] = dataclasses.field(default_factory=dict)
    background: dict | None = None
    background_hf: dict | None = None
    timing: dict = dataclasses.field(default_factory=dict)
    errors: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class EpisodeMeasure:
    sample: EefSample
    capability: dict
    cameras: dict[str, CameraMeasure]
    l0: dict | None
    timing: dict


def _frames(sample: EefSample, camera_id: str, root: str):
    return O.view_frames(sample, camera_id, root)


def measure_camera(sample: EefSample, camera_id: str, cap: dict, cfg: RunConfig) -> CameraMeasure:
    cam = sample.cameras[camera_id]
    fps = float(cam.media.get("fps") or 0) or 1.0 / TL.median_step(sample.t)
    t = sample.t if sample.t is not None else np.arange(sample.n_frames) / fps
    cm = CameraMeasure(camera_id, cap, t, fps)
    sub = cap["subitems"]
    prof = cfg.profile
    if sub[C.POSITION]["availability"] == C.AVAILABLE:
        seeds = O.find_seeds(cfg.seed_root, sample.sample_id, camera_id)
        ctx, targets = O.provider_inputs(sample, camera_id, media_root=cfg.lerobot_root, seeds=seeds,
                                         point_ids=cap["comparable_points"])
        t0 = time.perf_counter()
        try:
            provider = (TR.TemplateLKProvider(cfg.template, cfg.tracker) if seeds is None and cfg.template is not None
                        else TR.SeededLKProvider(cfg.tracker))
            cm.batch = provider.locate(_frames(sample, camera_id, cfg.lerobot_root), targets, ctx)
        except V.DecodeError as exc:
            cm.errors.append(f"{C.DECODE_FAILED}: {exc}")
        cm.timing["track_s"] = round(time.perf_counter() - t0, 3)
        if cm.batch is not None:
            for pid in cap["comparable_points"]:
                uv, vis, _ = O.on_sample_frames(cm.batch, cam, pid)
                cm.obs[pid], cm.obs_vis[pid] = uv, vis
                cm.positions[pid] = MX.position(sample, camera_id, pid, uv)
                cm.hf[pid] = MX.hf_share(cm.positions[pid]["e_vec"], t, fps)
            for aid in cap["observable_axes"]:
                min_len = prof.orientation["min_axis_len_px"] if prof else MX.DEFAULT_MIN_AXIS_LEN_PX
                cm.orientations[aid] = MX.orientation(sample, camera_id, aid, cm.obs, min_len_px=min_len)
            if sub[C.TEMPORAL]["availability"] == C.AVAILABLE:
                t1 = time.perf_counter()
                pids = cap["comparable_points"]
                decl = [cm.positions[p]["declared_uv"] for p in pids]
                obs = [cm.obs[p] for p in pids]
                cm.lag = MX.lag_scan(t, decl, obs, fps=fps, search_s=cfg.lag_search_s,
                                     gap_factor=cfg.interpolation_gap_factor)
                cm.local_lags = MX.local_lags(t, decl, obs, fps=fps, search_s=cfg.lag_search_s,
                                              gap_factor=cfg.interpolation_gap_factor)
                cm.timing["lag_s"] = round(time.perf_counter() - t1, 3)
    if sub[C.CAMERA_MOTION]["availability"] == C.AVAILABLE:
        exclude: dict[int, list] = {}
        if cm.batch is not None:                    # mask the independently observed gripper, never the projection
            for pid in cm.batch.uv:
                for f in np.flatnonzero(cm.batch.visibility[pid] == O.VISIBLE):
                    exclude.setdefault(int(f), []).append(cm.batch.uv[pid][f])
        t0 = time.perf_counter()
        try:
            n_media = int(cam.media["frame_count"])
            bg = MO.background_motion(_frames(sample, camera_id, cfg.lerobot_root), n_media, exclude=exclude)
            cm.background = _to_sample_frames(bg, cam.video_frame_index)
            cm.background_hf = MO.background_hf(cm.background, fps)
        except V.DecodeError as exc:
            cm.errors.append(f"{C.DECODE_FAILED}: {exc}")
        cm.timing["background_s"] = round(time.perf_counter() - t0, 3)
    return cm


def _to_sample_frames(bg: dict, vf: np.ndarray) -> dict:
    out = {}
    ok = (vf >= 0) & (vf < len(bg["dx_px"]))
    for k, v in bg.items():
        arr = np.full(len(vf), np.nan) if v.dtype.kind == "f" else np.zeros(len(vf), v.dtype)
        arr[ok] = v[vf[ok]]
        out[k] = arr
    return out


def measure_episode(sample: EefSample, cfg: RunConfig) -> EpisodeMeasure:
    t0 = time.perf_counter()
    observable = O.seeded_points(cfg.seed_root, sample) if cfg.seed_root else None
    if cfg.template is not None:                    # a camera with seeds keeps them (human anchors win)
        observable = {**cfg.template.observable_points(sample.cameras), **(observable or {})} or None
    cap = CAP.sample_capability(sample, observable=observable, allowed_mounts=cfg.allowed_mounts)
    cams = {}
    for cid in sample.cameras:
        cams[cid] = measure_camera(sample, cid, cap["cameras"][cid], cfg)
    l0 = None
    if cap["subitems"][C.STATE_MOTION]["availability"] == C.AVAILABLE:
        series = MO.state_series(sample)
        if series is not None and len(series.t) >= 16:
            l0 = MO.l0_metrics(series)
    return EpisodeMeasure(sample, cap, cams, l0, {"measure_s": round(time.perf_counter() - t0, 3)})


def declared_points(sample: EefSample, camera_id: str) -> list[str]:
    return declared_point_ids(sample, camera_id)


def ensure_dir(path: str | os.PathLike) -> pathlib.Path:
    p = pathlib.Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def to_jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): to_jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [to_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return to_jsonable(x.tolist())
    if isinstance(x, (np.floating, float)):
        return None if not np.isfinite(x) else round(float(x), 6)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


# --- assessment, detail and artifacts ----------------------------------------------------------

def config_digest(cfg: RunConfig) -> str:
    """Everything besides the input that changes a result: profile, search range, tracker, mounts."""
    from .profile import config_hash

    return config_hash(cfg.profile, {"lag_search_s": cfg.lag_search_s, "tracker": dataclasses.asdict(cfg.tracker),
                                     "allowed_mounts": cfg.allowed_mounts, "gap_factor": cfg.interpolation_gap_factor,
                                     "module_version": C.MODULE_VERSION})


def assess(measure: EpisodeMeasure, cfg: RunConfig) -> dict:
    """The per-episode ``detail`` payload (design 12 §11.3), statuses by sub-item and camera."""
    from . import assess as AS
    from . import diagnosis as DG

    s = measure.sample
    prof = cfg.profile
    cams: dict[str, dict] = {}
    segments: list[dict] = []
    for cid, cm in measure.cameras.items():
        cap = measure.capability["cameras"][cid]["subitems"]
        cells: dict[str, dict] = {}
        for k in C.CAMERA_SUBITEMS:
            if cap[k]["availability"] != C.AVAILABLE:
                cells[k] = {"status": C.UNSUPPORTED, "reasons": [cap[k]["reason_code"]]}
        if cm.errors:
            for k in (C.POSITION, C.ORIENTATION, C.TEMPORAL, C.CAMERA_MOTION):
                cells.setdefault(k, {"status": C.ERROR, "reasons": [C.DECODE_FAILED], "errors": cm.errors})
        if C.POSITION not in cells:
            cells[C.POSITION], seg = AS.position(cm, s, prof)
            segments += seg
        if C.ORIENTATION not in cells:
            cells[C.ORIENTATION], seg = AS.orientation(cm, s, prof)
            segments += seg
        if C.TEMPORAL not in cells:
            cells[C.TEMPORAL] = AS.temporal(cm, prof)
        if C.CAMERA_MOTION not in cells:
            cells[C.CAMERA_MOTION], seg = AS.camera_motion(cm, prof)
            segments += seg
        if C.INPUT_CONSISTENCY not in cells:
            cells[C.INPUT_CONSISTENCY] = AS.input_consistency(s, cid)
        cam = s.cameras[cid]
        cams[cid] = {"mount": cam.mount, "calibration": measure.capability["cameras"][cid]["calibration"],
                     "compared_points": list(cm.positions), "observed_axes": list(cm.orientations),
                     "observation": _obs_summary(cm), "subitems": cells, "timing": cm.timing}
    state_cell, seg = AS.state_motion(measure.l0, s, prof)
    if measure.capability["subitems"][C.STATE_MOTION]["availability"] != C.AVAILABLE:
        state_cell = {"status": C.UNSUPPORTED,
                      "reasons": [measure.capability["subitems"][C.STATE_MOTION]["reason_code"]]}
    segments += seg
    hypotheses = DG.hypotheses(s, measure.cameras, cams, state_cell, prof, segments)
    summary = AS.episode_summary(cams, state_cell)
    overall = _overall(summary)
    return {
        "schema_version": C.DETAIL_SCHEMA_VERSION, "module_version": C.MODULE_VERSION,
        "assessment_mode": "advisory", "sample_id": s.sample_id, "input_hash": s.input_hash,
        "config_hash": config_digest(cfg),
        "seeds_sha256": O.seeds_digest(cfg.seed_root, s.sample_id),
        "template_sha256": cfg.template.sha256 if cfg.template is not None else None,
        "threshold_profile": prof.summary() if prof else None,
        "uncalibrated": prof is None or not prof.calibrated,
        "overall": overall, "summary": summary,
        "capabilities": {k: v for k, v in measure.capability["subitems"].items()},
        "cameras": cams, "state_motion": state_cell, "segments": segments, "diagnosis": hypotheses,
        "review": {"status": "not_run", "truncated": False},
        "evidence": [], "timing": dict(measure.timing),
    }


def _obs_summary(cm: CameraMeasure) -> dict | None:
    if cm.batch is None:
        return None
    st = dict(cm.batch.stats)
    return {"method": cm.batch.method, "model_version": cm.batch.model_version,
            "seed_method": st.get("seed_method"), "anchors": st.get("anchors"),
            "seed_image_hash_match": f"{st.get('seed_hash_matches')}/{st.get('seed_hash_checked')}",
            "template_sha256": st.get("template_sha256"), "template_methods": st.get("template_methods"),
            "redetections": (f"{st.get('redetections_ok')}/{st.get('redetections_tried')}"
                             if st.get("redetections_tried") is not None else None),
            "not_for_accuracy_acceptance": st.get("seed_method") == "synthetic_fixture"
            or "synthetic_fixture" in (st.get("template_methods") or [])}


def _overall(summary: dict) -> str:
    """§9.3: candidate if any sub-item is suspect; else assessed / partially assessable / not assessable."""
    core = [summary[k]["status"] for k in (C.POSITION, C.ORIENTATION, C.TEMPORAL, C.STATE_MOTION, C.CAMERA_MOTION)]
    if C.SUSPECT in core:
        return "candidate"
    if C.ERROR in core:
        return "error"
    assessed = sum(s == C.OK for s in core)
    if assessed == 0:
        return "not_assessable"
    return "assessed" if all(s in (C.OK, C.UNSUPPORTED) for s in core) else "partially_assessable"


def curves_frame(measure: EpisodeMeasure, camera_id: str):
    """Per-frame curves of one camera (design 12 §11.3 ``curves/<ep>/<cam>.parquet``)."""
    import pandas as pd

    cm = measure.cameras[camera_id]
    s = measure.sample
    cols: dict[str, Any] = {"frame_index": np.arange(s.n_frames), "t_s": cm.t,
                            "video_frame_index": s.cameras[camera_id].video_frame_index}
    for pid, p in cm.positions.items():
        cols[f"e_px:{pid}"] = p["e_px"]
        cols[f"e_mm:{pid}"] = p["e_mm"]
        cols[f"decl_u:{pid}"], cols[f"decl_v:{pid}"] = p["declared_uv"][:, 0], p["declared_uv"][:, 1]
        cols[f"obs_u:{pid}"], cols[f"obs_v:{pid}"] = cm.obs[pid][:, 0], cm.obs[pid][:, 1]
        cols[f"obs_visibility:{pid}"] = cm.obs_vis[pid].astype(str)
    for aid, o in cm.orientations.items():
        cols[f"angle_deg:{aid}"] = o["angle_deg"]
        cols[f"signed_deg:{aid}"] = o["signed_deg"]
    if cm.background is not None:
        cols["bg_dx_px"], cols["bg_dy_px"] = cm.background["dx_px"], cm.background["dy_px"]
        cols["bg_hf_rms_px"] = cm.background_hf["hf_rms_px"]
    if measure.l0 is not None:
        per = measure.l0["per_state"]
        hf = np.full(s.n_frames, np.nan)
        hf[per["frame_index"]] = per["hf_pos_rms_m"] * 1e3
        cols["state_hf_rms_mm"] = hf
    return pd.DataFrame(cols)


def write_artifacts(measure: EpisodeMeasure, detail: dict, cfg: RunConfig) -> dict:
    """observations/, curves/, evidence/ under ``cfg.out_dir``; returns the evidence manifest."""
    if not cfg.out_dir:
        return {}
    import cv2
    import json

    out = pathlib.Path(cfg.out_dir)
    ep = f"{measure.sample.episode_index:06d}"
    evidence = []
    for cid, cm in measure.cameras.items():
        if cm.batch is not None:
            O.write_rows(out / "observations" / ep / f"{cid}.jsonl", O.observation_rows(cm.batch, measure.sample, cid))
        df = curves_frame(measure, cid)
        ensure_dir(out / "curves" / ep)
        df.to_parquet(out / "curves" / ep / f"{cid}.parquet", index=False)
        if cm.lag is not None:
            (out / "curves" / ep / f"{cid}.lag.json").write_text(json.dumps(
                {"taus_s": to_jsonable(cm.lag["taus_s"]), "residual_px": to_jsonable(cm.lag["residual_px"]),
                 "local": cm.local_lags}))
        if cfg.evidence_mode == "off":
            continue
        frames = set()
        for sg in detail["segments"]:
            if sg.get("camera_id") == cid:
                frames |= set(sg.get("evidence_frames", []))
        cam_cells = detail["cameras"][cid]["subitems"]
        flagged = any(c.get("status") == C.SUSPECT for c in cam_cells.values())
        if cfg.evidence_mode == "all" or (flagged and not frames):
            stack = np.stack([p["e_px"] for p in cm.positions.values()]) if cm.positions else None
            e = None if stack is None else np.where(np.isfinite(stack).any(0), np.nanmax(np.nan_to_num(stack, nan=-1.0), 0),
                                                    np.nan)
            if e is not None and np.isfinite(e).any():
                frames |= set(int(i) for i in np.argsort(-np.nan_to_num(e, nan=-1))[:3])
        if not frames or (cfg.evidence_mode == "flagged" and not flagged):
            continue
        frames = set(sorted(frames)[:: max(1, len(frames) // MAX_EVIDENCE_PER_CAMERA + 1)][:MAX_EVIDENCE_PER_CAMERA])
        vf = measure.sample.cameras[cid].video_frame_index
        want = {int(vf[f]): f for f in frames if vf[f] >= 0}
        d = ensure_dir(out / "evidence" / ep / cid)
        for fr in _frames(measure.sample, cid, cfg.lerobot_root):
            if fr.index not in want:
                continue
            f = want[fr.index]
            img = fr.bgr().copy()
            for pid, p in cm.positions.items():
                du = p["declared_uv"][f]
                ou = cm.obs[pid][f]
                if np.isfinite(du).all():
                    cv2.circle(img, (int(round(du[0])), int(round(du[1]))), 7, (0, 0, 255), 2)        # declared: red
                if np.isfinite(ou).all():
                    cv2.drawMarker(img, (int(round(ou[0])), int(round(ou[1]))), (0, 255, 0), cv2.MARKER_CROSS, 14, 2)
            name = f"frame_{f:06d}.jpg"
            cv2.imwrite(str(d / name), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
            evidence.append({"camera_id": cid, "frame_index": f, "path": f"evidence/{ep}/{cid}/{name}",
                             "legend": {"declared": "red circle", "observed": "green cross"}})
    return {"evidence": evidence}


def run_episode(sample: EefSample, cfg: RunConfig) -> tuple[dict, EpisodeMeasure]:
    measure = measure_episode(sample, cfg)
    detail = assess(measure, cfg)
    t0 = time.perf_counter()
    detail["evidence"] = write_artifacts(measure, detail, cfg).get("evidence", [])
    detail["timing"]["artifacts_s"] = round(time.perf_counter() - t0, 3)
    return to_jsonable(detail), measure
