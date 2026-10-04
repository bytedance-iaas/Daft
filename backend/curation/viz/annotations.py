"""Segment annotations of a LeRobot dataset and of external label files (design doc 18 §3.4, §4.5, D64).

There is no standard, but a handful of layouts recur; the known ones are read directly and anything
that looks like annotations in another layout is reported as 标注格式不支持 instead of guessed at:

=================  ===================================================================  ==========
format             where                                                                kind
=================  ===================================================================  ==========
subtask_index      per-frame ``subtask_index`` + ``meta/subtasks.parquet|jsonl`` (v3)   segments
language_persistent per-frame ``language_persistent`` lists, ``style: subtask`` (HIW)   segments
task_index         per-frame ``task_index`` that changes inside an episode (Galaxea)     segments
index_table        another ``*_index`` column + a ``meta/*.jsonl`` lookup (HABIT)         segments
string_column      a per-frame string column named like subtask / step / phase           segments
bool_segments      0 / 1 columns named ``is_*`` (``is_intervention_segment``)            segments
language_events    per-frame ``language_events`` lists                                   events
quality / labels   ``*quality_index``, ``next.success``, ``meta.rating``, ``task_status``  labels
argus              an uploaded Pantheon Argus JSON (``timeline`` / ``event_labels``,      all three
                   ``key_events``, ``completion``)
=================  ===================================================================  ==========

:func:`detect_sources` decides from ``info.json`` and the ``meta/`` listing alone (preflight and the
Daemon's dataset model); :func:`episode_annotations` cuts one episode's frames into tracks, events and
labels. Segments tile the episode on its own clock: a run ends where the next one starts.
"""
from __future__ import annotations

import io
import json
import math
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable

from .lerobot_info import BOOKKEEPING

PLACEHOLDERS = frozenset({"", "todo", "tbd", "none", "null", "nan", "n/a", "na", "-", "unknown",
                          "placeholder"})
_SEGMENT_WORDS = ("subtask", "sub_task", "step", "phase", "stage", "skill", "segment", "primitive")
#: an index column is an annotation only when its name says so (``source.state_step_index`` is not)
_INDEX_WORDS = ("task", "skill", "phase", "stage", "segment", "primitive", "label", "annotation")
#: RLDS bookkeeping flags: not segments
_RLDS_FLAGS = frozenset({"is_first", "is_last", "is_terminal"})
#: per-frame booleans that say whether the episode succeeded (read from its last frames)
_SUCCESS_COLUMNS = ("next.success", "is_episode_successful", "is_success", "success")
_KNOWN_INDEX = frozenset({"subtask_index", "coarse_task_index", "quality_index", "coarse_quality_index"})
_LABEL_TEXT_KEYS = ("task", "subtask", "text", "name", "label", "description", "instruction")
#: priority of the segment layouts for the primary track (lower first)
PRIORITY = {"subtask_index": 1, "language_persistent": 2, "task_index": 3, "index_table": 4,
            "string_column": 5, "argus": 6, "bool_segments": 7}


@dataclass
class Source:
    key: str
    kind: str                         # segments | events | labels
    name: str
    format: str
    source: str
    supported: bool = True
    reason: str | None = None
    primary: bool = False
    columns: list[str] = field(default_factory=list)   # data columns it reads
    table: str | None = None                            # meta file of the lookup

    def as_dict(self) -> dict:
        """C4 ``VizAnnotationSource``."""
        return {"key": self.key, "kind": self.kind, "name": self.name, "format": self.format,
                "source": self.source, "supported": self.supported, "reason": self.reason,
                "primary": self.primary}

    def as_preflight(self) -> dict:
        """C2 preflight ``dataset.segment_sources``."""
        return {"kind": self.kind, "format": self.format, "name": self.name, "source": self.source,
                "supported": self.supported, "reason": self.reason}


# ---------------------------------------------------------------- dataset level (metadata only)

