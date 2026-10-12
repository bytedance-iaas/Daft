"""The regression sample scorer on runs of findings (C2 2.0, design doc 17 §6.1): counting, scopes, the states that
stay out of the metrics, controls and ingestion, the baseline gate."""
from __future__ import annotations

import json
import os

import pytest

from regression_samples import score as S

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(HERE))
TAXONOMY = json.load(open(os.path.join(HERE, "taxonomy.json"), encoding="utf-8"))
FMAP = json.load(open(os.path.join(HERE, "finding_map.json"), encoding="utf-8"))
SUBSET = "lerobot_v21/demo"


# ---------------------------------------------------------------- fixtures

def episode(idx, problems=(), clean=(), phenomena=(), lineage=None, verdict=None):
    def entry(item, scope=None):
        if isinstance(item, dict):                   # a whole entry, e.g. {"item": ..., "unit": "subset"}
            return dict(item)
        e = {"item": item}
        if scope is not None:
            e["scope"] = scope
        return e
    probs = [entry(*p) if isinstance(p, tuple) else entry(p) for p in problems]
    cl = [entry(*c) if isinstance(c, tuple) else entry(c) for c in clean]
    ph = [entry(*p) if isinstance(p, tuple) else entry(p) for p in phenomena]
    return {"episode_id": f"demo:{idx}", "dataset": SUBSET, "episode_index": idx, "lineage": lineage or f"demo:{idx}",
            "verdict": verdict or ("defective" if probs else "clean_on_checked_items"),
            "problems": probs, "phenomena": ph, "clean": cl, "pending": []}


def expectation(*episodes):
    return {"set": "anchor", "set_version": "v1", "episodes": list(episodes)}


# ---------------------------------------------------------------- fixtures of runs

REGISTRY = json.load(open(os.path.join(REPO, "docs", "contracts", "modules.json"), encoding="utf-8"))
COVERS = S.registry_covers(REGISTRY)


def finding(code, item, camera=None, **extra):
    f = {"code": code, "item": item, "severity": "high", "message_zh": code, **extra}
    if camera:
        f["scope"] = {"camera": camera}
    return f


def record2(ep, module, findings=(), unassessable=(), status="ok"):
    """A record 2.0: it assessed every item the registry says it covers, except the unassessable ones."""
    covers = sorted(i for i, mods in COVERS.items() if module in mods)
    if status == "error":
        return {"episode_index": ep, "module": module, "status": "error", "findings": [], "assessed": [], "unassessable": [],
                "readings": {}, "details": {}, "evidence": [], "elapsed_s": 0.1, "error": {"kind": "execution", "incidents": []}}
    un = [{"item": i, "reason": "model_no_answer", "message_zh": "没有回答"} for i in unassessable]
    return {"episode_index": ep, "module": module, "status": "ok", "findings": list(findings),
            "assessed": [i for i in covers if i not in unassessable], "unassessable": un, "readings": {}, "details": {},
            "evidence": [], "elapsed_s": 0.1, "error": None}


