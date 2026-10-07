"""C1: the module registry is consistent and its JSON export is current."""
from __future__ import annotations

import json
import re

import jsonschema
import pytest

from curation.contracts import modules as M
from curation.contracts import schemas


def test_modules_in_block_and_stage_order():
    assert M.ids() == ("data_integrity", "timestamp_check", "kinematic_limits", "motion_quality",
                       "visual_quality", "video_action_sync", "eef_video_consistency", "task_success",
                       "camera_defects", "dedup")
    for block, stages in M.BLOCKS.items():
        order = [stages.index(m.stage) for m in M.by_block(block)]
        assert order == sorted(order), f"the {block} block's modules must follow its stage order"
    assert M.STAGES == M.BLOCKS["cpu"] + M.BLOCKS["vlm"]
    assert set(M.FULL_SET_STAGES) == {"dedup"}
    assert all(s in M.STAGES for s in M.FULL_SET_STAGES)


@pytest.mark.parametrize("spec", M.MODULES, ids=M.ids())
def test_module_spec(spec):
    assert spec.needs <= M.NEEDS
    assert set(spec.depends_on) <= M.DEPENDENCIES
    assert spec.block in M.BLOCKS and spec.stage in M.BLOCKS[spec.block]
    assert spec.stage != "autolabel", "autolabel is a stage, not a module"
    jsonschema.Draft202012Validator.check_schema(spec.param_schema)
    for table in spec.tables:
        assert table.default_sort in table.sortable
    assert spec.merge_units is None, "D23: the existing VLM modules do not merge"


def _taxonomy():
    return {it["id"]: it for it in M.taxonomy()["items"]}


@pytest.mark.parametrize("spec", M.MODULES, ids=M.ids())
def test_finding_codes(spec):
    """Design doc 17 §1.3 / §2.1: every code names an item of the bound taxonomy, controls are never a
    code's item, the defaults are well formed and the review lines exist."""
    items = _taxonomy()
    assert spec.codes, "every module reports findings"
    names = [c.code for c in spec.codes]
    assert len(set(names)) == len(names), "a code is unique within its module"
    for c in spec.codes:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", c.code), c.code
        assert c.severity in M.SEVERITIES and c.scope_kind in M.SCOPE_KINDS
        assert c.level in {lv for lv, _ in M.FINDING_LEVELS}
        if c.item is None:
            assert c.level == "info", f"{c.code}: only an info-level platform item may have no taxonomy item"
        else:
            assert c.item in items, f"{spec.id}.{c.code} names {c.item}, not in taxonomy {M.TAXONOMY_VERSION}"
            assert items[c.item]["kind"] != "control", f"{spec.id}.{c.code}: a control item is never a finding"
            if items[c.item]["level"] == "dataset" and c.scope_kind != "dataset":
                # a dataset-level item reported on an episode (the integrity module's byte copies)
                assert spec.id == "data_integrity" or spec.level == "dataset", c.code
        if c.level == "review":
            assert c.review_line and M.review_line(c.review_line).applies_to == "passed", c.code
        else:
            assert c.review_line is None, f"{c.code}: only review-level codes name a review line"
        if c.appealable:
            assert c.level == "blocking", f"{c.code}: only a blocking code can be appealed"
    assert spec.code(names[0]).code == names[0]
    with pytest.raises(KeyError):
        spec.code("no_such_code")


@pytest.mark.parametrize("spec", M.MODULES, ids=M.ids())
def test_coverage_declarations(spec):
    """§2.3: covers = the codes' items plus also_covers, all taxonomy ids, no control."""
    items = _taxonomy()
    assert set(spec.covers) >= {c.item for c in spec.codes if c.item}
    assert set(spec.covers) == {c.item for c in spec.codes if c.item} | set(spec.also_covers)
    assert len(set(spec.covers)) == len(spec.covers)
    for item in spec.covers:
        assert item in items and items[item]["kind"] != "control", item


def test_the_taxonomy_contract():
    """C6: the bound version, unique ids, dimensions, guards naming real non-control items."""
    tax = M.taxonomy()
    schemas.validate("taxonomy.schema.json", tax)
    assert tax["taxonomy_version"] == M.TAXONOMY_VERSION
    ids = [it["id"] for it in tax["items"]]
    assert len(ids) == len(set(ids))
    dims = {d["id"] for d in tax["dimensions"]}
    items = _taxonomy()
    for it in tax["items"]:
        assert it["dimension"] in dims and it["id"].split("-")[0] == it["dimension"], it["id"]
        for g in it.get("guards", ()):
            assert g in items and items[g]["kind"] != "control", (it["id"], g)


