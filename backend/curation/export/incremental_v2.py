"""Incremental export of a LeRobot v2 source (one parquet + one mp4 per camera per episode).

Per-file rules (design doc 06, section 4.3):

* frame table ``data/.../episode_NNNNNN.parquet``: its bytes depend on the source
  episode, its new ``episode_index``, its global frame offset and its ``task_index``.
  Unchanged -> untouched; otherwise rebuilt from the source exactly like v1's
  ``export_lerobot_v2`` does (read, overwrite the three columns,
  ``_to_parquet_fsx_safe``) and copied in.
* video ``videos/.../episode_NNNNNN.mp4``: a byte copy of the source video, so its
  bytes depend on the source file only.  Same place -> untouched (relabel, keep);
  other place -> renamed (renumber; no bytes copied); new -> copied whole from the
  source (add).  Nothing is ever decoded or re-encoded.
* ``meta/``: rebuilt every time (``episodes.jsonl``, ``tasks.jsonl``, ``info.json``,
  statistics, the camera-health and ``curation_episodes.jsonl`` sidecars), copied in
  only where the bytes differ.
"""
from __future__ import annotations

import json
import os
import threading
from collections import defaultdict

import numpy as np
import pandas as pd

from ..ingest import dsfs
from ..ingest.lerobot_reader import _v2_episode_list
from . import episode_stats
from .diff import Diff
from .incremental_base import FormatExporter, Wanted, task_table, written_tasks
from .lerobot_writer import (_copy_v2_stats, _to_parquet_fsx_safe, _write_camera_health,
                             _write_jsonl)
from .manifest import DATASET_DIR, EpisodeEntry, FileRecord, digest_json, task_key
from .safe_write import write_json
from .source import ExportError, ExportInputError

EPISODES_STATS = "meta/episodes_stats.jsonl"
STATS = "meta/stats.json"