def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-z]+", name.lower()) if t]


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def _lookup_table_for(column: str, tables: list[str], taken: set[str]) -> str | None:
    """The ``meta/*.jsonl`` lookup of an index column: same name, the table's words inside the
    column's (``human_role_subtask_index`` -> ``human_subtasks.jsonl``), or the known alias
    (``low_level_task_index`` -> ``subtasks.jsonl``)."""
    stem = column[:-len("_index")] if column.endswith("_index") else column
    names = {t: t.rsplit("/", 1)[-1].rsplit(".", 1)[0] for t in tables if t not in taken}
    for t, name in names.items():
        if name in (stem, stem + "s"):
            return t
    col_tokens = set(_tokens(stem))
    best = None
    for t, name in names.items():
        words = [_singular(w) for w in _tokens(name)]
        if words and set(words) <= col_tokens and (best is None or len(words) > best[1]):
            best = (t, len(words))
    if best:
        return best[0]
    aliases = {"low_level_task": "subtasks", "low_level_subtask": "subtasks", "sub_task": "subtasks"}
    want = aliases.get(stem)
    for t, name in names.items():
        if want and name == want:
            return t
    return None


def detect_sources(info: dict, meta_files: list[str], *,
                   peek: Callable[[str], dict | None] | None = None,
                   episode_fields: set[str] | frozenset = frozenset()) -> list[Source]:
    """The annotation layouts a LeRobot dataset uses, from ``info.json`` features and the files
    under ``meta/`` (``meta_files`` relative to the dataset). ``peek(rel)`` may return the first
    record of a ``meta/*.jsonl`` table (None when it cannot); ``episode_fields`` are the columns of
    the episode table (``task_status`` ...). The first segments source by priority is primary."""
    feats = {k: f for k, f in (info.get("features") or {}).items() if isinstance(f, dict)}
    metas = set(meta_files)
    out: list[Source] = []

    def dtype(k: str) -> str:
        return str(feats[k].get("dtype") or "")

    if "subtask_index" in feats:
        table = next((t for t in ("meta/subtasks.parquet", "meta/subtasks.jsonl") if t in metas), None)
        out.append(Source("subtask_index", "segments", "子任务", "subtask_index",
                          f"subtask_index + {table or 'meta/subtasks.*'}", table is not None,
                          None if table else "标注格式不支持：有 subtask_index 列，但没有查表文件 meta/subtasks.parquet / .jsonl",
                          columns=["subtask_index"], table=table))
    if "language_persistent" in feats:
        out.append(Source("language_persistent", "segments", "子任务（language）", "language_persistent",
                          "language_persistent（style = subtask）", columns=["language_persistent"]))
    if "language_events" in feats:
        out.append(Source("language_events", "events", "事件（language）", "language_events",
                          "language_events", columns=["language_events"]))
    tasks_table = next((t for t in ("meta/tasks.jsonl", "meta/tasks.parquet") if t in metas), None)
    if "task_index" in feats and "coarse_task_index" in feats:
        out.append(Source("task_index", "segments", "步骤", "task_index",
                          f"逐帧 task_index + {tasks_table or 'meta/tasks.*'}",
                          columns=["task_index"] + (["quality_index"] if "quality_index" in feats else []),
                          table=tasks_table))
    # other *_index columns with a lookup table (HABIT)
    tables = sorted(t for t in metas if t.startswith("meta/") and t.endswith(".jsonl")
                    and t.rsplit("/", 1)[-1] not in ("episodes.jsonl", "episodes_stats.jsonl", "tasks.jsonl"))
    taken: set[str] = set()
    for k in feats:
        if (not k.endswith("_index") or k in BOOKKEEPING or k in _KNOWN_INDEX
                or not dtype(k).startswith(("int", "uint"))
                or k.startswith(("annotation.", "annotations.", "label.", "labels."))   # reported as a family
                or "original" in k.lower()
                or not any(w in k[:-len("_index")].lower() for w in _INDEX_WORDS)):
            continue
        table = _lookup_table_for(k, tables, taken)
        if table and peek is not None and not _looks_like_lookup(peek(table)):
            table = None
        if table:
            taken.add(table)
        name = {"low_level_task_index": "机器人分步", "human_role_subtask_index": "人的分步"}.get(
            k, k[:-len("_index")])
        out.append(Source(k, "segments", name, "index_table", f"{k} + {table or 'meta/?.jsonl'}",
                          table is not None,
                          None if table else f"标注格式不支持：{k} 没有对得上的查表文件 meta/*.jsonl",
                          columns=[k], table=table))
    for k in feats:
        if dtype(k) == "string" and any(w in k.lower() for w in _SEGMENT_WORDS):
            out.append(Source(k, "segments", k, "string_column", f"逐帧文字列 {k}", columns=[k]))
    flags = [k for k in feats if (dtype(k) == "bool" or dtype(k).startswith(("int", "uint")))
             and k.rsplit(".", 1)[-1].startswith("is_") and k.rsplit(".", 1)[-1] not in _RLDS_FLAGS
             and k not in _SUCCESS_COLUMNS
             and (k.endswith("_segment") or k.startswith(("annotation.", "annotations.")))]
    if flags:
        out.append(Source("flags", "segments", "标志段", "bool_segments", "、".join(flags),
                          columns=flags))
    # labels
    for k in feats:
        if k.endswith("quality_index") and k != "quality_index":
            out.append(Source(k, "labels", "质量", "quality", f"{k} + {tasks_table or 'meta/tasks.*'}",
                              columns=[k], table=tasks_table))
    if "quality_index" in feats and not any(s.format == "task_index" for s in out):
        out.append(Source("quality_index", "labels", "质量", "quality",
                          f"quality_index + {tasks_table or 'meta/tasks.*'}", columns=["quality_index"],
                          table=tasks_table))
    success = next((k for k in _SUCCESS_COLUMNS if k in feats), None)
    if success:
        out.append(Source(success, "labels", "成败", "quality", success, columns=[success]))
    if "meta.rating" in feats:
        out.append(Source("meta.rating", "labels", "评分", "quality", "meta.rating", columns=["meta.rating"]))
    for k, name in (("task_status", "任务状态"), ("success", "成败"), ("rating", "评分")):
        if k in episode_fields:
            out.append(Source(f"episode:{k}", "labels", name, "episode_labels", f"episode 表的 {k}"))
    # annotation-looking columns nobody reads
    handled = {c for s in out for c in s.columns}
    unknown = sorted(k for k in feats if k not in handled
                     and k.startswith(("annotation.", "annotations.", "label.", "labels.")))
    if unknown:
        families = sorted({k.rsplit(".", 1)[0] + ".*" if "." in k else k for k in unknown})
        out.append(Source("unknown", "segments", "未识别的标注", "unknown", "、".join(families), False,
                          f"标注格式不支持：{'、'.join(families)} 的写法认不出"))
    _mark_primary(out)
    return out


