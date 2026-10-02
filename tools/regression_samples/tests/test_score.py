"""The regression sample scorer: counting, scopes, the states that stay out of the metrics, the baseline gate, and
the finding map against the platform it maps (module ids, finding codes, detail keys)."""
from __future__ import annotations

import json
import os
import re

import pytest

from regression_samples import score as S

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(HERE))
BACKEND = os.path.join(REPO, "backend")
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


def record(ep, module, verdict="pass", passed=True, details=None):
    return {"episode_index": ep, "module": module, "verdict": verdict, "passed": passed, "details": details or {}}


def write_run(root, records, verdicts=(), supported=True, availability=None):
    os.makedirs(root, exist_ok=True)
    for r in records:
        d = os.path.join(root, "checks", r["module"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "results.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(r) + "\n")
    if verdicts:
        d = os.path.join(root, "revisions", "r0001")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "verdicts.jsonl"), "w", encoding="utf-8") as fh:
            for v in verdicts:
                fh.write(json.dumps(v) + "\n")
    pre = {"format": {"supported": supported}, "modules": [{"id": m, "availability": a} for m, a in (availability or {}).items()]}
    with open(os.path.join(root, "preflight.json"), "w", encoding="utf-8") as fh:
        json.dump(pre, fh)
    return root


def decode_failed(ep, camera="front"):
    return record(ep, "data_integrity", "fail", False, {"findings": [
        {"code": "decode_failed", "level": "reject", "camera": camera, "message": f"{camera} 相机解码失败"}]})


def visual(ep, **cams):
    """cams: camera -> {sharpness, exposure, frozen_ratio}"""
    per = {f"observation.images.{c}": {"sharpness": 1.0, "exposure": 1.0, "frozen_ratio": 0.0, **v} for c, v in cams.items()}
    return record(ep, "visual_quality", "scored", None, {"per_camera_detail": per, "camera_liveness": {"dead_or_padded": []}})


def run_score(tmp_path, exp, records, **kw):
    run = write_run(str(tmp_path / "run"), records, **{k: v for k, v in kw.items() if k in ("verdicts", "supported", "availability")})
    return S.score(exp, TAXONOMY, FMAP, {SUBSET: run}, by_lineage=kw.get("by_lineage", False))


# ---------------------------------------------------------------- counting

def test_confusion_counts(tmp_path):
    exp = expectation(episode(0, problems=["FILE-4"]), episode(1, problems=["FILE-4"]),
                      episode(2, clean=["FILE-4"]), episode(3, clean=["FILE-4"]))
    recs = [decode_failed(0), record(1, "data_integrity"), decode_failed(2), record(3, "data_integrity")]
    row = run_score(tmp_path, exp, recs)["items"]["FILE-4"]
    assert (row["tp"], row["fn"], row["fp"], row["tn"]) == (1, 1, 1, 1)
    assert row["precision"] == 0.5 and row["recall"] == 0.5
    assert row["samples"] == {"fn": ["demo:1"], "fp": ["demo:2"]}


def test_a_single_timestamp_is_a_fragment(tmp_path):
    exp = expectation(episode(0, problems=["STRM-5"]), episode(1, clean=["STRM-5"]))
    recs = [record(0, "timestamp_check", "fail", False, {"reason": "只有 1 个时间戳,连时长都算不出"}),
            record(1, "timestamp_check", "fail", False, {"reason": "时间戳倒退", "ts": 1.0, "frame": 3})]
    row = run_score(tmp_path, exp, recs)["items"]["STRM-5"]
    assert (row["tp"], row["fp"], row["tn"]) == (1, 0, 1)


def test_camera_defects_levels_and_unknown(tmp_path):
    def cd(ep, glitch, per):
        detail = {"items": {"glitch": glitch, "shake": "none", "contamination": "none"},
                  "per_camera": {c: {"glitch": {"level": lv}, "shake": {"level": "none"}, "contamination": {"level": "none"}} for c, lv in per.items()}}
        return record(ep, "camera_defects", "abstain", None, json.dumps(detail))   # the detail may be a JSON string
    exp = expectation(episode(0, problems=[("IMG-5", {"stream": "observation.images.left"})]),
                      episode(1, problems=["IMG-5"]), episode(2, clean=["IMG-5"]))
    recs = [cd(0, "minor", {"left": "minor", "right": "none"}), cd(1, "unknown", {"left": "unknown"}), cd(2, "none", {"left": "none"})]
    row = run_score(tmp_path, exp, recs)["items"]["IMG-5"]
    assert (row["tp"], row["tn"]) == (1, 1)
    assert row["not_assessed"]["present"] == 1                 # the model did not answer: not a miss


