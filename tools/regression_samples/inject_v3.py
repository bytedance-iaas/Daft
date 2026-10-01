#!/usr/bin/env python
"""Index and reference faults (FILE-10) injected into a copy of a LeRobot v3.0 dataset.

A v3.0 dataset keeps many episodes in shared files: ``meta/episodes`` says where each one starts and ends in the data
file (``dataset_from_index`` / ``dataset_to_index``) and in each camera's video (``from_timestamp`` / ``to_timestamp``);
the data rows name their task by ``task_index`` into ``meta/tasks.parquet`` and number their frames with
``frame_index``. Each fault breaks one of these references for one episode; every other episode of the copy stays as it
was and is a control. Videos are hard-linked (never rewritten); the parquet files that carry a fault are rewritten.

    python tools/regression_samples/inject_v3.py --base <v3 dataset> --out <new dataset> \\
        --plan offset:10:6,offset:15:1,video_range:20:1.0,dangling_task:30,frame_index:40

Writes ``injection.json`` next to the data: one record per episode (the faulty ones with fault, item, scope, params;
the rest as controls), base and lineage as in ``inject.py``.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import time

import pandas as pd

ITEM = "FILE-10"


def copy_tree(src: str, dst: str) -> None:
    if os.path.exists(dst):
        shutil.rmtree(dst)
    for d, dirs, files in os.walk(src):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        rel = os.path.relpath(d, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for f in files:
            if f in ("manifest.json", "injection.json") or f.startswith("."):
                continue
            s, t = os.path.join(d, f), os.path.join(dst, rel, f)
            if f.endswith(".parquet") or f.endswith(".json"):
                shutil.copy2(s, t)                   # may be rewritten: a real copy
            else:
                try:
                    os.link(os.path.realpath(s), t)
                except OSError:
                    shutil.copy2(s, t)


class V3:
    def __init__(self, root: str):
        self.root = root
        self.info = json.load(open(os.path.join(root, "meta/info.json")))
        assert str(self.info["codebase_version"]).startswith("v3"), self.info["codebase_version"]
        self.ep_files = sorted(glob.glob(os.path.join(root, "meta/episodes/*/*.parquet")))
        self.eps = pd.concat([pd.read_parquet(f).assign(_file=f) for f in self.ep_files], ignore_index=True)
        self.cams = [k for k, v in self.info["features"].items() if v.get("dtype") == "video"]

    def row(self, ep: int) -> pd.Series:
        r = self.eps[self.eps["episode_index"] == ep]
        if r.empty:
            raise SystemExit(f"episode {ep} is not in the dataset")
        return r.iloc[0]

    def data_file(self, ep: int) -> str:
        r = self.row(ep)
        return os.path.join(self.root, self.info["data_path"].format(chunk_index=int(r["data/chunk_index"]), file_index=int(r["data/file_index"])))

    def save_episodes(self) -> None:
        for f in self.ep_files:
            part = self.eps[self.eps["_file"] == f].drop(columns="_file")
            part.to_parquet(f, index=False)

    def edit_data(self, ep: int, fn) -> dict:
        path = self.data_file(ep)
        d = pd.read_parquet(path)
        mask = d["episode_index"] == ep
        out = fn(d, mask)
        d.to_parquet(path, index=False)
        return out


def fault_offset(ds: V3, ep: int, rows: str = "6") -> dict:
    """the episode's row range in the shared data file starts and ends `rows` rows too late: it reads the next episode's first rows"""
    n = int(rows)
    i = ds.eps.index[ds.eps["episode_index"] == ep][0]
    before = (int(ds.eps.at[i, "dataset_from_index"]), int(ds.eps.at[i, "dataset_to_index"]))
    ds.eps.at[i, "dataset_from_index"] = before[0] + n
    ds.eps.at[i, "dataset_to_index"] = before[1] + n
    order = sorted(int(x) for x in ds.eps["episode_index"])
    k = order.index(ep)
    # an overlap / gap check sees the wrong range from both sides: the neighbours are not clean on FILE-10, they are not judged
    neighbours = [order[j] for j in (k - 1, k + 1) if 0 <= j < len(order)]
    return {"scope": {"field": "meta/episodes dataset_from_index, dataset_to_index"}, "severity": "obvious" if n > 1 else "borderline",
            "params": {"shift_rows": n, "was": list(before), "now": [before[0] + n, before[1] + n]}, "neighbours": neighbours}


def fault_video_range(ds: V3, ep: int, seconds: str = "1.0") -> dict:
    """the episode's stretch of every shared video ends `seconds` too late: it shows the next episode's first frames"""
    s = float(seconds)
    i = ds.eps.index[ds.eps["episode_index"] == ep][0]
    was = {}
    for cam in ds.cams:
        col = f"videos/{cam}/to_timestamp"
        was[cam] = float(ds.eps.at[i, col])
        ds.eps.at[i, col] = was[cam] + s
    return {"scope": {"field": "meta/episodes videos/*/to_timestamp", "streams": ds.cams}, "severity": "obvious",
            "params": {"extend_s": s, "was_to_timestamp": was}}


def fault_dangling_task(ds: V3, ep: int) -> dict:
    """the episode's rows name a task index that meta/tasks.parquet does not have"""
    tasks = pd.read_parquet(os.path.join(ds.root, "meta/tasks.parquet"))
    missing = int(tasks["task_index"].max()) + 7

    def fn(d, mask):
        was = sorted(set(int(x) for x in d.loc[mask, "task_index"]))
        d.loc[mask, "task_index"] = missing
        return {"was": was}
    out = ds.edit_data(ep, fn)
    return {"scope": {"field": "data task_index"}, "severity": "obvious", "params": {"task_index_now": missing, **out}}


