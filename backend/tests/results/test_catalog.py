"""The adjudication catalog is the registry's (C1 1.2, D43) and agrees with C2 and the CLI."""
from __future__ import annotations

from curation.contracts import modules as registry
from curation.pipeline.adjudication import LINE_DECISIONS

from daemon.results import catalog as C


def test_lines_and_decisions_come_from_the_registry():
    assert [(ln.id, ln.kind, ln.title_zh, ln.counts_as_pending, ln.ids()) for ln in C.LINES] == [
        (line.id, line.review_kind, line.title_zh, line.counts_as_pending,
         tuple(v for v, _ in line.decisions)) for line in registry.REVIEW_LINES]
    assert [(d.id, d.title_zh) for d in C.line("task_verdict").decisions] == list(
        registry.review_line("task_verdict").decisions)
    # the CLI applies exactly these (adjudicate-apply refuses anything else)
    assert {ln.id: ln.ids() for ln in C.LINES} == {k: tuple(v) for k, v in LINE_DECISIONS.items()}


def test_what_the_page_and_the_rules_derive():
    assert C.tab_lines("review") == ("task_verdict", "eef_check", "integrity_check")
    assert C.tab_lines("appeals") == ("reject_appeal",)
    # appeals never count as pending; the data integrity module's suspects do (design doc 14 §4.4)
    assert C.pending_lines() == ("task_verdict", "eef_check", "integrity_check")
    # one card asks one question, and a rewritten task text rides on the task verdict
    assert C.relabel_lines() == ("task_verdict",)
    assert [d.id for ln in C.LINES for d in ln.decisions if d.discard] == ["discard"]
    assert [d.id for d in C.line("task_verdict").decisions if d.verdict] == ["success", "failure"]
    assert not any(d.verdict or d.discard for d in C.line("eef_check").decisions)   # plain answers
    # intact / broken are the gate's result, not v1's whole-episode discard (design doc 14 §4.4)
    assert not any(d.verdict or d.discard for d in C.line("integrity_check").decisions)
    assert all(ln.decision("unsure").unsure for ln in C.LINES)


def test_items_name_their_line_or_their_kind():
    assert C.line_of_item({"kind": "task_verdict"}).id == "task_verdict"
    assert C.line_of_item({"kind": "task_verdict", "line": "task_verdict"}).id == "task_verdict"
    assert C.line_of_item({"kind": "reject_appeal", "line": "reject_appeal"}).id == "reject_appeal"
    assert C.line_of_item({"kind": "something_new"}) is None
    assert C.line_of_item({"kind": "task_verdict", "line": "not_in_the_registry"}) is None
