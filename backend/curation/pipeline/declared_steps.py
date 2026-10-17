"""The declared steps of an episode (module ``declared_steps``, registry 8.11): the step boundaries and the
task end a dataset records itself - TASK-1's *declared* track, as opposed to a detected one.

Nothing is measured here. The reader of the visualizer (``curation.viz.annotations``) already recognises the
layouts datasets use for their steps (``subtask_index`` + a table, a per-frame ``task_index`` that changes
inside an episode, HABIT's ``low_level_task_index`` + ``meta/subtasks.jsonl``, string step columns, boolean
segment flags) and, since 8.11, a controller-mode column (RSS 2026's ``observation.commander_state``:
inference -> restore). This module reads the same columns of one episode and reports:

* ``step_declared`` - one finding per step of the primary segments track (frames, label);
* ``task_end_declared`` - the first frame of the trailing post-task mode run when a mode column says the
  controller switched to restoring / resetting after the task.

Datasets without any of these are not assessed (TASK-1 ``not_applicable``: the dataset declares no steps).
Only LeRobot v2 / v3 datasets are read; mcap and Lance are reported as not assessed for now.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..core.contract import CheckResult
from ..ingest import dsfs
from ..ingest import lerobot_reader as lr
from ..viz import annotations as A

#: the modes a controller reports once the task is over (a mode column's trailing run with one of these
#: words names the task end); a task's ``post_task_modes`` parameter replaces the list
DEFAULT_POST_TASK_MODES = ("restore", "align", "reset", "home", "return", "idle", "done", "finish", "end")


@dataclass
class DeclaredSteps:
    """One dataset's declared-step reader, built once per stage; ``episode(index)`` reads one episode."""
    input_dir: str
    info: dict
    sources: list = field(default_factory=list)
    lookups: dict = field(default_factory=dict)
    tasks: dict = field(default_factory=dict)
    v3: bool = False
    _eps: dict = field(default_factory=dict)        # v2: episode_index -> episode table row
    _meta: Any = None                               # v3: the episode table (DataFrame)
    reason: str | None = None                       # why nothing can be read (no sources, a container ...)

    @classmethod
    def open(cls, input_dir: str) -> "DeclaredSteps":
        info = lr._load_info(input_dir)
        self = cls(input_dir=input_dir, info=info)
        meta_dir = dsfs.join(input_dir, "meta")
        try:
            meta_files = sorted("meta/" + dsfs.basename(p) for p in dsfs.listdir(meta_dir))
        except Exception:  # noqa: BLE001 - no meta listing: no tables
            meta_files = []
        peeks: dict[str, dict | None] = {}

        def peek(rel: str) -> dict | None:
            if rel not in peeks:
                try:
                    rows = A.parse_jsonl(dsfs.read_bytes(dsfs.join(input_dir, rel))[:16384])
                    peeks[rel] = rows[0] if rows else None
                except Exception:  # noqa: BLE001
                    peeks[rel] = None
            return peeks[rel]

        self.v3 = str(info.get("codebase_version", "")).startswith("v3")
        ep_fields: set[str] = set()
        if self.v3:
            self._meta = lr._load_episodes_meta(input_dir)
            ep_fields = set(self._meta.columns)
        else:
            self._eps = {int(e["episode_index"]): e for e in lr._v2_episode_list(input_dir)}
            first = next(iter(self._eps.values()), None)
            ep_fields = set(first) if isinstance(first, dict) else set()
        self.sources = [s for s in A.detect_sources(info, meta_files, peek=peek, episode_fields=ep_fields)
                        if s.kind == "segments" and s.supported and s.format != "unknown"]
        if not self.sources:
            self.reason = "数据集没有逐帧的分步或模式标注"
            return self
        for s in self.sources:
            if s.table and s.table not in self.lookups and s.format in ("subtask_index", "index_table"):
                self.lookups[s.table] = self._lookup(s.table)
        if any(s.format == "task_index" for s in self.sources):
            self.tasks = lr._load_tasks_map(input_dir) if hasattr(lr, "_load_tasks_map") else {}
        return self

    def _lookup(self, rel: str) -> dict[int, str]:
        path = dsfs.join(self.input_dir, rel)
        try:
            data = dsfs.read_bytes(path)
            if rel.endswith(".parquet"):
                return A.parse_lookup_parquet(data)
            return A.lookup_from_records(A.parse_jsonl(data))
        except Exception:  # noqa: BLE001 - an unreadable table: labels stay numbers
            return {}

    @property
    def columns(self) -> list[str]:
        cols = {c for s in self.sources for c in s.columns}
        return sorted(cols)

    def _frames(self, index: int):
        """(columns dict, times) of one episode, or None when it is not in the episode table."""
        feats = self.info.get("features") or {}
        want = [c for c in self.columns if c in feats]
        if not want:
            return None
        read = want + [c for c in ("timestamp", "frame_index", "index", "episode_index") if c in feats and c not in want]
        if self.v3:
            m = self._meta
            rows = m[m["episode_index"].astype(int) == int(index)]
            if not len(rows):
                return None
            ep = rows.iloc[0]
            path = dsfs.join(self.input_dir, self.info["data_path"].format(
                chunk_index=int(ep["data/chunk_index"]), file_index=int(ep["data/file_index"])))
            df = dsfs.read_parquet(path, columns=[c for c in read if c != "episode_index"] + ["episode_index"])
            df = df[df["episode_index"].astype(int) == int(index)]
        else:
            ep = self._eps.get(int(index))
            if ep is None:
                return None
            data_path, _ = lr._v2_episode_paths(self.input_dir, self.info, ep)
            df = dsfs.read_parquet(data_path, columns=read)
        if not len(df):
            return None
        fps = float(self.info.get("fps") or 0.0)
        if "timestamp" in df.columns:
            times = [float(x) for x in df["timestamp"].to_numpy()]
        elif fps:
            times = [i / fps for i in range(len(df))]
        else:
            times = [float(i) for i in range(len(df))]
        cols = {c: df[c].tolist() for c in want}
        return cols, times

    def episode(self, index: int, post_task_modes=DEFAULT_POST_TASK_MODES) -> CheckResult:
        detail: dict = {"sources": [s.format + ":" + s.key for s in self.sources]}
        if self.reason:
            detail["reason"] = self.reason
            return CheckResult(name="declared_steps", score=1.0, detail=detail)
        got = self._frames(index)
        if got is None:
            detail["reason"] = "这条 episode 读不到分步标注列"
            return CheckResult(name="declared_steps", score=1.0, detail=detail)
        cols, times = got
        fps = float(self.info.get("fps") or 0.0)
        ann = A.episode_annotations(self.sources, cols, times, lookups=self.lookups, tasks=self.tasks)
        tracks = []
        for t in ann.tracks:
            segs = []
            for sg in t["segments"]:
                a = _frame_at(times, sg["start_s"])
                b = max(a, _frame_at(times, sg["end_s"]) - 1)
                segs.append({"start_s": sg["start_s"], "end_s": sg["end_s"], "frames": [a, b],
                             "label": sg["label"], "quality": sg.get("quality"), "flags": sg.get("flags") or []})
            fmt = next((s.format for s in self.sources if s.key == t["key"]), None)
            tracks.append({"key": t["key"], "name": t["name"], "format": fmt, "primary": bool(t.get("primary")),
                           "segments": segs})
        # the steps are the first track that is not a controller-mode track; the reader's own choice may be one
        steps = next((t for t in tracks if t["format"] != "mode_column" and t["segments"]), None)
        for t in tracks:
            t["primary"] = t is steps
        detail["tracks"] = tracks
        detail["n_frames"] = len(times)
        detail["fps"] = fps
        # the task end: a mode column whose trailing run is a post-task mode
        words = tuple(str(w).lower() for w in (post_task_modes or DEFAULT_POST_TASK_MODES))
        for t in tracks:
            if t["format"] != "mode_column" or len(t["segments"]) < 2:
                continue
            last = t["segments"][-1]
            if any(w in str(last["label"]).lower() for w in words):
                before = t["segments"][-2]
                detail["task_end"] = {"time_s": last["start_s"], "frame": last["frames"][0],
                                      "mode_before": before["label"], "mode_after": last["label"],
                                      "column": t["key"]}
                break
        if not any(t["segments"] for t in tracks) and "task_end" not in detail:
            detail["reason"] = "分步标注列全是占位或空"
        return CheckResult(name="declared_steps", score=1.0, detail=detail)


def _frame_at(times: list[float], t: float) -> int:
    """The first frame whose time is at or after ``t`` (segments tile the clock; ``end_s`` is exclusive), with
    half a frame of slack: float32 timestamps sit a hair under their nominal value."""
    arr = np.asarray(times, dtype=np.float64)
    step = float(np.median(np.diff(arr))) if len(arr) > 1 else 0.0
    i = int(np.searchsorted(arr, float(t) - 0.5 * step, side="left"))
    return max(0, min(i, len(arr)))
