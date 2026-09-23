"""The three-file sample directory (sample.json, frames.jsonl, calibration.json) <-> one bundle entry.

``pack`` rewrites media URIs relative to the LeRobot root (``media_uri_base=lerobot_root``) and the
references to the in-bundle literals ``#frames`` / ``#calibration``; ``explode`` is the inverse.
No numeric value is touched in either direction.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib

from .. import contracts as C


def pack(sample_dir: str | os.PathLike, episode_index: int, *, media_root: str | os.PathLike) -> dict:
    folder = pathlib.Path(sample_dir)
    s = json.loads((folder / "sample.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (folder / s["annotations_path"]).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    cal = None
    if s["calibration_path"]:
        cal = json.loads((folder / s["calibration_path"]).read_text(encoding="utf-8"))
    s = copy.deepcopy(s)
    s["annotations_path"] = "#frames"
    s["calibration_path"] = "#calibration" if cal is not None else None
    root = pathlib.Path(media_root).resolve()
    for v in s["views"]:
        target = (folder / v["media"]["uri"]).resolve()
        v["media"]["uri"] = target.relative_to(root).as_posix()
    return {"episode_index": int(episode_index), "sample": s, "calibration": cal, "frames": rows}


def explode(entry: dict, out_dir: str | os.PathLike, *, media_root: str | os.PathLike) -> pathlib.Path:
    s = copy.deepcopy(entry["sample"])
    folder = pathlib.Path(out_dir) / s["sample_id"]
    folder.mkdir(parents=True, exist_ok=True)
    s["annotations_path"] = "frames.jsonl"
    s["calibration_path"] = "calibration.json" if entry["calibration"] is not None else None
    for v in s["views"]:
        v["media"]["uri"] = os.path.relpath(pathlib.Path(media_root) / v["media"]["uri"], folder)
    (folder / "sample.json").write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (folder / "frames.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n"
                                                 for r in entry["frames"]), encoding="utf-8")
    if entry["calibration"] is not None:
        (folder / "calibration.json").write_text(json.dumps(entry["calibration"], ensure_ascii=False, indent=2) + "\n",
                                                 encoding="utf-8")
    return folder


def bundle(entries: list[dict], *, dataset_id: str, codebase_version: str, fps: float, episode_count: int,
           generator: str | None = None, media_uri_base: str = "lerobot_root") -> dict:
    ds = {"id": dataset_id, "lerobot_codebase_version": codebase_version, "fps": float(fps),
          "episode_count": int(episode_count)}
    if generator:
        ds["generator"] = generator
    return {"schema_version": C.SCHEMA_VERSION, "container": C.CONTAINER, "dataset": ds,
            "media_uri_base": media_uri_base, "samples": sorted(entries, key=lambda e: e["episode_index"])}