def test_the_taxonomy_matches_the_sample_sets():
    """§1.3: the platform's taxonomy is the sample set's (same ids and names); its platform notes are not."""
    tools = json.loads((schemas.contracts_dir().parent.parent / "tools" / "regression_samples" /
                        "taxonomy.json").read_text(encoding="utf-8"))
    assert tools["taxonomy_version"] == M.TAXONOMY_VERSION
    ours = [(it["id"], it["name_zh"], it["kind"], it["level"], it.get("guards")) for it in M.taxonomy()["items"]]
    theirs = [(it["id"], it["name"], it["kind"], it["level"], it.get("guards")) for it in tools["items"]]
    assert ours == theirs


def test_v1_facts():
    """Design doc 05 section 1 / 17 §3: the funnel checks, then dedup on the whole set."""
    assert {m.id for m in M.by_stage("dedup")} == {"dedup"}
    assert "profile" not in M.STAGES, "the skill profile and its stage were removed"
    assert {m.id for m in M.by_block("vlm")} == {"eef_video_consistency", "task_success",
                                                 "camera_defects"}
    assert M.get("dedup").level == "dataset"
    assert {m.id for m in M.MODULES if m.produces_adjudication} == {"task_success", "dedup",
                                                                    "eef_video_consistency",
                                                                    "data_integrity"}
    assert {m.id for m in M.MODULES if "autolabel" in m.depends_on} == {"task_success",
                                                                        "camera_defects"}
    assert "autolabel" not in M.ids()


def test_the_default_policy_reproduces_todays_gates():
    """P18: today's hard gates block, today's suspects / abstentions / questions are review, everything
    else - the retired soft scores, the advisory camera defects, the profile's statistics - is info."""
    blocking = {m.id for m in M.MODULES if any(c.level == "blocking" for c in m.codes)}
    assert blocking == {"data_integrity", "timestamp_check", "kinematic_limits", "video_action_sync",
                        "eef_video_consistency", "task_success", "dedup"}
    for soft in ("motion_quality", "visual_quality", "camera_defects"):
        assert {c.level for c in M.get(soft).codes} == {"info"}, soft
    reviewed = {(m.id, c.review_line) for m in M.MODULES for c in m.codes if c.level == "review"}
    assert {line for _, line in reviewed} == {"integrity_check", "eef_check", "task_verdict"}
    assert {(m.id, c.code) for m in M.MODULES for c in m.codes if c.appealable} == {
        ("task_success", "failure"), ("eef_video_consistency", "inconsistent"), ("dedup", "duplicate")}


def test_p20_items_have_codes():
    """P20: the items that needed only a mapping are covered from the first stage on.

    LABEL-2 (one episode with several descriptions that disagree) left the list with the skill
    profile: it was the only module that read the descriptions against each other."""
    covered = {item for m in M.MODULES for item in m.covers}
    assert {"LABEL-3", "IMG-3", "TASK-1", "AV-3", "MV-3", "SET-3", "ACT-6"} <= covered
    assert "LABEL-2" not in covered


def test_the_eef_module_takes_part_in_the_verdict():
    """D49 / design doc 12 D-E11: one module, CPU then model; its rejects may be appealed and what it
    cannot settle goes to a person on eef_check."""
    assert M.native_ids() == ("data_integrity", "eef_video_consistency")
    spec = M.get("eef_video_consistency")
    assert (spec.block, spec.stage) == ("vlm", "vlm") and {"eef_input", "video", "vlm"} <= spec.needs
    assert spec.depends_on == () and "eef_video_review" not in M.ids()
    assert spec.code("inconsistent").level == "blocking" and spec.code("inconsistent").appealable
    assert spec.code("unsettled").review_line == "eef_check"
    props = spec.param_schema["properties"]
    assert "trajectory_json" in spec.param_schema["required"] and "review_windows_per_camera" in props
    assert [o["const"] for o in props["threshold_profile"]["oneOf"]] == ["demo"]   # no "no thresholds" any more
    assert "eef_review_windows" in [t.id for t in spec.tables]
    exported = {m["id"]: m for m in M.export()["modules"]}
    for gone in ("gate", "input_scope", "affects_dataset_verdict", "produces_adjudication", "review_lines",
                 "appealable"):
        assert gone not in exported["eef_video_consistency"], gone
    assert exported["eef_video_consistency"]["covers"] == ["MV-5"]