def _looks_like_lookup(rec: dict | None) -> bool:
    if not isinstance(rec, dict):
        return True                        # could not peek: trust the name
    has_index = any(k.endswith("_index") or k == "index" for k in rec)
    has_text = any(isinstance(rec.get(k), str) for k in _LABEL_TEXT_KEYS)
    return has_index and has_text


def _mark_primary(sources: list[Source]) -> None:
    segs = [s for s in sources if s.kind == "segments" and s.supported and s.format in PRIORITY
            and s.format != "bool_segments"]
    for s in sources:
        s.primary = False
    if segs:
        min(segs, key=lambda s: PRIORITY[s.format]).primary = True


def lookup_from_records(records: list[dict]) -> dict[int, str]:
    """``{index: text}`` of a lookup table (tasks / subtasks): the first ``*_index`` field and
    the first text field of each record."""
    out: dict[int, str] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        idx = next((rec[k] for k in rec if (k.endswith("_index") or k == "index")
                    and isinstance(rec[k], (int, float)) and not isinstance(rec[k], bool)), None)
        text = next((rec[k] for k in _LABEL_TEXT_KEYS if isinstance(rec.get(k), str)), None)
        if idx is not None and text is not None:
            out[int(idx)] = text
    return out


def parse_jsonl(data: bytes) -> list[dict]:
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def parse_lookup_parquet(data: bytes) -> dict[int, str]:
    import pyarrow.parquet as pq

    table = pq.read_table(io.BytesIO(data))
    cols = table.column_names
    idx_col = next((c for c in cols if c.endswith("_index") or c == "index"), None)
    text_col = next((c for c in cols if c in _LABEL_TEXT_KEYS), None)
    if text_col is None:                      # v3 tasks.parquet: the task text is the pandas index
        df = table.to_pandas()
        if idx_col is not None and df.index.dtype == object:
            return {int(i): str(t) for t, i in zip(df.index, df[idx_col])}
        return {}
    rows = table.select([c for c in (idx_col, text_col) if c]).to_pylist()
    if idx_col is None:
        return {i: str(r[text_col]) for i, r in enumerate(rows)}
    return {int(r[idx_col]): str(r[text_col]) for r in rows if r[idx_col] is not None}


