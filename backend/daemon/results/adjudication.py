"""The adjudication queue of one task (design doc 06 §5, 07 §6; C4 ``listAdjudication`` /
``submitAdjudication``; F3.3).

**Questions** are the current revision's ``review.json`` items, read as the CLI wrote
them (C2 1.5 ``final-list``, v1's queues; D42, D43) - the queue never re-derives them.
An item names its line (``line``; before C2 1.5 the line of its ``kind``), and
:mod:`.catalog` - the registry's review lines - says which tab shows it: ``label`` (a
label conflict from skill_profile's audit or task_success's kill guard; the original
annotation and the model's description come from the revision's ``label_audit.json``,
and the suggested new label is that description - v1 adopts it) and ``task_verdict``
on the review tab, ``reject_appeal`` on the appeals tab (rejects of an appealable
module: task_success, and dedup since D42 with ``duplicate_of``; physical and
structural gates are final - rule 2). A line the catalog does not know is not asked.

A question that was answered keeps its card after the answer was applied and the
episode left the newer revision's review: the question is taken from the newest older
revision that asked it, so ``decided`` / ``applied`` cards stay visible and can be
changed.

**Follow-ups** (C1 1.3 ``follow_ups``, C4 1.5.1): a card whose latest answer on a line
opens a follow-up gains that question - v1's relabel card: after ``adopt_suggestion`` /
``custom_label`` a label-only card also takes the task verdict (``success`` /
``failure`` / ``unsure``); answered, the machine takes it and does not re-judge; left
open, the episode is judged again with the new label (rule 4). It is listed on the card
while open, with ``follow_up_of`` naming the line whose answer opened it (C4 1.5.2; the
card's own questions carry null) and the source module of that line's question; it is
optional - it never makes a card pending and never blocks ``decided`` - and its answer
lapses once the answer that opened it changes: a lapsed answer is not shown, not
counted and not handed to the CLI (:meth:`Queue.executable`); once no answer opens it,
the question leaves the card.

**Decisions** are the repository's rows, append only; the latest per (task, line,
episode) counts. They never cross tasks (D32): the queue is this task's revision and
this task's rows only. C4 1.5 carries ``line`` and ``decision`` as open strings;
:meth:`Queue.answerable` is the one place that says whether a decision may answer a line
of an episode: a catalog line the episode's card asks (then any of the line's catalog
decisions) or a follow-up the card's answers open (then only the follow-up's decisions),
and the rules below on top. :meth:`Queue._stands` is the one place of the lapse rule.

**Card status** (optional follow-ups left out, except that their answers must be
executed before a card is ``applied``): a discard on any line decides the card (rule 1:
it wins over every verdict); else any ``unsure`` keeps it ``unsure`` - still pending,
still in the queue (rule 3); else every question answered makes it ``decided``
(``applied`` once every answer was executed); a relabel not executed yet also decides
it, because executing re-judges the task with the new label; anything else is
``pending``.

**Counts** (C4 ``AdjudicationCounts``: cards of the whole task, not the filtered page):
``pending`` and ``decided`` over the cards with a question on a ``counts_as_pending``
line - appeal candidates are optional - and ``unapplied`` over both tabs: cards with an
answer that was not executed yet (``unsure`` is not one). ``pending`` is what
``summary.pending_adjudication`` holds for the task list and the overview.
"""
from __future__ import annotations

import bisect
import logging
import threading
from dataclasses import dataclass
from typing import Iterable

from curation.contracts import modules as registry

from ..errors import ApiError
from ..pagination import CursorError, decode_cursor, encode_cursor
from ..repo import protocol as P
from . import catalog as C
from .revision import Revision
from .store import ResultStore

log = logging.getLogger("daemon.results")

CURSOR_KIND = "adjudication"