class V2Exporter(FormatExporter):
    fmt = "lerobot_v2"

    # ── plan ────────────────────────────────────────────────────────────────
    def plan(self, wanted: list[Wanted]) -> list[EpisodeEntry]:
        info = self.info
        cs = int(info["chunks_size"])
        self.video_keys = [k for k, v in info["features"].items() if v["dtype"] == "video"]
        self.guard.require("meta/info.json")
        self.guard.require("meta/episodes.jsonl")
        for rel in ("meta/tasks.jsonl", STATS, EPISODES_STATS):
            self.guard.identity(rel)
        rows = {int(e["episode_index"]): e for e in _v2_episode_list(self.guard.root)}
        unknown = [w.episode_index for w in wanted if w.episode_index not in rows]
        if unknown:
            raise ExportInputError(f"episodes not in the source meta/episodes.jsonl: {unknown[:8]}")

        self.rows: dict[int, dict] = {}
        self.src_data: dict[int, str] = {}
        self.src_videos: dict[int, dict[str, str]] = {}
        self.video_ident: dict[tuple[int, str], list] = {}
        entries: list[EpisodeEntry] = []
        missing: list[str] = []
        for w in wanted:
            i = w.episode_index
            row = rows[i]
            tasks, label = written_tasks(w, list(row.get("tasks") or []))
            # same path derivation as ingest.lerobot_reader._v2_episode_paths, relative
            data_rel = info["data_path"].format(episode_chunk=i // cs, episode_index=i)
            ident_data = self.guard.require(data_rel)
            vids: dict[str, str] = {}
            idents = []
            for vk in self.video_keys:
                rel = info["video_path"].format(episode_chunk=i // cs, video_key=vk, episode_index=i)
                ident = self.guard.identity(rel)
                idents.append([vk, ident])
                if ident is None:
                    missing.append(f"ep{i:06d}/{vk}")     # v1: copy what exists, report the rest
                    continue
                vids[vk] = rel
                self.video_ident[(i, vk)] = ident
            self.rows[i] = row
            self.src_data[i] = data_rel
            self.src_videos[i] = vids
            entries.append(EpisodeEntry(
                episode_index=i, new_index=0,
                content_key=digest_json(["lerobot_v2", ident_data, idents, row]),
                task_key=task_key(tasks, label), tasks=tasks, task_source=label,
                task_index=0, length=int(row["length"]), index_from=0, parquet=""))
        if missing:
            self.ctx.log("warn", f"the source lacks {len(missing)} video file(s); they are "
                                 f"left out (first: {missing[:5]})")

        self.task_strings, task_index_of = task_table(
            self.old.task_table if self.old is not None else None, [e.tasks for e in entries])
        cursor = 0
        for k, e in enumerate(entries):
            e.new_index = k
            e.task_index = task_index_of[e.tasks[0]]
            e.index_from = cursor
            cursor += e.length
            e.parquet = info["data_path"].format(episode_chunk=k // cs, episode_index=k)
            e.videos = {vk: info["video_path"].format(episode_chunk=k // cs, video_key=vk,
                                                      episode_index=k)
                        for vk in self.src_videos[e.episode_index]}
        self.total_frames = cursor

        # v2.1 loaders read meta/episodes_stats.jsonl, v2.0 loaders meta/stats.json
        has_ep_stats = self.guard.identity(EPISODES_STATS) is not None
        has_stats = self.guard.identity(STATS) is not None
        version = str(info.get("codebase_version") or "")
        if has_ep_stats:
            self.stats_mode = "source"
        elif version >= "v2.1":
            self.stats_mode = "computed"
            self.ctx.log("info", "the source has no meta/episodes_stats.jsonl; computing it "
                                 "(the official v2.1 loader requires it)")
        elif has_stats:
            self.stats_mode = "source"
        else:
            self.stats_mode = "none"
            self.ctx.log("warn", "the v2.0 source has no meta/stats.json; the official loader "
                                 "will not open the delivery")
        return entries

    @staticmethod
    def parquet_desc(e: EpisodeEntry) -> str:
        return digest_json(["v2-parquet", e.content_key, e.new_index, e.index_from, e.task_index])

    def video_desc(self, e: EpisodeEntry, vk: str) -> str:
        return digest_json(["v2-video", self.video_ident[(e.episode_index, vk)]])

    # ── data + videos ───────────────────────────────────────────────────────
    def write_files(self, entries: list[EpisodeEntry], diff: Diff) -> None:
        old_files = self.placer.old if self.old is not None else {}
        wanted: dict[str, tuple[str, str, EpisodeEntry, str | None]] = {}
        for e in entries:
            wanted[e.parquet] = ("parquet", self.parquet_desc(e), e, None)
            for vk, rel in e.videos.items():
                wanted[rel] = ("video", self.video_desc(e, vk), e, vk)

        kept = {rel for rel, (_k, desc, _e, _v) in wanted.items()
                if rel in old_files and old_files[rel].desc == desc}
        spare: dict[str, list[str]] = defaultdict(list)     # desc -> old paths free to move
        for rel in sorted(old_files):
            if rel not in kept:
                spare[old_files[rel].desc].append(rel)
        renames: list[tuple[str, str]] = []
        to_make: list[str] = []
        for rel in sorted(wanted):
            kind, desc, _e, _vk = wanted[rel]
            if rel in kept:
                continue
            if kind == "video" and spare.get(desc):
                renames.append((spare[desc].pop(0), rel))
            else:
                to_make.append(rel)

        for rel in sorted(kept):
            self.placer.record(rel, old_files[rel], written=False)
        self.rename_files(renames)
        for src, dst in renames:
            rec = old_files[src]
            self.placer.record(dst, FileRecord(rec.size, rec.sha256, wanted[dst][1]), written=False)

        self.numeric: dict[int, dict] = {}
        self.images: dict[tuple[int, str], dict] = {}
        self._lock = threading.Lock()
        jobs = []
        for rel in to_make:
            kind, desc, e, vk = wanted[rel]
            if kind == "parquet":
                jobs.append(lambda e=e, rel=rel, desc=desc: self._make_parquet(e, rel, desc))
            else:
                jobs.append(lambda e=e, vk=vk, rel=rel, desc=desc: self._copy_video(e, vk, rel, desc))
        self.ctx.add_work(len(jobs))
        self.ctx.run_jobs(jobs)
        n_vid = sum(1 for rel in to_make if wanted[rel][0] == "video")
        self.ctx.log("info", f"lerobot v2: {len(kept)} file(s) untouched, {len(renames)} video(s) "
                             f"renamed, {len(to_make) - n_vid} frame table(s) written, "
                             f"{n_vid} video(s) copied from the source")

    def _make_parquet(self, e: EpisodeEntry, rel: str, desc: str) -> None:
        src = self.guard.path(self.src_data[e.episode_index])
        df = dsfs.read_parquet(src)
        if len(df) != e.length:
            raise ExportError(f"episode {e.episode_index}: {len(df)} frames in {src}, but "
                              f"meta/episodes.jsonl says {e.length}")
        # the three columns v1's export_lerobot_v2 overwrites, the same way
        df["episode_index"] = e.new_index
        if "task_index" in df.columns:
            df["task_index"] = e.task_index
        if "index" in df.columns:
            df["index"] = np.arange(e.index_from, e.index_from + len(df), dtype=df["index"].dtype)
        out = self.gen_path(rel)
        _to_parquet_fsx_safe(df, out, index=False)
        self.placer.place(out, rel, desc)
        if self.stats_mode == "computed":
            stats = episode_stats.numeric_stats(df, self.info["features"])
            with self._lock:
                self.numeric[e.episode_index] = stats
        self.ctx.work_done()

    def _copy_video(self, e: EpisodeEntry, vk: str, rel: str, desc: str) -> None:
        src = self.guard.path(self.src_videos[e.episode_index][vk])
        local = src
        if dsfs.is_remote(src):
            # the SDK may write ranges in any order: land it on local scratch first
            local = self.ctx.scratch_path("dl", f"{e.episode_index}-{vk}.mp4")
            dsfs.copy_to_local(src, local)
        try:
            size, digest = self.target.put_file(local, f"{DATASET_DIR}/{rel}")
            if self.stats_mode == "computed":
                st = episode_stats.video_stats(local, e.length)
                if st is not None:
                    with self._lock:
                        self.images[(e.episode_index, vk)] = st
        finally:
            if local != src:
                os.remove(local)
        self.placer.record(rel, FileRecord(size, digest, desc), written=True)
        with self._lock:
            self.counters.videos_copied += 1
        self.ctx.work_done()

    # ── meta ────────────────────────────────────────────────────────────────
    def build_meta(self, entries: list[EpisodeEntry], out_dir: str) -> None:
        info = self.info
        cs = int(info["chunks_size"])
        n = len(entries)
        meta = os.path.join(out_dir, "meta")
        os.makedirs(meta, exist_ok=True)
        new_eps = []
        for e in entries:
            row = dict(self.rows[e.episode_index])     # extra source columns are inherited (v1)
            row["episode_index"] = e.new_index
            row["tasks"] = list(e.tasks)
            row["length"] = e.length
            new_eps.append(row)
        _write_jsonl(os.path.join(meta, "episodes.jsonl"), new_eps)
        _write_jsonl(os.path.join(meta, "tasks.jsonl"),
                     [{"task_index": i, "task": t} for i, t in enumerate(self.task_strings)])
        new_info = dict(info)
        new_info["total_episodes"] = n
        new_info["total_frames"] = int(self.total_frames)
        new_info["total_tasks"] = len(self.task_strings)
        new_info["total_videos"] = sum(len(e.videos) for e in entries)
        new_info["total_chunks"] = (n + cs - 1) // cs
        new_info["splits"] = {"train": f"0:{n}"}
        write_json(os.path.join(meta, "info.json"), new_info, indent=2)

        keep = [e.episode_index for e in entries]
        if self.stats_mode in ("source", "computed"):
            _copy_v2_stats(self.guard.root, out_dir, keep)   # v1: whatever the source has
        if self.stats_mode == "computed":
            _write_jsonl(os.path.join(meta, "episodes_stats.jsonl"), self._computed_stats(entries))
        _write_camera_health(out_dir, self.old_camera_health(), keep)
        _write_jsonl(os.path.join(meta, "curation_episodes.jsonl"),
                     [{"episode_index": e.new_index, "source_episode_index": e.episode_index,
                       "tasks": list(e.tasks), "instruction_source": e.task_source}
                      for e in entries])

    def _computed_stats(self, entries: list[EpisodeEntry]) -> list[dict]:
        """episodes_stats.jsonl rows when the source has none: fresh numbers for frame
        tables written now, the previous export's for the rest (their bytes did not
        change), video stats computed once when the video was first copied in."""
        old_rows: dict[int, dict] = {}
        if self.old is not None and self.old.stats == "computed":
            by_new = {e.new_index: e.episode_index for e in self.old.episodes}
            rel = f"{DATASET_DIR}/{EPISODES_STATS}"
            if self.target.exists(rel):
                for line in self.target.read_bytes(rel).decode("utf-8").splitlines():
                    if line.strip():
                        r = json.loads(line)
                        src = by_new.get(int(r["episode_index"]))
                        if src is not None:
                            old_rows[src] = r.get("stats") or {}
        out = []
        for e in entries:
            i = e.episode_index
            prev = old_rows.get(i, {})
            stats = {}
            numeric = self.numeric.get(i)
            if numeric is None:
                numeric = {k: v for k, v in prev.items() if k not in e.videos}
                if not numeric:
                    df = pd.read_parquet(self.target.path(f"{DATASET_DIR}/{e.parquet}"))
                    numeric = episode_stats.to_json(
                        episode_stats.numeric_stats(df, self.info["features"]))
            else:
                numeric = episode_stats.to_json(numeric)
            stats.update(numeric)
            for vk, rel in e.videos.items():
                img = self.images.get((i, vk))
                if img is not None:
                    stats[vk] = episode_stats.to_json({vk: img})[vk]
                elif vk in prev:
                    stats[vk] = prev[vk]
                else:
                    st = episode_stats.video_stats(self.target.path(f"{DATASET_DIR}/{rel}"),
                                                   e.length)
                    if st is not None:
                        stats[vk] = episode_stats.to_json({vk: st})[vk]
            out.append({"episode_index": e.new_index, "stats": stats})
        return out
