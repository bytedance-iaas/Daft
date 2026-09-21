"""Incremental export of a LeRobot v3 source (many episodes per parquet / mp4 file).

The delivered v3 dataset keeps v1's layout (``export_lerobot_v3``): frame tables and
each camera's video roll into ``file-NNN`` files at the size thresholds, an episode
never spans two files.  What an incremental export does per file:

* video file (per camera): holds a fixed set of episodes, each at a time window.
  None of its episodes dropped -> untouched, whatever happened to their numbering
  or task (the window is all that points into it).  Some dropped -> rebuilt from
  the source windows of the ones left (v1's ``_reencode_concat``: local temp file,
  whole-file copy).  All dropped -> deleted.  Added episodes are encoded into new
  files after the existing ones (v1's ``_RollingVideoWriter``).
* frame table file: must hold its episodes in global ``index`` order, so episodes
  keep the file they were in and an added episode joins the file of the episode
  before it.  A file is rewritten (cheap, v1's ``_RollingParquetWriter``) only when
  one of its episodes changed number, frame offset or task_index; a first export or
  a reordered list lays the files out afresh at the size threshold.
* ``meta/`` (``episodes/chunk-000/file-000.parquet``, ``tasks.parquet``, ``info.json``,
  ``stats.json``, sidecars): rebuilt every time, copied in where the bytes differ.

Only the video files a drop hits are re-encoded (design doc 06, section 4.3); a
relabel or a renumbering never touches a video.
"""
from __future__ import annotations

import os
import threading
from collections import defaultdict
from functools import partial

import numpy as np
import pandas as pd

from ..ingest import dsfs
from ..ingest.lerobot_reader import _load_episodes_meta, _load_tasks_map, _v3_task_index_of
from . import publish
from .diff import Diff
from .incremental_base import FormatExporter, Wanted, task_table, written_tasks
from .lerobot_writer import (_reencode_concat, _RollingParquetWriter, _RollingVideoWriter,
                             _size_mb, _to_parquet_fsx_safe, _write_camera_health, _write_jsonl)
from .manifest import (TASK_SOURCE_CAPTION, TASK_SOURCE_HUMAN, EpisodeEntry, digest_json,
                       task_key)
from .safe_write import write_json
from .source import ExportError, ExportInputError

MIB = 1024 * 1024


def _valid_tasks(t) -> bool:
    return isinstance(t, (list, tuple, np.ndarray)) and len(t) > 0


