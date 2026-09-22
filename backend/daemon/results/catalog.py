"""What a person can be asked on the adjudication page and what they can answer.

The lines come from the module registry (C1 1.3 ``REVIEW_LINES``, D43): id, the
``review.json`` kind that asks it, title, the list its episodes are in, whether an open
question counts as pending, the decisions with their titles, and the follow-ups it
opens. C4 1.5 carries ``line`` and ``decision`` as open strings that the Daemon checks
against this catalog and the card's questions; a new kind of review is a new registry
entry and needs no change here.

Derived here, and nowhere else:

* the tab a line is shown on - ``appeals`` for lines about rejected episodes
  (``applies_to: reject``), ``review`` otherwise;
* whether a card counts as pending or decided - lines with ``counts_as_pending``
  (appeal candidates are optional);
* follow-ups (C1 1.3 ``follow_ups``, C4 1.5.1): a question a card gains once its answer
  on the owning line is one of ``after`` - only on a card that does not ask that line
  already - with a subset of that line's decisions. v1's relabel card: after adopting a
  new label a person may give the task verdict (the machine takes it, no re-judging);
  left open, the episode is judged again with the new label. An optional follow-up never
  counts as pending, and its answer lapses once the answer that opened it changes;
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

#: The reason a follow-up question shows, by (owning line, follow-up line); the registry
#: has no text for it. Other follow-ups get a reason made of the titles.
FOLLOW_UP_REASONS: dict[tuple[str, str], str] = {
    ("label", "task_verdict"): "改标之后可以一并判成败（选填）：判了就以人的结论为准，不再按新标注重判；"
                               "不判则按新标注重新判定",
}


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
class FollowUpSpec:
    owner: str                          # the line whose answer opens it
    after: tuple[str, ...]              # the owner's decisions that open it
    line: str                           # the line it answers on
    decisions: tuple[str, ...]          # the subset of that line's decisions it offers
    optional: bool

    def opened_by(self, decision_id: str | None) -> bool:
        return decision_id in self.after


@dataclass(frozen=True)
class LineSpec:
    id: str
    title_zh: str
    kind: str                           # the review.json item kind that asks it
    tab: str                            # review | appeals
    counts_as_pending: bool
    decisions: tuple[DecisionSpec, ...]
    follow_ups: tuple[FollowUpSpec, ...] = ()

    def decision(self, decision_id: str) -> DecisionSpec | None:
        return next((d for d in self.decisions if d.id == decision_id), None)

    def ids(self) -> tuple[str, ...]:
        return tuple(d.id for d in self.decisions)

    def titles(self, ids, joiner: str = "、") -> str:
        return joiner.join(f"「{d.title_zh}」" for d in self.decisions if d.id in set(ids))


def _spec(line: registry.ReviewLine) -> LineSpec:
    return LineSpec(line.id, line.title_zh, line.review_kind,
                    "appeals" if line.applies_to == "reject" else "review", line.counts_as_pending,
                    tuple(DecisionSpec(value, title, MEANINGS.get(value, _PLAIN))
                          for value, title in line.decisions),
                    tuple(FollowUpSpec(line.id, tuple(f.after), f.line, tuple(f.decisions),
                                       bool(f.optional))
                          for f in getattr(line, "follow_ups", ()) or ()))


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


def follow_ups_onto(line_id: str) -> tuple[FollowUpSpec, ...]:
    """The follow-ups that answer on ``line_id`` (their owners are other lines)."""
    return tuple(f for ln in LINES for f in ln.follow_ups if f.line == line_id)


def follow_up_reason(f: FollowUpSpec) -> str:
    known = FOLLOW_UP_REASONS.get((f.owner, f.line))
    if known:
        return known
    owner, target = line(f.owner), line(f.line)
    opened = owner.titles(f.after, "或") if owner else "、".join(f.after)
    title = target.title_zh if target else f.line
    return f"{owner.title_zh if owner else f.owner}选了{opened}之后可以一并回答「{title}」" + (
        "（选填）" if f.optional else "")
