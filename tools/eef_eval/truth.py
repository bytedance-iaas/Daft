"""Evaluation truth for the two DEMO datasets. Only this offline evaluator reads it (design 12 §13.3, D-E5).

Layout (outside the repo, ``$CURATOR_EEF_DEMO_DATA``, default ``~/ws/ws_general/galbot``):
* ``<dataset>/evaluation/truth_pixels/<sample_id>/<camera_id>.jsonl`` - dense per-frame true pixels of the
  seeded points (same construction as the seeds: the geometry the video really shows, true calibration,
  video affine), in the observation schema with ``method=synthetic_fixture``;
* dataset2 ``corruptions.json`` and dataset1 ``ground_truth/episodes.jsonl`` + per-frame files - the
  injected fault, its parameters and the evaluation masks.
"""
from __future__ import annotations

import json
import os
import pathlib

import numpy as np

ROOT = pathlib.Path(os.environ.get("CURATOR_EEF_DEMO_DATA", "~/ws/ws_general/galbot")).expanduser()
DATASETS = {"dataset1": "eef_ds1_lr2", "dataset2": "eef_ds2_lr3"}


def dataset_dir(name: str) -> pathlib.Path:
    return ROOT / name


def lerobot_root(name: str) -> pathlib.Path:
    return ROOT / name / DATASETS[name]


def seed_root(name: str) -> pathlib.Path:
    return ROOT / name / "observations_seed"


def truth_pixels(name: str, sample_id: str, camera_id: str, n: int) -> dict[str, np.ndarray]:
    """point id -> (n, 2) true media pixels on the sample's frames (NaN where out of frame)."""
    path = ROOT / name / "evaluation" / "truth_pixels" / sample_id / f"{camera_id}.jsonl"
    out: dict[str, np.ndarray] = {}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        i = row["frame_index"]
        for pid, p in row["points"].items():
            arr = out.setdefault(pid, np.full((n, 2), np.nan))
            if p["uv_px"] is not None:
                arr[i] = p["uv_px"]
    return out


def faults(name: str) -> dict[int, dict]:
    """episode -> {kind, params, evaluate_mask (per frame) or None, cameras (affected) or None}."""
    base = ROOT / name
    out = {}
    if name == "dataset2":
        c = json.loads((base / "corruptions.json").read_text())
        for ep, e in c["episodes"].items():
            kind = "baseline" if e["type"] == "original" else e["type"]
            out[int(ep)] = {"kind": kind, "params": e.get("params", {}), "camera": e.get("camera"),
                            "start_frame": e.get("start_frame"), "raw": {k: v for k, v in e.items() if k != "per_frame"}}
        return out
    for line in (base / "ground_truth" / "episodes.jsonl").read_text().splitlines():
        e = json.loads(line)
        ep = int(e["episode_index"])
        rows = [json.loads(x) for x in (base / "ground_truth" / f"episode_{ep:06d}.jsonl").read_text().splitlines()]
        out[ep] = {"kind": e["parameters"]["kind"], "params": e["parameters"], "camera": None,
                   "evaluate": np.array([bool(r["evaluate"]) for r in rows]),
                   "active": np.array([bool(r["active"]) for r in rows]), "raw": e}
    return out
