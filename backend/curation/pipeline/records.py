"""Per-episode result records and the files of a run directory (design doc 02 §3.5, 06 §1).

A record is one line of ``checks/<module>/parts/<NNNN>.jsonl`` and of the
compacted ``checks/<module>/results.jsonl``
(``docs/contracts/cli/result-record.schema.json``). ``verdict`` is derived from
v1's tri-state ``passed`` and ``score`` exactly as the parity tool derives it for
v1's results (``tools/parity/records.py``), so the two sides compare line by line:

* ``pass`` / ``fail`` - ``passed`` is True / False (hard gates, dedup);
* ``abstain``        - ``passed`` is None and there is no score;
* ``scored``         - ``passed`` is None with a score (soft modules never vote);
* ``error``          - the module could not judge the episode properly (D33).

``details`` go through the same JSON round trip as v1's daft column
(``result_to_struct``: ``json.dumps(detail, default=str)``), so a numpy scalar
ends up exactly as v1 wrote it.

Parts are append-only: each ``check`` call writes one part, a line is written in
full and fsync'ed before the next one starts, and the highest part wins when an
episode appears in several (a retry appends a part, never edits an old one).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Iterable
from typing import Any

from ..contracts import modules as registry

SCHEMA_VERSION = "1.0"

CHECKS_DIR = "checks"
PARTS_DIR = "parts"
RESULTS_NAME = "results.jsonl"
INFLIGHT_NAME = "inflight.json"
CRASHES_NAME = "crashes.json"
SOURCE_MANIFEST_NAME = "source_manifest.json"
PLAN_NAME = "plan.json"
AUTOLABEL_FILE = "autolabel/captions.jsonl"
USAGE_FILE = "usage.jsonl"
LATENCY_FILE = "details/vlm_latency.csv"
REVISIONS_DIR = "revisions"
ADJUDICATION_DIR = "adjudication"

_PART_RE = re.compile(r"^([0-9]{4})\.jsonl$")


# ---------------------------------------------------------------- records

def gate_of(module: str) -> str:
    try:
        return registry.get(module).gate
    except KeyError:
        return "none"


def derive_verdict(passed: bool | None, score: float | None, error: dict | None) -> str:
    if error:
        return "error"
    if passed is True:
        return "pass"
    if passed is False:
        return "fail"
    return "scored" if score is not None else "abstain"


def parse_detail(detail: Any) -> dict:
    """v1's detail JSON string -> dict (same rule as the parity tool)."""
    if isinstance(detail, dict):
        return detail
    if detail in (None, ""):
        return {}
    try:
        out = json.loads(detail)
    except (TypeError, ValueError):
        return {"_unparsed": str(detail)}
    return out if isinstance(out, dict) else {"_value": out}


def record_from_struct(module: str, episode_index: int, struct: dict | None, *,
                       incidents: list[dict] | None = None,
                       evidence: Iterable[str] | None = None,
                       elapsed_s: float | None = None) -> dict:
    """A ``{passed, score, detail}`` struct (v1's check column value) -> a record.

    ``passed`` / ``score`` are coerced the way daft's struct column coerces them
    (bool / float64), so a numpy scalar never leaks into the JSON line.
    """
    struct = struct or {}
    passed = struct.get("passed")
    passed = None if passed is None else bool(passed)
    score = struct.get("score")
    score = None if score is None else float(score)
    details = parse_detail(struct.get("detail"))
    error = {"kind": "execution", "incidents": list(incidents)} if incidents else None
    return {"episode_index": int(episode_index), "module": module,
            "verdict": derive_verdict(passed, score, error), "passed": passed,
            "score": score, "gate": gate_of(module), "details": details,
            "evidence": list(evidence or []),
            "elapsed_s": None if elapsed_s is None else round(float(elapsed_s), 3),
            "error": error}


def dumps_line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, allow_nan=True, sort_keys=True,
                      default=_json_default) + "\n"


def _json_default(obj: Any):
    if hasattr(obj, "tolist"):
        return obj.tolist()
    if hasattr(obj, "item"):
        return obj.item()
    return str(obj)


# ---------------------------------------------------------------- paths

def module_dir(run_dir: str, module: str) -> str:
    return os.path.join(run_dir, CHECKS_DIR, module)


def parts_dir(run_dir: str, module: str) -> str:
    return os.path.join(module_dir(run_dir, module), PARTS_DIR)


def part_ids(run_dir: str, module: str) -> list[str]:
    d = parts_dir(run_dir, module)
    if not os.path.isdir(d):
        return []
    return sorted(m.group(1) for m in (_PART_RE.match(n) for n in os.listdir(d)) if m)


def next_part(run_dir: str, modules: Iterable[str]) -> str:
    """One more than the highest part of any of ``modules`` (``0001`` for the first)."""
    top = 0
    for m in modules:
        for p in part_ids(run_dir, m):
            top = max(top, int(p))
    return f"{top + 1:04d}"


def revision_dir(run_dir: str, revision: int) -> str:
    return os.path.join(run_dir, REVISIONS_DIR, f"r{int(revision):04d}")


def committed_revisions(run_dir: str) -> list[int]:
    base = os.path.join(run_dir, REVISIONS_DIR)
    if not os.path.isdir(base):
        return []
    out = []
    for name in os.listdir(base):
        if len(name) == 5 and name[0] == "r" and name[1:].isdigit() \
                and os.path.isfile(os.path.join(base, name, "commit.json")):
            out.append(int(name[1:]))
    return sorted(out)


