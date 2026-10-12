"""Per-episode result records and the files of a run directory (design doc 02 §3.5, 06 §1).

A record is one line of ``checks/<module>/parts/<NNNN>.jsonl`` and of the
compacted ``checks/<module>/results.jsonl`` (``docs/contracts/cli/result-record.schema.json``,
design doc 17 §1): ``status`` (ok / error, D33), the module's findings, the taxonomy items it
assessed and those it could not, readings and the module's own ``details``. A check still
answers with v1's ``{passed, score, detail}`` struct; :func:`record_from_struct` turns it into
findings. ``details`` go through a JSON round trip (``json.dumps(detail, default=str)``), so a
numpy scalar never reaches the line.

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

#: The format ``record_from_struct`` writes (C2 result-record): 2.0 since the policy verdicts (F12.3).
RECORD_VERSION = "2.0"

CHECKS_DIR = "checks"
PARTS_DIR = "parts"
RESULTS_NAME = "results.jsonl"
INFLIGHT_NAME = "inflight.json"
CRASHES_NAME = "crashes.json"
SOURCE_MANIFEST_NAME = "source_manifest.json"
PLAN_NAME = "plan.json"
USAGE_FILE = "usage.jsonl"
LATENCY_FILE = "details/vlm_latency.csv"
REVISIONS_DIR = "revisions"
ADJUDICATION_DIR = "adjudication"

_PART_RE = re.compile(r"^([0-9]{4})\.jsonl$")


# ---------------------------------------------------------------- records

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
                       elapsed_s: float | None = None, params: dict | None = None,
                       context: dict | None = None) -> dict:
    """A ``{passed, score, detail}`` struct (v1's check column value) -> a record 2.0.

    ``passed`` / ``score`` are coerced the way daft's struct column coerces them
    (bool / float64), so a numpy scalar never leaks into the JSON line, and turned into
    findings with :mod:`.findings` (design doc 17 §1), judged with the module's ``params``
    (defaults filled in).
    """
    struct = struct or {}
    passed = struct.get("passed")
    passed = None if passed is None else bool(passed)
    score = struct.get("score")
    score = None if score is None else float(score)
    details = parse_detail(struct.get("detail"))
    elapsed = None if elapsed_s is None else round(float(elapsed_s), 3)
    if incidents and module in DECODING_MODULES:
        from .incidents import corrupt_video

        if all(corrupt_video(i) for i in incidents):      # D93: the video is corrupt, not the run
            return record_undecodable(module, episode_index, passed, score, details, list(incidents),
                                      evidence=evidence, elapsed_s=elapsed, params=params, context=context)
    error = {"kind": "execution", "incidents": list(incidents)} if incidents else None
    return record_v2(module, episode_index, passed, score, details, error=error,
                     evidence=evidence, elapsed_s=elapsed, params=params, context=context)


#: the modules that decode an episode's videos themselves and report a corrupt one (design doc 28 §2.2, D93)
DECODING_MODULES = ("visual_quality", "video_action_sync", "task_success")
_CAMERA_IN_PATH = re.compile(r"observation\.images\.([A-Za-z0-9_.-]+?)/")


def _camera_of(incident: dict) -> str | None:
    if incident.get("camera"):
        return str(incident["camera"]).split(".")[-1]
    m = _CAMERA_IN_PATH.search(str(incident.get("cause") or ""))
    return m.group(1) if m else None


def record_undecodable(module: str, episode_index: int, passed: bool | None, score: float | None, details: dict,
                       incidents: list[dict], *, evidence: Iterable[str] | None = None,
                       elapsed_s: float | None = None, params: dict | None = None,
                       context: dict | None = None) -> dict:
    """A record of an episode whose video the decoder found corrupt (design doc 28 §2.2, D93): ``status: ok`` with a
    blocking ``decode_failed`` finding per camera (FILE-4), what the module could still judge from the other cameras
    (frame stage), and every other item unassessable (``video_undecodable``). The incidents stay in ``details``."""
    from . import findings

    judged = (passed is not None or score is not None) and not details.get("internal_error")
    got = findings.derive(module, passed, score, details, params, context) if judged else findings.Derived()
    by_camera: dict[str | None, str] = {}
    for i in incidents:
        by_camera.setdefault(_camera_of(i), str(i.get("cause") or ""))
    for cam, cause in by_camera.items():
        # the decoder's own words stay in the readings and in details.undecodable
        got.add(module, "decode_failed", (f"{cam} 相机的" if cam else "") + "视频解不开，文件已损坏",
                scope={"camera": cam} if cam else None, readings={"cause": cause[:200]})
    if not judged:
        for item in registry_get(module).covers:
            if item != "FILE-4":
                got.cannot(item, "video_undecodable", "视频解不开（文件损坏）")
    details = {**details, "undecodable": incidents}
    return {"episode_index": int(episode_index), "module": module, "details": details,
            "evidence": list(evidence or []), "elapsed_s": elapsed_s, "status": "ok",
            "findings": got.findings, "assessed": findings.assessed(module, got.unassessable),
            "unassessable": got.unassessable, "readings": got.readings, "error": None}


#: why a module wrote a record without judging the episode (``details.skipped``, design doc 28 §3, D94)
SKIP_NO_TASK_TEXT = "no_task_text"
SKIP_INTEGRITY = "integrity_rejected"


def record_skipped(module: str, episode_index: int, reason: str, text: str, *,
                   elapsed_s: float | None = None) -> dict:
    """A record of an episode the module did not look at (D92: data integrity rejected it): ``status: ok``, no
    findings, every item it covers unassessable for ``reason``. Not an error: retries leave it alone."""
    title = dict(registry_reasons()).get(reason, reason)
    return {"episode_index": int(episode_index), "module": module,
            "details": {"skipped": reason, "reason": text[:300]},
            "evidence": [], "elapsed_s": elapsed_s, "status": "ok", "findings": [], "assessed": [],
            "unassessable": [{"item": item, "reason": reason, "message_zh": title}
                             for item in registry_get(module).covers],
            "readings": {}, "error": None}


def skipped_reason(rec: dict | None) -> str | None:
    """Why the record holds no judgement (``details.skipped``), or None (D94)."""
    if not isinstance(rec, dict) or rec.get("status") != "ok":
        return None
    reason = (rec.get("details") or {}).get("skipped")
    return str(reason) if reason else None


def registry_reasons() -> tuple[tuple[str, str], ...]:
    from ..contracts import modules as registry

    return registry.UNASSESSABLE_REASONS


def record_v2(module: str, episode_index: int, passed: bool | None, score: float | None,
              details: dict, *, error: dict | None = None, evidence: Iterable[str] | None = None,
              elapsed_s: float | None = None, params: dict | None = None,
              context: dict | None = None) -> dict:
    """A result record 2.0 (C2 ``record_2``): ``status``, findings and coverage from the module's answer."""
    from . import findings

    base = {"episode_index": int(episode_index), "module": module, "details": details,
            "evidence": list(evidence or []), "elapsed_s": elapsed_s}
    if error:
        return {**base, "status": "error", "findings": [], "assessed": [], "unassessable": [],
                "readings": {}, "error": error}
    got = findings.derive(module, passed, score, details, params, context)
    return {**base, "status": "ok", "findings": got.findings,
            "assessed": findings.assessed(module, got.unassessable),
            "unassessable": got.unassessable, "readings": got.readings, "error": None}


# ---------------------------------------------------------------- reading

def is_error(rec: dict | None) -> bool:
    """The module could not judge the episode (D33)."""
    return isinstance(rec, dict) and rec.get("status") == "error"


def score_of(rec: dict | None) -> float | None:
    """The module's score: ``readings.score`` (soft scores are readings, P18)."""
    if not isinstance(rec, dict):
        return None
    v = (rec.get("readings") or {}).get("score")
    return None if v is None or isinstance(v, bool) else float(v)


def default_level(module: str, finding: dict) -> str:
    """A finding's level under the default policy (its code's level in the registry)."""
    try:
        return registry_get(module).code(str(finding.get("code"))).level
    except KeyError:
        return "info"


def legacy_verdict(rec: dict | None) -> str | None:
    """The record's verdict word, for the views that still count by it (the report's summary counts
    and the modules' own views): a record with a finding that blocks by default is ``fail``, one with
    a review finding ``abstain``, one with a score ``scored``; one that assessed none of the items its
    own per-episode codes report on, or could not assess an item it rejects on, ``abstain``; else
    ``pass``."""
    if not isinstance(rec, dict):
        return None
    if rec.get("status") == "error":
        return "error"
    if skipped_reason(rec):                  # D94: the module did not judge the episode (no task text, integrity)
        return "skipped"
    levels = {default_level(rec["module"], f) for f in rec.get("findings") or []}
    if "blocking" in levels:
        return "fail"
    if "review" in levels:
        return "abstain"
    if score_of(rec) is not None:
        return "scored"
    try:
        codes = registry_get(rec["module"]).codes
    except KeyError:
        codes = ()
    gate_items = {c.item for c in codes if c.level == "blocking"}
    own_items = {c.item for c in codes if c.item and c.scope_kind != "dataset"}
    assessed = set(rec.get("assessed") or [])
    if not (assessed & own_items if own_items else assessed) \
            or any(u.get("item") in gate_items for u in rec.get("unassessable") or []):
        return "abstain"                     # it assessed nothing of its own, or not what it rejects on
    return "pass"


def check_counts(records: dict[int, dict], episodes: Iterable[int]) -> tuple[str, dict, list[int], dict]:
    """``check --json`` 2.0 for one module over ``episodes`` (C2 ``check``): (schema_version, the episode
    counts - ok / error -, the error episodes, finding code -> episodes). An episode without a record
    counts as an error."""
    errors: list[int] = []
    counts = {"total": 0, "ok": 0, "error": 0}
    found: dict[str, int] = {}
    for e in episodes:
        counts["total"] += 1
        rec = records.get(e)
        if rec is None or is_error(rec):
            counts["error"] += 1
            errors.append(e)
            continue
        counts["ok"] += 1
        for code in {f.get("code") for f in rec.get("findings") or []}:
            found[code] = found.get(code, 0) + 1
    return "2.0", counts, errors, dict(sorted(found.items()))


def registry_get(module: str):
    from ..contracts import modules as registry

    return registry.get(module)


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
