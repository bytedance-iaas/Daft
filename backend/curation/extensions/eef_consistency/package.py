"""The request package between the EEF module's two halves (design doc 23 §2.1, §2.3, D76, D77).

The CPU half (the CPU block's ``vlm_prep`` stage) measures, renders and builds every request the model half will
send; it keeps them in the run directory, ``scratch/vlm/<episode>/<module>/``: ``requests.json`` (each request's
text, window, frame ids, cache key and the files of its stills and videos) beside the files, and ``prep.json`` -
the partial record (what the CPU found, what the model half needs to finish the record). The model half (``vlm``)
reads them back, asks, merges and writes the record; the package goes once that record is on disk, stays while
it erred (a retry asks again without rendering again) and goes with the task's scratch directory at its end.

Nothing here asks a model or decodes a video. ``prep.json`` is written last: a package without it is incomplete
(a CPU half that stopped half way) and is made again.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import os
import shutil
from typing import Any

#: the layout of a package; another version is made again rather than read
VERSION = 1
PREP_NAME, REQUESTS_NAME = "prep.json", "requests.json"
_DATA_URL = "data:video/mp4;base64,"


def path(run_dir: str, module: str, episode: int) -> str:
    """The package directory of one episode's module (design doc 23 §2.3)."""
    return os.path.join(run_dir, "scratch", "vlm", f"{int(episode):06d}", module)


def root(run_dir: str) -> str:
    """Every package of the run: the Daemon removes it when the task ends."""
    return os.path.join(run_dir, "scratch", "vlm")


def _plain(value: Any) -> Any:
    """JSON for what a window carries (its CPU segment may hold numpy numbers)."""
    import numpy as np

    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def _atomic(path_: str, data: bytes) -> None:
    tmp = f"{path_}.tmp-{os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path_)


def write(directory: str, prep: dict, requests: list) -> None:
    """Keep ``requests`` (``review.Request``) and the partial record ``prep``; any older package there goes first."""
    if os.path.isdir(directory):
        shutil.rmtree(directory)
    os.makedirs(directory, exist_ok=True)
    rows = []
    for i, req in enumerate(requests):
        images = []
        for k, img in enumerate(req.images):
            name = f"r{i:03d}_i{k:02d}.jpg"
            _atomic(os.path.join(directory, name), img["jpeg"])
            images.append({"role": img["role"], "frame_index": int(img["frame_index"]), "file": name})
        videos = []
        for k, clip in enumerate(req.videos):
            if not clip.url.startswith(_DATA_URL):
                raise ValueError(f"a request video that is not inline mp4: {clip.url[:40]}")
            name = f"r{i:03d}_v{k:02d}.mp4"
            _atomic(os.path.join(directory, name), base64.b64decode(clip.url[len(_DATA_URL):]))
            videos.append({**clip.metadata(), "file": name})
        rows.append({"window": _plain(dataclasses.asdict(req.window)), "text": req.text,
                     "frame_ids": [int(f) for f in req.frame_ids], "key": req.key, "images": images, "videos": videos})
    _atomic(os.path.join(directory, REQUESTS_NAME), json.dumps({"version": VERSION, "requests": rows}).encode())
    _atomic(os.path.join(directory, PREP_NAME),
            json.dumps({"version": VERSION, **_plain(prep)}, ensure_ascii=False, allow_nan=False).encode())


def read_prep(directory: str) -> dict | None:
    """The partial record, or None: no package, an incomplete one, or one of another layout."""
    try:
        with open(os.path.join(directory, PREP_NAME), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("version") == VERSION else None


def read_requests(directory: str) -> list:
    """The requests as the CPU half built them (``review.Request``, with their stills and videos)."""
    from ...adapters.video_input import VideoClip
    from . import review as R

    with open(os.path.join(directory, REQUESTS_NAME), encoding="utf-8") as fh:
        doc = json.load(fh)
    out = []
    for row in doc["requests"]:
        images = []
        for img in row["images"]:
            with open(os.path.join(directory, img["file"]), "rb") as fh:
                images.append({"role": img["role"], "frame_index": img["frame_index"], "jpeg": fh.read()})
        videos = []
        for v in row["videos"]:
            with open(os.path.join(directory, v["file"]), "rb") as fh:
                url = _DATA_URL + base64.b64encode(fh.read()).decode("ascii")
            videos.append(VideoClip(camera=v["camera"], url=url, sha256=v["sha256"], start_s=float(v["start_s"]),
                                    end_s=float(v["end_s"]), frames=int(v["frames"]), byte_size=int(v["byte_size"])))
        out.append(R.Request(window=R.Window(**row["window"]), text=row["text"], images=images,
                             frame_ids=list(row["frame_ids"]), key=row["key"], videos=videos))
    return out


def remove(directory: str) -> None:
    """The package of a record now on disk (D77): gone, and the directories above it that are left empty (its
    episode's, ``scratch/vlm``, ``scratch``)."""
    shutil.rmtree(directory, ignore_errors=True)
    parent = os.path.dirname(directory)
    for _ in range(3):
        try:
            os.rmdir(parent)
        except OSError:
            return
        parent = os.path.dirname(parent)
