"""Customer World_Policy buffer samples (reference/droid, reference/LVP) -> eef-video/1.0.0 entries.

Migration rules (docs/contracts/eef/migration.md §3-§4, design 12 §3.5, D-E10):
* DROID grid: views 0-2 are camera stills (``source_view_<i>`` local ids, view 2 is the wrist camera),
  view 3 is a ``pose_visualization`` and never an observation source;
* the 65-step ``eef_delta_gt`` is kept verbatim in a raw sidecar (``semantics: unresolved``); ``eef`` stays
  null and the timebase is ``index_only`` until the customer signs off the §3.4 checklist;
* only row 0 maps to the stills; nothing invents a video timeline;
* LVP: plain images, no EEF -> the module reports ``unsupported``, never a pass.
The output uses ``media_uri_base=bundle_file``: media and the raw sidecar live next to the bundle.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil

import numpy as np

from .. import contracts as C

UNRESOLVED = ["translation unit", "normalization", "delta convention", "coordinate frame", "timebase",
              "gripper semantics"]
IDENTITY3 = np.eye(3).tolist()


def _image_size(path: pathlib.Path) -> list[int]:
    from PIL import Image

    with Image.open(path) as im:
        return [int(im.width), int(im.height)]


def convert(source_dir: str | os.PathLike, out_dir: str | os.PathLike, *, family: str, episode_index: int) -> dict:
    """Copy the stills into ``out_dir/<sample_id>/`` and return the bundle entry."""
    src = pathlib.Path(source_dir)
    raw = json.loads((src / "sample.json").read_text(encoding="utf-8"))
    sid = f"reference_{family}_{episode_index:03d}"
    dst = pathlib.Path(out_dir) / sid
    dst.mkdir(parents=True, exist_ok=True)
    droid = family == "droid"
    n = 65 if droid else 1
    if droid and raw.get("model_payload_meta", {}).get("num_frames"):
        n = int(raw["model_payload_meta"]["num_frames"])
    views = []
    for x in raw["views"]:
        idx = int(x["view_index"])
        camera_view = droid and idx < 3
        kind = "camera" if camera_view else ("pose_visualization" if droid else "image")
        shutil.copyfile(src / x["image_file"], dst / x["image_file"])
        views.append({"view_id": f"view_{idx}", "kind": kind,
                      "camera_id": f"source_view_{idx}" if camera_view else None,
                      "mount": ("wrist" if idx == 2 else "fixed_external") if camera_view else "not_applicable",
                      "media": {"kind": "image", "uri": f"{sid}/{x['image_file']}",
                                "image_size_wh": _image_size(src / x["image_file"]), "frame_count": 1, "fps": None,
                                "clip_start_s": 0, "clip_end_s": None}})
    sample = {"schema_version": C.SCHEMA_VERSION, "sample_id": sid,
              "source": {"dataset": family, "episode_id": str(episode_index), "instruction": None},
              "frame_count": n, "timebase": "index_only", "annotations_path": "#frames", "calibration_path": None,
              "eef_frame": None, "reference_frame": None, "views": views, "point_definitions": {},
              "axis_definitions": {}, "raw_pose_sequence": None,
              "notes": [f"source sample name: {raw.get('name', '')}".strip()]}
    if droid:
        seq = raw.get("eef_delta_gt") or []
        payload = next((v for v in seq if v is not None), None)
        if payload is not None:
            (dst / "raw_pose_sequence.json").write_text(json.dumps({"source_field": "eef_delta_gt",
                                                                    "value": payload}) + "\n", encoding="utf-8")
            sample["raw_pose_sequence"] = {"path": f"{sid}/raw_pose_sequence.json", "semantics": "unresolved",
                                           "unresolved_fields": list(UNRESOLVED)}
        sample["notes"].append("65-step source sequence kept unresolved; stills only, no 65-frame video.")
    else:
        sample["notes"].append("images only, no EEF: EEF consistency is unsupported, not passed.")
    frames = []
    for i in range(n):
        cams = {}
        if i == 0:
            for v in views:
                if v["kind"] == "camera":
                    cams[v["camera_id"]] = {"video_frame_index": 0, "video_timestamp_s": 0,
                                            "image_size_wh": v["media"]["image_size_wh"], "calibration_id": None,
                                            "T_reference_camera": None, "H_media_from_calibration": IDENTITY3,
                                            "projection": None}
        frames.append({"schema_version": C.SCHEMA_VERSION, "sample_id": sid, "frame_index": i, "timestamp_s": None,
                       "source_state_index": None, "source_timing": [], "eef": None, "gripper": None,
                       "cameras": cams})
    return {"episode_index": int(episode_index), "sample": sample, "calibration": None, "frames": frames}