# ---------------------------------------------------------------- one episode

def _runs(values: list) -> list[tuple[int, int, Any]]:
    """[(start row, end row exclusive, value)] of consecutive equal values."""
    out = []
    start = 0
    for i in range(1, len(values) + 1):
        if i == len(values) or values[i] != values[start]:
            out.append((start, i, values[start]))
            start = i
    return out


def _bounds(times: list[float], a: int, b: int) -> tuple[float, float]:
    start = times[a]
    if b < len(times):
        end = times[b]
    else:
        step = (times[-1] - times[0]) / (len(times) - 1) if len(times) > 1 else 0.0
        end = times[-1] + step
    return round(float(start), 3), round(float(end), 3)


def _scalar(v):
    if hasattr(v, "tolist"):
        v = v.tolist()
    if isinstance(v, list) and len(v) == 1:
        v = v[0]
    return v


def _text(v) -> str:
    v = _scalar(v)
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v)


def _is_placeholder(text: str) -> bool:
    return text.strip().lower() in PLACEHOLDERS


def _quality(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip().lower()
    if t in ("qualified", "good", "pass", "success", "合格"):
        return "qualified"
    if t in ("unqualified", "bad", "fail", "failure", "不合格"):
        return "unqualified"
    return None


def _language(v) -> list[dict]:
    v = v.tolist() if hasattr(v, "tolist") else v
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    if isinstance(v, dict):
        v = [v]
    return [x for x in v or [] if isinstance(x, dict)]


@dataclass
class EpisodeAnnotations:
    tracks: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    labels: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"tracks": self.tracks, "events": self.events, "labels": self.labels,
                "warnings": self.warnings}


def _segment(start: float, end: float, label: str, **kw) -> dict:
    seg = {"start_s": start, "end_s": max(start, end), "label": label, "quality": kw.get("quality"),
           "contribution": kw.get("contribution"), "arm": kw.get("arm"), "flags": kw.get("flags") or []}
    return seg


def _persistent_segments(vals: list, times: list[float]) -> list[dict]:
    """``language_persistent`` (LeRobot v3, HIW): every frame carries the timed statements in force;
    each ``style: subtask`` statement holds from its ``timestamp`` until the next one (statements
    without a timestamp start at the first frame that carries them)."""
    starts: dict[tuple[float, str], None] = {}
    for i, v in enumerate(vals):
        for x in _language(v):
            if str(x.get("style") or "subtask") != "subtask" or not x.get("content"):
                continue
            t = float(x["timestamp"]) if isinstance(x.get("timestamp"), (int, float)) else times[i]
            starts.setdefault((round(t, 3), str(x["content"])), None)
    if not starts or not times:
        return []
    step = (times[-1] - times[0]) / (len(times) - 1) if len(times) > 1 else 0.0
    end_of_episode = round(times[-1] + step, 3)
    items = sorted(k for k in starts if k[0] < end_of_episode)
    out = []
    for i, (t, label) in enumerate(items):
        end = items[i + 1][0] if i + 1 < len(items) else end_of_episode
        if end > t:
            out.append(_segment(max(t, round(times[0], 3)), end, label))
    return out


