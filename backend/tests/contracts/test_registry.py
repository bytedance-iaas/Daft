"""C1: the module registry is consistent and its JSON export is current."""
from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest

from curation.contracts import modules as M
from curation.contracts import schemas


def test_modules_in_block_and_stage_order():
    assert M.ids() == ("data_integrity", "timestamp_check", "kinematic_limits", "motion_quality",
                       "visual_quality", "video_action_sync", "eef_video_consistency", "task_success",
                       "camera_defects", "duplicate_check")
    for block, stages in M.BLOCKS.items():
        order = [stages.index(m.stage) for m in M.by_block(block)]
        assert order == sorted(order), f"the {block} block's modules must follow its stage order"
    assert M.STAGES == M.BLOCKS["cpu"] + M.BLOCKS["vlm"]
    assert "full_set_stages" not in M.export(), "D70: every stage judges one episode at a time"


@pytest.mark.parametrize("spec", M.MODULES, ids=M.ids())
def test_module_spec(spec):
    assert spec.needs <= M.NEEDS
    assert set(spec.depends_on) <= M.DEPENDENCIES
    assert spec.block in M.BLOCKS and spec.stage in M.BLOCKS[spec.block]
    assert spec.stage != "autolabel", "the caption pass is gone (3.2); no stage of that name"
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


def test_taxonomy_ids_keep_their_meaning():
    """C6 is frozen since taxonomy 2.0 (2026-10-14, the sample set v2): an id keeps its dimension, name, kind, level
    and guards. A new item is appended to taxonomy_frozen.json with the next number of its dimension; a dropped one
    stays there under ``dropped``, so its number is never used again. Editing an entry there changes what results
    already made mean: give a changed item a new number instead."""
    frozen = json.loads(Path(__file__).with_name("taxonomy_frozen.json").read_text(encoding="utf-8"))
    items = {it["id"]: it for it in M.taxonomy()["items"]}
    dropped = set(frozen["dropped"])
    assert dropped <= set(frozen["items"])
    assert set(items) == set(frozen["items"]) - dropped, "an item is added to (or dropped in) taxonomy_frozen.json too"
    numbers: dict[str, list[int]] = {}
    for iid, (dim, name, kind, level, guards) in frozen["items"].items():
        prefix, n = iid.rsplit("-", 1)
        assert prefix == dim, iid
        numbers.setdefault(dim, []).append(int(n))
        if iid in items:
            it = items[iid]
            assert [it["dimension"], it["name_zh"], it["kind"], it["level"], it.get("guards") or []] == \
                [dim, name, kind, level, guards], f"{iid} is frozen: give a changed item a new number"
    for dim, ns in numbers.items():
        assert ns == list(range(1, len(ns) + 1)), f"{dim}: numbers follow on, none skipped or used twice"


def test_v1_facts():
    """Design doc 05 section 1 / 17 §3: the per-episode checks, then the duplicate check (D87: reported only)."""
    assert {m.id for m in M.by_stage("duplicate")} == {"duplicate_check"}
    assert "dedup" not in M.ids() and "dedup" not in M.STAGES, "exact dedup and its stage were removed (D87)"
    assert "profile" not in M.STAGES, "the skill profile and its stage were removed"
    assert {m.id for m in M.by_block("vlm")} == {"eef_video_consistency", "task_success",
                                                 "camera_defects"}
    assert M.get("duplicate_check").level == "dataset"
    assert {c.level for c in M.get("duplicate_check").codes} == {"info"}
    assert not M.get("duplicate_check").produces_adjudication and not M.get("duplicate_check").appealable
    assert "duplicate_content" not in {c.code for c in M.get("data_integrity").codes}
    assert {m.id for m in M.MODULES if m.produces_adjudication} == {"task_success",
                                                                    "eef_video_consistency",
                                                                    "data_integrity",
                                                                    "video_action_sync"}     # 7.3 sync_check
    assert not any(m.depends_on for m in M.MODULES), "3.2: no data dependency is left"
    assert "autolabel" not in M.ids() and "autolabel" not in M.STAGES and M.BLOCKS["vlm"] == ("vlm",)
    assert dict(M.UNASSESSABLE_REASONS)["no_task_text"]


