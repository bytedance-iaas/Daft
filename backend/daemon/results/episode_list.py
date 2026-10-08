"""The episodes of a result revision, for the report's Episode tab (design doc 03 §6, F6.2).

C4 ``listTaskEpisodes`` (``GET /tasks/{id}/episodes``): every episode of the three
final lists - which list it is in, the modules that put it there, and whether a
person still has to answer something about it - in episode order, one cursor page at
a time. The tab's search box and its filters are the query parameters:

* ``list`` - ``passed`` / ``reject`` / ``held``;
* ``review`` - only episodes with (``true``) or without (``false``) an open question:
  on the current revision the adjudication cards still to decide (pending or unsure,
  the 待裁 of the adjudication page); on a history revision, which nobody adjudicates
  any more, the questions that revision asked;
* ``q`` - an episode number, matched as a substring of the index: ``12``, ``ep12`` and
  ``ep 12`` all find ep 12 (and ep 112, ep 120...); leading zeros are dropped, so the
  v1 id ``ep000012`` works too;
* ``level`` / ``item`` (findings results, C2 2.0; design doc 17 §5.4) - only episodes with a
  finding the task's policy graded at that level / with a finding of that taxonomy item.

On a findings revision every episode also carries the ``items`` and ``levels`` of its
findings, every module together, from its list entry (C2 final-list 2.0 ``findings``): the
levels the task's policy gives them after the applied human decisions.

The cursor carries the revision and the last episode returned; the filters are bound
into it (another filter set is a 400), a cursor of another revision is 409
``result_changed``. ``total`` counts the filtered set, ``counts`` the whole revision.
"""
from __future__ import annotations

import bisect
import re

from curation.contracts import modules as registry

from ..errors import ApiError
from ..pagination import CursorError, decode_cursor, encode_cursor
from ..repo import protocol as P
from . import adjudication as A
from . import catalog as C
from .revision import LISTS, Revision
from .store import ResultStore

CURSOR_KIND = "task_episodes"
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

_NUMBER = re.compile(r"^(?:ep)?\s*0*(\d+)$", re.IGNORECASE)
_NOTHING = re.compile(r"^(?:ep)?$", re.IGNORECASE)
_ITEM = re.compile(r"^[A-Z]+-[0-9]+$")
LEVELS = ("blocking", "review", "info")


def normalize_query(q: str | None) -> str | None:
    """``"ep 12"`` -> ``"12"``; ``None`` / blank / a bare ``ep`` -> no filter;
    anything else that is not an episode number is a 400."""
    text = (q or "").strip()
    if _NOTHING.match(text):
        return None
    m = _NUMBER.match(text)
    if m is None:
        raise ApiError("validation_failed",
                       f"只能按 episode 编号搜索（数字，可以带 ep 前缀，如 12、ep12），收到的是「{text[:40]}」",
                       details={"errors": [{"field": "q", "problem": "not an episode number"}]})
    return m.group(1)


def open_questions(store: ResultStore, repo: P.Repository, task: P.Task,
                   rev: Revision) -> dict[int, list[str]]:
    """episode -> the source modules of the questions a person still has to answer."""
    if rev.number == task.result_rev:
        queue = A.Queue(store, repo, task)
        if queue.revision == rev.number:
            out: dict[int, list[str]] = {}
            for card in queue.cards("review"):
                if card.status in ("pending", "unsure") and card.counts_as_pending():
                    out[card.episode] = list(dict.fromkeys(
                        q.source_module for q in card.questions if q.source_module))
            return out
    pending = set(C.pending_lines())
    out = {}
    for ep, entry in rev.review().items():
        mods = []
        for item in entry.get("review") or []:
            ln = C.line_of_item(item) if isinstance(item, dict) else None
            if ln is not None and ln.id in pending and item.get("source_module"):
                mods.append(str(item["source_module"]))
        if mods:
            out[ep] = list(dict.fromkeys(mods))
    return out


