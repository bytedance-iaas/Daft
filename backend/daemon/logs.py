"""Task logs on the work directory (design doc 03, section 11; 06 §1).

Layout under ``<work>/<task_id>/`` (the task's work directory, 00 §4.2):

* ``logs/<stage>.jsonl`` - the main run, one file per stage;
* ``logs/<subtask_id>/<stage>.jsonl`` - each subtask, so a retry of the ``vlm``
  stage never appends to the main run's ``vlm.jsonl``.

Each line is one C3 progress-protocol object (``progress.schema.json``) exactly
as the CLI wrote it to stderr, plus lines the Daemon adds itself (stage
``system``). ``GET /tasks/{id}/logs`` returns the ``kind: log`` lines.

Paging goes **backwards**: the first page is the newest lines ("follow the
latest", 07 §4.2), ``next_cursor`` leads to older ones. The cursor keeps, per
file, the byte offset below which lines have not been returned yet. Files only
grow at the end, so every line is returned exactly once however much is
appended while someone pages; lines written after the first page are newer
than it and show up on a fresh first page (or live over SSE).
"""
from __future__ import annotations

import heapq
import json
import os
import pathlib
import re
import threading
from typing import Any, Iterator

from .pagination import decode_cursor, encode_cursor
from .repo.protocol import CursorPage

LEVEL_RANK = {"debug": 0, "info": 1, "warn": 2, "error": 3}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CHUNK = 64 * 1024
#: Stop scanning after this many bytes per request; the page then ends early with has_more.
_SCAN_BUDGET = 64 * 1024 * 1024

ALL_RUNS = object()     # ``subtask`` filter: main run and every subtask


def _check_name(value: str, what: str) -> str:
    if not _NAME_RE.match(value or ""):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


class TaskLogs:
    def __init__(self, work_dir: os.PathLike | str):
        self.work_dir = pathlib.Path(work_dir)
        self._append_lock = threading.Lock()

    # -- layout ------------------------------------------------------------------
    def task_dir(self, task_id: str) -> pathlib.Path:
        return self.work_dir / _check_name(task_id, "task id")

    def logs_dir(self, task_id: str, subtask_id: str | None = None) -> pathlib.Path:
        base = self.task_dir(task_id) / "logs"
        return base / _check_name(subtask_id, "subtask id") if subtask_id else base

    def log_path(self, task_id: str, stage: str, subtask_id: str | None = None) -> pathlib.Path:
        return self.logs_dir(task_id, subtask_id) / f"{_check_name(stage, 'stage')}.jsonl"

    # -- writing (W5 and the Daemon's own system lines) ----------------------------
    def append(self, task_id: str, stage: str, line: dict, *, subtask_id: str | None = None) -> None:
        """Append one C3 line; one ``write`` per line so readers never see half of it."""
        path = self.log_path(task_id, stage, subtask_id)
        data = (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._append_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as f:
                f.write(data)

    # -- reading ------------------------------------------------------------------
    def _files(self, task_id: str, stage: str | None, subtask) -> list[tuple[str, str | None, pathlib.Path]]:
        """(key, subtask id, path) for every log file the filters select."""
        root = self.logs_dir(task_id)
        if not root.is_dir():
            return []
        runs: list[str | None]
        if subtask is ALL_RUNS:
            runs = [None] + sorted(p.name for p in root.iterdir()
                                   if p.is_dir() and _NAME_RE.match(p.name))
        else:
            runs = [subtask or None]
        out = []
        for run in runs:
            folder = root / run if run else root
            if not folder.is_dir():
                continue
            for p in sorted(folder.glob("*.jsonl")):
                name = p.name[: -len(".jsonl")]
                if not _NAME_RE.match(name) or (stage is not None and name != stage):
                    continue
                out.append((f"{run or ''}/{name}", run, p))
        return out

    def page(self, task_id: str, *, stage: str | None = None, subtask=ALL_RUNS,
             min_level: str | None = None, cursor: str | None = None,
             limit: int = 50) -> CursorPage[dict]:
        """C4 ``LogLine`` items, newest first. ``subtask``: ``ALL_RUNS``, ``""`` (main) or an id."""
        if limit < 1:
            raise ValueError("limit starts at 1")
        scope = {"task": task_id, "stage": stage,
                 "subtask": "*" if subtask is ALL_RUNS else (subtask or ""), "level": min_level}
        floor = LEVEL_RANK[min_level] if min_level else 0
        files = self._files(task_id, stage, subtask)
        if cursor:
            ends = decode_cursor(cursor, "logs", scope=scope)
            if not isinstance(ends, dict):
                raise ValueError("logs cursor must hold file offsets")
        else:
            ends = {key: _complete_size(path) for key, _, path in files}
        budget = [_SCAN_BUDGET]
        iters: list[tuple[str, str | None, _BackwardLines]] = []
        for key, run, path in files:
            end = ends.get(key)
            if not isinstance(end, int) or end <= 0:
                continue                      # appeared after the first page, or fully read
            iters.append((key, run, _BackwardLines(path, end, budget)))
        heap: list = []

        def push(i: int) -> None:
            """Put file ``i``'s newest unreturned matching line on the heap (if any)."""
            key, run, it = iters[i]
            for start, raw in it:
                item = _log_line(raw, key.split("/", 1)[1], run, floor)
                if item is not None:
                    heapq.heappush(heap, (-item["ts"], i, -start, item))
                    return

        for i in range(len(iters)):
            push(i)
        items: list[dict] = []
        new_ends = {k: v for k, v in ends.items() if isinstance(v, int)}
        while heap and len(items) < limit:
            _, i, neg_start, item = heapq.heappop(heap)
            items.append(item)
            new_ends[iters[i][0]] = -neg_start
            push(i)
        waiting = {entry[1] for entry in heap}
        for i, (key, _, it) in enumerate(iters):
            if i not in waiting:
                # everything from it.position up was returned or filtered out
                new_ends[key] = it.position
        has_more = bool(heap) or any(not it.finished for _, _, it in iters)
        new_ends = {k: v for k, v in new_ends.items() if v > 0}
        next_cursor = encode_cursor("logs", new_ends, scope=scope) if has_more else None
        return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)