def test_the_default_policy_reproduces_todays_gates():
    """P18: today's hard gates block, today's suspects / abstentions / questions are review, everything
    else - the retired soft scores, the advisory camera defects, the profile's statistics - is info. Since 5.0
    the EEF module rejects nothing (D81): a person may, answering a conflict."""
    blocking = {m.id for m in M.MODULES if any(c.level == "blocking" for c in m.codes)}
    assert blocking == {"data_integrity", "timestamp_check", "kinematic_limits", "video_action_sync",
                        "task_success", "visual_quality"}
    for soft in ("motion_quality", "visual_quality", "camera_defects"):
        # 7.1 (D93): a corrupt video rejects in the modules that decode it, visual quality included
        assert {c.level for c in M.get(soft).codes if c.code != "decode_failed"} == {"info"}, soft
    for m in ("visual_quality", "video_action_sync", "task_success"):
        c = M.finding_code(m, "decode_failed")
        assert (c.item, c.level, c.appealable) == ("FILE-4", "blocking", False) and "FILE-4" in M.get(m).covers
    reviewed = {(m.id, c.review_line) for m in M.MODULES for c in m.codes if c.level == "review"}
    assert {line for _, line in reviewed} == {"integrity_check", "eef_check", "task_verdict", "sync_check"}
    # 7.3: the sync check's cameras that disagree are asked, the whole-episode offset still rejects by machine
    assert ("video_action_sync", "sync_check") in reviewed
    assert M.finding_code("video_action_sync", "misaligned_all").level == "review"            # 7.5: every sync finding is asked
    assert {(m.id, c.code) for m in M.MODULES for c in m.codes if c.appealable} == {
        ("task_success", "failure")}


def test_p20_items_have_codes():
    """P20: the items that needed only a mapping are covered from the first stage on.

    LABEL-1 (one episode with several descriptions that disagree) left the list with the skill
    profile: it was the only module that read the descriptions against each other."""
    covered = {item for m in M.MODULES for item in m.covers}
    assert {"LABEL-2", "IMG-3", "TASK-1", "AV-3", "MV-3", "ACT-6"} <= covered
    assert "LABEL-1" not in covered


def test_the_eef_module_gives_opinions():
    """D49: one module, CPU then model; 5.0 (design doc 25 §7.3, D81): opinions with a confidence - nothing it
    reports rejects, a conflict between its channels is asked on eef_check. 7.0 (D90): the codes it no longer
    reports are gone from the catalogue."""
    assert M.native_ids() == ("data_integrity", "eef_video_consistency", "duplicate_check")
    spec = M.get("eef_video_consistency")
    assert (spec.block, spec.stage) == ("vlm", "vlm") and {"eef_input", "video"} <= spec.needs
    assert spec.depends_on == () and "eef_video_review" not in M.ids()
    assert (spec.code("inconsistent").level, spec.code("inconsistent").appealable) == ("info", False)
    assert (spec.code("conflict").level, spec.code("conflict").review_line) == ("review", "eef_check")
    assert not spec.appealable and spec.review_lines == ("eef_check",)
    assert not {"unsettled", "opinion_mismatch"} & {c.code for c in spec.codes}
    exported = {m["id"]: m for m in M.export()["modules"]}
    assert all("retired" not in c for c in exported["eef_video_consistency"]["codes"])
    props = spec.param_schema["properties"]
    # 4.3 (design doc 24): the trajectory is generated from the dataset; 7.0 (D90): the parameters the dataset
    # declaration replaced (gripper_calibration, record_mapping) are gone
    assert "trajectory_json" not in spec.param_schema.get("required", []) and "review_windows_per_camera" in props
    assert M.upload_params(spec.id) == {"trajectory_json": "eef_trajectory", "observation_seeds": "eef_observation_seeds",
                                        "gripper_template": "eef_gripper_template"}
    assert [o["const"] for o in props["threshold_profile"]["oneOf"]] == ["demo"]   # no "no thresholds" any more
    assert {"eef_review_windows", "eef_opinions"} <= {t.id for t in spec.tables}
    exported = {m["id"]: m for m in M.export()["modules"]}
    for gone in ("gate", "input_scope", "affects_dataset_verdict", "produces_adjudication", "review_lines",
                 "appealable"):
        assert gone not in exported["eef_video_consistency"], gone
    # 4.1 (design doc 22 §5.3): a wrist camera's own motion against the poses, its time offset the episode's AV-1
    assert exported["eef_video_consistency"]["covers"] == ["MV-4", "AV-1"]
    ego = spec.code("ego_motion_suspect")
    assert (ego.level, ego.item, ego.scope_kind) == ("info", "MV-4", "camera")
    assert props["ego_motion_window_s"]["default"] == 0.5 and "eef_ego_motion" in [t.id for t in spec.tables]