def finding_index(rev: Revision) -> dict[int, tuple[list[str], list[str]]] | None:
    """episode -> (the taxonomy items of its findings, the levels they have), from the list entries'
    ``findings``; None when the lists carry none (a revision of the funnel, C2 1.0)."""
    def make():
        entries = rev.entries()
        if not any(isinstance(e.get("findings"), list) for _, e in entries.values()):
            return None
        order = {it["id"]: i for i, it in enumerate(registry.taxonomy()["items"])}
        out: dict[int, tuple[list[str], list[str]]] = {}
        for ep, (_, entry) in entries.items():
            graded = [f for f in entry.get("findings") or [] if isinstance(f, dict)]
            items = {f["item"] for f in graded if isinstance(f.get("item"), str)}
            levels = {f.get("level") for f in graded}
            out[ep] = (sorted(items, key=lambda i: (order.get(i, len(order)), i)),
                       [lv for lv in LEVELS if lv in levels])
        return out
    return rev.store.derived.get_or_make(("finding_index", rev.key), make)


def check_item(item: str | None) -> str | None:
    if item is None or item == "":
        return None
    if not _ITEM.match(item):
        raise ApiError("validation_failed", f"检测项编号的格式是「字母-数字」（如 STRM-5），收到的是「{item[:40]}」",
                       details={"errors": [{"field": "item", "problem": "not a taxonomy item id"}]})
    return item


def reason_modules(list_name: str, entry: dict) -> list[str]:
    """The modules that put the episode where it is: the deciding modules of a reject,
    the modules that failed on a held episode, none for a passed one."""
    reasons = [r for r in entry.get("reasons") or [] if isinstance(r, dict)]
    if list_name == "reject":
        mods = [r.get("module") for r in reasons if r.get("kind") != "execution_error"]
    elif list_name == "held":
        mods = [r.get("module") for r in reasons if r.get("kind") == "execution_error"] \
            or [r.get("module") for r in reasons]
    else:
        mods = []
    return [str(m) for m in dict.fromkeys(mods) if m]


def page(store: ResultStore, repo: P.Repository, task: P.Task, rev: Revision, *,
         list_name: str | None, review: bool | None, q: str | None, cursor: str | None,
         limit: int, level: str | None = None, item: str | None = None) -> dict:
    needle = normalize_query(q)
    item = check_item(item)
    entries = rev.entries()
    asking = open_questions(store, repo, task, rev)
    found = finding_index(rev)
    counts = {"all": 0, **{name: 0 for name in LISTS}, "review": 0}
    items = []
    for ep in sorted(entries):
        name, entry = entries[ep]
        counts["all"] += 1
        counts[name] += 1
        counts["review"] += ep in asking
        if list_name is not None and name != list_name:
            continue
        if review is not None and (ep in asking) != review:
            continue
        if needle is not None and needle not in str(ep):
            continue
        its, lvs = found.get(ep, ([], [])) if found is not None else ([], [])
        if level is not None and level not in lvs:
            continue
        if item is not None and item not in its:
            continue
        row = {"episode_index": ep, "list": name, "review": ep in asking,
               "reason_modules": reason_modules(name, entry), "review_modules": asking.get(ep, [])}
        if found is not None:
            row.update(items=its, levels=lvs)
        items.append(row)
    scope = {"task": task.id, "list": list_name, "review": review, "q": needle}
    if level is not None or item is not None:            # cursors of 1.x filter sets stay valid
        scope.update(level=level, item=item)
    start = 0
    if cursor:
        key = decode_cursor(cursor, CURSOR_KIND, scope=scope)
        if not (isinstance(key, list) and len(key) == 2 and all(
                isinstance(v, int) and not isinstance(v, bool) for v in key)):
            raise CursorError("cursor has no revision and episode")
        cursor_rev, last = key
        if cursor_rev != rev.number:
            raise ApiError("result_changed",
                           f"结果版本已从 r{cursor_rev} 换成 r{rev.number}，episode 列表请从头重新加载",
                           details={"cursor_revision": cursor_rev, "revision": rev.number})
        start = bisect.bisect_right([i["episode_index"] for i in items], last)
    chunk = items[start:start + limit]
    more = start + len(chunk) < len(items)
    next_cursor = encode_cursor(CURSOR_KIND, [rev.number, chunk[-1]["episode_index"]],
                                scope=scope) if more else None
    return {"items": chunk, "next_cursor": next_cursor, "has_more": more,
            "total": len(items), "counts": counts, "revision": rev.number}
