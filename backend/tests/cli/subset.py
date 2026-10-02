"""A LeRobot v2.1 subset that keeps its source's episode numbers (F12.8).

Cutting a few episodes out of a bigger dataset without renumbering them leaves exactly this: the
episode table lists 1, 3, 5, their parquet and video files keep their names, the totals are the
subset's. The platform reads such a dataset by those numbers - the preflight writes them as
``dataset.episode_indices`` and every selection picks from them.
"""
from __future__ import annotations

import json
import os
import re

_FILE = re.compile(r"episode_(\d+)\.(parquet|mp4)$")


def keep_episodes(root: str, keep) -> str:
    """Drop every episode of the LeRobot v2.1 dataset at ``root`` but ``keep``, in place."""
    keep = {int(k) for k in keep}
    meta = os.path.join(root, "meta")
    path = os.path.join(meta, "episodes.jsonl")
    with open(path, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    kept = [r for r in rows if int(r["episode_index"]) in keep]
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in kept)
    videos = 0
    for dirpath, _, files in os.walk(root):
        for name in files:
            m = _FILE.search(name)
            if not m:
                continue
            if int(m.group(1)) not in keep:
                os.remove(os.path.join(dirpath, name))
            elif m.group(2) == "mp4":
                videos += 1
    with open(os.path.join(meta, "info.json"), encoding="utf-8") as fh:
        info = json.load(fh)
    info.update(total_episodes=len(kept), total_frames=sum(int(r["length"]) for r in kept), total_videos=videos)
    with open(os.path.join(meta, "info.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1)
    return root
