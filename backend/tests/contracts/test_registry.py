"""C1: the module registry is consistent and its JSON export is current."""
from __future__ import annotations

import json

import jsonschema
import pytest

from curation.contracts import modules as M
from curation.contracts import schemas


def test_eight_modules_in_stage_order():
    assert M.ids() == ("timestamp_check", "kinematic_limits", "motion_quality", "visual_quality",
                       "video_action_sync", "task_success", "dedup", "skill_profile")
    order = [M.STAGE_ORDER.index(m.stage) for m in M.MODULES]
    assert order == sorted(order), "registry order must follow the stage order"


@pytest.mark.parametrize("spec", M.MODULES, ids=M.ids())
def test_module_spec(spec):
    assert spec.needs <= M.NEEDS
    assert set(spec.depends_on) <= M.DEPENDENCIES
    assert spec.stage in M.STAGE_ORDER
    jsonschema.Draft202012Validator.check_schema(spec.param_schema)
    for table in spec.tables:
        assert table.default_sort in table.sortable
    assert spec.merge_units is None, "D23: the existing VLM modules do not merge"


def test_v1_facts():
    """Design doc 05 section 1: the six funnel checks then dedup and profile on the kept set."""
    assert {m.id for m in M.by_stage("post_verdict")} == {"dedup", "skill_profile"}
    assert M.get("dedup").gate == "dedup" and M.get("skill_profile").gate == "none"
    assert {m.id for m in M.MODULES if m.produces_adjudication} == {"task_success", "dedup",
                                                                    "skill_profile"}
    assert {m.id for m in M.MODULES if "vlm" in m.needs} == {"task_success", "skill_profile"}
    assert "autolabel" not in M.ids()


def test_review_lines():
    """D42 / D43: the review catalog is v1's three lines; modules name the lines they raise."""
    assert [line.id for line in M.REVIEW_LINES] == ["label", "task_verdict", "reject_appeal"]
    for line in M.REVIEW_LINES:
        assert line.decisions and len({c for c, _ in line.decisions}) == len(line.decisions)
        assert M.review_line_of_kind(line.review_kind) is line
    assert M.review_line("reject_appeal").applies_to == "reject"
    assert not M.review_line("reject_appeal").counts_as_pending     # an appeal is optional
    assert all(M.review_line(x).counts_as_pending for x in ("label", "task_verdict"))
    raised = {x for m in M.MODULES for x in m.review_lines}
    assert raised <= {line.id for line in M.REVIEW_LINES}
    assert {m.id for m in M.MODULES if m.appealable} == {"task_success", "dedup"}
    for m in M.MODULES:
        assert m.produces_adjudication == bool(m.review_lines or m.appealable), m.id
    # v1: after adopting a new label a person may give the task verdict (no re-judge)
    f = M.follow_up("label", "adopt_suggestion", "task_verdict")
    assert f is not None and f.optional and set(f.decisions) == {"success", "failure", "unsure"}
    assert M.follow_up("label", "keep_label", "task_verdict") is None
    for line in M.REVIEW_LINES:
        for fu in line.follow_ups:
            target = {c for c, _ in M.review_line(fu.line).decisions}
            assert set(fu.decisions) <= target and set(fu.after) <= {c for c, _ in line.decisions}
    # physical and structural gates and soft scores are final
    assert not any(M.appealable(m.id) for m in M.MODULES if m.gate in ("soft", "none")
                   or m.id in ("timestamp_check", "kinematic_limits", "video_action_sync"))


def test_params_validate():
    M.validate_params("video_action_sync", {"sync_plots": "all"})
    M.validate_params("timestamp_check", {})
    with pytest.raises(jsonschema.ValidationError):
        M.validate_params("video_action_sync", {"sync_plots": "none"})   # v1 says off
    with pytest.raises(jsonschema.ValidationError):
        M.validate_params("dedup", {"concurrency": 4})                   # dedup never runs in parallel


def test_modules_json_is_current_and_fits_the_api_schema():
    exported = M.export()
    assert json.loads((schemas.contracts_dir() / "modules.json").read_text()) == exported
    schemas.validate("openapi.yaml#/components/schemas/ModuleRegistry", exported)


def test_result_record_module_pattern_accepts_every_id():
    pattern = schemas.load("cli/common.schema.json")["$defs"]["module_id"]["pattern"]
    import re

    assert all(re.fullmatch(pattern, mid) for mid in M.ids())
