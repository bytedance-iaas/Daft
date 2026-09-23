"""preflight -> ... -> verify on the 8-episode fixture with the fake model (W3 acceptance).

One run directory is built once for the module, command by command in the
Daemon's order, against a local fake VLM endpoint. Every command's ``--json``
is checked against its C2 schema and every stderr line against C3 as it runs
(``pipeline.run``); the tests below check the files the commands leave behind.
"""
from __future__ import annotations

import json
import os

import pytest

from curation.contracts import schemas

from .conftest import ENV_VARS
from .fakevlm_server import FakeVlmServer
from .pipeline import Chain, read_jsonl, results

MODULES = ["timestamp_check", "kinematic_limits", "motion_quality", "visual_quality",
           "video_action_sync", "task_success", "dedup", "skill_profile"]


@pytest.fixture(scope="module")
def chain(tmp_path_factory, mini_dataset):
    tmp = tmp_path_factory.mktemp("chain")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV_VARS:
            mp.delenv(name, raising=False)
        with FakeVlmServer(delay_s=0.003) as vlm:
            c = Chain(mini_dataset, str(tmp / "run"), vlm.url)
            c.front()
            c.funnel()
            c.post()
            c.deliver(str(tmp / "delivery"))
            c.vlm_calls = list(vlm.calls)
            c.max_in_flight = vlm.max_in_flight
            c.delivery = str(tmp / "delivery")
            yield c


def test_every_step_ran_and_fits_its_contract(chain):
    assert list(chain.steps) == ["preflight", "plan", "snapshot", "autolabel", "numeric",
                                 "frame", "vlm", "funnel", "dedup", "profile_vlm", "final",
                                 "report", "export", "verify"]
    assert all(s.rc == 0 for s in chain.steps.values())


def test_by_default_one_model_request_at_a_time(chain):
    """No --concurrency anywhere in the chain: never two model requests in flight."""
    assert len(chain.vlm_calls) > 100 and chain.max_in_flight == 1


def test_the_funnel_takes_the_survivors_of_each_stage(chain):
    s = chain.steps
    assert s["numeric"].doc["modules"]["timestamp_check"]["episodes"]["total"] == 8
    assert s["numeric"].doc["modules"]["timestamp_check"]["episodes"]["fail"] == 2
    assert s["frame"].doc["modules"]["video_action_sync"]["episodes"]["total"] == 6
    ts = s["vlm"].doc["modules"]["task_success"]["episodes"]
    assert ts == {"total": 6, "pass": 3, "fail": 0, "abstain": 3, "scored": 0, "error": 0}
    assert s["autolabel"].doc["counts"] == {"total": 2, "ok": 2, "unclear": 0, "error": 0}
    assert s["dedup"].doc["modules"]["dedup"]["episodes"]["fail"] == 1       # 7 copies 3
    assert s["profile_vlm"].doc["modules"]["skill_profile"]["episodes"]["total"] == 5


def test_files_fit_their_contracts(chain):
    rd = chain.rd
    for m in MODULES:
        rows = read_jsonl(os.path.join(rd, "checks", m, "results.jsonl"))
        assert rows, m
        for r in rows:
            assert schemas.errors("cli/result-record.schema.json", r) == [], (m, r)
    for line in read_jsonl(os.path.join(rd, "autolabel", "captions.jsonl")):
        assert schemas.errors("cli/autolabel-line.schema.json", line) == []
    rev = os.path.join(rd, "revisions", "r0001")
    for line in read_jsonl(os.path.join(rev, "verdicts.jsonl")):
        assert schemas.errors("cli/verdict-line.schema.json", line) == []
    for name in ("passed", "reject", "held", "review"):
        doc = json.load(open(os.path.join(rev, f"{name}.json"), encoding="utf-8"))
        assert schemas.errors("cli/final-list.schema.json", doc) == [], name
    for name, ref in (("commit.json", "cli/commit.schema.json"),
                      ("report.json", "cli/report.schema.json")):
        doc = json.load(open(os.path.join(rev, name), encoding="utf-8"))
        assert schemas.errors(ref, doc) == [], name
    man = json.load(open(os.path.join(rd, "export", "manifest.json"), encoding="utf-8"))
    assert schemas.errors("cli/export-manifest.schema.json", man) == []
    plan = json.load(open(os.path.join(rd, "plan.json"), encoding="utf-8"))
    assert schemas.errors("cli/plan.schema.json", plan) == []


