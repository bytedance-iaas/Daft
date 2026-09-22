"""What a person can be asked on the adjudication page and what they can answer.

The lines come from the module registry (C1 1.2 ``REVIEW_LINES``, D43): id, the
``review.json`` kind that asks it, title, the list its episodes are in, whether an open
question counts as pending, and the decisions with their titles. C4 1.5 carries
``line`` and ``decision`` as open strings that the Daemon checks against this catalog
and the card's questions; a new kind of review is a new registry entry and needs no
change here.

Derived here, and nowhere else:

* the tab a line is shown on - ``appeals`` for lines about rejected episodes
  (``applies_to: reject``), ``review`` otherwise;
* whether a card counts as pending or decided - lines with ``counts_as_pending``
  (appeal candidates are optional);
* what a decision *means* for the rules on top of the catalog. The registry gives values
  and titles only, so v1's decisions carry their meaning as flags (a value the table does
  not know is a plain answer):

  - ``relabel`` - sets a new task text (``new_label``); ``needs_label`` - the person must
    type it (otherwise the question's suggestion is taken);
  - ``discard`` - drops the whole episode; it wins over every verdict on the card (rule 1);
  - ``unsure`` - recorded, changes nothing, the card stays pending (rule 3);
  - ``verdict`` - a person's task verdict; next to a discard it is refused, and after a
    relabel it stands without re-judging (rule 4).
"""
from __future__ import annotations

from dataclasses import dataclass

from curation.contracts import modules as registry

TABS = ("review", "appeals")

#: v1's optional verdict (rule 4, ``manifest.success_block_mode``): on a card whose label
#: was changed, the task verdict can be given although no module asked for it.
OPTIONAL_VERDICT = ("task_verdict", "task_success")      # (line, the module it answers for)
OPTIONAL_REASON = "改标之后可以直接判成败：判了就以人的结论为准，不再按新标注重跑任务成败判定"


@dataclass(frozen=True)
class Meaning:
    relabel: bool = False
    needs_label: bool = False
    discard: bool = False
    unsure: bool = False
    verdict: bool = False


#: What v1's decision values mean (design doc 06 §5.1).
MEANINGS: dict[str, Meaning] = {
    "adopt_suggestion": Meaning(relabel=True),
    "custom_label": Meaning(relabel=True, needs_label=True),
    "discard": Meaning(discard=True),
    "unsure": Meaning(unsure=True),
    "success": Meaning(verdict=True),
    "failure": Meaning(verdict=True),
}
_PLAIN = Meaning()


@dataclass(frozen=True)
class DecisionSpec:
    id: str
    title_zh: str
    meaning: Meaning = _PLAIN

    @property
    def relabel(self) -> bool:
        return self.meaning.relabel

    @property
    def needs_label(self) -> bool:
        return self.meaning.needs_label

    @property
    def discard(self) -> bool:
        return self.meaning.discard

    @property
    def unsure(self) -> bool:
        return self.meaning.unsure

    @property
    def verdict(self) -> bool:
        return self.meaning.verdict


@dataclass(frozen=True)
class LineSpec:
    id: str
    title_zh: str
    kind: str                           # the review.json item kind that asks it
    tab: str                            # review | appeals
    counts_as_pending: bool
    decisions: tuple[DecisionSpec, ...]

    def decision(self, decision_id: str) -> DecisionSpec | None:
        return next((d for d in self.decisions if d.id == decision_id), None)

    def ids(self) -> tuple[str, ...]:
        return tuple(d.id for d in self.decisions)


def _spec(line: registry.ReviewLine) -> LineSpec:
    return LineSpec(line.id, line.title_zh, line.review_kind,
                    "appeals" if line.applies_to == "reject" else "review", line.counts_as_pending,
                    tuple(DecisionSpec(value, title, MEANINGS.get(value, _PLAIN))
                          for value, title in line.decisions))


LINES: tuple[LineSpec, ...] = tuple(_spec(line) for line in registry.REVIEW_LINES)
_BY_ID = {ln.id: ln for ln in LINES}
_BY_KIND = {ln.kind: ln for ln in LINES}


def line(line_id: str | None) -> LineSpec | None:
    return _BY_ID.get(line_id) if isinstance(line_id, str) else None


def line_of_item(item: dict) -> LineSpec | None:
    """The line a ``review.json`` item asks: its ``line`` (C2 1.5), else the line of its
    ``kind``; None for one the catalog does not know (it is not asked)."""
    if item.get("line") is not None:
        return line(item.get("line"))
    return _BY_KIND.get(item.get("kind")) if isinstance(item.get("kind"), str) else None


def tab_lines(tab: str) -> tuple[str, ...]:
    return tuple(ln.id for ln in LINES if ln.tab == tab)


def pending_lines() -> tuple[str, ...]:
    return tuple(ln.id for ln in LINES if ln.counts_as_pending)


def order(line_id: str) -> int:
    return next((i for i, ln in enumerate(LINES) if ln.id == line_id), len(LINES))


def decision(line_id: str | None, decision_id: str | None) -> DecisionSpec | None:
    ln = line(line_id)
    return ln.decision(decision_id) if ln is not None and decision_id else None


def is_relabel(line_id: str, decision_id: str | None) -> bool:
    d = decision(line_id, decision_id)
    return bool(d and d.relabel)


def relabel_lines() -> tuple[str, ...]:
    """Lines whose answers can set a new task text (v1: the label line)."""
    return tuple(ln.id for ln in LINES if any(d.relabel for d in ln.decisions))