def _complete_size(path: pathlib.Path) -> int:
    """File size up to the last newline (a line still being written is not served)."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size == 0:
        return 0
    with open(path, "rb") as f:
        pos = size
        while pos > 0:
            step = min(_CHUNK, pos)
            f.seek(pos - step)
            chunk = f.read(step)
            nl = chunk.rfind(b"\n")
            if nl >= 0:
                return pos - step + nl + 1
            pos -= step
    return 0


class _BackwardLines:
    """``(start_offset, line)`` for the complete lines in ``[0, end)``, newest first.

    ``end`` sits on a line boundary. ``position`` is the start of the last line
    handed out (everything from there to ``end`` has been seen); ``finished``
    says the scan reached the start of the file. ``budget`` (a one-item list,
    shared by the files of one request) caps the bytes read.
    """

    def __init__(self, path: pathlib.Path, end: int, budget: list[int]):
        self._path, self._budget = path, budget
        self.position = end
        self.finished = False
        self._gen = self._run(end)

    def __iter__(self) -> Iterator[tuple[int, bytes]]:
        return self._gen

    def _run(self, end: int) -> Iterator[tuple[int, bytes]]:
        try:
            f = open(self._path, "rb")
        except OSError:
            self.finished, self.position = True, 0
            return
        with f:
            pos, buf = end, b""               # buf = bytes [pos, position), ends with "\n"
            while pos > 0:
                if self._budget[0] <= 0:
                    return
                step = min(_CHUNK, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                self._budget[0] -= step
                while True:
                    nl = buf.rfind(b"\n", 0, len(buf) - 1)
                    if nl < 0:
                        break
                    start = pos + nl + 1
                    line = buf[nl + 1:-1]
                    buf = buf[: nl + 1]
                    self.position = start
                    if line:
                        yield start, line
            line = buf[:-1] if buf.endswith(b"\n") else buf
            self.position = 0
            self.finished = True
            if line:
                yield 0, line


def _log_line(raw: bytes, stage: str, subtask_id: str | None, floor: int) -> dict | None:
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get("kind") != "log":
        return None
    level, msg = obj.get("level"), obj.get("msg")
    if level not in LEVEL_RANK or not isinstance(msg, str) or LEVEL_RANK[level] < floor:
        return None
    ts = obj.get("ts")
    item: dict[str, Any] = {"ts": ts if isinstance(ts, int) and ts >= 0 else 0, "stage": stage,
                            "subtask_id": subtask_id, "level": level, "msg": msg}
    ep = obj.get("episode_index")
    item["episode_index"] = ep if isinstance(ep, int) and ep >= 0 else None
    return item