def test_final_lists_are_v1s_verdicts(chain):
    rev = os.path.join(chain.rd, "revisions", "r0001")

    def eps(name):
        doc = json.load(open(os.path.join(rev, f"{name}.json"), encoding="utf-8"))
        return [e["episode_index"] for e in doc["episodes"]]

    assert eps("passed") == [0, 1, 3, 4, 6]
    assert eps("reject") == [2, 5, 7]
    assert eps("held") == []
    assert eps("review") == [0, 3, 7]                 # 0 and 3 abstained, 7 is a copy
    reject = json.load(open(os.path.join(rev, "reject.json"), encoding="utf-8"))
    dup = [e for e in reject["episodes"] if e["episode_index"] == 7][0]
    assert dup["reasons"][0]["kind"] == "duplicate" and dup["reasons"][0]["duplicate_of"] == 3
    passed = json.load(open(os.path.join(rev, "passed.json"), encoding="utf-8"))
    sources = {e["episode_index"]: e["task_text"]["source"] for e in passed["episodes"]}
    assert sources == {0: "原始标注", 1: "原始标注", 3: "原始标注", 4: "自产caption",
                       6: "自产caption"}


def test_export_delivers_passed_and_verify_writes_complete(chain):
    exp = chain.steps["export"].doc
    assert exp["episodes"] == 5 and exp["incremental"] is False
    assert chain.steps["verify"].doc["failed"] == []
    assert chain.steps["verify"].doc["complete_marker"] is True
    assert os.path.isfile(os.path.join(chain.delivery, "_COMPLETE"))
    assert os.path.isfile(os.path.join(chain.delivery, "export", "manifest.json"))
    man = json.load(open(os.path.join(chain.rd, "export", "manifest.json"), encoding="utf-8"))
    assert [e["episode_index"] for e in man["episodes"]] == [0, 1, 3, 4, 6]
    assert {e["episode_index"]: e["task"]["source"] for e in man["episodes"]}[4] == "自产caption"


def test_usage_is_booked_per_module_on_both_ledgers(chain):
    lines = []
    for name in ("autolabel", "vlm", "profile_vlm"):
        lines += [e for e in chain.steps[name].events if e["kind"] == "usage"]
    assert {e["module"] for e in lines} == {"autolabel", "task_success", "skill_profile"}
    assert {e["ledger"] for e in lines} == {"actual", "attributed"}
    actual = [e for e in lines if e["ledger"] == "actual"]
    posts = [c for c in chain.vlm_calls if c["path"].endswith("/chat/completions")]
    assert sum(e["requests"] for e in actual) == len(posts)          # every request, once
    assert sum(e["requests_unknown_usage"] for e in actual) == 0
    assert {e["call_kind"] for e in actual} >= {"probe", "endstate", "caption", "llm"}
    report = json.load(open(os.path.join(chain.rd, "revisions", "r0001", "report.json"),
                            encoding="utf-8"))
    tu = report["overview"]["token_usage"]
    assert tu["requests"] == len(posts)
    assert tu["prompt"] == sum(e["prompt_tokens"] for e in actual)
    persisted = read_jsonl(os.path.join(chain.rd, "usage.jsonl"))
    assert len(persisted) == len(lines)