def episode_annotations(sources: list[Source], columns: dict[str, list], times: list[float], *,
                        lookups: dict[str, dict[int, str]] | None = None,
                        episode_row: dict | None = None,
                        tasks: dict[int, str] | None = None,
                        primary: str | None = None) -> EpisodeAnnotations:
    """One episode's tracks / events / labels. ``columns`` holds the frame values of every column
    the sources read (missing ones are skipped), ``times`` the episode time of each frame,
    ``lookups`` the tables by meta path, ``tasks`` the task table (task / quality texts),
    ``primary`` a track key chosen for the subtitle bar (display config)."""
    lookups = lookups or {}
    tasks = tasks or {}
    row = episode_row or {}
    out = EpisodeAnnotations()
    n = len(times)

    def col(name: str) -> list | None:
        vals = columns.get(name)
        if vals is None or len(vals) != n or n == 0:
            return None
        return [_scalar(v) for v in vals]

    tracks: list[tuple[int, dict]] = []
    for s in sources:
        if not s.supported:
            out.warnings.append({"code": "annotation_unsupported", "message": s.reason or "标注格式不支持"})
            continue
        if s.format in ("subtask_index", "index_table", "task_index"):
            vals = col(s.columns[0])
            if vals is None:
                continue
            table = tasks if s.format == "task_index" else lookups.get(s.table or "", {})
            runs = _runs(vals)
            if s.format == "task_index" and len(runs) < 2:
                continue
            quality = col("quality_index") if s.format == "task_index" else None
            segs = []
            for a, b, v in runs:
                label = table.get(int(v)) if isinstance(v, (int, float)) and v == v else None
                start, end = _bounds(times, a, b)
                q = _quality(tasks.get(int(quality[a]))) if quality and isinstance(quality[a], (int, float)) else None
                segs.append(_segment(start, end, label if label is not None else f"{s.columns[0]} = {v}", quality=q))
            tracks.append((PRIORITY.get(s.format, 9), {"key": s.key, "name": s.name, "source": s.source,
                                                        "primary": False, "segments": segs}))
        elif s.format == "language_persistent":
            vals = columns.get(s.columns[0])
            if vals is None or len(vals) != n:
                continue
            segs = _persistent_segments(vals, times)
            if segs:
                tracks.append((PRIORITY["language_persistent"], {"key": s.key, "name": s.name,
                                                                  "source": s.source, "primary": False,
                                                                  "segments": segs}))
        elif s.format == "language_events":
            vals = columns.get(s.columns[0])
            if vals is None or len(vals) != n:
                continue
            seen = set()
            for i, v in enumerate(vals):
                for x in _language(v):
                    text = str(x.get("content") or "")
                    if not text:
                        continue
                    t = float(x["timestamp"]) if isinstance(x.get("timestamp"), (int, float)) else times[i]
                    if (round(t, 3), text) in seen:
                        continue
                    seen.add((round(t, 3), text))
                    out.events.append({"t_s": round(t, 3), "label": text, "outcome": None, "source": s.source})
        elif s.format == "string_column":
            vals = col(s.columns[0])
            if vals is None:
                continue
            texts = [_text(v) for v in vals]
            if all(_is_placeholder(t) for t in texts):
                sample = next((t for t in texts if t.strip()), "")
                out.warnings.append({"code": "annotation_placeholder",
                                     "message": f"标注格式不支持：{s.columns[0]} 列全是占位文字"
                                                + (f" {sample}" if sample else "（空）")})
                continue
            segs = [_segment(*_bounds(times, a, b), t) for a, b, t in _runs(texts) if not _is_placeholder(t)]
            tracks.append((PRIORITY["string_column"], {"key": s.key, "name": s.name, "source": s.source,
                                                        "primary": False, "segments": segs}))
        elif s.format == "bool_segments":
            flags_per_row = [[] for _ in range(n)]
            for c in s.columns:
                vals = col(c)
                if vals is None:
                    continue
                for i, v in enumerate(vals):
                    if v not in (None, 0, False, "0", "false") and v == v:
                        flags_per_row[i].append(c.rsplit(".", 1)[-1])
            segs = [_segment(*_bounds(times, a, b), "、".join(f), flags=list(f))
                    for a, b, f in _runs([tuple(f) for f in flags_per_row]) if f]
            if segs:
                tracks.append((PRIORITY["bool_segments"], {"key": s.key, "name": s.name, "source": s.source,
                                                            "primary": False, "segments": segs}))
        elif s.format == "quality":
            vals = col(s.columns[0]) if s.columns else None
            if not vals:
                continue
            if s.key in _SUCCESS_COLUMNS:
                value = "成功" if any(v in (True, 1, 1.0, "1", "true") for v in vals[-3:]) else "失败"
            elif s.key.endswith("quality_index"):
                v = vals[0]
                text = tasks.get(int(v)) if isinstance(v, (int, float)) else None
                value = {"qualified": "合格", "unqualified": "不合格"}.get(_quality(text) or "", text or str(v))
            else:
                value = _text(vals[0])
            out.labels.append({"key": s.key, "name": s.name, "value": value, "source": s.source})
        elif s.format == "episode_labels":
            field_name = s.key.split(":", 1)[1]
            v = row.get(field_name)
            if v is not None and _text(v):
                out.labels.append({"key": s.key, "name": s.name, "value": _text(v), "source": s.source})
    tracks.sort(key=lambda kv: kv[0])
    chosen = next((t for _, t in tracks if t["key"] == primary), None) if primary else None
    if chosen is None:            # flag segments are overlays, not steps: never the subtitle by default
        chosen = next((t for p, t in tracks if p != PRIORITY["bool_segments"]), None)
    if chosen is not None:
        chosen["primary"] = True
    out.tracks = [t for _, t in tracks]
    return out


