"""Location of the DEMO datasets (outside the repo; tests that need them skip when absent)."""
from __future__ import annotations

import os
import pathlib

import pytest

ROOT = pathlib.Path(os.environ.get("CURATOR_EEF_DEMO_DATA", "~/ws/ws_general/galbot")).expanduser()
DATASETS = {
    "dataset1": {"lerobot": "eef_ds1_lr2", "episodes": 11, "frames": 304, "cameras": 1},
    "dataset2": {"lerobot": "eef_ds2_lr3", "episodes": 7, "frames": 287, "cameras": 2},
}


def path(name: str) -> pathlib.Path:
    return ROOT / name


def require(name: str) -> pathlib.Path:
    p = path(name)
    if not (p / "trajectory.json").is_file():
        pytest.skip(f"DEMO data {p} not present (set CURATOR_EEF_DEMO_DATA)")
    return p


def lerobot_root(name: str) -> pathlib.Path:
    return path(name) / DATASETS[name]["lerobot"]
