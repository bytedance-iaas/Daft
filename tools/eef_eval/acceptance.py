"""F5.7: acceptance of the frozen ``demo`` profile on held-out variants (design 12 §13.3 supplementary scan).

``dataset3`` is built for this and nothing else: another seed and a magnitude sweep of the six fault kinds
(``galbot/dataset3/sweep_recipes.json``), from the same raw DROID episode as dataset2 - so it is held out
from the thresholds (set on the two baselines of dataset1 / dataset2 at profile 0.2 and not touched since)
but it is the same scene: a detection-limit scan, not a generalisation test across robots or scenes.

Every episode runs in a fresh subprocess (its peak memory and CPU time are its own): the module with the
frozen profile, then the VLM review's windows against a stand-in model that answers every request (the
request count is what a real backend would receive). Only then does this script read the truth
(``corruptions.json``, dense ``evaluation/truth_pixels``) and score, per group (fault kind x magnitude):

* detection - the expected sub-item is ``suspect`` (on the affected camera when the fault has one);
* false alarms - a core sub-item ``suspect`` that the fault does not explain (``ALLOWED``; baseline: any);
* localization - the independent observation against the true pixels (P95 px), coverage and abstention
  (share of ``unknown`` among the camera sub-items that could be assessed);
* fitted vs injected - constant rotation angle, extrinsics translation / rotation, time offset;
* cost - CPU seconds per minute of video and camera, peak memory, VLM requests per episode.

    PYTHONPATH=backend:tools .venv/bin/python -m eef_eval.acceptance [--dataset dataset3] [--jobs 4]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import pathlib
import resource
import subprocess
import sys
import time

import numpy as np

from . import truth

CORE = ("position_2d", "orientation_2d", "temporal_alignment", "state_motion", "camera_motion")
EXPECTED = {"traj_drift": "position_2d", "gripper_orient": "orientation_2d", "traj_jitter": "state_motion",
            "video_shake": "camera_motion", "sync_offset": "temporal_alignment", "calib_offset": "position_2d"}
#: sub-items a fault also moves (physically implied), so they are not false alarms
ALLOWED = {"baseline": set(), "traj_drift": {"orientation_2d"}, "gripper_orient": {"position_2d"},
           "traj_jitter": {"position_2d", "orientation_2d"}, "video_shake": {"position_2d"},
           "sync_offset": {"position_2d", "orientation_2d"}, "calib_offset": {"orientation_2d"}}
CAMERA_KEY = {"exterior_1_left": "27432424_left", "exterior_2_left": "28221883_left"}
MAGNITUDE = {"traj_drift": ("pos_peak_m", "m"), "gripper_orient": ("angle_deg", "deg"),
             "traj_jitter": ("pos_sigma_m", "m"), "video_shake": ("peak_px", "px"),
             "sync_offset": ("delta_frames", "frames"), "calib_offset": ("shift_m", "m")}
ANSWER = {"review_status": "uncertain", "target_visible": True, "tracking_target_correct": "support",
          "position_support": "uncertain", "orientation_support": "uncertain",
          "offset_direction": "unclear", "offset_magnitude_class": "unclear", "evidence_frame_ids": [],
          "reason_codes": [], "explanation": "stand-in"}


def _peak_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(rss / 1024 / 1024 if sys.platform == "darwin" else rss / 1024, 1)


def worker(dataset: str, ep: int, out: str) -> dict:
    """One episode, in its own process: module + review windows, no truth."""
    from curation.cli.eef_review import review_episode
    from curation.extensions.eef_consistency import load, profile, runner
    from curation.extensions.eef_consistency import review as R
    from curation.pipeline.records import module_dir

    r = load.load_bundle(truth.dataset_dir(dataset) / "trajectory.json", lerobot_root=truth.lerobot_root(dataset),
                         episodes=[ep])
    assert r.ok, r.errors[:3]
    s = r.samples[ep]
    run_dir = str(pathlib.Path(out) / "run" / dataset)
    cfg = runner.RunConfig(lerobot_root=str(truth.lerobot_root(dataset)), seed_root=str(truth.seed_root(dataset)),
                           profile=profile.load("demo"), out_dir=module_dir(run_dir, "eef_video_consistency"),
                           evidence_mode="off")
    u0 = resource.getrusage(resource.RUSAGE_SELF)
    t0 = time.perf_counter()
    detail, _ = runner.run_episode(s, cfg)
    module_wall = time.perf_counter() - t0
    u1 = resource.getrusage(resource.RUSAGE_SELF)
    calls = []

    def ask(req, history):
        calls.append(len(req.images))
        return json.dumps({**ANSWER, "evidence_frame_ids": req.frame_ids[:1]})

    review = review_episode(s, {"details": detail, "verdict": "abstain"}, run_dir=run_dir,
                            media_root=str(truth.lerobot_root(dataset)), ask=ask, cache=R.Cache(None), model="stand-in",
                            per_camera=3, frames_per_window=6, out_dir=module_dir(run_dir, "eef_video_review"))
    u2 = resource.getrusage(resource.RUSAGE_SELF)
    cams = len(s.cameras)
    return {"episode_index": ep, "n_frames": s.n_frames, "detail": detail, "review_summary": review["summary"],
            "video_s": round(s.n_frames / float(next(iter(s.cameras.values())).media.get("fps") or 15.0), 3),
            "cameras": cams, "module_cpu_s": round(u1.ru_utime + u1.ru_stime - u0.ru_utime - u0.ru_stime, 2),
            "review_cpu_s": round(u2.ru_utime + u2.ru_stime - u1.ru_utime - u1.ru_stime, 2),
            "module_wall_s": round(module_wall, 2), "wall_s": round(time.perf_counter() - t0, 2),
            "peak_rss_mb": _peak_mb(), "vlm_requests": len(calls),
            "vlm_images": sum(calls)}


def _run_one(dataset: str, ep: int, out: str) -> dict:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(["backend", "tools", os.environ.get("PYTHONPATH", "")])}
    p = subprocess.run([sys.executable, "-m", "eef_eval.acceptance", "--worker", str(ep), "--dataset", dataset,
                        "--out", out], capture_output=True, text=True, env=env, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"episode {ep}: {p.stderr[-2000:]}")
    return json.loads(p.stdout.strip().splitlines()[-1])


# ----------------------------------------------------------------------------------- scoring (truth)


def _magnitude(kind: str, params: dict):
    if kind not in MAGNITUDE:
        return None
    key, unit = MAGNITUDE[kind]
    v = params.get(key)
    return (v[0] if isinstance(v, list) else v), unit


def _cams(detail: dict, sub: str) -> dict[str, str]:
    return {c: d["subitems"][sub]["status"] for c, d in detail["cameras"].items() if sub in d["subitems"]}


def _localization(dataset: str, detail: dict, sample_id: str, n: int, run_dir: pathlib.Path, ep: int) -> dict:
    import pandas as pd

    errs, cov = [], []
    for cid in detail["cameras"]:
        path = run_dir / "checks" / "eef_video_consistency" / "curves" / f"{ep:06d}" / f"{cid}.parquet"
        if not path.is_file():
            continue
        df = pd.read_parquet(path)
        try:
            tp = truth.truth_pixels(dataset, sample_id, cid, n)
        except FileNotFoundError:
            continue
        for pid, t in tp.items():
            if f"obs_u:{pid}" not in df:
                continue
            o = np.stack([df[f"obs_u:{pid}"].to_numpy(float), df[f"obs_v:{pid}"].to_numpy(float)], 1)
            has_t = np.isfinite(t).all(1)
            both = has_t & np.isfinite(o).all(1)
            if has_t.any():
                cov.append(both.sum() / has_t.sum())
            if both.any():
                errs.append(float(np.percentile(np.linalg.norm(o[both] - t[both], axis=1), 95)))
    return {"p95_px_median": round(float(np.median(errs)), 2) if errs else None,
            "p95_px_max": round(float(max(errs)), 2) if errs else None,
            "coverage_median": round(float(np.median(cov)), 3) if cov else None}


def _fitted(kind: str, detail: dict, fault: dict) -> dict | None:
    hyp = {"gripper_orient": "constant_orientation_error", "calib_offset": "extrinsics_error",
           "sync_offset": "time_offset"}.get(kind)
    if hyp is None:
        return None
    got = [h for h in detail["diagnosis"] if h["hypothesis"] == hyp and h["supported"]]
    f = got[0]["fitted"] if got else None
    p = fault["params"]
    if kind == "gripper_orient":
        return {"injected_deg": p["angle_deg"], "fitted_deg": f and f.get("delta_rotation_deg")}
    if kind == "calib_offset":
        return {"injected_mm": p["shift_m"][0] * 1e3, "injected_deg": p["rot_deg"][1],
                "fitted_mm": f and f.get("delta_translation_mm"), "fitted_deg": f and f.get("delta_rotation_deg")}
    temporal = [c["subitems"]["temporal_alignment"].get("metrics", {}).get("lag_s")
                for c in detail["cameras"].values() if "temporal_alignment" in c["subitems"]]
    # the record lags the video by delta frames: u_visual(t) = u_declared(t + lag), lag = +delta / fps
    return {"injected_s": round(p["delta_frames"] / 15.0, 3), "fitted_s": (f or {}).get("lag_s"),
            "camera_lag_s": temporal}


def score(dataset: str, rows: list[dict], out: pathlib.Path) -> dict:
    faults = truth.faults(dataset)                   # the truth, read after every detector run
    episodes = []
    for r in rows:
        d, ep = r["detail"], r["episode_index"]
        f = faults[ep]
        kind = f["kind"]
        expected = EXPECTED.get(kind)
        cam = CAMERA_KEY.get(f.get("camera") or "")
        if expected is None:
            detected = None
        elif cam:
            detected = _cams(d, expected).get(cam) == "suspect"
        else:
            detected = d["summary"][expected]["status"] == "suspect"
        suspects = sorted(k for k in CORE if d["summary"][k]["status"] == "suspect")
        false_alarms = sorted(set(suspects) - {expected} - ALLOWED[kind])
        if cam and expected:                         # the fault's camera only: the other one should stay quiet
            other = [c for c in d["cameras"] if c != cam]
            false_alarms += [f"{expected}@{c}" for c in other if _cams(d, expected).get(c) == "suspect"]
        cells = [(k, c["subitems"][k]["status"]) for c in d["cameras"].values() for k in CORE if k in c["subitems"]]
        judged = [s for _, s in cells if s != "unsupported"]
        abstain = round(sum(s == "unknown" for s in judged) / len(judged), 3) if judged else None
        loc = _localization(dataset, d, d["sample_id"], r["n_frames"], out / "run" / dataset, ep)
        mag = _magnitude(kind, f["params"])
        episodes.append({
            "episode_index": ep, "kind": kind, "magnitude": mag[0] if mag else None, "unit": mag[1] if mag else None,
            "expected": expected, "detected": detected, "suspects": suspects, "false_alarms": false_alarms,
            "abstention": abstain, "localization": loc, "fitted": _fitted(kind, d, f),
            "overall": d["overall"],
            "cost": {"cpu_s_per_video_min_per_camera": round(r["module_cpu_s"] / (r["video_s"] / 60.0) / r["cameras"], 1),
                     "wall_s_per_video_min_per_camera": round(r["module_wall_s"] / (r["video_s"] / 60.0) / r["cameras"], 1),
                     "module_cpu_s": r["module_cpu_s"], "review_cpu_s": r["review_cpu_s"],
                     "peak_rss_mb": r["peak_rss_mb"], "vlm_requests": r["vlm_requests"],
                     "vlm_images": r["vlm_images"], "video_s": r["video_s"]}})
    groups: dict[str, dict] = {}
    for e in episodes:
        g = groups.setdefault(e["kind"], {"episodes": 0, "detected": 0, "false_alarm_episodes": 0, "by_magnitude": []})
        g["episodes"] += 1
        g["detected"] += bool(e["detected"])
        g["false_alarm_episodes"] += bool(e["false_alarms"])
        g["by_magnitude"].append({"magnitude": e["magnitude"], "unit": e["unit"], "detected": e["detected"],
                                  "false_alarms": e["false_alarms"], "episode_index": e["episode_index"]})
    for kind, g in groups.items():
        hits = sorted(abs(x["magnitude"]) for x in g["by_magnitude"] if x["detected"] and x["magnitude"] is not None)
        misses = sorted(abs(x["magnitude"]) for x in g["by_magnitude"] if x["detected"] is False and x["magnitude"] is not None)
        g["smallest_detected"] = hits[0] if hits else None
        g["largest_missed"] = misses[-1] if misses else None
        g["by_magnitude"].sort(key=lambda x: (x["magnitude"] is None, abs(x["magnitude"] or 0)))
    cost = [e["cost"] for e in episodes]

    def q(key, fn=np.median):
        vals = [c[key] for c in cost if c[key] is not None]
        return round(float(fn(vals)), 1) if vals else None

    locs = [e["localization"]["p95_px_median"] for e in episodes if e["localization"]["p95_px_median"] is not None]
    return {"episodes": episodes, "groups": groups,
            "totals": {"episodes": len(episodes),
                       "faulty": sum(e["expected"] is not None for e in episodes),
                       "detected": sum(bool(e["detected"]) for e in episodes),
                       "false_alarm_episodes": sum(bool(e["false_alarms"]) for e in episodes),
                       "baseline_false_alarms": sum(bool(e["false_alarms"]) for e in episodes if e["kind"] == "baseline"),
                       "abstention_median": round(float(np.median([e["abstention"] for e in episodes
                                                                   if e["abstention"] is not None])), 3),
                       "localization_p95_px_median": round(float(np.median(locs)), 2) if locs else None,
                       "cpu_s_per_video_min_per_camera_median": q("cpu_s_per_video_min_per_camera"),
                       "cpu_s_per_video_min_per_camera_max": q("cpu_s_per_video_min_per_camera", max),
                       "wall_s_per_video_min_per_camera_median": q("wall_s_per_video_min_per_camera"),
                       "peak_rss_mb_max": q("peak_rss_mb", max),
                       "vlm_requests_per_episode_median": q("vlm_requests"),
                       "vlm_requests_per_episode_max": q("vlm_requests", max)}}


def markdown(rep: dict) -> str:
    t = rep["totals"]
    lines = ["# EEF–视频一致性 · 留出集验收（F5.7）", "",
             f"profile `{rep['profile']['name']} {rep['profile']['version']}`（未校准，定数后没动过），数据集 `{rep['dataset']}`："
             "换了随机种子、按幅度扫描的留出变体，没参与定阈值；但和 dataset2 出自同一条原始 DROID episode，"
             "是同一场景上的检出下限扫描，不是跨场景的泛化验收。", "",
             f"- {t['episodes']} 条（有故障 {t['faulty']} 条，检出 {t['detected']} 条）；出现误报的条数 {t['false_alarm_episodes']}"
             f"（基准条上 {t['baseline_false_alarms']}）；弃权率中位数 {t['abstention_median']}；"
             f"独立观测对真值 P95 误差中位数 {t['localization_p95_px_median']} px",
             f"- 成本：每分钟视频每路相机 CPU {t['cpu_s_per_video_min_per_camera_median']} 秒（用户 + 系统态、含 OpenCV 线程，最多 "
             f"{t['cpu_s_per_video_min_per_camera_max']}；墙钟 {t['wall_s_per_video_min_per_camera_median']} 秒，{rep['jobs']} 路并行），"
             f"峰值内存最多 {t['peak_rss_mb_max']} MB，VLM 复核每条请求数中位数 "
             f"{t['vlm_requests_per_episode_median']}（最多 {t['vlm_requests_per_episode_max']}）", "",
             "| 故障 | 条数 | 检出 | 有误报 | 检出的最小幅度 | 漏检的最大幅度 | 逐幅度（✓ 检出 / ✗ 漏检，括号里是误报） |",
             "|---|---|---|---|---|---|---|"]
    for kind, g in sorted(rep["groups"].items()):
        cells = []
        for x in g["by_magnitude"]:
            mark = "—" if x["detected"] is None else ("✓" if x["detected"] else "✗")
            fa = f"（{'、'.join(x['false_alarms'])}）" if x["false_alarms"] else ""
            cells.append(f"{x['magnitude'] if x['magnitude'] is not None else ''}{x['unit'] or ''} {mark}{fa}")
        unit = g["by_magnitude"][0]["unit"] or ""
        small = "" if g["smallest_detected"] is None else f"{g['smallest_detected']} {unit}"
        large = "" if g["largest_missed"] is None else f"{g['largest_missed']} {unit}"
        lines.append(f"| {kind} | {g['episodes']} | {g['detected']} | {g['false_alarm_episodes']} | {small} | {large} | "
                     f"{'；'.join(cells)} |")
    lines += ["", "拟合值与注入值（被支持的诊断）：", ""]
    for e in rep["episodes"]:
        if e["fitted"]:
            lines.append(f"- ep{e['episode_index']} {e['kind']} {e['magnitude']}{e['unit']}：{json.dumps(e['fitted'], ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="dataset3")
    ap.add_argument("--out", default="tools/eef_eval/reports")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--worker", type=int, default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.worker is not None:
        print(json.dumps(worker(a.dataset, a.worker, a.out), default=str))
        return 0
    from curation.extensions.eef_consistency import load, profile

    r = load.load_bundle(truth.dataset_dir(a.dataset) / "trajectory.json", lerobot_root=truth.lerobot_root(a.dataset))
    assert r.ok, r.errors[:3]
    out = pathlib.Path(a.out)
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as pool:
        futs = {pool.submit(_run_one, a.dataset, ep, str(out)): ep for ep in sorted(r.samples)}
        rows = []
        for fut in cf.as_completed(futs):
            rows.append(fut.result())
            print(f"ep{futs[fut]} done ({len(rows)}/{len(futs)})", file=sys.stderr, flush=True)
    rows.sort(key=lambda x: x["episode_index"])
    rep = {"schema_version": "eef-acceptance-report/1.0", "dataset": a.dataset,
           "profile": profile.load("demo").summary(),
           "note": "held out from the thresholds (another seed, a magnitude sweep) but built from the same raw DROID "
                   "episode as dataset2: a detection-limit scan on one scene, not a generalisation test",
           "jobs": a.jobs, "seconds": round(time.perf_counter() - t0, 1), **score(a.dataset, rows, out)}
    out.mkdir(parents=True, exist_ok=True)
    (out / "acceptance.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str) + "\n")
    (out / "acceptance.md").write_text(markdown(rep), encoding="utf-8")
    print(json.dumps(rep["totals"], indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
