"""F5.2 report: the P-A provider on every DEMO episode and camera (design 12 §13.2).

Per episode x camera: coverage (visible / requested), abstention (uncertain), occluded / out-of-frame,
localisation error against the dense evaluation truth, the seed image-hash agreement and the time taken;
plus the independence experiment on real data (the declared projection moved by 30 px, observations
must not move). The seeds and the truth are ``synthetic_fixture``: these numbers describe the tracker on
DEMO data and are **not** a visual-accuracy acceptance (design 12 §0.6-2).

    PYTHONPATH=backend:tools .venv/bin/python -m eef_eval.observation_report --out tools/eef_eval/reports/observations.json
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys
import time

import numpy as np

from curation.extensions.eef_consistency import load
from curation.extensions.eef_consistency import observations as O
from curation.extensions.eef_consistency import tracking as T
from curation.extensions.eef_consistency import video as V

from . import truth


def locate(sample, cam, root, seeds):
    ctx, targets = O.provider_inputs(sample, cam, media_root=root, seeds=seeds)
    frames = V.iter_clip(ctx.media_path, clip_start_s=ctx.clip_start_s, clip_end_s=ctx.clip_end_s, fps=ctx.fps,
                         frame_count=ctx.media_frame_count)
    return T.SeededLKProvider().locate(frames, targets, ctx)


def camera_row(name, sample, cam_id):
    root = truth.lerobot_root(name)
    seeds = O.find_seeds(truth.seed_root(name), sample.sample_id, cam_id)
    t0 = time.perf_counter()
    batch = locate(sample, cam_id, root, seeds)
    elapsed = time.perf_counter() - t0
    cam = sample.cameras[cam_id]
    tp = truth.truth_pixels(name, sample.sample_id, cam_id, sample.n_frames)
    n = sample.n_frames
    pts = {}
    for pid in batch.uv:
        uv, vis, _ = O.on_sample_frames(batch, cam, pid)
        err = np.linalg.norm(uv - tp[pid], axis=1)
        ok = np.isfinite(err)
        pts[pid] = {"visible": round(float((vis == O.VISIBLE).mean()), 3),
                    "uncertain": round(float((vis == O.UNCERTAIN).mean()), 3),
                    "occluded_or_out": round(float(np.isin(vis, [O.OCCLUDED, O.OUT_OF_FRAME]).mean()), 3),
                    "err_p50_px": round(float(np.median(err[ok])), 2) if ok.any() else None,
                    "err_p95_px": round(float(np.percentile(err[ok], 95)), 2) if ok.any() else None,
                    "err_max_px": round(float(err[ok].max()), 2) if ok.any() else None}
    return {"dataset": name, "episode_index": sample.episode_index, "camera_id": cam_id, "frames": n,
            "anchors": batch.stats["anchors"],
            "seed_image_hash_match": f"{batch.stats['seed_hash_matches']}/{batch.stats['seed_hash_checked']}",
            "elapsed_s": round(elapsed, 2), "points": pts, "batch": batch}


def perturbation(name, entry_payload, sample, cam_id):
    """Move every provided projection point by +30 px in u and rerun: observations must be identical."""
    moved = copy.deepcopy(entry_payload)
    for f in moved["frames"]:
        cf = f["cameras"].get(cam_id)
        if cf and cf["projection"]:
            w = cf["image_size_wh"][0]
            for p in cf["projection"]["points"].values():
                if p["uv_px"] is not None:
                    p["uv_px"] = [p["uv_px"][0] + 30.0, p["uv_px"][1]]
                    if p["status"] in ("valid", "out_of_frame"):
                        p["in_frame"] = bool(0 <= p["uv_px"][0] < w and 0 <= p["uv_px"][1] < cf["image_size_wh"][1])
                        p["status"] = "valid" if p["in_frame"] else "out_of_frame"
    bundle = {"schema_version": "eef-video/1.0.0", "container": "trajectory-bundle/1.0",
              "dataset": {"id": name, "lerobot_codebase_version": "n/a", "fps": 15.0, "episode_count": 1},
              "media_uri_base": "lerobot_root", "samples": [moved]}
    r = load.load_bundle(bundle, check_media=False)
    s2 = r.samples[sample.episode_index]
    seeds = O.find_seeds(truth.seed_root(name), sample.sample_id, cam_id)
    a = locate(sample, cam_id, truth.lerobot_root(name), seeds)
    b = locate(s2, cam_id, truth.lerobot_root(name), seeds)
    same = all(np.array_equal(a.uv[p], b.uv[p], equal_nan=True) and np.array_equal(a.visibility[p], b.visibility[p])
               for p in a.uv)
    shifted = float(np.nanmedian(s2.cameras[cam_id].provided[next(iter(a.uv))].uv[:, 0]
                                 - sample.cameras[cam_id].provided[next(iter(a.uv))].uv[:, 0]))
    return {"dataset": name, "episode_index": sample.episode_index, "camera_id": cam_id,
            "projection_shift_px": round(shifted, 3), "observations_identical": bool(same)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="tools/eef_eval/reports/observations.json")
    ap.add_argument("--datasets", nargs="*", default=sorted(truth.DATASETS))
    a = ap.parse_args(argv)
    rows, perturb = [], []
    for name in a.datasets:
        path = truth.dataset_dir(name) / "trajectory.json"
        payload = json.loads(path.read_text())
        r = load.load_bundle(path, lerobot_root=truth.lerobot_root(name))
        assert r.ok, r.errors[:3]
        for ep, s in sorted(r.samples.items()):
            for cam_id in s.cameras:
                row = camera_row(name, s, cam_id)
                row.pop("batch")
                rows.append(row)
                print(f"{name} ep{ep} {cam_id}: " + " ".join(
                    f"{p}={v['visible']:.2f}/{v['err_p95_px']}" for p, v in row["points"].items()), file=sys.stderr)
        entry = next(e for e in payload["samples"] if e["episode_index"] == 0)
        s0 = r.samples[0]
        perturb.append(perturbation(name, entry, s0, next(iter(s0.cameras))))
    vis = [v["visible"] for r_ in rows for v in r_["points"].values()]
    p95 = [v["err_p95_px"] for r_ in rows for v in r_["points"].values() if v["err_p95_px"] is not None]
    report = {"schema_version": "eef-observation-report/1.0", "provider": T.MODEL_VERSION,
              "seeds": "synthetic_fixture (DEMO only, not an accuracy acceptance)", "rows": rows,
              "summary": {"cameras": len(rows), "visible_median": float(np.median(vis)),
                          "visible_min": float(np.min(vis)), "err_p95_median_px": float(np.median(p95)),
                          "err_p95_max_px": float(np.max(p95)),
                          "seconds_total": round(sum(r_["elapsed_s"] for r_ in rows), 1)},
              "independence": perturb}
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(report["summary"], indent=1))
    print(json.dumps(perturb, indent=1))


if __name__ == "__main__":
    main()
