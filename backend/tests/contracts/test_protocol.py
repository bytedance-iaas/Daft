"""C5: the Repository protocol and the state machine of design doc 01."""
from __future__ import annotations

import dataclasses
import inspect
import re

from curation.contracts import schemas
from daemon.repo import protocol as P


def test_transitions_match_design_doc_table():
    """Edges from the table in design doc 01, section 3.1."""
    doc = (schemas.contracts_dir().parent / "design" / "01-data-model.md").read_text(encoding="utf-8")
    table = doc.split("### 3.1 合法迁移表", 1)[1].split("###", 1)[0]
    edges = set()
    for line in table.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("从", "---") or set(cells[0]) <= {"-"}:
            continue
        if "⇄" in cells[0]:
            a, b = [x.strip() for x in cells[0].split("⇄")]
            edges |= {(a, b), (b, a)}
            continue
        sources = [s.strip() for s in cells[0].split("/")]
        steps = [[t.strip() for t in part.split("/")] for part in cells[1].split("→")]
        chain = [sources] + steps
        for frm_set, to_set in zip(chain, chain[1:]):
            for f in frm_set:
                for t in to_set:
                    edges.add((f, t))
    code = {(f, t) for f, tos in P.TASK_TRANSITIONS.items() for t in tos}
    assert code == edges


def test_state_machine_invariants():
    assert P.TERMINAL_STATES == {"stopped", "succeeded", "completed_with_errors", "failed"}
    assert "created" not in P.SUBTASK_TRANSITIONS
    assert P.can_transition("created", "queued")
    assert not P.can_transition("failed", "queued")             # continue = a resume subtask
    assert P.can_transition("completed_with_errors", "succeeded")  # D25 recompute
    assert P.can_transition("stopped", "succeeded")               # a resume subtask finished
    assert not P.can_transition("stopped", "succeeded", subtask=True)  # subtasks are never resumed
    assert not P.can_transition("failed", "completed_with_errors", subtask=True)
    assert set(P.SUBTASK_PARENT_STATES) == {"retry", "resume", "apply_adjudication", "reexport"}
    for kind, parents in P.SUBTASK_PARENT_STATES.items():
        assert parents <= P.TERMINAL_STATES, kind


def test_state_enums_agree_with_the_api():
    api = schemas.load("openapi.yaml")["components"]["schemas"]
    assert set(api["TaskState"]["enum"]) == set(P.TASK_TRANSITIONS)
    task_fields = {f.name for f in dataclasses.fields(P.Task)}
    assert {"result_rev", "delivery_stale", "pause_reason", "vlm_snapshot",
            "source_fingerprint", "deleted_at", "owner_id"} <= task_fields


def test_protocol_uses_cas_for_state():
    sig = inspect.signature(P.Repository.update_task_state)
    assert list(sig.parameters)[:4] == ["self", "task_id", "frm", "to"]
    assert "expected" in inspect.signature(P.Repository.switch_result_rev).parameters


def test_every_repository_method_is_documented():
    undocumented = [name for name, fn in inspect.getmembers(P.Repository, inspect.isfunction)
                    if not name.startswith("_") and not (fn.__doc__ or re.search(
                        r"\.\.\.", inspect.getsource(fn)))]
    assert not undocumented
