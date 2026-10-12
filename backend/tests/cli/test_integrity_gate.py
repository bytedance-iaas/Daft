"""What data integrity rejects no other module judges; a corrupt video is a blocking finding (design doc 28, D92-D94).

The v2 fixture with one kind of damage per episode (``integrity_samples.damage_lerobot_v2``: 0-4 and 6 rejected by
data integrity), plus a stream corrupted in the middle of episode 7's exterior video - the structure intact, so data
integrity's default L1 tier passes it, as it passed droid200's 14, 15 and 27. The commands run in the Daemon's
order, data integrity first.
"""
from __future__ import annotations

import json
import os

import pytest

from curation.contracts import schemas

from . import integrity_samples as S
from .fakevlm_server import FakeVlmServer
from .pipeline import Chain, read_jsonl, results, verdict_of

INTEGRITY = "data_integrity"
REJECTED = {0, 1, 2, 3, 4, 6}
OTHERS = ("timestamp_check", "kinematic_limits", "motion_quality", "visual_quality", "video_action_sync",
          "duplicate_check", "task_success", "camera_defects")
DECODERS = ("visual_quality", "video_action_sync", "task_success")


class GatedChain(Chain):
    """The main run with data integrity in the plan and run first, as the Daemon's CPU block does."""

    def front(self, episodes: str = "0-7") -> None:
        pf = self.step("preflight", "preflight", "--input", self.ds, "--vlm-backend", "ark")
        with open(self.path("preflight.json"), "w", encoding="utf-8") as fh:
            json.dump(pf.doc, fh)
        self.step("plan", "plan", "--preflight", self.path("preflight.json"), "--modules",
                  "data_integrity,timestamp_check,kinematic_limits,motion_quality,visual_quality,"
                  "video_action_sync,task_success,duplicate_check",
                  "--episodes", episodes, "--out", self.path("plan.json"))
        self.step("snapshot", "snapshot", "--input", self.ds, "--episodes", episodes,
                  "--out", self.path("source_manifest.json"))
        self.step("integrity", "check", "--modules", INTEGRITY, *self.common(), "--episodes", episodes)


@pytest.fixture(scope="module")
def gated(tmp_path_factory, mini_dataset) -> GatedChain:
    import shutil

    tmp = tmp_path_factory.mktemp("gate")
    ds = str(tmp / "mini")
    shutil.copytree(mini_dataset, ds)
    S.damage_lerobot_v2(ds)
    video = S.video(ds, "exterior", 7)
    for k in range(10, 40):                     # frames in the middle: the head and the tail stay readable
        S.garble_sample(video, k)
    with pytest.MonkeyPatch.context() as mp:
        from .conftest import ENV_VARS

        for name in ENV_VARS:
            mp.delenv(name, raising=False)
        with FakeVlmServer() as vlm:
            c = GatedChain(ds, str(tmp / "run"), vlm.url)
            c.front()
            c.checks()
            c.post()
    return c


def test_data_integrity_rejects_what_it_did_and_passes_the_corrupt_stream(gated):
    recs = results(gated.rd, INTEGRITY)
    assert {e for e, r in recs.items() if verdict_of(r) == "fail"} == REJECTED
    assert verdict_of(recs[7]) == "pass"            # L1 reads the file's head and tail only


def test_no_other_module_judges_an_episode_data_integrity_rejected(gated):
    """D92: a skip record per module - status ok, no finding, every covered item unassessable, the reason given."""
    for m in OTHERS:
        recs = results(gated.rd, m)
        assert sorted(recs) == list(range(8)), m
        for e in REJECTED:
            r = recs[e]
            assert schemas.errors("cli/result-record.schema.json", r) == []
            assert (r["status"], r["findings"], r["assessed"], r["error"]) == ("ok", [], [], None), (m, e, r)
            assert r["details"]["skipped"] == "integrity_rejected"
            assert r["details"]["reason"].startswith("数据完整性已判废：")
            assert {u["reason"] for u in r["unassessable"]} == {"integrity_rejected"}
            assert verdict_of(r) == "skipped"
        for e in set(range(8)) - REJECTED:
            assert recs[e]["details"].get("skipped") in (None, "no_task_text"), (m, e)


def test_the_check_output_counts_the_skips_apart(gated):
    """D94: ``check --json`` names why it skipped; skipped episodes are ok, not errors."""
    for step, modules in (("numeric", Chain.NUMERIC), ("frame", Chain.FRAME), ("duplicate", "duplicate_check"),
                          ("vlm", "task_success")):
        doc = gated.steps[step].doc["modules"]
        for m in modules.split(","):
            assert doc[m]["skipped_reasons"]["integrity_rejected"] == len(REJECTED), (step, m, doc[m])
            assert doc[m]["episodes"]["error"] == 0 and doc[m]["error_episodes"] == [], (step, m)