def test_row_invalid_is_split_by_its_message(tmp_path):
    nan = record(0, "data_integrity", "fail", False, {"findings": [{"code": "row_invalid", "message": "数据不合规：ep000000: action 含 3 个 NaN/Inf"}]})
    order = record(1, "data_integrity", "fail", False, {"findings": [{"code": "row_invalid", "message": "数据不合规：ep000001: 时间戳非严格递增(帧 3→4)"}]})
    exp = expectation(episode(0, problems=["FILE-6"], clean=["STRM-4"]), episode(1, problems=["STRM-4"], clean=["FILE-6"]))
    items = run_score(tmp_path, exp, [nan, order])["items"]
    assert (items["FILE-6"]["tp"], items["FILE-6"]["fp"]) == (1, 0)
    assert (items["STRM-4"]["tp"], items["STRM-4"]["fp"]) == (1, 0)


def test_a_camera_scoped_expectation_needs_a_finding_on_that_camera(tmp_path):
    exp = expectation(episode(0, problems=[("IMG-4", {"stream": "observation.images.wrist"})]),
                      episode(1, problems=[("IMG-4", {"stream": "observation.images.wrist"})]),
                      episode(2, clean=[("IMG-4", "stream=wrist")]))
    recs = [visual(0, front={"sharpness": 0.1}, wrist={}), visual(1, front={}, wrist={"sharpness": 0.2}),
            visual(2, front={"sharpness": 0.1}, wrist={})]
    row = run_score(tmp_path, exp, recs)["items"]["IMG-4"]
    assert (row["tp"], row["fn"], row["fp"], row["tn"]) == (1, 1, 0, 1)   # the front camera's finding matches no expectation


def test_camera_names_from_different_writers_match():
    assert S._matches(["robot0_sensor_camera0_compressed"], [frozenset({"robot0"})])
    assert S._matches(["wrist"], [S.scope_cameras({"stream": "observation.images.wrist"})])
    assert not S._matches(["robot1_sensor_camera0_compressed"], [frozenset({"robot0"})])
    assert S.scope_cameras({"stream": "action", "arm": "left"}) is None


def test_present_and_absent_on_different_cameras_of_one_episode(tmp_path):
    exp = expectation(episode(0, problems=[("IMG-2", {"stream": "wrist"})], clean=[("IMG-2", "stream=front")]),
                      episode(1, problems=[("IMG-2", {"stream": "wrist"})], clean=[("IMG-2", "stream=front")]))
    recs = [visual(0, wrist={"exposure": 0.1}, front={}), visual(1, wrist={"exposure": 0.1}, front={"exposure": 0.2})]
    row = run_score(tmp_path, exp, recs)["items"]["IMG-2"]
    assert (row["tp"], row["fp"], row["tn"]) == (2, 1, 1)


# ---------------------------------------------------------------- states outside the metrics

def test_not_run_unsupported_and_error_stay_out_of_the_metrics(tmp_path):
    exp = expectation(episode(0, problems=["ACT-2"]), episode(1, problems=["ACT-2"]), episode(2, problems=["FILE-4"]))
    recs = [record(0, "data_integrity"), record(2, "data_integrity", "error", None)]
    row = run_score(tmp_path, exp, recs, availability={"motion_quality": "unsupported"})["items"]
    assert row["ACT-2"]["not_assessed"] == {"present": 2, "absent": 0, "of_which_unsupported": 2}
    assert row["ACT-2"]["recall"] is None
    assert row["FILE-4"]["error"] == {"present": 1, "absent": 0} and row["FILE-4"]["fn"] == 0


def test_items_without_a_rule_are_gaps(tmp_path):
    exp = expectation(episode(0, problems=["MV-2"]))
    row = run_score(tmp_path, exp, [record(0, "data_integrity")])["items"]["MV-2"]
    assert row["mapped"] is False and row["present"] == 1 and row["recall"] is None


