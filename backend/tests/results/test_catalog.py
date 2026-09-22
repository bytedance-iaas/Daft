"""The adjudication catalog agrees with C4 1.4, C2 decisions.json and the CLI."""
from __future__ import annotations

from curation.contracts import schemas
from curation.pipeline.adjudication import LINE_DECISIONS

from daemon.results import catalog as C


def test_lines_and_decisions_are_the_contracts():
    assert {ln.id: ln.ids() for ln in C.LINES} == {k: tuple(v) for k, v in LINE_DECISIONS.items()}
    fields = schemas.load("openapi.yaml")["components"]["schemas"]["DecisionFields"]["properties"]
    assert set(fields["line"]["enum"]) == {ln.id for ln in C.LINES}
    assert set(fields["decision"]["enum"]) == {d.id for ln in C.LINES for d in ln.decisions}
    kinds = schemas.load("cli/final-list.schema.json")["$defs"]["entry"]["properties"]["review"][
        "items"]["properties"]["kind"]["enum"]
    assert {ln.kind for ln in C.LINES} == set(kinds)


def test_what_decisions_mean():
    assert C.relabel_lines() == ("label",)
    assert [d.id for ln in C.LINES for d in ln.decisions if d.discard] == ["discard", "discard"]
    assert [d.id for d in C.line("task_verdict").decisions if d.verdict] == ["success", "failure"]
    assert all(ln.decision("unsure").unsure for ln in C.LINES)
    assert C.line("label").decision("custom_label").needs_label
    assert not C.line("label").decision("adopt_suggestion").needs_label
    assert C.tab_lines("review") == ("label", "task_verdict")
    assert C.tab_lines("appeals") == ("reject_appeal",)
    assert C.line_for_kind("label_conflict").id == "label"
    assert C.line_for_kind("something_new") is None