def test_a_corrupt_stream_is_a_blocking_finding_in_every_module_that_decodes_it(gated):
    """D93: decode_failed (FILE-4) on the exterior camera, not an execution error; camera defects cannot be read."""
    for m in DECODERS:
        r = results(gated.rd, m)[7]
        assert r["status"] == "ok" and r["error"] is None, (m, r)
        failed = [f for f in r["findings"] if f["code"] == "decode_failed"]
        assert [f["scope"] for f in failed] == [{"camera": "exterior"}], (m, r["findings"])
        assert any("InvalidDataError" in str(i.get("cause")) for i in r["details"]["undecodable"]), m
        assert verdict_of(r) == "fail"
    cam = results(gated.rd, "camera_defects")[7]
    assert cam["status"] == "ok" and {u["reason"] for u in cam["unassessable"]} == {"video_undecodable"}


def test_the_verdicts_hold_nobody_back_and_name_only_the_rejecting_findings(gated):
    lines = {ln["episode_index"]: ln for ln in read_jsonl(os.path.join(gated.rd, "revisions", "r0001",
                                                                       "verdicts.jsonl"))}
    assert not [e for e, ln in lines.items() if ln["verdict"] == "held"], lines
    for e in REJECTED:
        ln = lines[e]
        assert ln["verdict"] == "drop" and not ln.get("error_modules"), ln
        assert "执行出错" not in ln["reason"], ln
    assert lines[7]["verdict"] == "drop"
    assert {(b["module"], b["code"]) for b in lines[7]["blocking"]} >= {(m, "decode_failed") for m in DECODERS}


def test_the_report_counts_the_skips_apart(gated):
    """D94: a section's assessed episodes leave the skipped out; it says how many it skipped and why."""
    with open(os.path.join(gated.rd, "revisions", "r0001", "report.json"), encoding="utf-8") as fh:
        report = json.load(fh)
    s = {m["id"]: m["summary"] for m in report["modules"]}
    for m in ("timestamp_check", "visual_quality", "duplicate_check"):
        assert s[m]["skipped_episodes"] == len(REJECTED), m
        assert s[m]["skipped_reasons"] == [{"name": "integrity_rejected", "count": len(REJECTED)}], m
        assert s[m]["assessed_episodes"] == 8 - len(REJECTED), m
        assert s[m]["counts"]["skipped"] == len(REJECTED), m
    assert "skipped_episodes" not in s[INTEGRITY] or s[INTEGRITY]["skipped_episodes"] == 0
    md = open(os.path.join(gated.rd, "revisions", "r0001", "report.md"), encoding="utf-8").read()
    assert "评估 2 条;跳过 6 条(数据完整性已判废)" in md


@pytest.mark.parametrize("make", ["v2", "v3"])
def test_task_texts_without_the_semantics_vote_are_the_readers_own(make, tmp_path, mini_dataset):
    """design doc 28 §1: when a broken file among the first episodes fails the reader's semantics vote, the task
    texts come from ``meta/`` alone - the same ones the reader gives on a readable dataset."""
    from curation.ingest import lerobot_reader as lr
    from curation.pipeline import rows

    if make == "v2":
        ds = mini_dataset
    else:
        from .v3_fixture import make_mini_lerobot_v3

        ds = make_mini_lerobot_v3(str(tmp_path / "v3"))
    full = lr.read_lerobot_meta(ds, skip_missing=True)
    keys = ("episode_id", "episode_index", "instruction")
    assert rows._task_texts(ds, None, None) == [{k: r[k] for k in keys} for r in full]
    assert [r["episode_index"] for r in rows._task_texts(ds, None, {1, 3})] == [1, 3]


def test_under_report_only_the_broken_files_still_reject(gated, tmp_path):
    """droid200-test ran report_only: data integrity's rejects and the corrupt stream reject all the same (policy 3,
    D93); nothing is held."""
    import shutil

    from curation.pipeline import policy as policy_mod

    from .pipeline import run

    rd = str(tmp_path / "run")
    shutil.copytree(gated.rd, rd)
    path = os.path.join(rd, "run.json")
    doc = json.load(open(path, encoding="utf-8")) if os.path.isfile(path) else {}
    doc["policy"] = policy_mod.Policy.of("report_only").to_json()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    res = run("aggregate", "--run-dir", rd, "--revision", "2", "--episodes", "0-7")
    assert res.rc == 0, res.doc
    lines = {ln["episode_index"]: ln for ln in read_jsonl(os.path.join(rd, "revisions", "r0002", "verdicts.jsonl"))}
    assert {e for e, ln in lines.items() if ln["verdict"] == "drop"} == REJECTED | {7}
    assert not [e for e, ln in lines.items() if ln["verdict"] == "held"]