def test_the_data_integrity_module_is_the_first_stage():
    """1.11 / design doc 14, D50: the CPU block's first stage, run by v2 itself; its rejects are final and
    its suspects are asked; one parameter, the decode test, off."""
    assert M.BLOCKS["cpu"][0] == "integrity" and M.ids()[0] == "data_integrity"
    spec = M.get("data_integrity")
    assert (spec.block, spec.stage, spec.level) == ("cpu", "integrity", "episode")
    assert spec.native and spec.needs == frozenset({"raw_bytes"})
    assert not spec.appealable and spec.review_lines == ("integrity_check",)
    assert {c.scope_kind for c in spec.codes if c.code in ("orphan_files", "dark_camera", "table_overlap")} == {"dataset"}
    props = spec.param_schema["properties"]
    assert list(props) == ["decode_test"] and props["decode_test"]["default"] is False
    assert props["decode_test"]["description"].endswith("耗时相当于把全部视频完整解码一次")   # what it does and costs
    assert "native" not in M.export()["modules"][0]                # internal, not part of C1's JSON


def test_review_lines():
    """D42 / D43: the review catalog, modules naming the lines they raise.

    The label line went with the skill profile (registry 3.0): a label conflict the kill guard
    finds now asks the task verdict, so one card asks one question."""
    assert [line.id for line in M.REVIEW_LINES] == ["task_verdict", "reject_appeal", "eef_check",
                                                    "integrity_check"]
    for line in M.REVIEW_LINES:
        assert line.decisions and len({c for c, _ in line.decisions}) == len(line.decisions)
        assert M.review_line_of_kind(line.review_kind) is line
    assert M.review_line("reject_appeal").applies_to == "reject"
    assert not M.review_line("reject_appeal").counts_as_pending     # an appeal is optional
    assert all(M.review_line(x).counts_as_pending
               for x in ("task_verdict", "eef_check", "integrity_check"))
    integ = M.review_line("integrity_check")
    assert (integ.review_kind, integ.applies_to) == ("integrity_suspect", "passed")
    assert [c for c, _ in integ.decisions] == ["intact", "broken", "unsure"]
    eef = M.review_line("eef_check")
    assert (eef.review_kind, eef.applies_to) == ("eef_consistency", "passed")
    assert [c for c, _ in eef.decisions] == ["consistent", "inconsistent", "unsure"]
    assert M.get("eef_video_consistency").review_lines == ("eef_check",)
    raised = {x for m in M.MODULES for x in m.review_lines}
    assert raised <= {line.id for line in M.REVIEW_LINES}
    assert {m.id for m in M.MODULES if m.appealable} == {"task_success", "dedup", "eef_video_consistency"}
    assert M.get("task_success").review_lines == ("task_verdict",)
    for m in M.MODULES:
        assert m.produces_adjudication == bool(m.review_lines or m.appealable), m.id
    # one card asks one question: the label line and the follow-up it owned are both gone
    assert not hasattr(M, "follow_up") and not hasattr(M, "FollowUp")
    assert all(not hasattr(line, "follow_ups") for line in M.REVIEW_LINES)
    # physical and structural gates are final; info-only modules reject nothing
    assert not any(M.appealable(m.id) for m in M.MODULES
                   if m.id in ("timestamp_check", "kinematic_limits", "video_action_sync", "data_integrity",
                               "motion_quality", "visual_quality", "camera_defects"))


def test_params_validate():
    M.validate_params("eef_video_consistency", {"trajectory_json": "/data/trajectory.json", "lag_search_s": 0.5})
    with pytest.raises(jsonschema.ValidationError):
        M.validate_params("eef_video_consistency", {})                  # the file is required
    with pytest.raises(jsonschema.ValidationError):
        M.validate_params("eef_video_consistency", {"trajectory_json": "x", "threshold_profile": "strict"})
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


def test_camera_defects_rides_on_task_success():
    """Registry 1.14: answered inside task_success's per-camera reviews; info only; never selected alone."""
    spec = M.get("camera_defects")
    assert spec.rides_on == "task_success" and (spec.block, spec.stage) == ("vlm", "vlm")
    assert {c.level for c in spec.codes} == {"info"} and not spec.produces_adjudication
    assert spec.covers == ("IMG-5", "IMG-6", "IMG-7")
    assert M.riders_of("task_success") == ("camera_defects",) and M.riders_of("dedup") == ()
    assert M.with_riders(["task_success", "dedup"]) == ["task_success", "camera_defects", "dedup"]
    assert M.with_riders(["dedup"]) == ["dedup"]                     # no host, no rider
    assert M.with_riders(["camera_defects", "task_success"]) == ["task_success", "camera_defects"]
    exported = {m["id"]: m for m in M.export()["modules"]}
    assert exported["camera_defects"]["rides_on"] == "task_success"
    assert "rides_on" not in exported["task_success"]              # only set where it applies