def test_lineage_weights(tmp_path):
    exp = expectation(episode(0, problems=["FILE-4"], lineage="rec:1"), episode(1, problems=["FILE-4"], lineage="rec:1"),
                      episode(2, problems=["FILE-4"]))
    recs = [decode_failed(0), record(1, "data_integrity"), decode_failed(2)]
    row = run_score(tmp_path, exp, recs, by_lineage=True)["items"]["FILE-4"]
    assert row["tp"] == 1.5 and row["fn"] == 0.5 and row["recall"] == 0.75


def test_lineage_weights_count_only_the_versions_that_carry_the_item(tmp_path):
    # the original recording is clean on FILE-4, its injected copy is not: each is the only evidence of its side
    exp = expectation(episode(0, clean=["FILE-4"], lineage="rec:1"), episode(1, problems=["FILE-4"], lineage="rec:1"))
    row = run_score(tmp_path, exp, [record(0, "data_integrity"), decode_failed(1)], by_lineage=True)["items"]["FILE-4"]
    assert (row["tp"], row["tn"]) == (1, 1)


# ---------------------------------------------------------------- episode level, controls, ingestion

def test_episode_level_uses_the_funnel_verdict(tmp_path):
    exp = expectation(episode(0, problems=["FILE-4"]), episode(1, clean=["FILE-4"]), episode(2, clean=["FILE-4"]))
    recs = [decode_failed(0), record(1, "data_integrity"), record(2, "data_integrity")]
    verdicts = [{"episode_index": 0, "verdict": "drop", "hard_fails": ["data_integrity"]},
                {"episode_index": 1, "verdict": "drop", "hard_fails": ["data_integrity"]}, {"episode_index": 2, "verdict": "held"}]
    el = run_score(tmp_path, exp, recs, verdicts=verdicts)["episode_level"]
    assert (el["tp"], el["fp"], el["held_clean"]) == (1, 1, 1) and el["precision"] == 0.5


def test_a_drop_for_an_item_nobody_checked_is_not_a_false_alarm(tmp_path):
    exp = expectation(episode(0, clean=["FILE-4"]), episode(1, clean=["FILE-4", "ACT-4"]), episode(2, clean=["IMG-2"]))
    recs = [record(e, "data_integrity") for e in range(3)] + [visual(2, front={"exposure": 0.9})]
    verdicts = [{"episode_index": 0, "verdict": "drop", "hard_fails": ["kinematic_limits"]},     # nobody checked ACT-4 there
                {"episode_index": 1, "verdict": "drop", "hard_fails": ["kinematic_limits"]},     # ACT-4 was checked clean
                {"episode_index": 2, "verdict": "drop", "hard_fails": [], "soft_score": 0.4}]     # soft score: the scored modules
    el = run_score(tmp_path, exp, recs, verdicts=verdicts)["episode_level"]
    assert (el["fp"], el["dropped_outside_checked"]) == (2, 1)
    assert el["samples"]["fp"] == ["demo:1", "demo:2"]


def test_a_dataset_level_item_counts_once_per_subset(tmp_path):
    exp = expectation(*[episode(i, problems=["SET-1"]) for i in range(3)], episode(3, problems=[]),
                      *[episode(i, problems=[{"item": "LABEL-3", "unit": "subset"}]) for i in (4, 5)])
    dup = record(1, "data_integrity", "fail", False, {"findings": [{"code": "duplicate_content", "message": "与另一条重复"}]})
    recs = [record(0, "data_integrity"), dup, record(2, "data_integrity")]
    items = run_score(tmp_path, exp, recs)["items"]
    assert (items["SET-1"]["present"], items["SET-1"]["tp"], items["SET-1"]["fn"]) == (1, 1, 0)
    assert items["SET-1"]["unit"] == "subset"
    assert items["LABEL-3"]["present"] == 1 and items["LABEL-3"]["mapped"] is False


def test_end_to_end_recall_keeps_what_was_not_assessed(tmp_path):
    exp = expectation(episode(0, problems=["FILE-4"]), episode(1, problems=["FILE-4"]), episode(2, problems=["FILE-4"]))
    recs = [decode_failed(0), record(1, "data_integrity", "error", None)]          # 2 is never run
    row = run_score(tmp_path, exp, recs)["items"]["FILE-4"]
    assert row["recall"] == 1.0 and row["recall_end_to_end"] == round(1 / 3, 4) and row["assessed_share"] == round(1 / 3, 4)