def test_report_follows_the_registry(chain):
    rev = os.path.join(chain.rd, "revisions", "r0001")
    report = json.load(open(os.path.join(rev, "report.json"), encoding="utf-8"))
    assert [m["id"] for m in report["modules"]] == MODULES
    assert all(m["state"] == "succeeded" for m in report["modules"])
    by_id = {m["id"]: m for m in report["modules"]}
    # 7 is a copy (D42): no task question; dedup's appeal candidate never counts as pending
    assert by_id["task_success"]["adjudication"] == {"pending": 2, "appealable": 0}
    assert by_id["dedup"]["adjudication"] == {"pending": 0, "appealable": 1}
    assert by_id["timestamp_check"]["adjudication"] is None
    assert report["overview"]["counts"] == {"total": 8, "passed": 5, "rejected": 3,
                                            "held": 0, "review": 3, "skipped": 0}
    assert report["integrity"]["skipped_episodes"] == []
    for m in report["modules"]:
        for t in m["tables"]:
            assert os.path.isfile(os.path.join(rev, t["file"]))
    commit = json.load(open(os.path.join(rev, "commit.json"), encoding="utf-8"))
    assert commit["parts"]["task_success"] == ["0001"]
    assert "report.json" in commit["files"] and "passed.json" in commit["files"]
    md = open(os.path.join(rev, "report.md"), encoding="utf-8").read()
    assert "通过 5" in md and "「时间戳检查」2 条" in md


def test_report_summaries_are_chart_ready(chain):
    """06 §6.2 (F6.2): statistics from the real details, the 1.0 keys kept, no episode lists."""
    rev = os.path.join(chain.rd, "revisions", "r0001")
    report = json.load(open(os.path.join(rev, "report.json"), encoding="utf-8"))
    s = {m["id"]: m["summary"] for m in report["modules"]}
    for m, summary in s.items():
        assert "counts" in summary, m
        assert "ep0000" not in json.dumps(summary), f"{m} lists episodes"
    ts = s["timestamp_check"]
    assert ts["fail_kinds"] == {"gap": 1, "fragment": 1}                       # kept as in 1.0
    assert {x["name"]: x["count"] for x in ts["fail_reasons"]} == {
        "out_of_order": 0, "gap": 1, "fragment": 1, "jitter": 0}
    assert sum(b["count"] for b in ts["duration_hist"]) == 8 and ts["duration_total_s"] > 0
    assert s["kinematic_limits"]["violation_episodes"] == 0
    assert s["kinematic_limits"]["limits_profile"] == "franka"
    mq = s["motion_quality"]
    assert sum(b["count"] for b in mq["score_hist"]) == 8 and "mean_score" in mq
    subs = {x["name"]: x for x in mq["subscores"]}
    assert subs["smoothness"]["n"] == 8 and subs["smoothness"]["in_total"] is True
    assert subs["actuator_saturation"]["mean"] is None and subs["actuator_saturation"]["na_reason"]
    vq = s["visual_quality"]
    assert [c["camera"] for c in vq["cameras"]] == ["exterior", "wrist"]
    assert all(c["n"] == 6 and sum(c["hist"]) == 6 for c in vq["cameras"])
    sync = s["video_action_sync"]
    assert sum(v["count"] for v in sync["verdicts"]) == 6 and sync["lag_tol_s"] == 0.25
    assert [c["camera"] for c in sync["cameras"]] == ["exterior", "wrist"] and sync["sync_advice"]
    task = s["task_success"]
    assert sum(j["count"] for j in task["judgements"]) == task["counts"]["total"] - task["counts"]["error"]
    assert [x["name"] for x in task["layers"]] == ["probe", "endstate", "label_guard", "arbitration"]
    assert "arbitration" in task and "abstain_reasons" in task                # kept as in 1.0
    assert s["dedup"]["group_sizes"] == [{"name": "2", "count": 1}]
    assert (s["dedup"]["collision_groups"], s["dedup"]["removed"]) == (1, 1)
    sp = s["skill_profile"]
    assert sum(f["count"] for f in sp["family_distribution"]) == sp["counts"]["total"]
    assert sp["families"] == len([f for f in sp["family_tree"] if f["name"] != "未归类"])


def test_a_committed_revision_is_never_rewritten(chain):
    from .pipeline import run

    res = run("aggregate", "--run-dir", chain.rd, "--phase", "final", "--revision", "1",
              "--episodes", "0-7")
    assert res.rc == 2 and "committed" in res.doc["error"]["message"]
    res = run("report", "--run-dir", chain.rd, "--revision", "1")
    assert res.rc == 2
