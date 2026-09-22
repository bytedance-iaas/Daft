"""What a person can be asked on the adjudication page and what they can answer: one catalog.

C4 1.4 fixes three lines (``DecisionFields.line``) and their decisions, the same as C2
``decisions.json`` and the CLI's ``pipeline.adjudication.LINE_DECISIONS`` (a test keeps
them equal). C1 1.5 will declare the review kinds in the module registry (line id,
title, decisions with titles, which modules' rejects are appealable) and C4 opens
``line`` / ``decision`` to strings the Daemon validates against that catalog and the
card's questions. Everything that says which lines exist, which ``review.json`` kind
asks which line, which tab shows it, which decisions it takes and what a decision
means lives here, so that switch replaces this module and nothing else.

What a decision means is carried as flags, never by comparing names elsewhere:

* ``relabel`` - sets a new task text (``new_label``); ``needs_label`` - the person must
  type it (otherwise the question's suggestion is taken);
* ``discard`` - drops the whole episode; it wins over every verdict on the card (rule 1);
* ``unsure`` - recorded, changes nothing, the card stays pending (rule 3);
* ``verdict`` - a person's task verdict; next to a discard it is refused, and after a
  relabel it stands without re-judging (rule 4).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DecisionSpec:
    id: str
    title_zh: str
    relabel: bool = False
    needs_label: bool = False
    discard: bool = False
    unsure: bool = False
    verdict: bool = False


@dataclass(frozen=True)
class LineSpec:
    id: str
    title_zh: str
    kind: str                           # the review.json item kind that asks it
    tab: str                            # review | appeals
    decisions: tuple[DecisionSpec, ...]

    def decision(self, decision_id: str) -> DecisionSpec | None:
        return next((d for d in self.decisions if d.id == decision_id), None)

    def ids(self) -> tuple[str, ...]:
        return tuple(d.id for d in self.decisions)


_UNSURE = DecisionSpec("unsure", "拿不准", unsure=True)
_DISCARD = DecisionSpec("discard", "整条弃用", discard=True)

#: v1's three lines (design doc 06 §5.1), in the order a card shows its questions.
LINES: tuple[LineSpec, ...] = (
    LineSpec("label", "标注分歧", "label_conflict", "review", (
        DecisionSpec("adopt_suggestion", "采纳建议改标", relabel=True),
        DecisionSpec("custom_label", "自行改写标注", relabel=True, needs_label=True),
        DecisionSpec("keep_label", "维持原标注"), _UNSURE, _DISCARD)),
    LineSpec("task_verdict", "任务成败", "task_verdict", "review", (
        DecisionSpec("success", "判成功", verdict=True),
        DecisionSpec("failure", "判失败", verdict=True), _UNSURE, _DISCARD)),
    LineSpec("reject_appeal", "被拒复议", "reject_appeal", "appeals", (
        DecisionSpec("restore", "恢复为可用"), DecisionSpec("keep_rejected", "维持拒绝"), _UNSURE)),
)
TABS = ("review", "appeals")

#: v1's optional verdict (rule 4, ``manifest.success_block_mode``): on a card whose label
#: was changed, the task verdict can be given even though no module asked for it.
OPTIONAL_VERDICT = ("task_verdict", "task_success")      # (line, the module it answers for)
OPTIONAL_REASON = "改标之后可以直接判成败：判了就以人的结论为准，不再按新标注重跑任务成败判定"

_BY_ID = {ln.id: ln for ln in LINES}
_BY_KIND = {ln.kind: ln for ln in LINES}


def line(line_id: str) -> LineSpec | None:
    return _BY_ID.get(line_id)


def line_for_kind(kind: str) -> LineSpec | None:
    """The line a ``review.json`` item of ``kind`` asks, None for a kind nobody answers."""
    return _BY_KIND.get(kind)


def tab_lines(tab: str) -> tuple[str, ...]:
    return tuple(ln.id for ln in LINES if ln.tab == tab)


def order(line_id: str) -> int:
    return next((i for i, ln in enumerate(LINES) if ln.id == line_id), len(LINES))


def decision(line_id: str, decision_id: str) -> DecisionSpec | None:
    ln = line(line_id)
    return ln.decision(decision_id) if ln is not None else None


def is_relabel(line_id: str, decision_id: str | None) -> bool:
    d = decision(line_id, decision_id) if decision_id else None
    return bool(d and d.relabel)


def relabel_lines() -> tuple[str, ...]:
    """Lines whose answers can set a new task text (v1: the label line)."""
    return tuple(ln.id for ln in LINES if any(d.relabel for d in ln.decisions))