@dataclass(frozen=True)
class Question:
    episode: int
    line: str
    source_module: str
    reason: str
    annotation: str | None = None
    caption: str | None = None
    suggestion: str | None = None
    priority: str | None = None
    duplicate_of: int | None = None
    revision: int = 0
    optional: bool = False              # an optional follow-up: never pending, never blocking
    follow_up_of: str | None = None     # the line whose answer opened it (C1 1.3 follow_ups)


def _str(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _join(texts: Iterable[str]) -> str:
    return "；".join(dict.fromkeys(t for t in texts if t and t != "未注明")) or "未注明"


def _reject_reasons(rev: Revision, episode: int) -> list[str]:
    hit = rev.entries().get(episode)
    if hit is None or hit[0] != "reject":
        return []
    return [str(r.get("text") or "") for r in hit[1].get("reasons") or []
            if isinstance(r, dict) and r.get("kind") != "execution_error"]


def _task_text(rev: Revision, episode: int) -> str | None:
    hit = rev.entries().get(episode)
    if hit is not None:
        tt = hit[1].get("task_text")
        if isinstance(tt, dict) and _str(tt.get("text")):
            return tt["text"]
    rec = rev.record("task_success", episode)
    return _str(((rec or {}).get("details") or {}).get("task_desc"))


def questions_of(rev: Revision) -> dict[tuple[int, str], Question]:
    """Every question revision ``rev`` asks, by (episode, line)."""
    def make():
        out: dict[tuple[int, str], Question] = {}
        audit = rev.audit_entries()
        relabel = set(C.relabel_lines())
        for ep, entry in rev.review().items():
            asked: dict[str, list[dict]] = {}
            for item in entry.get("review") or []:
                ln = C.line_of_item(item) if isinstance(item, dict) else None
                if ln is not None:
                    asked.setdefault(ln.id, []).append(item)
            for line_id, items in asked.items():
                ln = C.line(line_id)
                first = items[0]
                reasons = [str(i.get("reason") or "") for i in items]
                dup = next((i["duplicate_of"] for i in items if isinstance(i.get("duplicate_of"), int)
                            and not isinstance(i.get("duplicate_of"), bool)), None)
                q = {"source_module": str(first.get("source_module") or ""),
                     "priority": _str(first.get("priority")), "annotation": _task_text(rev, ep),
                     "duplicate_of": dup}
                if line_id in relabel:                  # the annotation against the description
                    a = audit.get(ep) or {}
                    caption = _str(a.get("caption"))
                    q.update(annotation=_str(a.get("label")) or q["annotation"],
                             caption=caption, suggestion=caption,
                             priority=q["priority"] or _str(a.get("priority")))
                if ln.tab == "appeals":                 # why it was rejected
                    reasons += _reject_reasons(rev, ep)
                out[(ep, line_id)] = Question(ep, line_id, reason=_join(reasons),
                                              revision=rev.number, **q)
        return out

    return rev.store.derived.get_or_make(("questions", rev.key), make)


def decision_json(a: P.Adjudication) -> dict:
    """C4 ``Decision``."""
    return {"episode_index": int(a.episode_index), "line": a.line, "decision": a.decision,
            "new_label": a.new_label, "note": a.note, "id": int(a.id),
            "decided_by": a.decided_by, "decided_at": int(a.decided_at),
            "applied": a.applied_in_subtask is not None}


def _spec(a: P.Adjudication | None) -> C.DecisionSpec | None:
    return C.decision(a.line, a.decision) if a is not None else None


def _unsure(a: P.Adjudication | None) -> bool:
    spec = _spec(a)
    return bool(spec and spec.unsure)


@dataclass
class Card:
    episode: int
    questions: list[Question]
    status: str

    def unapplied(self, standing: dict) -> bool:
        """Something to execute: a standing answer not applied yet (``unsure`` is not one)."""
        for q in self.questions:
            d = standing.get((q.episode, q.line))
            if d is not None and d.applied_in_subtask is None and not _unsure(d):
                return True
        return False

    def to_json(self, standing: dict) -> dict:
        qs = []
        for q in self.questions:
            d = standing.get((q.episode, q.line))
            qs.append({"line": q.line, "source_module": q.source_module, "reason": q.reason,
                       "duplicate_of": q.duplicate_of, "follow_up_of": q.follow_up_of,
                       "annotation": q.annotation, "caption": q.caption,
                       "suggestion": q.suggestion, "priority": q.priority,
                       "latest_decision": decision_json(d) if d is not None else None})
        return {"episode_index": self.episode, "status": self.status, "questions": qs}

    def counts_as_pending(self) -> bool:
        pending = C.pending_lines()
        return any(q.line in pending and not q.optional for q in self.questions)


def card_status(questions: list[Question], standing: dict) -> str:
    """pending / decided / unsure / applied of one card, from its standing answers.

    An optional follow-up never blocks ``decided`` and never makes a card pending or
    unsure; an answer on it still has to be executed before the card is ``applied``.
    """
    required = [q for q in questions if not q.optional]
    decs = [standing.get((q.episode, q.line)) for q in required]
    extra = [d for q in questions if q.optional
             if (d := standing.get((q.episode, q.line))) is not None and not _unsure(d)]
    specs = [_spec(d) for d in decs]
    discard = next((d for d, s in zip(decs, specs) if s is not None and s.discard), None)
    if discard is not None:                             # rule 1
        return "applied" if discard.applied_in_subtask else "decided"
    if any(s is not None and s.unsure for s in specs):
        return "unsure"                                 # rule 3: stays in the queue
    if all(d is not None for d in decs):
        done = all(d.applied_in_subtask for d in decs + extra)
        return "applied" if done else "decided"
    if any(s is not None and s.relabel and d.applied_in_subtask is None
           for d, s in zip(decs, specs)):
        return "decided"                                # rule 4: executing re-judges it
    return "pending"


class Queue:
    """The task's questions, decisions and cards at its current revision.

    ``asked`` are the questions modules asked (current revision, or an older one for an
    answered question); ``questions`` add the follow-ups that are open now; ``decisions``
    are the latest rows per (episode, line); ``standing`` are those that count - a
    follow-up answer lapses once the answer that opened it changes (:meth:`_stands`).
    """

    def __init__(self, store: ResultStore, repo: P.Repository, task: P.Task, *,
                 backfill: bool = True):
        self.task = task
        self.rev = store.current(task, backfill=backfill)
        self.revision = self.rev.number if self.rev is not None else 0
        self.decisions = {(int(a.episode_index), a.line): a
                          for a in repo.latest_adjudications(task.id)}
        self.selected = {m.module_id for m in repo.get_task_modules(task.id) if m.selected}
        self.asked: dict[tuple[int, str], Question] = {}
        if self.rev is not None:
            self.asked = self._asked(store, backfill)
        self.questions = {**self.asked, **self._open_follow_ups()}
        self.standing = {key: d for key, d in self.decisions.items() if self._stands(key, d)}
        self._cards: dict[str, list[Card]] = {}

    def _asked(self, store: ResultStore, backfill: bool) -> dict[tuple[int, str], Question]:
        qs = dict(questions_of(self.rev))
        missing = [k for k in self.decisions if k not in qs]
        for n in range(self.revision - 1, 0, -1):             # answered, then left the review
            if not missing:
                break
            try:
                older = store.revision(self.task, n, backfill=backfill)
            except ApiError:
                continue
            found = questions_of(older)
            for k in [k for k in missing if k in found]:
                qs[k] = found[k]
                missing.remove(k)
        return qs

    # -- follow-ups (C1 1.3 follow_ups; v1's verdict after a relabel) --------------------
    def _follow_up(self, ep: int, line_id: str, answers: dict) -> tuple[C.FollowUpSpec, Question] | None:
        """The follow-up on ``line_id`` that ``answers`` open on episode ``ep``'s card, with
        the question it asks; None when the card asks that line itself or nothing opens it."""
        if (ep, line_id) in self.asked:
            return None
        for f in C.follow_ups_onto(line_id):
            owner = self.asked.get((ep, f.owner))
            opener = answers.get((ep, f.owner))
            opener = opener.decision if isinstance(opener, P.Adjudication) else opener
            if owner is not None and f.opened_by(opener):
                return f, Question(ep, line_id, owner.source_module, C.follow_up_reason(f),
                                   annotation=owner.annotation, revision=owner.revision,
                                   optional=f.optional, follow_up_of=f.owner)
        return None

    def _open_follow_ups(self) -> dict[tuple[int, str], Question]:
        out = {}
        for ep, owner_line in list(self.asked):
            for f in C.line(owner_line).follow_ups if C.line(owner_line) else ():
                hit = self._follow_up(ep, f.line, self.decisions)
                if hit is not None:
                    out[(ep, f.line)] = hit[1]
        return out

    def _stands(self, key: tuple[int, str], d: P.Adjudication) -> bool:
        """Whether a latest answer counts. The one place of the lapse rule: an answer on a
        follow-up counts only while the answer that opened it is still the owner's latest
        and was given before it, and only with one of the follow-up's decisions."""
        ep, line_id = key
        if key in self.asked:
            return True
        hit = self._follow_up(ep, line_id, self.decisions)
        if hit is None:
            # no follow-up open on it: an answer on a follow-up line lapsed; anything else
            # has no question left (an older revision's files are gone) and still counts
            return not C.follow_ups_onto(line_id)
        f, _ = hit
        opener = self.decisions[(ep, f.owner)]
        return d.id > opener.id and d.decision in f.decisions

    # -- cards ------------------------------------------------------------------------
    def cards(self, tab: str) -> list[Card]:
        if tab not in self._cards:
            lines = C.tab_lines(tab)
            by_ep: dict[int, list[Question]] = {}
            for (ep, line), q in self.questions.items():
                if line in lines:
                    by_ep.setdefault(ep, []).append(q)
            cards = []
            for ep in sorted(by_ep):
                qs = sorted(by_ep[ep], key=lambda q: (q.optional, C.order(q.line)))
                cards.append(Card(ep, qs, card_status(qs, self.standing)))
            self._cards[tab] = cards
        return self._cards[tab]

    def counts(self) -> dict:
        """C4 ``AdjudicationCounts``: cards of the whole task. ``pending`` / ``decided`` over
        the cards with a question on a ``counts_as_pending`` line; ``unapplied`` over all."""
        cards = [c for tab in C.TABS for c in self.cards(tab)]
        counted = [c for c in cards if c.counts_as_pending()]
        decided = sum(1 for c in counted if c.status in ("decided", "applied"))
        unapplied = sum(1 for c in cards if c.unapplied(self.standing))
        return {"decided": decided, "pending": len(counted) - decided, "unapplied": unapplied}

    def executable(self) -> list[P.Adjudication]:
        """The decisions an apply subtask hands to ``curation adjudicate-apply``: the latest
        per episode and line that still stand and were not applied yet, oldest first.
        Lapsed follow-up answers are left out; ``unsure`` rows go along (they change nothing
        and keep the CLI's record complete)."""
        return sorted((d for d in self.standing.values() if d.applied_in_subtask is None),
                      key=lambda d: d.id)

    def page(self, *, tab: str, status: str, source: str | None, cursor: str | None,
             limit: int) -> dict:
        """C4 ``listAdjudication``: cards by episode, a cursor bound to the revision."""
        cards = [c for c in self.cards(tab) if _keep(c, status, source, self.standing)]
        scope = {"task": self.task.id, "tab": tab, "status": status, "source": source}
        start = 0
        if cursor:
            key = decode_cursor(cursor, CURSOR_KIND, scope=scope)
            if not (isinstance(key, list) and len(key) == 2 and all(
                    isinstance(v, int) and not isinstance(v, bool) for v in key)):
                raise CursorError("cursor has no revision and episode")
            rev, last = key
            if rev != self.revision:
                raise ApiError("result_changed",
                               f"结果版本已从 r{rev} 换成 r{self.revision}，裁决队列请从头重新加载",
                               details={"cursor_revision": rev, "revision": self.revision})
            start = bisect.bisect_right([c.episode for c in cards], last)
        chunk = cards[start:start + limit]
        more = start + len(chunk) < len(cards)
        next_cursor = encode_cursor(CURSOR_KIND, [self.revision, chunk[-1].episode],
                                    scope=scope) if more else None
        return {"items": [c.to_json(self.standing) for c in chunk], "next_cursor": next_cursor,
                "has_more": more, "counts": self.counts()}

    # -- submissions ------------------------------------------------------------------
    def check(self, items: list[dict]) -> list[dict]:
        """Validate a submission against the queue, all or nothing; the rows to append.
        Items are checked in order, and later ones see the earlier ones' answers."""
        if self.rev is None:
            raise ApiError("task_state_conflict",
                           "这个任务还没有结果版本，没有可以裁决的条目；任务跑完之后再来",
                           details={"state": self.task.state, "result_rev": 0})
        latest = {key: d.decision for key, d in self.decisions.items()}
        rows = []
        for i, item in enumerate(items):
            ep, line_id, decision_id = int(item["episode_index"]), item["line"], item["decision"]
            where = f"decisions.{i}"
            question, spec = self.answerable(ep, line_id, decision_id, latest, where)
            rows.append({"episode_index": ep, "line": line_id, "decision": decision_id,
                         "new_label": _new_label(question, spec, item.get("new_label"), ep, where),
                         "note": item.get("note")})
            latest[(ep, line_id)] = decision_id
        return rows

    def answerable(self, ep: int, line_id: str, decision_id: str, latest: dict,
                   where: str) -> tuple[Question, C.DecisionSpec]:
        """Whether ``decision_id`` may answer ``line_id`` of episode ``ep`` - the one place
        that says so. ``latest`` holds the answers in force (decision values), the
        submission's so far included. The line must be a catalog line; the card must ask
        it, or a follow-up the card's answers open must answer on it (then only the
        follow-up's decisions); the decision must be one of the line's; a verdict next to
        a discard on the card is refused (rule 1). Raises ``validation_failed`` naming the
        field; returns the question and the decision."""
        name = f"ep{ep:06d}"
        ln = C.line(line_id)
        if ln is None:
            raise _bad(f"没有 {line_id} 这条裁决线（可选：{'、'.join(x.id for x in C.LINES)}）",
                       f"{where}.line")
        spec = ln.decision(decision_id)
        if spec is None:
            raise _bad(f"{name}：{ln.title_zh}不能选 {decision_id}（可以选："
                       f"{'、'.join(f'{d.title_zh}（{d.id}）' for d in ln.decisions)}）",
                       f"{where}.decision")
        question = self.asked.get((ep, line_id))
        if question is None:
            hit = self._follow_up(ep, line_id, latest)
            if hit is None:
                raise self._not_asked(ep, ln, latest, where)
            f, question = hit
            if decision_id not in f.decisions:
                raise _bad(f"{name}：这里的{ln.title_zh}只能选{ln.titles(f.decisions)}",
                           f"{where}.decision")
            if not any(f.line in registry.get(m).review_lines for m in self.selected
                       if m in registry.ids()):
                raise _bad(f"{name}：这个任务没有勾选产生「{ln.title_zh}」的模块，不能回答",
                           f"{where}.line")
        if spec.verdict and any(
                getattr(C.decision(r, latest.get((ep, r))), "discard", False)
                for r in C.relabel_lines() if r != line_id):
            raise _bad(f"{name} 已经「整条弃用」，弃用的条目不再判成败；要判成败，先撤销弃用",
                       f"{where}.decision")
        return question, spec

    def _not_asked(self, ep: int, ln: C.LineSpec, latest: dict, where: str) -> ApiError:
        name = f"ep{ep:06d}"
        if ln.tab == "appeals":
            return _bad(f"{name} 不在复议列表里，不能复议：只有可复议模块判的拒绝能复议，"
                        f"物理与结构检查的拒绝是终局", f"{where}.episode_index")
        closed = [f for f in C.follow_ups_onto(ln.id) if (ep, f.owner) in self.asked]
        if closed:
            f = closed[0]
            owner = C.line(f.owner)
            return _bad(f"{name} 先在「{owner.title_zh}」选{owner.titles(f.after, '或')}，才能回答"
                        f"「{ln.title_zh}」", f"{where}.line")
        if ln.id in C.relabel_lines():
            return _bad(f"{name} 没有待裁决的标注分歧，不能裁决标注", f"{where}.episode_index")
        return _bad(f"{name} 不在这个任务的待裁决队列里（没有要人判成败的问题）",
                    f"{where}.episode_index")


def _relabel_titles(joiner: str) -> str:
    titles = dict.fromkeys(f"「{d.title_zh}」" for ln in C.LINES for d in ln.decisions if d.relabel)
    return joiner.join(titles)


def _new_label(question: Question, spec: C.DecisionSpec, raw, ep: int, where: str) -> str | None:
    """``new_label`` only for a relabel; typed when the decision needs it, else the suggestion."""
    name = f"ep{ep:06d}"
    value = (raw.strip() or None) if isinstance(raw, str) else None
    if value is not None and not spec.relabel:
        raise _bad(f"{name}：只有{_relabel_titles('和')}可以带 new_label", f"{where}.new_label")
    if spec.relabel and value is None:
        if spec.needs_label:
            raise _bad(f"{name}：{spec.title_zh}要填写新标注", f"{where}.new_label")
        value = question.suggestion
        if value is None:
            typed = next((f"「{d.title_zh}」" for ln in C.LINES for d in ln.decisions
                          if d.needs_label), "自己填写")
            raise _bad(f"{name} 没有建议的新标注，请用{typed}", f"{where}.new_label")
    return value


def _bad(message: str, field: str) -> ApiError:
    return ApiError("validation_failed", message,
                    details={"errors": [{"field": field, "problem": message}]})


def _keep(card: Card, status: str, source: str | None, standing: dict) -> bool:
    if source and not any(q.source_module == source for q in card.questions):
        return False
    if status == "pending":
        return card.status in ("pending", "unsure")
    if status == "decided":
        return card.status in ("decided", "applied")
    if status == "unapplied":
        return card.unapplied(standing)
    return True


def known_source(source: str | None) -> None:
    if source is not None and source not in registry.ids():
        raise ApiError("validation_failed",
                       f"没有 {source} 这个质检模块（可选：{'、'.join(registry.ids())}）",
                       details={"errors": [{"field": "source", "problem": "unknown module"}]})


# ---------------------------------------------------------------------------
# recording decisions, the CSV copy and the task summary
# ---------------------------------------------------------------------------

#: ``skipped`` (D40) is there only when the report has episodes left out for missing sources
_SUMMARY_COUNTS = ("total", "passed", "rejected", "held", "review", "skipped")
_copy_locks: dict[str, threading.Lock] = {}
_copy_guard = threading.Lock()


def _copy_lock(task_id: str) -> threading.Lock:
    with _copy_guard:
        return _copy_locks.setdefault(task_id, threading.Lock())


def summary_of(rev: Revision, counts: dict) -> dict:
    """The task summary of revision ``rev`` (C4 ``Summary``) plus ``pending_adjudication``."""
    overview = rev.report().get("overview") or {}
    c = overview.get("counts") or {}
    out: dict = {k: c[k] for k in _SUMMARY_COUNTS
                 if isinstance(c.get(k), int) and not isinstance(c.get(k), bool) and c[k] >= 0}
    rate = overview.get("pass_rate")
    if rate is None or isinstance(rate, (int, float)) and not isinstance(rate, bool):
        out["pass_rate"] = rate
    out["pending_adjudication"] = int(counts["pending"])
    return out


def refresh_summary(store: ResultStore, repo: P.Repository, task_id: str, *,
                    owner: str = P.DEFAULT_OWNER) -> tuple[dict | None, dict]:
    """Recompute the task's summary from its current revision and decisions; returns
    ``(summary written or kept, counts)``. The orchestration calls it after every
    revision switch, the adjudication endpoints after every submission.

    The counts are taken inside one repository transaction, so the last refresh to run
    has seen every decision appended before it (the file reads are cached beforehand).
    """
    task = repo.get_task(task_id, owner=owner)
    if int(task.result_rev or 0) < 1:
        return None, {"decided": 0, "pending": 0, "unapplied": 0}
    Queue(store, repo, task)                  # warm the file caches (and backfill) first
    with repo.transaction():
        cur = repo.get_task(task_id, owner=owner)
        queue = Queue(store, repo, cur, backfill=False)
        counts = queue.counts()
        if queue.rev is None:
            return None, counts
        owned = (*_SUMMARY_COUNTS, "pass_rate", "pending_adjudication")
        kept = {k: v for k, v in (cur.summary if isinstance(cur.summary, dict) else {}).items()
                if k not in owned}                     # other writers' keys stay
        merged = {**kept, **summary_of(queue.rev, counts)}
        if merged != cur.summary:
            repo.set_task_summary(task_id, merged)
    return merged, counts


def write_copies(store: ResultStore, repo: P.Repository, task: P.Task) -> bool:
    """``<run dir>/human-decisions/*.csv``: this task's decisions in v1's columns and words,
    written by the CLI's own writer (``pipeline.adjudication.write_human_copies``). The
    database is the authority (design doc 01 §2.7); a failed copy is only a warning.

    C5 reads back the latest decision per line and episode only, so the copy holds those
    rows, oldest first - v1's readers take the last row per episode, the same answer.
    """
    from curation.pipeline.adjudication import Decisions, write_human_copies

    run_dir = store.task_dir(task.id)
    if not run_dir.is_dir():
        log.warning("no local run directory for task %s; decisions not copied to "
                    "human-decisions/ yet", task.id)
        return False
    with _copy_lock(task.id):
        rows = [{"id": int(a.id), "episode_index": int(a.episode_index), "line": a.line,
                 "decision": a.decision, "new_label": a.new_label, "note": a.note,
                 "decided_at": int(a.decided_at)} for a in repo.latest_adjudications(task.id)]
        try:
            write_human_copies(str(run_dir), Decisions(rows))
        except Exception:  # noqa: BLE001 - the database already has them
            log.warning("writing human-decisions/ of task %s failed", task.id, exc_info=True)
            return False
    return True


def submit(store: ResultStore, repo: P.Repository, task_id: str, items: list[dict], *,
           owner: str, actor: str, at: int) -> dict:
    """Record decisions (append only; nothing is executed, D10) -> C4 ``AdjudicationCounts``."""
    from ..transitions import record

    task = repo.get_task(task_id, owner=owner)
    rows = Queue(store, repo, task).check(items)
    created = repo.append_adjudication(
        [P.AdjudicationCreate(task_id=task.id, episode_index=r["episode_index"], line=r["line"],
                              decision=r["decision"], decided_by=actor, new_label=r["new_label"],
                              note=r["note"], owner_id=task.owner_id) for r in rows], at=at)
    record(repo, action="task.adjudicate", task_id=task.id, actor=actor, at=at,
           owner=task.owner_id, detail={"decisions": len(created),
                                        "ids": [a.id for a in created][:50]})
    write_copies(store, repo, task)
    try:
        _, counts = refresh_summary(store, repo, task.id, owner=owner)
    except Exception:  # noqa: BLE001 - the decisions are recorded; the list catches up later
        log.warning("summary of task %s not refreshed after a submission", task.id,
                    exc_info=True)
        counts = Queue(store, repo, repo.get_task(task.id, owner=owner)).counts()
    return counts