class V3Exporter(FormatExporter):
    fmt = "lerobot_v3"

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        info = self.info
        self.video_mb = _size_mb(info, "video_files_size_in_mb", 200.0, self.params.get("video_file_mb"))
        self.data_mb = _size_mb(info, "data_files_size_in_mb", 100.0, self.params.get("data_file_mb"))
        self.cs = max(1, int(info.get("chunks_size") or 1000))
        self.fps = float(info["fps"])
        self.video_keys = [k for k, v in info["features"].items() if v["dtype"] == "video"]

    def resolved_params(self) -> dict:
        return {"video_file_mb": self.video_mb, "data_file_mb": self.data_mb}

    def data_rel(self, fid: int) -> str:
        c, f = divmod(int(fid), self.cs)
        return self.info["data_path"].format(chunk_index=c, file_index=f)

    def video_rel(self, vk: str, fid: int) -> str:
        c, f = divmod(int(fid), self.cs)
        return self.info["video_path"].format(video_key=vk, chunk_index=c, file_index=f)

    # ── plan ────────────────────────────────────────────────────────────────
    def plan(self, wanted: list[Wanted]) -> list[EpisodeEntry]:
        info, root = self.info, self.guard.root
        self.guard.require("meta/info.json")
        prefix = root.rstrip("/") + "/"
        for p in dsfs.glob(dsfs.join(root, "meta", "episodes", "chunk-*", "file-*.parquet")):
            rel = p[len(prefix):] if p.startswith(prefix) else os.path.relpath(p, root)
            self.guard.require(rel.replace(os.sep, "/"))
        for rel in ("meta/tasks.parquet", "meta/stats.json"):
            self.guard.identity(rel)

        ep_meta = _load_episodes_meta(root).set_index("episode_index", drop=False)
        keep = [w.episode_index for w in wanted]
        unknown = [i for i in keep if i not in ep_meta.index]
        if unknown:
            raise ExportInputError(f"episodes not in the source meta/episodes: {unknown[:8]}")
        sel = ep_meta.loc[keep].reset_index(drop=True)
        # same fallback as v1's export_lerobot_v3 when the episodes table has no tasks
        self.src_tasks_missing = "tasks" not in sel.columns or any(
            not _valid_tasks(t) for t in sel["tasks"])
        tmap: dict = {}
        tidx: dict = {}
        if self.src_tasks_missing:
            tmap = _load_tasks_map(root)
            tidx = _v3_task_index_of(root, info, sel) if tmap else {}
        self.sel = sel

        self.src: dict[int, tuple[str, int, int, dict]] = {}
        entries: list[EpisodeEntry] = []
        for w, (_, ep) in zip(wanted, sel.iterrows(), strict=True):
            i = w.episode_index
            t = ep["tasks"] if "tasks" in sel.columns else None
            src_tasks = [str(x) for x in t] if _valid_tasks(t) else [str(tmap.get(tidx.get(i), ""))]
            tasks, label = written_tasks(w, src_tasks)
            data_rel = info["data_path"].format(chunk_index=int(ep["data/chunk_index"]),
                                                file_index=int(ep["data/file_index"]))
            lo, hi = int(ep["dataset_from_index"]), int(ep["dataset_to_index"])
            ident_data = self.guard.require(data_rel)
            windows, idents = {}, []
            for vk in self.video_keys:
                rel = info["video_path"].format(video_key=vk,
                                                chunk_index=int(ep[f"videos/{vk}/chunk_index"]),
                                                file_index=int(ep[f"videos/{vk}/file_index"]))
                a = float(ep[f"videos/{vk}/from_timestamp"])
                b = float(ep[f"videos/{vk}/to_timestamp"])
                idents.append([vk, self.guard.require(rel), a, b])
                windows[vk] = (rel, a, b)
            self.src[i] = (data_rel, lo, hi, windows)
            entries.append(EpisodeEntry(
                episode_index=i, new_index=0,
                content_key=digest_json(["lerobot_v3", ident_data, lo, hi, idents]),
                task_key=task_key(tasks, label), tasks=tasks, task_source=label,
                task_index=0, length=hi - lo, index_from=0, parquet=""))
        self.has_override = any(e.task_source in (TASK_SOURCE_CAPTION, TASK_SOURCE_HUMAN)
                                for e in entries)
        self.task_strings, task_index_of = task_table(
            self.old.task_table if self.old is not None else None, [e.tasks for e in entries])
        cursor = 0
        for k, e in enumerate(entries):
            e.new_index = k
            e.task_index = task_index_of[e.tasks[0]]
            e.index_from = cursor
            cursor += e.length
        self.total_frames = cursor
        self.stats_mode = "source" if self.guard.identity("meta/stats.json") else "none"
        return entries

    # ── data + videos ───────────────────────────────────────────────────────
    def write_files(self, entries: list[EpisodeEntry], diff: Diff) -> None:
        self._lock = threading.Lock()
        self.new_win: dict[tuple[int, str], tuple[int, float, float]] = {}
        retained = {e.episode_index for e in entries if diff.classes[e.episode_index] != "add"}
        with publish.activate(self.placer):       # v1's rolling writers hand sealed files here
            self._write_videos(entries, retained)
            self._write_data(entries, retained)
        for e in entries:
            e.parquet = self.data_rel(e.data_file)
            e.video_files = {vk: self.new_win[(e.episode_index, vk)][0] for vk in self.video_keys}
            e.windows = {vk: [self.new_win[(e.episode_index, vk)][1],
                              self.new_win[(e.episode_index, vk)][2]] for vk in self.video_keys}
            e.videos = {vk: self.video_rel(vk, fid) for vk, fid in e.video_files.items()}
        self._describe_videos(entries)

    def _write_videos(self, entries: list[EpisodeEntry], retained: set[int]) -> None:
        old_eps = self.old.episodes if self.old is not None else []
        by_idx = {e.episode_index: e for e in entries}
        jobs = []
        rebuilt = untouched = 0
        for vk in self.video_keys:
            files: dict[int, list[EpisodeEntry]] = defaultdict(list)
            for oe in old_eps:
                if vk in oe.video_files:
                    files[oe.video_files[vk]].append(oe)
            next_id = max(files) + 1 if files else 0
            for fid, members in sorted(files.items()):
                members.sort(key=lambda m: m.windows[vk][0])
                left = [by_idx[m.episode_index] for m in members if m.episode_index in retained]
                if len(left) == len(members):
                    rel = self.video_rel(vk, fid)
                    self.placer.record(rel, self.old.files[rel], written=False)
                    for m in members:
                        self.new_win[(m.episode_index, vk)] = (fid, m.windows[vk][0], m.windows[vk][1])
                    untouched += 1
                elif left:
                    left.sort(key=lambda e: e.new_index)
                    jobs.append(partial(self._rebuild_video, vk, fid, left))
                    rebuilt += 1
                # else: every episode in it was dropped; the leftover sweep deletes it
            adds = [e for e in entries if e.episode_index not in retained]
            if adds:
                jobs.append(partial(self._encode_new, vk, next_id, adds))
        self.ctx.add_work(len(jobs))
        self.ctx.run_jobs(jobs)
        self.ctx.log("info", f"lerobot v3: {untouched} video file(s) untouched, {rebuilt} rebuilt "
                             f"without dropped episodes, {self.counters.videos_reencoded - rebuilt} "
                             "new file(s) for added episodes")

    def _check_frames(self, e: EpisodeEntry, vk: str, a: float, b: float) -> None:
        n = int(round((b - a) * self.fps))
        if n != e.length:
            self.ctx.log("warn", f"episode {e.episode_index} {vk}: {n} video frames for "
                                 f"{e.length} rows of data")

    def _rebuild_video(self, vk: str, fid: int, members: list[EpisodeEntry]) -> None:
        windows = []
        for e in members:
            rel, a, b = self.src[e.episode_index][3][vk]
            windows.append({"path": self.guard.path(rel), "from_ts": a, "to_ts": b})
        rel = self.video_rel(vk, fid)
        out = self.gen_path(rel)
        bounds = _reencode_concat(windows, out, self.fps)
        self.placer.place(out, rel)
        with self._lock:
            for e, (a, b) in zip(members, bounds, strict=True):
                self._check_frames(e, vk, a, b)
                self.new_win[(e.episode_index, vk)] = (fid, a, b)
            self.counters.videos_reencoded += 1
        self.ctx.work_done()

    def _encode_new(self, vk: str, first_id: int, adds: list[EpisodeEntry]) -> None:
        from ..adapters.decode import decode_window
        vw = _RollingVideoWriter(self.placer.gen_root, self.info["video_path"], vk, self.fps,
                                 self.video_mb * MIB, self.cs)
        vw.n_file = first_id                   # continue after the files already delivered
        for e in adds:
            rel, a, b = self.src[e.episode_index][3][vk]
            frames, _ = decode_window(self.guard.path(rel), a, b)
            c, f, s, t = vw.add(frames)
            with self._lock:
                self._check_frames(e, vk, s, t)
                self.new_win[(e.episode_index, vk)] = (c * self.cs + f, s, t)
        vw.close()
        with self._lock:
            self.counters.videos_reencoded += vw.n_file - first_id
        self.ctx.work_done()

    def _describe_videos(self, entries: list[EpisodeEntry]) -> None:
        """Content descriptor of each video file written now: its episodes' source windows."""
        members: dict[str, list] = defaultdict(list)
        for e in entries:
            for vk in self.video_keys:
                rel, a, b = self.src[e.episode_index][3][vk]
                members[e.videos[vk]].append((e.windows[vk][0], [e.content_key, vk, rel, a, b]))
        for rel, items in members.items():
            rec = self.placer.records.get(rel)
            if rec is not None and self.placer.was_written(rel):
                rec.desc = digest_json(["v3-video", [x for _t, x in sorted(items, key=lambda y: y[0])]])

    # frame tables
    def _data_desc(self, members: list[EpisodeEntry]) -> str:
        return digest_json(["v3-data", [[e.content_key, e.new_index, e.index_from, e.task_index,
                                         e.length] for e in members]])

    def _piece(self, e: EpisodeEntry, cache: dict) -> pd.DataFrame:
        src_rel, lo, hi, _w = self.src[e.episode_index]
        if src_rel not in cache:               # consecutive episodes share a source file
            cache.clear()
            cache[src_rel] = dsfs.read_parquet(self.guard.path(src_rel))
        df = cache[src_rel]
        base = int(df["index"].iloc[0])
        piece = df.iloc[lo - base: hi - base].copy()
        if len(piece) != e.length:
            raise ExportError(f"episode {e.episode_index}: {len(piece)} frames in {src_rel}, "
                              f"meta/episodes says {e.length}")
        # the columns v1's export_lerobot_v3 overwrites, the same way
        piece["episode_index"] = e.new_index
        piece["task_index"] = e.task_index
        piece["index"] = np.arange(e.index_from, e.index_from + len(piece), dtype=piece["index"].dtype)
        return piece

    def _layout(self, entries: list[EpisodeEntry], retained: set[int]) -> dict[int, int] | None:
        """Keep every retained episode in its data file; added ones join the file of the
        episode before them.  None when that cannot keep the files in index order."""
        if self.old is None or not retained:
            return None
        old_file = {e.episode_index: e.data_file for e in self.old.episodes}
        first = next(old_file[e.episode_index] for e in entries if e.episode_index in retained)
        out: dict[int, int] = {}
        prev = None
        for e in entries:
            if e.episode_index in retained:
                fid = old_file[e.episode_index]
                if prev is not None and fid < prev:
                    return None                # the passed list reordered episodes
                prev = fid
            out[e.episode_index] = prev if prev is not None else first
        return out

    def _write_data(self, entries: list[EpisodeEntry], retained: set[int]) -> None:
        layout = self._layout(entries, retained)
        cache: dict = {}
        if layout is None:
            dw = _RollingParquetWriter(self.placer.gen_root, self.info["data_path"],
                                       self.data_mb * MIB, self.cs)
            self.ctx.add_work(1)
            for e in entries:
                c, f = dw.add(self._piece(e, cache))
                e.data_file = c * self.cs + f
            dw.close()
            self.ctx.work_done()
            groups: dict[int, list[EpisodeEntry]] = defaultdict(list)
            for e in entries:
                groups[e.data_file].append(e)
            for fid, members in groups.items():
                self.placer.records[self.data_rel(fid)].desc = self._data_desc(members)
            self.ctx.log("info", f"lerobot v3: frame tables laid out afresh ({len(groups)} file(s))")
            return
        groups = defaultdict(list)
        for e in entries:
            e.data_file = layout[e.episode_index]
            groups[e.data_file].append(e)
        old_files = self.placer.old
        todo = []
        for fid, members in sorted(groups.items()):
            rel, desc = self.data_rel(fid), self._data_desc(members)
            if rel in old_files and old_files[rel].desc == desc:
                self.placer.record(rel, old_files[rel], written=False)
            else:
                todo.append((fid, members, desc))
        self.ctx.add_work(len(todo))
        for fid, members, desc in todo:
            dw = _RollingParquetWriter(self.placer.gen_root, self.info["data_path"], float("inf"), self.cs)
            dw.n_file = fid                    # write exactly this file
            for e in members:
                dw.add(self._piece(e, cache))
            dw.close()
            self.placer.records[self.data_rel(fid)].desc = desc
            self.ctx.work_done()
        self.ctx.log("info", f"lerobot v3: {len(groups) - len(todo)} frame table file(s) untouched, "
                             f"{len(todo)} rewritten")

    # ── meta ────────────────────────────────────────────────────────────────
    def build_meta(self, entries: list[EpisodeEntry], out_dir: str) -> None:
        info, cs, n = self.info, self.cs, len(entries)
        meta = os.path.join(out_dir, "meta")
        os.makedirs(meta, exist_ok=True)
        new_meta = self.sel.copy()        # source rows: stats/* and other columns inherited (v1)
        if self.has_override or self.src_tasks_missing:
            new_meta["tasks"] = [list(e.tasks) for e in entries]
        new_meta["episode_index"] = np.arange(n)
        new_meta["data/chunk_index"] = [e.data_file // cs for e in entries]
        new_meta["data/file_index"] = [e.data_file % cs for e in entries]
        new_meta["dataset_from_index"] = [e.index_from for e in entries]
        new_meta["dataset_to_index"] = [e.index_from + e.length for e in entries]
        new_meta["meta/episodes/chunk_index"] = 0
        new_meta["meta/episodes/file_index"] = 0
        for vk in self.video_keys:
            new_meta[f"videos/{vk}/chunk_index"] = [e.video_files[vk] // cs for e in entries]
            new_meta[f"videos/{vk}/file_index"] = [e.video_files[vk] % cs for e in entries]
            new_meta[f"videos/{vk}/from_timestamp"] = [e.windows[vk][0] for e in entries]
            new_meta[f"videos/{vk}/to_timestamp"] = [e.windows[vk][1] for e in entries]
        _to_parquet_fsx_safe(new_meta, os.path.join(meta, "episodes", "chunk-000", "file-000.parquet"),
                             index=False)
        tasks_df = pd.DataFrame({"task_index": range(len(self.task_strings))},
                                index=pd.Index(self.task_strings))
        _to_parquet_fsx_safe(tasks_df, os.path.join(meta, "tasks.parquet"))
        new_info = dict(info)
        new_info["total_episodes"] = n
        new_info["total_frames"] = int(self.total_frames)
        new_info["total_tasks"] = len(self.task_strings)
        new_info["splits"] = {"train": f"0:{n}"}
        if "total_videos" in new_info:
            new_info["total_videos"] = len(self.video_keys)
        write_json(os.path.join(meta, "info.json"), new_info, indent=2)
        if self.stats_mode == "source":
            dsfs.copy_to_local(self.guard.path("meta/stats.json"), os.path.join(meta, "stats.json"))
        keep = [e.episode_index for e in entries]
        _write_camera_health(out_dir, self.old_camera_health(), keep)
        _write_jsonl(os.path.join(meta, "curation_episodes.jsonl"),
                     [{"episode_index": e.new_index, "source_episode_index": e.episode_index,
                       "tasks": list(e.tasks), "instruction_source": e.task_source}
                      for e in entries])
