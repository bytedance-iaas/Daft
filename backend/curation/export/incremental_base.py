"""Machinery shared by the two incremental export paths (LeRobot v2 and v3).

The flow of one export (``FormatExporter.run``):

1. ``plan``: read the source metadata, resolve every passed episode's task text,
   number the episodes, compute ``content_key`` / ``task_key`` and the output paths.
2. the journal ``_EXPORTING`` is written before the export directory is touched.
3. ``write_files``: data and video files, format specific.  Unchanged files are left
   alone, moved files are renamed, everything else is produced in local scratch and
   copied in whole (``LocalTarget.put_file``).
4. meta files are always rebuilt (KB-sized) in scratch and copied in only when their
   bytes differ from what is there.
5. whatever is under ``lerobot_curated/`` and not part of the new export is deleted.
6. ``manifest.detail.json`` then ``manifest.json`` (the commit point), then the
   journal is removed.

A crash anywhere in 3-6 leaves the journal behind; the next export then does not
trust the directory and rebuilds it from scratch.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .diff import Diff
from .manifest import (DATASET_DIR, DETAIL_NAME, EXPORT_IMPL_VERSION, JOURNAL_NAME,
                       JOURNAL_SCHEMA, MANIFEST_NAME, TASK_SOURCE_CAPTION, TASK_SOURCE_HUMAN,
                       TASK_SOURCE_NONE, TASK_SOURCE_ORIGINAL, TASK_SOURCES, EpisodeEntry,
                       ExportState, FileRecord, check_manifest)
from .source import ExportInputError, SourceGuard
from .target import TEMP_PREFIX, LocalTarget, sha256_file

LogFn = Callable[[str, str], None]           # (level, message)
ProgressFn = Callable[[int, int], None]      # (done, total)


# ── inputs ───────────────────────────────────────────────────────────────────

@dataclass
class Wanted:
    """One entry of passed.json: an episode the delivery must contain."""

    episode_index: int
    task_text: str | None = None
    task_source: str | None = None


def parse_passed(doc) -> tuple[list[Wanted], list[dict]]:
    """``passed.json`` (a path, the document, or its ``episodes`` list) -> wanted episodes.

    Order is kept: it is the order of the delivered dataset.
    """
    if isinstance(doc, (str, os.PathLike)):
        with open(doc, encoding="utf-8") as f:
            doc = json.load(f)
    if isinstance(doc, dict):
        if doc.get("list", "passed") != "passed":
            raise ExportInputError(f"expected the passed list, got list={doc.get('list')!r}")
        entries = list(doc.get("episodes") or [])
    else:
        entries = list(doc)
    wanted: list[Wanted] = []
    seen: set[int] = set()
    for e in entries:
        try:
            idx = int(e["episode_index"])
        except (KeyError, TypeError, ValueError):
            raise ExportInputError(f"passed entry without a valid episode_index: {e!r}") from None
        if idx < 0 or idx in seen:
            raise ExportInputError(f"episode {idx} is negative or listed twice in passed")
        seen.add(idx)
        tt = e.get("task_text") or {}
        src = tt.get("source")
        if src is not None and src not in TASK_SOURCES:
            raise ExportInputError(f"episode {idx}: unknown task_text.source {src!r}")
        text = tt.get("text")
        wanted.append(Wanted(idx, None if text is None else str(text), src))
    return wanted, entries


def task_table(previous: list[str] | None,
               tasks_per_episode: list[list[str]]) -> tuple[list[str], dict[str, int]]:
    """The task table (``meta/tasks``) and each text's ``task_index``.

    A first or full export uses v1's rule (``lerobot_writer._task_table``: first
    appearance, de-duplicated).  An incremental export keeps the previous table's
    numbers: a slot nobody uses any more goes to a new text first (a relabel that
    replaces a text moves nothing), a slot still free after that is filled with the
    last text, and the remaining new texts are appended.  With v1's rule one relabel
    to a new text would renumber every later text and rewrite every later frame table.
    """
    from .lerobot_writer import _task_table

    first, _ = _task_table(tasks_per_episode)
    if previous is None:
        return first, {t: i for i, t in enumerate(first)}
    used = set(first)
    table: list[str | None] = [str(t) if str(t) in used else None for t in previous]
    have = {t for t in table if t is not None}
    new = [t for t in first if t not in have]
    for i in [i for i, t in enumerate(table) if t is None]:
        if not new:
            break
        table[i] = new.pop(0)
    while None in table:
        while table and table[-1] is None:
            table.pop()
        if None in table:
            table[table.index(None)] = table.pop()
    out = [t for t in table if t is not None] + new
    return out, {t: i for i, t in enumerate(out)}


def written_tasks(w: Wanted, source_tasks: list[str]) -> tuple[list[str], str]:
    """The task texts written for an episode and their ``instruction_source``.

    v1 semantics (``pipeline/run.py`` / ``rejudge.py`` building ``task_overrides``): a
    caption or a human relabel replaces the episode's tasks with that one text; the
    original annotation (or nothing) keeps the source's tasks as they are.
    """
    text = w.task_text or ""
    if w.task_source in (TASK_SOURCE_CAPTION, TASK_SOURCE_HUMAN) and text.strip():
        return [text], w.task_source
    tasks = [str(t) for t in source_tasks] or [""]
    if w.task_source in (TASK_SOURCE_ORIGINAL, TASK_SOURCE_NONE):
        return tasks, w.task_source
    return tasks, (TASK_SOURCE_ORIGINAL if any(t.strip() for t in tasks) else TASK_SOURCE_NONE)


# ── run context: logging, progress, scratch, worker threads ─────────────────

def _default_log(level: str, msg: str) -> None:
    print(f"[export] {level}: {msg}", file=sys.stderr, flush=True)


class _LineWriter(io.TextIOBase):
    """stdout replacement that turns v1's progress prints into log lines (the CLI's
    stdout carries only the final JSON)."""

    def __init__(self, log: LogFn):
        self._log = log
        self._buf = ""
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self._log("info", line.strip())
        return len(s)

    def flush(self) -> None:
        with self._lock:
            if self._buf.strip():
                self._log("info", self._buf.strip())
            self._buf = ""


class RunContext:
    def __init__(self, *, log: LogFn | None = None, progress: ProgressFn | None = None,
                 scratch_dir: str | None = None, concurrency: int = 1):
        self._log = log or _default_log
        self._progress = progress
        base = scratch_dir or os.environ.get("CURATION_EXPORT_SCRATCH") or tempfile.gettempdir()
        os.makedirs(base, exist_ok=True)
        self.scratch = tempfile.mkdtemp(prefix="curation-export-", dir=base)
        self.concurrency = max(1, int(concurrency or 1))
        self._done = 0
        self._total = 0
        self._last_emit = 0.0
        self._lock = threading.Lock()

    def log(self, level: str, msg: str) -> None:
        self._log(level, msg)

    def scratch_path(self, *parts: str) -> str:
        p = os.path.join(self.scratch, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def add_work(self, n: int) -> None:
        with self._lock:
            self._total += n
        self._emit(force=True)

    def work_done(self, n: int = 1) -> None:
        with self._lock:
            self._done += n
        self._emit()

    def _emit(self, force: bool = False) -> None:
        """At most one progress call a second (plus every change of the total and the
        last step): a big export has hundreds of thousands of files."""
        if self._progress is None:
            return
        with self._lock:
            now = time.monotonic()
            if not (force or self._done >= self._total or now - self._last_emit >= 1.0):
                return
            self._last_emit = now
            done, total = self._done, self._total
        self._progress(done, total)

    @contextlib.contextmanager
    def capture_stdout(self):
        w = _LineWriter(self._log)
        with contextlib.redirect_stdout(w):
            try:
                yield
            finally:
                w.flush()

    def run_jobs(self, jobs: list[Callable[[], None]]) -> None:
        """Run independent jobs; one at a time unless ``concurrency`` > 1."""
        if self.concurrency == 1 or len(jobs) <= 1:
            for job in jobs:
                job()
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = [pool.submit(job) for job in jobs]
            for fut in futures:
                fut.result()

    def close(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)


# ── placing files into the export directory ─────────────────────────────────

class Placer:
    """Copies finished scratch files into ``lerobot_curated/`` and records them.

    It is also the ``export.publish`` hook while v1's rolling writers run: they call
    ``publish.file_done(path)`` when a file is sealed, which lands here.  A file whose
    bytes equal what is already at its destination is not copied (the destination
    is not touched).  Scratch copies are deleted once placed, so scratch holds only
    the files being written.
    """

    def __init__(self, target: LocalTarget, gen_root: str, old: dict[str, FileRecord]):
        self.target = target
        self.gen_root = os.path.abspath(gen_root)
        self.old = old
        self.records: dict[str, FileRecord] = {}
        self.written: list[str] = []            # copied in by this export, in order
        self._written: set[str] = set()
        self._lock = threading.Lock()

    def place(self, local_path: str, rel: str, desc: str | None = None) -> FileRecord:
        """Copy a scratch file to ``lerobot_curated/<rel>`` unless those bytes are already
        there; the scratch file is deleted either way."""
        size, digest = sha256_file(local_path)
        prev = self.old.get(rel)
        same = (prev is not None and prev.sha256 == digest and prev.size == size
                and self.target.size(f"{DATASET_DIR}/{rel}") == size)
        if not same:
            size, digest = self.target.put_file(local_path, f"{DATASET_DIR}/{rel}")
        with contextlib.suppress(OSError):
            os.remove(local_path)
        rec = FileRecord(size=size, sha256=digest, desc=desc or digest)
        self.record(rel, rec, written=not same)
        return rec

    def record(self, rel: str, rec: FileRecord, *, written: bool) -> None:
        with self._lock:
            self.records[rel] = rec
            if written and rel not in self._written:
                self._written.add(rel)
                self.written.append(rel)

    def was_written(self, rel: str) -> bool:
        return rel in self._written

    # the publish hook (a duck-typed export.publish.Publisher): v1's rolling writers
    # call publish.file_done(path) when they seal a file
    def file_done(self, path: str) -> bool:
        rel = os.path.relpath(os.path.abspath(path), self.gen_root).replace(os.sep, "/")
        if rel.startswith(".."):
            raise ValueError(f"{path} is not under the export scratch {self.gen_root}")
        self.place(path, rel)
        return True


# ── previous export ─────────────────────────────────────────────────────────

@dataclass
class PreviousExport:
    state: ExportState | None          # trusted for an incremental export, or None
    reasons: list[str] = field(default_factory=list)   # why it is not trusted
    manifest: dict | None = None       # the raw manifest, when it at least parses


def load_previous(target: LocalTarget, *, source_format: str, codebase_version: str,
                  params: dict) -> PreviousExport:
    """The previous export in ``target`` and whether an incremental export may build on it."""
    if not target.exists(MANIFEST_NAME):
        return PreviousExport(None, ["no previous export"])
    try:
        manifest = target.read_json(MANIFEST_NAME)
    except (OSError, ValueError) as e:
        return PreviousExport(None, [f"manifest.json unreadable ({e})"])
    raw = manifest if not check_manifest(manifest) else None
    if target.exists(JOURNAL_NAME):
        try:
            journal = target.read_json(JOURNAL_NAME)
        except (OSError, ValueError):
            journal = {}
        if journal.get("to") != manifest.get("fingerprint"):
            return PreviousExport(None, ["the previous export was interrupted"], raw)
    try:
        detail = target.read_json(DETAIL_NAME)
        state = ExportState.from_documents(manifest, detail)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return PreviousExport(None, [f"previous manifest not usable ({e})"], raw)
    if detail.get("impl") != EXPORT_IMPL_VERSION:
        return PreviousExport(None, [f"exporter version changed ({detail.get('impl')} -> "
                                     f"{EXPORT_IMPL_VERSION})"], raw)
    if state.source_format != source_format or state.codebase_version != codebase_version:
        return PreviousExport(None, [f"source format changed ({state.source_format} "
                                     f"{state.codebase_version} -> {source_format} "
                                     f"{codebase_version})"], raw)
    if state.params != params:
        return PreviousExport(None, [f"export parameters changed ({state.params} -> {params})"], raw)
    present = {k[len(DATASET_DIR) + 1:]: v for k, v in target.list_files(DATASET_DIR).items()}
    bad = sorted(rel for rel, rec in state.files.items() if present.get(rel) != rec.size)
    if bad:
        return PreviousExport(None, [f"{len(bad)} delivered file(s) missing or changed "
                                     f"since the previous export, e.g. {bad[:3]}"], raw)
    return PreviousExport(state, [], raw)


# ── the common part of an export ────────────────────────────────────────────

@dataclass
class Counters:
    videos_copied: int = 0
    videos_reencoded: int = 0
    renamed: int = 0


class FormatExporter:
    """Base of the v2 / v3 exporters; subclasses implement plan / write_files / build_meta."""

    fmt = ""

    def __init__(self, ctx: RunContext, guard: SourceGuard, info: dict, target: LocalTarget,
                 old: ExportState | None, *, camera_health: dict | None, params: dict):
        self.ctx = ctx
        self.guard = guard
        self.info = info
        self.target = target
        self.old = old
        self.camera_health = camera_health
        self.params = params                # requested (normalized) parameters
        self.counters = Counters()
        self.placer = Placer(target, os.path.join(ctx.scratch, "gen"),
                             old.files if old is not None else {})
        self.stats_mode = "none"
        self.renamed: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def set_previous(self, state: ExportState | None) -> None:
        """Build on ``state`` (a trusted previous export) instead of starting empty."""
        self.old = state
        self.placer.old = state.files if state is not None else {}

    # subclass API
    def resolved_params(self) -> dict:
        return {}

    def plan(self, wanted: list[Wanted]) -> list[EpisodeEntry]:
        raise NotImplementedError

    def write_files(self, entries: list[EpisodeEntry], diff: Diff) -> None:
        raise NotImplementedError

    def build_meta(self, entries: list[EpisodeEntry], out_dir: str) -> None:
        raise NotImplementedError

    # helpers for subclasses
    def gen_path(self, rel: str) -> str:
        return self.ctx.scratch_path("gen", *rel.split("/"))

    def old_camera_health(self) -> dict | None:
        """The previous export's camera-health sidecar, in the shape ``_write_camera_health``
        takes (v1 ``rejudge`` carries it across re-exports the same way)."""
        rel = f"{DATASET_DIR}/meta/curation_camera_health.json"
        if self.camera_health is not None or not self.target.exists(rel):
            return self.camera_health
        try:
            old = self.target.read_json(rel)
        except (OSError, ValueError):
            return None
        return {"dataset": old.get("dataset") or {},
                "episodes": {r["source_episode_id"]: r for r in old.get("episodes") or []
                             if r.get("source_episode_id")}}

    def rename_files(self, renames: list[tuple[str, str]]) -> None:
        """Dataset-relative renames, in two phases so that swaps and shifts are safe."""
        if not renames:
            return
        stage = self.target.make_stage_dir(DATASET_DIR)
        moved = []
        for k, (src, dst) in enumerate(renames):
            tmp = f"{stage}/{k:06d}"
            self.target.rename(f"{DATASET_DIR}/{src}", tmp)
            moved.append((tmp, dst))
        for tmp, dst in moved:
            self.target.rename(tmp, f"{DATASET_DIR}/{dst}")
        self.target.remove_tree(stage)
        self.counters.renamed += len(renames)
        self.renamed.extend(renames)

    # the common flow
    def run(self, entries: list[EpisodeEntry], diff: Diff, fingerprint: str) -> ExportState:
        self.target.write_json(JOURNAL_NAME, {
            "schema": JOURNAL_SCHEMA, "from": self.old.fingerprint if self.old else None,
            "to": fingerprint, "started_at": int(time.time() * 1000), "pid": os.getpid()})
        if self.old is None:
            # full export: nothing already there is trusted
            self.target.remove_tree(DATASET_DIR)
        if entries:
            self.write_files(entries, diff)
            meta_root = os.path.join(self.ctx.scratch, "meta-out")
            self.build_meta(entries, meta_root)
            meta_files = self._place_tree(meta_root)
        else:
            self.target.remove_tree(DATASET_DIR)
            meta_files = []
            self.ctx.log("warn", "the passed list is empty: the delivery holds no dataset")
        self._delete_leftovers()
        state = ExportState(source_format=self.fmt, fingerprint=fingerprint, episodes=entries,
                            meta_files=meta_files, files=dict(self.placer.records),
                            codebase_version=str(self.info.get("codebase_version") or ""),
                            params=self.resolved_params(),
                            source={"uri": self.guard.root, "digest": self.guard.digest()},
                            stats=self.stats_mode, task_table=list(self.task_strings))
        self.target.write_json(DETAIL_NAME, state.detail(), compact=True)
        self.target.write_json(MANIFEST_NAME, state.manifest())
        self.target.delete(JOURNAL_NAME)
        return state

    def _place_tree(self, root: str) -> list[str]:
        placed = []
        for cur, _dirs, names in os.walk(root):
            for name in sorted(names):
                full = os.path.join(cur, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if any(p.startswith(TEMP_PREFIX) for p in rel.split("/")):
                    continue
                self.placer.place(full, rel)
                placed.append(rel)
        return sorted(placed)

    def _delete_leftovers(self) -> None:
        """Delete everything under lerobot_curated/ that is not part of this export."""
        keep = set(self.placer.records)
        for full_rel in sorted(self.target.list_files(DATASET_DIR)):
            rel = full_rel[len(DATASET_DIR) + 1:]
            if rel not in keep:
                self.target.delete(full_rel)
                self.deleted.append(rel)