def test_a_two_camera_scope_written_as_text_keeps_both_cameras():
    assert S.scope_cameras("streams=left_camera_rgb_image,right_camera_rgb_image") == frozenset({"left_camera_rgb_image", "right_camera_rgb_image"})
    assert S.scope_cameras({"streams": ["observation.images.front", "wrist"]}) == frozenset({"front", "wrist"})
    assert S.scope_cameras("stream=action arm=left") is None


def test_a_control_fails_when_its_defect_is_reported(tmp_path):
    exp = expectation(episode(0, phenomena=["IMG-10"]), episode(1, phenomena=["IMG-10"]))
    recs = [visual(0, left={"exposure": 0.1}), visual(1, left={})]
    row = run_score(tmp_path, exp, recs)["items"]["IMG-10"]
    assert row["control"]["pass"] == 1 and row["control"]["fail"] == 1 and row["control"]["failed"] == ["demo:0"]


def test_a_control_with_a_defect_of_its_own_may_be_dropped(tmp_path):
    exp = expectation(episode(0, phenomena=["IMG-10"], problems=["FILE-4"]), episode(1, phenomena=["IMG-10"]))
    recs = [visual(0, left={}), decode_failed(0), visual(1, left={})]
    verdicts = [{"episode_index": 0, "verdict": "drop", "hard_fails": ["data_integrity"]}, {"episode_index": 1, "verdict": "drop"}]
    row = run_score(tmp_path, exp, recs, verdicts=verdicts)["items"]["IMG-10"]
    assert (row["control"]["pass"], row["control"]["fail"], row["control"]["failed"]) == (1, 1, ["demo:1"])


def test_the_map_controls_are_the_taxonomy_guards():
    guards = {i["id"]: i["guards"] for i in TAXONOMY["items"] if i["kind"] == "control"}
    assert guards == {k: v["not_items"] for k, v in FMAP["controls"]["items"].items()}


def test_ingestion_follows_the_preflight(tmp_path):
    exp = expectation(episode(0, phenomena=["SET-4"]))
    doc = run_score(tmp_path, exp, [], supported=False)
    assert doc["ingestion"] == {SUBSET: {"handled": False, "preflight_supported": False}}


# ---------------------------------------------------------------- the command and the baseline gate

def test_command_writes_the_score_and_gates_on_the_baseline(tmp_path):
    exp = expectation(*[episode(i, problems=["FILE-4"]) for i in range(6)])
    exp_path = tmp_path / "expectation.json"
    exp_path.write_text(json.dumps(exp), encoding="utf-8")
    run = write_run(str(tmp_path / "runs" / "demo"), [decode_failed(i) for i in range(3)] + [record(i, "data_integrity") for i in range(3, 6)])
    (tmp_path / "runs" / "runs.json").write_text(json.dumps({"demo": {"set": "anchor", "subset": SUBSET}}), encoding="utf-8")
    base = ["--expectation", str(exp_path), "--taxonomy", os.path.join(HERE, "taxonomy.json"), "--runs-map", str(tmp_path / "runs" / "runs.json")]
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
    assert run


def test_bad_input_exits_2(tmp_path):
    (tmp_path / "e.json").write_text(json.dumps(expectation(episode(0))), encoding="utf-8")
    assert S.main(["--expectation", str(tmp_path / "e.json"), "--taxonomy", os.path.join(HERE, "taxonomy.json")]) == 2
    assert S.main(["--expectation", str(tmp_path / "missing.json")]) == 2


# ---------------------------------------------------------------- the map against the platform

def test_map_names_only_taxonomy_items_and_registered_modules():
    ids = {i["id"] for i in TAXONOMY["items"]}
    assert FMAP["taxonomy_version"] == TAXONOMY["taxonomy_version"]
    modules = {m["id"] for m in json.load(open(os.path.join(REPO, "docs", "contracts", "modules.json"), encoding="utf-8"))["modules"]}
    for r in FMAP["rules"]:
        assert set(r["items"]) <= ids, r["id"]
        assert r["module"] in modules, r["id"]
    for item, c in FMAP["controls"]["items"].items():
        assert item in ids and set(c["not_items"]) <= ids
    assert len({r["id"] for r in FMAP["rules"]}) == len(FMAP["rules"])


def _source(*parts):
    return open(os.path.join(BACKEND, *parts), encoding="utf-8").read()