# ---------------------------------------------------------------- reading

def read_jsonl(path: str) -> list[dict]:
    """Every complete JSON line; a torn last line (a kill mid-write) is ignored."""
    out: list[dict] = []
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            if i >= len(lines) - 2:          # the last (possibly unterminated) line
                continue
            raise
    return out


def load_parts(run_dir: str, module: str) -> dict[int, tuple[str, dict]]:
    """``{episode_index: (part, record)}``: later lines win in a part, higher parts win."""
    out: dict[int, tuple[str, dict]] = {}
    for part in part_ids(run_dir, module):
        for rec in read_jsonl(os.path.join(parts_dir(run_dir, module), f"{part}.jsonl")):
            out[int(rec["episode_index"])] = (part, rec)
    return out


def latest_results(run_dir: str, module: str,
                   episodes: list[int] | None = None) -> dict[int, dict]:
    """The current record of every episode of ``module`` (from the parts)."""
    from .episode_state import EpisodeState, state_path

    index = state_path(run_dir)
    if index.is_file():
        store = EpisodeState(index)
        try:
            if store.indexed(module):
                return store.records(module, episodes)
        finally:
            store.close()
    parts = load_parts(run_dir, module)
    if parts:
        found = {i: rec for i, (_, rec) in parts.items()}
        return found if episodes is None else {e: found[e] for e in episodes if e in found}
    # a run directory restored from the delivery may only have the compacted file
    found = {int(r["episode_index"]): r
             for r in read_jsonl(os.path.join(module_dir(run_dir, module), RESULTS_NAME))}
    return found if episodes is None else {e: found[e] for e in episodes if e in found}


def parts_used(run_dir: str, module: str) -> list[str]:
    """The parts that hold at least one current record (what a revision was built from)."""
    return sorted({part for part, _ in load_parts(run_dir, module).values()})


def compact(run_dir: str, module: str) -> str:
    """Write ``results.jsonl``: one current record per episode, sorted by index."""
    path = os.path.join(module_dir(run_dir, module), RESULTS_NAME)
    from .episode_state import EpisodeState, state_path

    index = state_path(run_dir)
    if index.is_file():
        store = EpisodeState(index)
        try:
            if store.indexed(module):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
                with open(tmp, "w", encoding="utf-8") as fh:
                    for _, data in store.db.execute(
                            "SELECT episode, record FROM results WHERE module=? ORDER BY episode",
                            (module,)):
                        fh.write(dumps_line(json.loads(data)))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
                return path
        finally:
            store.close()
    rows = [rec for _, rec in sorted(latest_results(run_dir, module).items())]
    write_text_atomic(path, "".join(dumps_line(r) for r in rows))
    return path


# ---------------------------------------------------------------- writing

def write_text_atomic(path: str, text: str) -> None:
    """Write a whole file so readers see the old one or the complete new one."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_json_atomic(path: str, doc: Any, *, indent: int | None = 1) -> None:
    write_text_atomic(path, json.dumps(doc, ensure_ascii=False, indent=indent,
                                       allow_nan=True, default=_json_default) + "\n")


class AppendLog:
    """An append-only JSON Lines file: whole line, flush, fsync (SIGKILL-safe)."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(path, "a", encoding="utf-8")

    def write(self, record: dict) -> None:
        line = dumps_line(record)
        with self._lock:
            self._fh.write(line)
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()


class PartWriter:
    """The part files of one ``check`` call, one per module."""

    def __init__(self, run_dir: str, modules: Iterable[str], part: str, *, index: bool = True):
        self.part = part
        self._logs = {m: AppendLog(os.path.join(parts_dir(run_dir, m), f"{part}.jsonl"))
                      for m in modules}
        self._index = None
        if index:
            from .episode_state import EpisodeState, state_path

            path = state_path(run_dir)
            if path.is_file():
                self._index = EpisodeState(path)
        self._indexed = {m for m in self._logs if self._index is not None
                         and self._index.indexed(m)}

    def write(self, record: dict) -> None:
        self._logs[record["module"]].write(record)
        if self._index is not None and record["module"] in self._indexed:
            self._index.put_result(record)

    def close(self) -> None:
        for log in self._logs.values():
            log.close()
        if self._index is not None:
            self._index.close()


class Inflight:
    """``checks/<module>/inflight.json``: the episodes being worked on right now.

    The Daemon reads it after a crash to know which episodes were in the process's
    hands (design doc 04, section 7). An episode is added when its work starts and
    removed when its record is on disk; the file goes when the call ends cleanly.
    """

    def __init__(self, run_dir: str, modules: Iterable[str], part: str):
        self.paths = [os.path.join(module_dir(run_dir, m), INFLIGHT_NAME) for m in modules]
        self.part = part
        self._eps: set[int] = set()
        self._lock = threading.Lock()

    def _flush(self) -> None:
        doc = {"pid": os.getpid(), "part": self.part, "episodes": sorted(self._eps),
               "updated_at": int(time.time() * 1000)}
        for path in self.paths:
            write_json_atomic(path, doc, indent=None)

    def add(self, episode: int) -> None:
        with self._lock:
            self._eps.add(int(episode))
            self._flush()

    def remove(self, episode: int) -> None:
        with self._lock:
            self._eps.discard(int(episode))
            self._flush()

    def clear(self) -> None:
        with self._lock:
            self._eps.clear()
            for path in self.paths:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass


def read_inflight(run_dir: str, module: str) -> dict | None:
    path = os.path.join(module_dir(run_dir, module), INFLIGHT_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True