def write_run2(root, records, lists=None, dataset_findings=None, availability=None, supported=True):
    """A run directory of findings: parts, the final lists and a report 2.0 of revision 1."""
    os.makedirs(root, exist_ok=True)
    by_module = {}
    for r in records:
        by_module.setdefault(r["module"], []).append(r)
    for module, recs in by_module.items():
        d = os.path.join(root, "checks", module, "parts")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "0001.jsonl"), "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in recs)
    rev = os.path.join(root, "revisions", "r0001")
    os.makedirs(rev, exist_ok=True)
    for name in ("passed", "reject", "held"):
        eps = [e for e in (lists or {}).get(name, [])]
        with open(os.path.join(rev, f"{name}.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema_version": "2.0", "list": name, "revision": 1, "count": len(eps), "episodes": eps}, fh)
    report = {"schema_version": "2.0", "revision": 1, "modules": [
        {"id": m, "summary": {"dataset_findings": fs}} for m, fs in (dataset_findings or {}).items()]}
    with open(os.path.join(rev, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    pre = {"format": {"supported": supported}, "modules": [{"id": m, "availability": a} for m, a in (availability or {}).items()]}
    with open(os.path.join(root, "preflight.json"), "w", encoding="utf-8") as fh:
        json.dump(pre, fh)
    return root


def score2(tmp_path, exp, records, by_lineage=False, **kw):
    run = write_run2(str(tmp_path / "run2"), records, **kw)
    return S.score(exp, TAXONOMY, FMAP, {SUBSET: run}, registry=REGISTRY, by_lineage=by_lineage)


def decode_failed(ep, camera=None):
    return record2(ep, "data_integrity", [finding("decode_failed", "FILE-4", camera)])


def test_a_run_of_findings_is_scored_from_the_findings_themselves(tmp_path):
    exp = expectation(episode(0, problems=["FILE-4"]), episode(1, problems=["FILE-4"]),
                      episode(2, clean=["FILE-4"]), episode(3, clean=["FILE-4"]))
    recs = [record2(0, "data_integrity", [finding("decode_failed", "FILE-4")]), record2(1, "data_integrity"),
            record2(2, "data_integrity", [finding("decode_failed", "FILE-4")]), record2(3, "data_integrity")]
    doc = score2(tmp_path, exp, recs)
    row = doc["items"]["FILE-4"]
    assert (row["tp"], row["fn"], row["fp"], row["tn"]) == (1, 1, 1, 1)
    # registry 7.1 (D93): every module that decodes the videos covers FILE-4 too
    assert row["modules"] == ["data_integrity", "task_success", "video_action_sync", "visual_quality"]
    assert row["mapped"] is True
    assert doc["schema_version"] == "2.0"
    assert row["by_module"]["data_integrity"]["precision"] == 0.5


def test_the_eef_modules_opinions_count_by_their_confidence(tmp_path):
    """Registry 5.0 (design doc 25 §7): the EEF module reports every cell at "possibly" or above; only its
    "inconsistent" ones (p from the high band on, a conflict among them) count as found - unless asked otherwise."""
    exp = expectation(episode(0, problems=["MV-4"]), episode(1, problems=["MV-4"]), episode(2, clean=["MV-4"]),
                      episode(3, clean=["MV-4"]))
    eef = "eef_video_consistency"
    recs = [record2(0, eef, [finding("inconsistent", "MV-4", "ext", readings={"p": 0.8, "label": "inconsistent"})]),
            record2(1, eef, [finding("conflict", "MV-4", "ext", readings={"p": 0.95, "label": "inconsistent"})]),
            record2(2, eef, [finding("inconsistent", "MV-4", "ext", readings={"p": 0.55, "label": "possibly_inconsistent"})]),
            record2(3, eef)]
    row = score2(tmp_path, exp, recs)["items"]["MV-4"]
    assert (row["tp"], row["fn"], row["fp"], row["tn"]) == (2, 0, 0, 2)
    run = write_run2(str(tmp_path / "run_low"), recs)
    low = S.score(exp, TAXONOMY, FMAP, {SUBSET: run}, registry=REGISTRY, eef_min_p=0.4)["items"]["MV-4"]
    assert (low["tp"], low["fp"]) == (2, 1)                        # "possibly" counted: one more false alarm


def test_not_assessed_comes_from_the_records_and_errors_from_their_status(tmp_path):
    """An item a module could not assess is not assessed; a module that failed on the episode is an error."""
    exp = expectation(episode(0, problems=["TASK-4"]), episode(1, problems=["TASK-4"]), episode(2, problems=["TASK-4"]))
    recs = [record2(0, "task_success", [finding("failure", "TASK-4")]),
            record2(1, "task_success", unassessable=["TASK-4"]),
            record2(2, "task_success", status="error")]
    row = score2(tmp_path, exp, recs)["items"]["TASK-4"]
    assert (row["tp"], row["fn"]) == (1, 0)
    assert row["not_assessed"]["present"] == 1 and row["error"]["present"] == 1
    assert row["recall"] == 1.0 and row["recall_end_to_end"] == round(1 / 3, 4)


def test_an_item_no_module_covers_is_a_gap_in_a_run_of_findings(tmp_path):
    exp = expectation(episode(0, problems=["MV-1", "FILE-4"]), episode(1, clean=["MV-1"]))
    recs = [record2(0, "data_integrity", [finding("decode_failed", "FILE-4")]), record2(1, "data_integrity")]
    doc = score2(tmp_path, exp, recs)
    items = doc["items"]
    assert "MV-1" not in COVERS
    assert items["MV-1"]["mapped"] is False and (items["MV-1"]["present"], items["MV-1"]["absent"]) == (1, 1)
    assert items["MV-1"]["recall"] is None and items["FILE-4"]["tp"] == 1
    assert "no module covers them" in S.markdown(doc) and "MV-1" in S.markdown(doc)


def test_a_finding_on_one_camera_matches_that_camera(tmp_path):
    exp = expectation(episode(0, problems=[("IMG-2", {"stream": "observation.images.wrist"})], clean=[("IMG-2", {"stream": "front"})]),
                      episode(1, problems=[("IMG-2", {"stream": "front"})]))
    recs = [record2(0, "visual_quality", [finding("exposure_low", "IMG-2", camera="wrist")]),
            record2(1, "visual_quality", [finding("exposure_low", "IMG-2", camera="wrist")])]
    row = score2(tmp_path, exp, recs)["items"]["IMG-2"]
    assert (row["tp"], row["tn"], row["fn"]) == (1, 1, 1)


def test_several_modules_on_one_item_count_as_their_union_and_each_on_its_own(tmp_path):
    """STRM-1 is covered by data_integrity and visual_quality: either one finding it is a TP of the item."""
    exp = expectation(episode(0, problems=["STRM-1"]), episode(1, problems=["STRM-1"]), episode(2, clean=["STRM-1"]))
    recs = [record2(0, "data_integrity", [finding("stream_missing", "STRM-1")]), record2(0, "visual_quality"),
            record2(1, "data_integrity"), record2(1, "visual_quality", [finding("dead_or_padded", "STRM-1", camera="front")]),
            record2(2, "data_integrity"), record2(2, "visual_quality")]
    row = score2(tmp_path, exp, recs)["items"]["STRM-1"]
    assert (row["tp"], row["fn"], row["tn"]) == (2, 0, 1)
    assert row["by_module"]["data_integrity"] == {"tp": 1, "fp": 0, "fn": 1, "tn": 1, "precision": 1.0, "recall": 0.5}
    assert row["by_module"]["visual_quality"]["recall"] == 0.5


def test_a_dataset_level_finding_of_the_report_counts_once_per_subset(tmp_path):
    exp = expectation(*[episode(i, problems=["FILE-8"]) for i in range(3)])
    recs = [record2(i, "data_integrity") for i in range(3)]
    row = score2(tmp_path, exp, recs, dataset_findings={"data_integrity": [
        {"code": "metadata_invalid", "item": "FILE-8", "severity": "high", "message_zh": "帧率与时间戳不符", "unit": "dataset"}]})["items"]["FILE-8"]
    assert (row["present"], row["tp"], row["fn"]) == (1, 1, 0) and row["unit"] == "subset"


def test_a_dataset_level_finding_on_an_episode_level_item_is_not_spread_over_the_subset(tmp_path):
    """data_integrity's table check also sums its findings up for the set; the episodes' own records say which ones."""
    exp = expectation(episode(0, problems=["FILE-10"]), episode(1, clean=["FILE-10"]), episode(2, clean=["FILE-10"]))
    recs = [record2(0, "data_integrity", [finding("table_inconsistent", "FILE-10")]), record2(1, "data_integrity"),
            record2(2, "data_integrity")]
    row = score2(tmp_path, exp, recs, dataset_findings={"data_integrity": [
        {"code": "table_overlap", "item": "FILE-10", "severity": "medium", "message_zh": "帧区间有重叠", "unit": "dataset",
         "readings": {"episodes": [0]}}]})["items"]["FILE-10"]
    assert (row["tp"], row["fp"], row["tn"]) == (1, 0, 2) and row["unit"] == "episode"


def test_the_episode_level_reads_the_final_lists(tmp_path):
    exp = expectation(episode(0, problems=["FILE-4"]), episode(1, clean=["FILE-4"]), episode(2, clean=["FILE-4"]),
                      episode(3, clean=["FILE-4"]))
    recs = [record2(e, "data_integrity", [finding("decode_failed", "FILE-4")] if e == 0 else []) for e in range(4)]
    reason = lambda m: {"module": m, "kind": "finding", "code": "x", "item": "FILE-4", "text": "x"}  # noqa: E731
    lists = {"reject": [{"episode_index": 0, "reasons": [reason("data_integrity")]},
                        {"episode_index": 1, "reasons": [reason("data_integrity")]},
                        {"episode_index": 2, "reasons": [reason("timestamp_check")]}],     # covers no FILE-4
             "held": [{"episode_index": 3, "reasons": [{"module": "duplicate_check", "kind": "execution_error", "text": "x"}]}]}
    el = score2(tmp_path, exp, recs, lists=lists)["episode_level"]
    assert (el["tp"], el["fp"], el["dropped_outside_checked"], el["held_clean"]) == (1, 1, 1, 1)


def test_intervals_are_counted_per_item(tmp_path):
    exp = expectation(episode(0, problems=["STRM-3"]), episode(1, problems=["STRM-3"]))
    recs = [record2(0, "timestamp_check", [finding("gap", "STRM-3", frames=[37, 38])]),
            record2(1, "timestamp_check", [finding("gap", "STRM-3")])]
    row = score2(tmp_path, exp, recs)["items"]["STRM-3"]
    assert row["intervals"] == {"findings": 2, "with_interval": 1, "share": 0.5}


def test_a_control_fails_when_a_finding_reports_its_guarded_item(tmp_path):
    exp = expectation(episode(0, phenomena=["IMG-10"]), episode(1, phenomena=["IMG-10"]))
    recs = [record2(0, "visual_quality", [finding("exposure_low", "IMG-2", camera="left")]), record2(1, "visual_quality")]
    row = score2(tmp_path, exp, recs)["items"]["IMG-10"]
    assert (row["control"]["pass"], row["control"]["fail"], row["control"]["failed"]) == (1, 1, ["demo:0"])

def test_camera_names_from_different_writers_match():
    assert S._matches(["robot0_sensor_camera0_compressed"], [frozenset({"robot0"})])
    assert S._matches(["wrist"], [S.scope_cameras({"stream": "observation.images.wrist"})])
    assert not S._matches(["robot1_sensor_camera0_compressed"], [frozenset({"robot0"})])
    assert S.scope_cameras({"stream": "action", "arm": "left"}) is None


def test_a_two_camera_scope_written_as_text_keeps_both_cameras():
    assert S.scope_cameras("streams=left_camera_rgb_image,right_camera_rgb_image") == frozenset({"left_camera_rgb_image", "right_camera_rgb_image"})
    assert S.scope_cameras({"streams": ["observation.images.front", "wrist"]}) == frozenset({"front", "wrist"})
    assert S.scope_cameras("stream=action arm=left") is None


def test_unsupported_and_not_run_stay_out_of_the_metrics(tmp_path):
    exp = expectation(episode(0, problems=["ACT-2"]), episode(1, problems=["ACT-2"]))
    row = score2(tmp_path, exp, [record2(0, "data_integrity")], availability={"motion_quality": "unsupported"})["items"]["ACT-2"]
    assert row["not_assessed"] == {"present": 2, "absent": 0, "of_which_unsupported": 2}
    assert row["recall"] is None and row["recall_end_to_end"] == 0.0


def test_a_run_the_preflight_refused_leaves_its_items_not_assessed(tmp_path):
    """No record, no report: the items are the registry's and not assessed because no module could run - not items
    without a check."""
    exp = expectation(episode(0, problems=["TASK-1"]))
    row = score2(tmp_path, exp, [], supported=False, availability={"motion_quality": "unsupported"})["items"]["TASK-1"]
    assert "motion_quality" in COVERS["TASK-1"] and row["mapped"] is True
    assert row["not_assessed"] == {"present": 1, "absent": 0, "of_which_unsupported": 1}


def test_lineage_weights(tmp_path):
    exp = expectation(episode(0, problems=["FILE-4"], lineage="rec:1"), episode(1, problems=["FILE-4"], lineage="rec:1"),
                      episode(2, problems=["FILE-4"]))
    recs = [decode_failed(0), record2(1, "data_integrity"), decode_failed(2)]
    row = score2(tmp_path, exp, recs, by_lineage=True)["items"]["FILE-4"]
    assert row["tp"] == 1.5 and row["fn"] == 0.5 and row["recall"] == 0.75


def test_lineage_weights_count_only_the_versions_that_carry_the_item(tmp_path):
    # the original recording is clean on FILE-4, its injected copy is not: each is the only evidence of its side
    exp = expectation(episode(0, clean=["FILE-4"], lineage="rec:1"), episode(1, problems=["FILE-4"], lineage="rec:1"))
    row = score2(tmp_path, exp, [record2(0, "data_integrity"), decode_failed(1)], by_lineage=True)["items"]["FILE-4"]
    assert (row["tp"], row["tn"]) == (1, 1)


def test_a_control_with_a_defect_of_its_own_may_be_dropped(tmp_path):
    exp = expectation(episode(0, phenomena=["IMG-10"], problems=["FILE-4"]), episode(1, phenomena=["IMG-10"]))
    recs = [record2(0, "visual_quality"), decode_failed(0), record2(1, "visual_quality")]
    reason = {"module": "data_integrity", "kind": "finding", "code": "decode_failed", "item": "FILE-4", "text": "x"}
    lists = {"reject": [{"episode_index": 0, "reasons": [reason]}, {"episode_index": 1, "reasons": [reason]}]}
    row = score2(tmp_path, exp, recs, lists=lists)["items"]["IMG-10"]
    assert (row["control"]["pass"], row["control"]["fail"], row["control"]["failed"]) == (1, 1, ["demo:1"])


def test_ingestion_follows_the_preflight(tmp_path):
    exp = expectation(episode(0, phenomena=["SET-2"]))
    doc = score2(tmp_path, exp, [], supported=False)
    assert doc["ingestion"] == {SUBSET: {"handled": False, "preflight_supported": False}}


def test_a_run_made_before_findings_is_refused(tmp_path):
    """A run of C2 1.0 records (D59) cannot be scored: the subset is run again."""
    run = tmp_path / "old"
    (run / "checks" / "data_integrity").mkdir(parents=True)
    (run / "checks" / "data_integrity" / "results.jsonl").write_text(json.dumps(
        {"episode_index": 0, "module": "data_integrity", "verdict": "pass", "passed": True, "details": {}}) + "\n")
    with pytest.raises(S.InputError, match="before findings"):
        S.score(expectation(episode(0, problems=["FILE-4"])), TAXONOMY, FMAP, {SUBSET: str(run)}, registry=REGISTRY)


def test_scoring_needs_the_registry(tmp_path):
    exp = expectation(episode(0, problems=["FILE-4"]))
    run = write_run2(str(tmp_path / "run2"), [record2(0, "data_integrity")])
    with pytest.raises(S.InputError, match="registry"):
        S.score(exp, TAXONOMY, FMAP, {SUBSET: run})


def test_the_registry_covers_every_taxonomy_item_it_names():
    items = {i["id"] for i in TAXONOMY["items"]}
    assert set(COVERS) <= items
    assert COVERS["TASK-4"] == {"task_success"} and COVERS["STRM-1"] >= {"data_integrity", "visual_quality"}


# ---------------------------------------------------------------- the map

def test_the_map_controls_are_the_taxonomy_guards():
    guards = {i["id"]: i["guards"] for i in TAXONOMY["items"] if i["kind"] == "control"}
    assert guards == {k: v["not_items"] for k, v in FMAP["controls"]["items"].items()}


def test_the_map_names_only_taxonomy_items():
    ids = {i["id"] for i in TAXONOMY["items"]}
    assert FMAP["taxonomy_version"] == TAXONOMY["taxonomy_version"]
    assert "rules" not in FMAP                                     # the records name their items (C2 2.0)
    for item, c in FMAP["controls"]["items"].items():
        assert item in ids and set(c["not_items"]) <= ids
    assert set(FMAP["ingestion"]["items"]) <= ids


# ---------------------------------------------------------------- the command and the baseline gate

def _runs_map(tmp_path, records):
    write_run2(str(tmp_path / "runs" / "demo"), records)
    (tmp_path / "runs" / "runs.json").write_text(json.dumps({"demo": {"set": "anchor", "subset": SUBSET}}), encoding="utf-8")
    return str(tmp_path / "runs" / "runs.json")


def test_command_writes_the_score_and_gates_on_the_baseline(tmp_path):
    exp = expectation(*[episode(i, problems=["FILE-4"]) for i in range(6)])
    exp_path = tmp_path / "expectation.json"
    exp_path.write_text(json.dumps(exp), encoding="utf-8")
    runs = _runs_map(tmp_path, [decode_failed(i) for i in range(3)] + [record2(i, "data_integrity") for i in range(3, 6)])
    base = ["--expectation", str(exp_path), "--taxonomy", os.path.join(HERE, "taxonomy.json"), "--runs-map", runs]
    assert S.main(base + ["--out", str(tmp_path / "s.json"), "--markdown", str(tmp_path / "s.md")]) == 0
    doc = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert doc["items"]["FILE-4"]["recall"] == 0.5
    assert "| FILE-4 |" in (tmp_path / "s.md").read_text(encoding="utf-8")
    better = json.loads(json.dumps(doc))
    better["items"]["FILE-4"]["recall"] = 0.9
    (tmp_path / "base.json").write_text(json.dumps(better), encoding="utf-8")
    assert S.main(base + ["--baseline", str(tmp_path / "base.json"), "--min-support", "5"]) == 3
    assert S.main(base + ["--baseline", str(tmp_path / "base.json"), "--min-support", "7"]) == 0   # too few episodes to judge
    assert S.main(base + ["--baseline", str(tmp_path / "s.json")]) == 0


def test_an_expectation_of_taxonomy_1_3_is_scored_with_the_new_ids(tmp_path):
    """Sample set v1 on TOS is still 1.3 (taxonomy 2.0 renumbered every dimension): ids go through ``renumbered``."""
    old = expectation(episode(0, problems=["TASK-12", "LABEL-5"], clean=["TASK-5"]), episode(1, problems=["FILE-4"]))
    old["taxonomy"] = "taxonomy.json v1.3 (design 16 §3, 68 items in 9 dimensions)"
    new, tax = S.upgrade_expectation(old, {"taxonomy_version": "1.3", "items": []}, TAXONOMY)
    assert tax is TAXONOMY and TAXONOMY["renumbered"]["from_version"] == "1.3"
    assert [p["item"] for p in new["episodes"][0]["problems"]] == ["TASK-10", "LABEL-4"]
    assert [c["item"] for c in new["episodes"][0]["clean"]] == ["TASK-4"]
    assert new["episodes"][1]["problems"] == [{"item": "FILE-4"}]          # unchanged ids stay
    assert old["episodes"][0]["problems"][0]["item"] == "TASK-12"           # the input is not modified
    same = expectation(episode(0, problems=["TASK-10"]))
    assert S.upgrade_expectation(same, TAXONOMY, TAXONOMY) == (same, TAXONOMY)
    newer = dict(same, taxonomy="taxonomy.json v9.0 (later)")
    with pytest.raises(S.InputError):
        S.upgrade_expectation(newer, TAXONOMY, TAXONOMY)
    # the command: the set's own taxonomy.json next to the expectation is 1.3, the score is in 2.0
    (tmp_path / "set").mkdir()
    (tmp_path / "set" / "taxonomy.json").write_text(json.dumps(dict(TAXONOMY, taxonomy_version="1.3")), encoding="utf-8")
    (tmp_path / "set" / "expectation.json").write_text(json.dumps(old), encoding="utf-8")
    runs = _runs_map(tmp_path, [decode_failed(1), record2(0, "data_integrity")])
    assert S.main(["--expectation", str(tmp_path / "set" / "expectation.json"), "--runs-map", runs,
                   "--out", str(tmp_path / "s.json")]) == 0
    doc = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert doc["taxonomy_version"] == "2.0" and doc["items"]["TASK-10"]["present"] == 1 and doc["items"]["FILE-4"]["tp"] == 1


def test_bad_input_exits_2(tmp_path):
    (tmp_path / "e.json").write_text(json.dumps(expectation(episode(0))), encoding="utf-8")
    assert S.main(["--expectation", str(tmp_path / "e.json"), "--taxonomy", os.path.join(HERE, "taxonomy.json")]) == 2
    assert S.main(["--expectation", str(tmp_path / "missing.json")]) == 2
