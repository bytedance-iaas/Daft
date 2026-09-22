"""The orchestration's rules, as pure functions (design doc 01 §2.3-§2.5, 06 §3).

* module states come from the CLI's per-verdict counts, never from the exit code
  (02 §3.5): no error -> ``succeeded``, some -> ``completed_with_errors``; exit 4 is
  ``failed``;
* the terminal state (01 §2.5 constraint 3 with D35): no selected module ``failed``
  and ``held`` empty -> ``succeeded``, else ``completed_with_errors``. An episode a
  module erred on but another module rejected for good is not held (the CLI's
  aggregate already put it in ``reject``);
* the summary the task list and the overview read (``total``, ``passed``,
  ``rejected``, ``held``, ``review``, ``pass_rate``, ``pending_adjudication``);
* episode selections, the ``run_id`` of a batch, listing fingerprints and what
  changed between two listings (D37's ``SourceChange``).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
from typing import Iterable

from ..repo import protocol as P

FUNNEL_STAGES = ("numeric", "frame", "vlm")
#: review.json item kind -> adjudication line (06 §5.1)
REVIEW_LINE = {"task_verdict": "task_verdict", "label_conflict": "label",
               "reject_appeal": "reject_appeal"}
SAMPLE_KEYS = 20


# ---------------------------------------------------------------- modules and states

def module_state(counts: dict) -> str:
    return "completed_with_errors" if int(counts.get("error") or 0) > 0 else "succeeded"


def failed_modules(rows: Iterable[P.TaskModule]) -> list[str]:
    return sorted(m.module_id for m in rows if m.selected and m.state == "failed")


def terminal_state(rows: Iterable[P.TaskModule], held: int) -> str:
    rows = list(rows)
    if failed_modules(rows) or int(held) > 0:
        return "completed_with_errors"
    return "succeeded"


# ---------------------------------------------------------------- episodes

def selected_episodes(selector: dict, count: int) -> list[int]:
    """The task's episodes (01 §2.3): all, the first N, or an explicit list, within range."""
    mode = (selector or {}).get("mode", "all")
    if mode == "head":
        return list(range(min(int(selector["n"]), int(count))))
    if mode == "explicit":
        return sorted({int(i) for i in selector.get("indices") or [] if 0 <= int(i) < count})
    return list(range(int(count)))


def max_episodes(selector: dict) -> int | None:
    """v1's head-N (``--max-episodes N``): it also bounds the semantics sample (CLI README)."""
    if (selector or {}).get("mode") == "head":
        return int(selector["n"])
    return None


# ---------------------------------------------------------------- results

