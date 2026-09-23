"""Design 12 §13.3: the controlled-fault matrix on the two DEMO datasets (18 episodes).

Runs the module offline (``runner.run_episode`` with the ``demo`` profile), then - and only here - reads
the injected fault of every episode from the evaluation truth and checks each matrix cell. The matrix
is a functional regression on same-source synthetic variants, not a generalisation accuracy.

    PYTHONPATH=backend:tools .venv/bin/python -m eef_eval.matrix [--out tools/eef_eval/reports]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from curation.extensions.eef_consistency import load, profile, runner

from . import truth

EXPECTED_LAG_FRAMES = {("dataset1", 9): -3.0, ("dataset1", 10): -8.0, ("dataset2", 5): 5.0}
KIND = {  # source fault kind -> matrix row
    "baseline": "baseline", "trajectory_drift": "drift", "traj_drift": "drift",
    "orientation": "orientation", "gripper_orient": "orientation",
    "trajectory_jitter": "state_jitter", "traj_jitter": "state_jitter",
    "video_jitter": "video_jitter", "video_shake": "video_jitter",
    "video_lag": "lag", "sync_offset": "lag", "calib_offset": "extrinsics",
}
ALLOWED = {  # §13.3 "应出的诊断": nothing else may be supported
    "baseline": set(), "drift": {"pose_drift_or_kinematics"}, "orientation": {"constant_orientation_error"},
    "state_jitter": {"jitter_source=record"}, "video_jitter": {"jitter_source=video"}, "lag": {"time_offset"},
    "extrinsics": {"extrinsics_error"},
}
CAMERA_KEY = {"exterior_1_left": "27432424_left", "exterior_2_left": "28221883_left"}


def _st(detail, sub):
    return detail["summary"][sub]["status"]


def _cam(detail, cam, sub):
    return detail["cameras"][cam]["subitems"][sub]["status"]


def _hyp(detail, kind, cam=None, **match):
    out = [h for h in detail["diagnosis"] if h["hypothesis"] == kind and h["supported"]
           and (cam is None or h["camera_id"] == cam)]
    for k, v in match.items():
        out = [h for h in out if h.get("fitted", {}).get(k) == v]
    return out


def check(name: str, ep: int, fault: dict, d: dict) -> list[dict]:
    """Matrix cells for one episode: (cell, expected, actual, ok)."""
    row = KIND[fault["kind"]]
    cells = []

    def cell(what, expected, actual, ok):
        cells.append({"cell": what, "expected": expected, "actual": actual, "ok": bool(ok)})

    cams = list(d["cameras"])
    if row == "baseline":
        for sub in ("position_2d", "orientation_2d", "temporal_alignment", "state_motion", "camera_motion"):
            cell(sub, "ok", _st(d, sub), _st(d, sub) == "ok")
        cov = [p["coverage"]["coverage"] for c in d["cameras"].values()
               for p in c["subitems"]["position_2d"].get("points", {}).values()]
        cell("position coverage reported", "> 0", min(cov) if cov else None, bool(cov) and min(cov) > 0)
        cell("no supported hypothesis", "[]", [h["hypothesis"] for h in d["diagnosis"] if h["supported"]],
             not any(h["supported"] for h in d["diagnosis"]))
    if row == "drift":
        cell("position_2d", "suspect", _st(d, "position_2d"), _st(d, "position_2d") == "suspect")
        for sub in ("temporal_alignment", "state_motion", "camera_motion"):
            cell(sub, "ok", _st(d, sub), _st(d, sub) == "ok")
        cell("diagnosis", "pose_drift_or_kinematics", bool(_hyp(d, "pose_drift_or_kinematics")),
             bool(_hyp(d, "pose_drift_or_kinematics")))
    if row == "orientation":
        cell("orientation_2d", "suspect", _st(d, "orientation_2d"), _st(d, "orientation_2d") == "suspect")
        for sub in ("temporal_alignment", "state_motion", "camera_motion"):
            cell(sub, "ok", _st(d, sub), _st(d, sub) == "ok")
        cell("diagnosis", "constant_orientation_error", bool(_hyp(d, "constant_orientation_error")),
             bool(_hyp(d, "constant_orientation_error")))
    if row == "state_jitter":
        cell("state_motion", "suspect", _st(d, "state_motion"), _st(d, "state_motion") == "suspect")
        for sub in ("temporal_alignment", "camera_motion"):
            cell(sub, "ok", _st(d, sub), _st(d, sub) == "ok")
        cell("diagnosis", "jitter_source=record", bool(_hyp(d, "jitter_source", source="record")),
             bool(_hyp(d, "jitter_source", source="record")))
    if row == "video_jitter":
        shaken = [CAMERA_KEY[fault["camera"]]] if fault.get("camera") else cams
        for cam in cams:
            exp = "suspect" if cam in shaken else "ok"
            act = _cam(d, cam, "camera_motion")
            cell(f"camera_motion[{cam}]", exp, act, act == exp)
        for sub in ("state_motion", "temporal_alignment"):
            cell(sub, "ok", _st(d, sub), _st(d, sub) == "ok")
        ok = all(_hyp(d, "jitter_source", cam, source="video") for cam in shaken)
        cell("diagnosis", "jitter_source=video on the shaken camera", ok, ok)
    if row == "lag":
        exp = EXPECTED_LAG_FRAMES[(name, ep)]
        cell("temporal_alignment", "suspect", _st(d, "temporal_alignment"), _st(d, "temporal_alignment") == "suspect")
        for cam in cams:
            m = d["cameras"][cam]["subitems"]["temporal_alignment"].get("metrics", {})
            lag = m.get("lag_frames")
            cell(f"lag_frames[{cam}]", f"{exp:+.0f} (+/-0.5), {exp / 15:+.3f} s", lag,
                 lag is not None and abs(lag - exp) <= 0.5)
        for sub in ("state_motion", "camera_motion"):
            cell(sub, "ok", _st(d, sub), _st(d, sub) == "ok")
        h = _hyp(d, "time_offset")
        cell("diagnosis", "time_offset with the right sign", [x["fitted"]["lag_s"] for x in h],
             bool(h) and all((x["fitted"]["lag_s"] > 0) == (exp > 0) for x in h))
    if row == "extrinsics":
        bad = CAMERA_KEY[fault["camera"]]
        for cam in cams:
            exp = "suspect" if cam == bad else "ok"
            act = _cam(d, cam, "position_2d")
            cell(f"position_2d[{cam}]", exp, act, act == exp)
        for sub in ("temporal_alignment", "state_motion", "camera_motion"):
            cell(sub, "ok", _st(d, sub), _st(d, sub) == "ok")
        h = _hyp(d, "extrinsics_error", bad)
        fit = h[0]["fitted"] if h else None
        cell(f"diagnosis extrinsics_error[{bad}]", "~30 mm, ~2 deg", fit,
             bool(fit) and 20 <= fit["delta_translation_mm"] <= 40 and 1.0 <= fit["delta_rotation_deg"] <= 3.0)
        cell("no extrinsics hypothesis on the good camera", "none", len(_hyp(d, "extrinsics_error",
             next(c for c in cams if c != bad))), not _hyp(d, "extrinsics_error", next(c for c in cams if c != bad)))
    allowed = ALLOWED[row]
    got = sorted({h["hypothesis"] if h["hypothesis"] != "jitter_source" else f"jitter_source={h['fitted']['source']}"
                  for h in d["diagnosis"] if h["supported"]})
    cell("no unexpected supported hypothesis", sorted(allowed) or "none", got, set(got) <= allowed)
    return cells


def heavier(results: dict) -> list[dict]:
    """§13.3 '重档 > 轻档' on dataset1's paired light / heavy variants."""
    def med(ep, sub, key):
        d = results[("dataset1", ep)]
        vals = []
        for c in d["cameras"].values():
            cellv = c["subitems"][sub]
            for child in list(cellv.get("points", {}).values()) + list(cellv.get("axes", {}).values()):
                v = child.get("metrics", {}).get(key)
                if v is not None:
                    vals.append(v)
            v = cellv.get("metrics", {}).get(key)
            if v is not None:
                vals.append(v)
        return max(vals) if vals else None

    pairs = [("drift", 1, 2, "position_2d", "median_px"), ("orientation", 3, 4, "orientation_2d", "median_deg"),
             ("video jitter", 7, 8, "camera_motion", "hf_rms_max_px")]
    out = []
    for label, lo, hi, sub, key in pairs:
        a, b = med(lo, sub, key), med(hi, sub, key)
        out.append({"cell": f"heavy > light: {label} (ep{hi} vs ep{lo}, {key})", "expected": "heavy > light",
                    "actual": [a, b], "ok": a is not None and b is not None and b > a})
    s5 = results[("dataset1", 5)]["state_motion"]["metrics"]["hf_pos_rms_max_mm"]
    s6 = results[("dataset1", 6)]["state_motion"]["metrics"]["hf_pos_rms_max_mm"]
    out.append({"cell": "heavy > light: state jitter (ep6 vs ep5, hf_pos_rms_max_mm)", "expected": "heavy > light",
                "actual": [s5, s6], "ok": s6 > s5})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="tools/eef_eval/reports")
    ap.add_argument("--datasets", nargs="*", default=sorted(truth.DATASETS))
    a = ap.parse_args(argv)
    prof = profile.load("demo")
    out = pathlib.Path(a.out)
    results, rows = {}, []
    t_all = time.perf_counter()
    for name in a.datasets:
        r = load.load_bundle(truth.dataset_dir(name) / "trajectory.json", lerobot_root=truth.lerobot_root(name))
        assert r.ok, r.errors[:3]
        cfg = runner.RunConfig(lerobot_root=str(truth.lerobot_root(name)), seed_root=str(truth.seed_root(name)),
                               profile=prof, out_dir=str(out / "run" / name), evidence_mode="flagged")
        faults = truth.faults(name)                       # evaluation truth: read after the detector ran
        for ep, s in sorted(r.samples.items()):
            t0 = time.perf_counter()
            detail, _ = runner.run_episode(s, cfg)
            results[(name, ep)] = detail
            cells = check(name, ep, faults[ep], detail)
            rows.append({"dataset": name, "episode_index": ep, "fault": faults[ep]["kind"],
                         "row": KIND[faults[ep]["kind"]], "elapsed_s": round(time.perf_counter() - t0, 2),
                         "summary": {k: v["status"] for k, v in detail["summary"].items()},
                         "supported_hypotheses": sorted({(h["hypothesis"] if h["hypothesis"] != "jitter_source" else
                                                          f"jitter_source={h['fitted']['source']}")
                                                         for h in detail["diagnosis"] if h["supported"]}),
                         "cells": cells, "pass": all(c["ok"] for c in cells)})
            print(f"{name} ep{ep:<2} {faults[ep]['kind']:<18} {'PASS' if rows[-1]['pass'] else 'FAIL'} "
                  + " ".join(f"{c['cell']}={c['actual']}" for c in cells if not c["ok"]), file=sys.stderr, flush=True)
    extra = heavier(results) if "dataset1" in a.datasets else []
    report = {"schema_version": "eef-matrix-report/1.0", "profile": prof.summary(),
              "note": "same-source synthetic variants; functional regression only, not a generalisation accuracy",
              "episodes": rows, "ordering": extra,
              "summary": {"episodes": len(rows), "passed": sum(r_["pass"] for r_ in rows),
                          "cells": sum(len(r_["cells"]) for r_ in rows) + len(extra),
                          "cells_ok": sum(c["ok"] for r_ in rows for c in r_["cells"]) + sum(c["ok"] for c in extra),
                          "seconds": round(time.perf_counter() - t_all, 1)}}
    out.mkdir(parents=True, exist_ok=True)
    (out / "matrix.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n")
    with open(out / "matrix_details.jsonl", "w") as fh:
        for (name, ep), d in sorted(results.items()):
            fh.write(json.dumps({"dataset": name, "episode_index": ep, "detail": d}, default=str) + "\n")
    print(json.dumps(report["summary"], indent=1))
    for c in extra:
        print(("PASS " if c["ok"] else "FAIL ") + c["cell"], c["actual"])
    return 0 if report["summary"]["passed"] == len(rows) and all(c["ok"] for c in extra) else 1


if __name__ == "__main__":
    sys.exit(main())