def test_the_eef_module_runs_in_two_halves():
    """5.3 (design doc 23 §1-§2.1, design doc 25 F5.24b): its CPU half in the CPU block's second root vlm_prep, its
    records from vlm when a model is asked; no other module has a CPU half of its own yet."""
    spec = M.get("eef_video_consistency")
    assert (spec.prep_stage, spec.stage, M.PREP_STAGES[spec.prep_stage]) == ("vlm_prep", "vlm", "vlm")
    assert M.BLOCKS["cpu"][-1] == "vlm_prep" and M.ROOT_STAGES == ("vlm_prep",)
    assert [m.id for m in M.MODULES if m.prep_stage] == ["eef_video_consistency"]
    exported = M.export()
    assert exported["root_stages"] == ["vlm_prep"] and exported["prep_stages"] == {"vlm_prep": "vlm"}
    assert {m["id"]: m.get("prep_stage") for m in exported["modules"]}["eef_video_consistency"] == "vlm_prep"


def test_the_eef_module_asks_the_model_by_its_switch():
    """5.2 (design doc 25 §4.2-§4.3, D84): the EEF module no longer needs a model - ``use_vlm`` (on by default) says
    whether it asks one; the gripper reference applies only where a camera is a fixed third-person one."""
    spec = M.get("eef_video_consistency")
    assert "vlm" not in spec.needs and spec.vlm_switch == "use_vlm"
    assert spec.asks_model() and spec.asks_model({"use_vlm": True}) and not spec.asks_model({"use_vlm": False})
    assert M.get("task_success").asks_model({"use_vlm": False})          # a module that needs one always asks
    assert not M.get("data_integrity").asks_model()
    props = spec.param_schema["properties"]
    assert props["use_vlm"] == {**props["use_vlm"], "type": "boolean", "default": True}
    assert {k for k, p in props.items() if p.get("x-applies-when") == M.THIRD_PERSON_CAMERA} == {
        "observation_seeds", "gripper_template"}
    exported = {m["id"]: m for m in M.export()["modules"]}
    assert exported["eef_video_consistency"]["vlm_switch"] == "use_vlm"
    assert "vlm_switch" not in exported["task_success"]


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
    assert list(props) == ["full_read", "decode_test"] and props["decode_test"]["default"] is False
    assert props["full_read"]["default"] is False                  # L2 is a task's choice (design doc 23 §3.1)
    assert props["decode_test"]["description"].endswith("耗时相当于把全部视频完整解码一次")   # what it does and costs
    assert "native" not in M.export()["modules"][0]                # internal, not part of C1's JSON


def test_review_lines():
    """D42 / D43: the review catalog, modules naming the lines they raise.

    The label line went with the skill profile (registry 3.0): a label conflict the kill guard
    finds now asks the task verdict, so one card asks one question."""
    assert [line.id for line in M.REVIEW_LINES] == ["task_verdict", "reject_appeal", "eef_check",
                                                    "integrity_check", "sync_check"]
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
    assert {m.id for m in M.MODULES if m.appealable} == {"task_success"}     # EEF rejects nothing (D81), duplicates neither (D87)
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
    M.validate_params("eef_video_consistency", {})                      # the trajectory is generated (design doc 24)
    with pytest.raises(jsonschema.ValidationError):
        M.validate_params("eef_video_consistency", {"use_vlm": "yes"})
    with pytest.raises(jsonschema.ValidationError):
        M.validate_params("eef_video_consistency", {"trajectory_json": "x", "threshold_profile": "strict"})
    M.validate_params("video_action_sync", {"sync_plots": "all"})
    M.validate_params("timestamp_check", {})
    with pytest.raises(jsonschema.ValidationError):
        M.validate_params("video_action_sync", {"sync_plots": "none"})   # v1 says off
    with pytest.raises(jsonschema.ValidationError):
        M.validate_params("duplicate_check", {"concurrency": 4})         # it never runs in parallel


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
    assert spec.covers == ("IMG-5", "IMG-6", "IMG-7", "IMG-8", "MV-2")       # 7.2: occlusion, camera identity
    assert M.riders_of("task_success") == ("camera_defects",) and M.riders_of("duplicate_check") == ()
    assert M.with_riders(["task_success", "duplicate_check"]) == ["task_success", "camera_defects", "duplicate_check"]
    assert M.with_riders(["duplicate_check"]) == ["duplicate_check"]     # no host, no rider
    assert M.with_riders(["camera_defects", "task_success"]) == ["task_success", "camera_defects"]
    exported = {m["id"]: m for m in M.export()["modules"]}
    assert exported["camera_defects"]["rides_on"] == "task_success"
    assert "rides_on" not in exported["task_success"]              # only set where it applies
