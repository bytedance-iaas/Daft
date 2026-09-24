"""The adjudication catalog is the registry's (C1 1.2, D43) and agrees with C2 and the CLI."""
from __future__ import annotations

from curation.contracts import modules as registry
from curation.pipeline.adjudication import LINE_DECISIONS

from daemon.results import catalog as C


def test_lines_and_decisions_come_from_the_registry():
    assert [(ln.id, ln.kind, ln.title_zh, ln.counts_as_pending, ln.ids()) for ln in C.LINES] == [
        (line.id, line.review_kind, line.title_zh, line.counts_as_pending,
         tuple(v for v, _ in line.decisions)) for line in registry.REVIEW_LINES]
    assert [(d.id, d.title_zh) for d in C.line("label").decisions] == list(
        registry.review_line("label").decisions)
    # the CLI applies exactly these (adjudicate-apply refuses anything else)
    assert {ln.id: ln.ids() for ln in C.LINES} == {k: tuple(v) for k, v in LINE_DECISIONS.items()}


def test_what_the_page_and_the_rules_derive():
    assert C.tab_lines("review") == ("label", "task_verdict", "eef_check")
    assert C.tab_lines("appeals") == ("reject_appeal",)
    assert C.pending_lines() == ("label", "task_verdict", "eef_check")   # appeals never count as pending
    assert C.relabel_lines() == ("label",)
    assert [d.id for ln in C.LINES for d in ln.decisions if d.discard] == ["discard", "discard"]
    assert [d.id for d in C.line("task_verdict").decisions if d.verdict] == ["success", "failure"]
    assert not any(d.verdict or d.discard or d.relabel for d in C.line("eef_check").decisions)   # plain answers
    assert all(ln.decision("unsure").unsure for ln in C.LINES)
    assert C.line("label").decision("custom_label").needs_label
    assert not C.line("label").decision("adopt_suggestion").needs_label


def test_follow_ups_come_from_the_registry():
    """C1 1.3: after a relabel the label line opens an optional task verdict (v1's card)."""
    (f,) = C.line("label").follow_ups
    expected = registry.follow_up("label", "adopt_suggestion", "task_verdict")
    assert (f.owner, f.after, f.line, f.decisions, f.optional) == (
        "label", tuple(expected.after), expected.line, tuple(expected.decisions), expected.optional)
    assert f.opened_by("custom_label") and not f.opened_by("keep_label")
    assert "discard" not in f.decisions
    assert C.follow_ups_onto("task_verdict") == (f,)
    assert C.follow_ups_onto("label") == () and C.follow_ups_onto("reject_appeal") == ()
    assert "选填" in C.follow_up_reason(f)


def test_items_name_their_line_or_their_kind():
    assert C.line_of_item({"kind": "label_conflict"}).id == "label"
    assert C.line_of_item({"kind": "label_conflict", "line": "label"}).id == "label"
    assert C.line_of_item({"kind": "reject_appeal", "line": "reject_appeal"}).id == "reject_appeal"
    assert C.line_of_item({"kind": "something_new"}) is None
    assert C.line_of_item({"kind": "task_verdict", "line": "not_in_the_registry"}) is None