# ---------------------------------------------------------------- external label files (Argus)

def argus_annotations(doc: dict, *, source: str = "外部标注") -> EpisodeAnnotations:
    """One episode of a Pantheon Argus-style file: ``timeline`` or ``event_labels`` -> one track,
    ``key_events`` -> events, ``completion`` / ``goal_alignment`` -> labels."""
    out = EpisodeAnnotations()
    rows = doc.get("timeline") if isinstance(doc.get("timeline"), list) else doc.get("event_labels")
    segs = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not isinstance(r.get("t_s"), (int, float)):
            continue
        start = float(r["t_s"])
        end = float(r["end_s"]) if isinstance(r.get("end_s"), (int, float)) else start
        label = " · ".join(str(r[k]) for k in ("verb_class", "object") if r.get(k)) or str(r.get("label") or "")
        segs.append(_segment(round(start, 3), round(end, 3), label,
                             contribution=r.get("contribution") if isinstance(r.get("contribution"), str) else None,
                             arm=r.get("arm") if isinstance(r.get("arm"), str) else None))
    segs.sort(key=lambda s: (s["start_s"], s["end_s"]))
    if segs:
        out.tracks.append({"key": "external", "name": "外部标注", "source": source, "primary": False,
                           "segments": segs})
    for e in doc.get("key_events") if isinstance(doc.get("key_events"), list) else []:
        if isinstance(e, dict) and isinstance(e.get("t_s"), (int, float)):
            out.events.append({"t_s": round(float(e["t_s"]), 3), "label": str(e.get("label") or e.get("event") or ""),
                               "outcome": e.get("outcome") if isinstance(e.get("outcome"), str) else None,
                               "source": source})
    comp = doc.get("completion")
    if isinstance(comp, dict) and comp.get("outcome") is not None:
        out.labels.append({"key": "external:completion", "name": "完成情况", "value": _text(comp.get("outcome")),
                           "source": source})
    goal = doc.get("goal_alignment")
    if isinstance(goal, dict) and isinstance(goal.get("relation"), str):
        out.labels.append({"key": "external:goal", "name": "与任务描述", "value": goal["relation"], "source": source})
    return out