def fault_frame_index(ds: V3, ep: int) -> dict:
    """the episode's frame numbers repeat one value and then skip one: ..., 20, 21, 21, 23, ... (row count unchanged)"""
    def fn(d, mask):
        idx = d.index[mask]
        k = len(idx) // 3
        d.loc[idx[k], "frame_index"] = int(d.loc[idx[k - 1], "frame_index"])
        return {"repeated_at_row_of_episode": int(k), "value": int(d.loc[idx[k], "frame_index"])}
    out = ds.edit_data(ep, fn)
    return {"scope": {"field": "data frame_index"}, "severity": "obvious", "params": out}


FAULTS = {"offset": fault_offset, "video_range": fault_video_range, "dangling_task": fault_dangling_task, "frame_index": fault_frame_index}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--plan", required=True, help="comma list of fault:episode[:param]")
    a = ap.parse_args(argv)
    copy_tree(a.base, a.out)
    ds = V3(a.out)
    base = os.path.basename(a.base.rstrip("/"))
    records = {}
    for spec in a.plan.split(","):
        name, ep, *param = spec.split(":")
        ep = int(ep)
        rec = FAULTS[name](ds, ep, *param)
        records[ep] = {"episode_index": ep, "base_episode": ep, "lineage": f"{base}:{ep}", "fault": name, "item": ITEM, **rec}
        print(f"ep {ep:4d}: {name:14s} {rec['severity']:10s} {json.dumps(rec['params'])[:100]}", flush=True)
    ds.save_episodes()
    for ep in sorted(int(x) for x in ds.eps["episode_index"]):
        records.setdefault(ep, {"episode_index": ep, "base_episode": ep, "lineage": f"{base}:{ep}", "fault": None, "severity": None, "item": None,
                                "note": "untouched episode of the copy: a control"})
    json.dump({"schema_version": "0.1", "base": base, "note": f"index and reference faults (FILE-10) in a copy of {base}; plan {a.plan}",
               "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "episodes": [records[k] for k in sorted(records)]},
              open(os.path.join(a.out, "injection.json"), "w"), ensure_ascii=False, indent=1)
    print("INJECT_V3_DONE", len(records), "episodes ->", a.out)


if __name__ == "__main__":
    main()