def test_map_uses_codes_the_platform_writes():
    """A renamed code or detail key on the platform must fail here, not silently zero a metric."""
    integrity = _source("curation", "extensions", "integrity", "findings.py")
    codes = set(re.findall(r'^\s+"([a-z_]+)": \((?:REJECT|SUSPECT|DATASET)', integrity, re.M))
    kinematics = _source("curation", "core", "checks", "kinematics.py")
    motion = _source("curation", "core", "checks", "motion_quality.py")
    visual = _source("curation", "core", "checks", "visual_quality.py") + _source("curation", "pipeline", "funnel.py")
    sync = _source("curation", "core", "checks", "video_action_sync.py")
    task = _source("curation", "core", "checks", "task_success.py") + _source("curation", "pipeline", "run.py")
    eef = _source("curation", "extensions", "eef_consistency", "decide.py")
    for r in FMAP["rules"]:
        m = r["match"]
        for c in m.get("finding_codes", []):
            assert c in codes, (r["id"], c)
        for t in m.get("violation_types", []):
            assert f'"{t}"' in kinematics, (r["id"], t)
        for key in [m.get("subscore"), m.get("detail_nonempty")]:
            if key:
                assert f'"{key}"' in motion, (r["id"], key)
        if m.get("camera_detail"):
            assert m["camera_detail"] in visual, r["id"]
        if m.get("camera_list"):
            assert m["camera_list"].split(".")[-1] in visual, r["id"]
        for v in m.get("detail_verdicts", []):
            assert v in (sync if r["module"] == "video_action_sync" else task), (r["id"], v)
        for c in m.get("camera_codes", []):
            assert f'"{c}"' in sync, (r["id"], c)
        for o in m.get("detail_outcomes", []):
            assert o.upper() in eef or o in eef, (r["id"], o)
    vlm = _source("curation", "adapters", "video_vlm.py")
    items_line = re.search(r"CAMERA_CHECK_ITEMS = \((.*)\)", vlm).group(1)
    levels_line = re.search(r"CAMERA_CHECK_LEVELS = \((.*)\)", vlm).group(1)
    for r in FMAP["rules"]:
        cl = r["match"].get("camera_levels")
        if cl:
            assert f'"{cl["key"]}"' in items_line, r["id"]
            assert all(f'"{lv}"' in levels_line for lv in cl["levels"]), r["id"]
    for kind in S.TIMESTAMP_FAIL_KINDS:
        assert kind in _source("curation", "pipeline", "report_stats.py")
    # the message patterns: every alternative is text the platform writes
    texts = (_source("curation", "ingest", "validate.py") + integrity + _source("curation", "core", "checks", "video_action_sync.py"))
    for r in FMAP["rules"]:
        for key in ("message_regex", "reason_regex"):
            for alt in (r["match"].get(key) or "").split("|"):
                if alt:
                    assert alt in texts, (r["id"], alt)


@pytest.mark.parametrize("item", ["FILE-4", "STRM-1", "IMG-4", "ACT-2", "AV-1", "TASK-5", "SET-1"])
def test_core_items_are_mapped(item):
    assert any(item in r["items"] for r in FMAP["rules"])


# ---------------------------------------------------------------- runs of findings (C2 2.0, design doc 17 §6.1)

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


def write_run2(root, records, lists=None, dataset_findings=None, availability=None):
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
    pre = {"format": {"supported": True}, "modules": [{"id": m, "availability": a} for m, a in (availability or {}).items()]}
    with open(os.path.join(root, "preflight.json"), "w", encoding="utf-8") as fh:
        json.dump(pre, fh)
    return root


def score2(tmp_path, exp, records, **kw):
    run = write_run2(str(tmp_path / "run2"), records, **kw)
    return S.score(exp, TAXONOMY, FMAP, {SUBSET: run}, registry=REGISTRY)


def test_a_run_of_findings_is_scored_from_the_findings_themselves(tmp_path):
    exp = expectation(episode(0, problems=["FILE-4"]), episode(1, problems=["FILE-4"]),
                      episode(2, clean=["FILE-4"]), episode(3, clean=["FILE-4"]))
    recs = [record2(0, "data_integrity", [finding("decode_failed", "FILE-4")]), record2(1, "data_integrity"),
            record2(2, "data_integrity", [finding("decode_failed", "FILE-4")]), record2(3, "data_integrity")]
    doc = score2(tmp_path, exp, recs)
    row = doc["items"]["FILE-4"]
    assert (row["tp"], row["fn"], row["fp"], row["tn"]) == (1, 1, 1, 1)
    assert row["modules"] == ["data_integrity"] and row["mapped"] is True
    assert doc["formats"] == {SUBSET: "2.0"} and doc["schema_version"] == "2.0"
    assert row["by_module"]["data_integrity"]["precision"] == 0.5