def is_argus(doc: Any) -> bool:
    return isinstance(doc, dict) and (isinstance(doc.get("timeline"), list)
                                      or isinstance(doc.get("event_labels"), list))


_TRAILING_INT = re.compile(r"(\d+)(?=\D*$)")


def episode_of_name(name: str) -> int | None:
    """The episode a file name stands for: its last run of digits (``episode_000012.json`` -> 12)."""
    base = name.rsplit("/", 1)[-1]
    m = _TRAILING_INT.search(base.rsplit(".", 1)[0])
    return int(m.group(1)) if m else None


class AnnotationFileError(ValueError):
    def __init__(self, message: str, errors: list[dict]):
        super().__init__(message)
        self.errors = errors


def read_label_file(data: bytes, name: str) -> dict[int, dict]:
    """``{episode index: Argus document}`` of an uploaded label file: a zip of per-episode JSON files
    named by episode index, a JSON list / mapping of documents with their ``episode_index`` (or
    keyed by it), or one JSON document for the episode its file name names. Raises
    :class:`AnnotationFileError` with located errors."""
    errors: list[dict] = []
    out: dict[int, dict] = {}

    def take(doc: Any, where: str, index: int | None) -> None:
        if not is_argus(doc):
            errors.append({"field": where, "problem": "不是认得的标注格式：要有 timeline 或 event_labels",
                           "code": "annotation_unsupported"})
            return
        idx = doc.get("episode_index") if isinstance(doc.get("episode_index"), int) else index
        if idx is None:
            errors.append({"field": where, "problem": "看不出是哪条 episode：文件名里要有编号，或写 episode_index",
                           "code": "episode_unknown"})
            return
        if idx in out:
            errors.append({"field": where, "problem": f"episode {idx} 出现了两次", "code": "duplicate",
                           "episode_index": idx})
            return
        out[int(idx)] = doc

    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                members = [m for m in zf.infolist() if not m.is_dir() and m.filename.lower().endswith(".json")
                           and not m.filename.rsplit("/", 1)[-1].startswith(".")]
                if not members:
                    raise AnnotationFileError("zip 里没有 .json 文件", [{"field": None, "problem": "no json"}])
                for m in members:
                    try:
                        doc = json.loads(zf.read(m).decode("utf-8"))
                    except (ValueError, UnicodeDecodeError) as e:
                        errors.append({"field": m.filename, "problem": f"不是合法的 JSON：{e}"[:200]})
                        continue
                    take(doc, m.filename, episode_of_name(m.filename))
        except zipfile.BadZipFile as e:
            raise AnnotationFileError(f"不是合法的 zip：{e}", [{"field": None, "problem": "bad zip"}]) from None
    else:
        try:
            doc = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise AnnotationFileError("不是合法的 JSON", [{"field": None, "problem": str(e)[:200]}]) from None
        if isinstance(doc, list):
            for i, d in enumerate(doc):
                take(d, f"[{i}]", None)
        elif isinstance(doc, dict) and not is_argus(doc) and doc and all(
                re.fullmatch(r"\d+", str(k)) for k in doc):
            for k, d in doc.items():
                take(d, str(k), int(k))
        elif isinstance(doc, dict) and isinstance(doc.get("episodes"), dict):
            for k, d in doc["episodes"].items():
                take(d, f"episodes.{k}", int(k) if re.fullmatch(r"\d+", str(k)) else None)
        else:
            take(doc, name, episode_of_name(name))
    if errors and not out:
        raise AnnotationFileError(errors[0]["problem"], errors)
    if errors:
        raise AnnotationFileError(f"{len(errors)} 处不合格（例如 {errors[0]['field']}：{errors[0]['problem']}）", errors)
    return out