def read_list(rev_dir: pathlib.Path, name: str) -> dict | None:
    try:
        with open(rev_dir / f"{name}.json", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _review_line(item: dict) -> tuple[str, bool]:
    """(the adjudication line of a review.json item, whether an open one is pending).

    C2 1.5 items carry ``line``; older ones only ``kind``. The registry's catalog (C1 1.2,
    D43) says whether the line must be decided: appeals are optional and never pending
    (D42). A line the catalog does not know counts as pending - better asked than lost.
    """
    from curation.contracts import modules as registry

    line_id, kind = item.get("line"), item.get("kind")
    try:
        spec = registry.review_line(line_id) if line_id else registry.review_line_of_kind(kind)
    except KeyError:
        return str(line_id or REVIEW_LINE.get(kind, kind)), True
    return spec.id, bool(spec.counts_as_pending)


def pending_adjudication(review: dict | None, latest: Iterable[P.Adjudication]) -> int:
    """Episodes of ``review.json`` with a question that must be decided and is not yet.

    A question is answered by the latest decision on its line for that episode;
    "unsure" is a legal answer that keeps the episode in the queue (06 §5.1, rule 3).
    """
    answered = {(a.episode_index, a.line) for a in latest if a.decision != "unsure"}
    n = 0
    for entry in (review or {}).get("episodes") or []:
        ep = int(entry.get("episode_index", -1))
        lines = {line for line, pending in map(_review_line, entry.get("review") or [])
                 if pending}
        if any((ep, line) not in answered for line in lines):
            n += 1
    return n


def summary(rev_dir: pathlib.Path, latest: Iterable[P.Adjudication] = ()) -> dict:
    """C4 ``Summary`` of a committed revision, plus ``pending_adjudication``."""
    counts = {}
    for name in ("passed", "reject", "held", "review"):
        doc = read_list(rev_dir, name)
        counts[name] = int((doc or {}).get("count") or 0)
    total = counts["passed"] + counts["reject"] + counts["held"]
    out = {"total": total, "passed": counts["passed"], "rejected": counts["reject"],
           "held": counts["held"], "review": counts["review"],
           "pass_rate": round(counts["passed"] / total, 4) if total else None,
           "pending_adjudication": pending_adjudication(read_list(rev_dir, "review"), latest)}
    report = read_list(rev_dir, "report") or {}
    skipped = ((report.get("overview") or {}).get("counts") or {}).get("skipped")
    if isinstance(skipped, int) and not isinstance(skipped, bool) and skipped > 0:
        out["skipped"] = skipped                   # D40: missing source files, not in total
    return out


# ---------------------------------------------------------------- batches

def run_id_for(now_ms: int, tz_offset_minutes: int = 8 * 60) -> str:
    """``YYYYMMDD-HHMMSS`` in the site's time zone (v1's batch directory names)."""
    tz = datetime.timezone(datetime.timedelta(minutes=int(tz_offset_minutes)))
    return datetime.datetime.fromtimestamp(now_ms / 1000.0, tz).strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------- listings (D27, D37)

def _identity(obj: dict) -> str:
    if obj.get("etag") is not None:
        return str(obj["etag"]).strip().strip('"')
    return str(obj.get("mtime_ns"))


def listing_digest(objects: Iterable[dict]) -> str:
    """C2 source-manifest ``summary.digest``: one ``key TAB size TAB identity`` line per
    object, sorted by key, sha256 (the same rule as ``curation.cli.lerobot_meta.fingerprint``)."""
    h = hashlib.sha256()
    for obj in sorted(objects, key=lambda o: o["key"]):
        h.update(f"{obj['key']}\t{int(obj['size'])}\t{_identity(obj)}\n".encode())
    return "sha256:" + h.hexdigest()


def meta_fingerprint(manifest: dict) -> str:
    """The preflight's ``meta_fingerprint`` recomputed from a snapshot: the metadata objects."""
    return listing_digest(o for o in manifest.get("objects") or []
                          if str(o.get("key", "")).startswith("meta/"))


def fingerprint_of(manifest: dict) -> dict:
    """What a dataset registration and a task keep of a listing (``objects``, ``bytes``, ``digest``)."""
    s = manifest.get("summary") or {}
    return {"objects": int(s.get("count") or 0), "bytes": int(s.get("bytes") or 0),
            "digest": str(s.get("digest") or "")}


def source_change(old: dict | None, new: dict, *, meta_changed: bool, preflighted_at: int,
                  old_fingerprint: dict | None = None) -> dict:
    """C4 ``SourceChange`` between the kept listing ``old`` (None when it was not kept) and
    ``new``: added, removed and modified objects, a few keys (added first)."""
    new_objs = {o["key"]: (int(o["size"]), _identity(o)) for o in new.get("objects") or []}
    if old is not None:
        old_objs = {o["key"]: (int(o["size"]), _identity(o)) for o in old.get("objects") or []}
        added = sorted(set(new_objs) - set(old_objs))
        removed = sorted(set(old_objs) - set(new_objs))
        modified = sorted(k for k in set(old_objs) & set(new_objs) if old_objs[k] != new_objs[k])
        sample = (added + modified + removed)[:SAMPLE_KEYS]
        return {"meta_changed": bool(meta_changed), "added": len(added), "removed": len(removed),
                "modified": len(modified), "sample_keys": sample,
                "preflighted_at": int(preflighted_at)}
    before = int((old_fingerprint or {}).get("objects") or (old_fingerprint or {}).get("count")
                 or 0)
    after = len(new_objs)
    return {"meta_changed": bool(meta_changed), "added": max(0, after - before),
            "removed": max(0, before - after), "modified": 0, "sample_keys": [],
            "preflighted_at": int(preflighted_at)}