def test_not_assessed_comes_from_the_records_and_errors_from_their_status(tmp_path):
    """An item a module could not assess is not assessed; a module that failed on the episode is an error."""
    exp = expectation(episode(0, problems=["TASK-5"]), episode(1, problems=["TASK-5"]), episode(2, problems=["TASK-5"]))
    recs = [record2(0, "task_success", [finding("failure", "TASK-5")]),
            record2(1, "task_success", unassessable=["TASK-5"]),
            record2(2, "task_success", status="error")]
    row = score2(tmp_path, exp, recs)["items"]["TASK-5"]
    assert (row["tp"], row["fn"]) == (1, 0)
    assert row["not_assessed"]["present"] == 1 and row["error"]["present"] == 1
    assert row["recall"] == 1.0 and row["recall_end_to_end"] == round(1 / 3, 4)


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
    exp = expectation(*[episode(i, problems=["SET-3"]) for i in range(3)])
    recs = [record2(i, "timestamp_check") for i in range(3)]
    row = score2(tmp_path, exp, recs, dataset_findings={"timestamp_check": [
        {"code": "duration_outlier", "item": "SET-3", "severity": "low", "message_zh": "时长离群", "unit": "dataset"}]})["items"]["SET-3"]
    assert (row["present"], row["tp"], row["fn"]) == (1, 1, 0) and row["unit"] == "subset"


def test_the_episode_level_reads_the_final_lists(tmp_path):
    exp = expectation(episode(0, problems=["FILE-4"]), episode(1, clean=["FILE-4"]), episode(2, clean=["FILE-4"]),
                      episode(3, clean=["FILE-4"]))
    recs = [record2(e, "data_integrity", [finding("decode_failed", "FILE-4")] if e == 0 else []) for e in range(4)]
    reason = lambda m: {"module": m, "kind": "finding", "code": "x", "item": "FILE-4", "text": "x"}  # noqa: E731
    lists = {"reject": [{"episode_index": 0, "reasons": [reason("data_integrity")]},
                        {"episode_index": 1, "reasons": [reason("data_integrity")]},
                        {"episode_index": 2, "reasons": [reason("task_success")]}],
             "held": [{"episode_index": 3, "reasons": [{"module": "dedup", "kind": "execution_error", "text": "x"}]}]}
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


def test_runs_of_both_formats_are_scored_together(tmp_path):
    """The baseline of 1b30fb224 is 1.0, the runs after it 2.0: one score reads both, each its own way."""
    exp = {"set": "anchor", "set_version": "v1", "episodes": [
        {**episode(0, problems=["FILE-4"]), "dataset": "lerobot_v21/old", "episode_id": "old:0"},
        {**episode(0, problems=["FILE-4"]), "dataset": "lerobot_v21/new", "episode_id": "new:0"}]}
    old = write_run(str(tmp_path / "old"), [decode_failed(0)])
    new = write_run2(str(tmp_path / "new"), [record2(0, "data_integrity", [finding("decode_failed", "FILE-4")])])
    doc = S.score(exp, TAXONOMY, FMAP, {"lerobot_v21/old": old, "lerobot_v21/new": new}, registry=REGISTRY)
    assert doc["formats"] == {"lerobot_v21/new": "2.0", "lerobot_v21/old": "1.0"}
    assert doc["items"]["FILE-4"]["tp"] == 2


def test_a_run_of_findings_needs_the_registry(tmp_path):
    exp = expectation(episode(0, problems=["FILE-4"]))
    run = write_run2(str(tmp_path / "run2"), [record2(0, "data_integrity")])
    with pytest.raises(S.InputError, match="registry"):
        S.score(exp, TAXONOMY, FMAP, {SUBSET: run})


def test_the_registry_covers_every_taxonomy_item_it_names():
    items = {i["id"] for i in TAXONOMY["items"]}
    assert set(COVERS) <= items
    assert COVERS["TASK-5"] == {"task_success"} and COVERS["STRM-1"] >= {"data_integrity", "visual_quality"}
