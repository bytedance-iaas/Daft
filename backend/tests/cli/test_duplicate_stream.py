"""The duplicate check as a streaming segment (D87, design doc 26 §1; D70).

An episode is hashed when it arrives, the video content is read only where the action bytes
collide, and a later call groups against the records already in the run directory. Every
member of a group names the others; nothing is kept first or dropped. The synthetic
fixture's episode 7 is a byte copy of episode 3.
"""
from __future__ import annotations

import json
import os

import pytest

MODULE = "duplicate_check"


def records(run_dir: str) -> dict[int, dict]:
    from curation.pipeline.records import latest_results

    return latest_results(run_dir, MODULE)


def groups(run_dir: str) -> dict:
    with open(os.path.join(run_dir, "checks", MODULE, "groups.json"), encoding="utf-8") as fh:
        return json.load(fh)


def details(rec: dict) -> dict:
    return rec.get("details") or {}


def same_as(rec: dict) -> list[int]:
    return details(rec).get("same_as") or []


def codes(rec: dict) -> list[str]:
    return [f["code"] for f in rec["findings"]]


@pytest.fixture
def run_dir(tmp_path) -> str:
    return str(tmp_path / "run")


def check(cli, dataset: str, run_dir: str, episodes: str, *extra):
    res = cli("check", "--modules", MODULE, "--input", dataset, "--run-dir", run_dir,
              "--episodes", episodes, *extra)
    assert res.rc == 0, res.err
    return res


def test_both_members_name_each_other_and_every_record_carries_its_action_hash(cli, dataset, run_dir):
    check(cli, dataset, run_dir, "0-7")
    recs = records(run_dir)
    assert sorted(recs) == list(range(8))
    assert all(details(recs[e]).get("action_hash") for e in recs), "resume reads the hashes back"
    assert (same_as(recs[3]), same_as(recs[7])) == ([7], [3])
    assert codes(recs[3]) == codes(recs[7]) == ["duplicate"]
    assert recs[7]["findings"][0]["readings"] == {"group_id": 3, "same_as": [3]}
    assert not any(recs[e]["findings"] for e in recs if e not in (3, 7))
    g = groups(run_dir)
    assert g["action_collisions"] == [[3, 7]] and g["groups"] == [[3, 7]]
    assert sorted(g["fingerprints"]) == ["3", "7"], "only the collision reads video content"
    assert g["order"] == list(range(8))


def test_a_second_call_groups_against_the_records_already_written(cli, dataset, run_dir):
    """A retry or a resumed run judges some episodes only: the group still forms, because the
    segment seeds its state from the records in the run directory, and the member judged earlier
    gets a new record naming the later one."""
    check(cli, dataset, run_dir, "0-3")
    assert not records(run_dir)[3]["findings"]
    assert groups(run_dir)["action_collisions"] == []
    res = check(cli, dataset, run_dir, "4-7")
    assert res.doc["modules"][MODULE]["episodes"]["total"] == 4
    recs = records(run_dir)
    assert (same_as(recs[3]), same_as(recs[7])) == ([7], [3])
    g = groups(run_dir)
    assert g["action_collisions"] == [[3, 7]] and g["groups"] == [[3, 7]]
    assert g["order"] == list(range(8)), "the episodes judged earlier come first"


def test_resume_keeps_the_group_and_does_not_judge_it_again(cli, dataset, run_dir):
    check(cli, dataset, run_dir, "0-7")
    before = records(run_dir)
    res = check(cli, dataset, run_dir, "0-7", "--resume")
    assert res.doc["modules"][MODULE]["skipped_existing"] == 8
    after = records(run_dir)
    for ep in (3, 7):
        assert {k: v for k, v in after[ep].items() if k != "elapsed_s"} \
            == {k: v for k, v in before[ep].items() if k != "elapsed_s"}, "the record is the one already there"
    assert groups(run_dir)["groups"] == [[3, 7]]


def test_without_the_copy_no_video_content_is_read(cli, dataset, run_dir):
    """Pass two is the expensive one (it hashes video files); nothing collides here, so it
    never runs and no fingerprint is computed."""
    check(cli, dataset, run_dir, "0-6")
    recs = records(run_dir)
    assert not any(r["findings"] for r in recs.values())
    assert all("fingerprint" not in details(r) for r in recs.values())
    assert groups(run_dir) == {"order": list(range(7)), "action_collisions": [],
                               "fingerprints": {}, "groups": []}


def test_the_duplicates_go_on(cli, dataset, run_dir, tmp_path):
    """What the segment hands the next step: everything - a duplicate is reported, not dropped (D87)."""
    out = tmp_path / "survivors.txt"
    check(cli, dataset, run_dir, "0-7", "--survivors-out", str(out))
    assert [int(ln) for ln in out.read_text().split()] == list(range(8))


def test_the_group_forms_whatever_order_it_saw_them(cli, dataset, run_dir):
    """The Daemon hands episodes over as the earlier segments finish them, so the segment can meet
    the copy before the original. Once it has seen both, both name each other; the group is named by
    its lowest member."""
    check(cli, dataset, run_dir, "7")                 # the copy arrives first and looks clean
    assert not records(run_dir)[7]["findings"]
    check(cli, dataset, run_dir, "3")                 # its original follows
    recs = records(run_dir)
    assert (same_as(recs[3]), same_as(recs[7])) == ([7], [3])
    assert details(recs[3])["group_id"] == details(recs[7])["group_id"] == 3
    assert details(recs[7])["action_hash"] == details(recs[3])["action_hash"]
    g = groups(run_dir)
    assert g["groups"] == [[3, 7]] and g["action_collisions"] == [[3, 7]]


def test_a_formed_group_stays_when_the_rest_arrives(cli, dataset, run_dir):
    """The records naming the group are not undone by the episodes that come after them."""
    check(cli, dataset, run_dir, "7")
    check(cli, dataset, run_dir, "3")
    check(cli, dataset, run_dir, "0-6", "--resume")
    recs = records(run_dir)
    assert sorted(recs) == list(range(8))
    assert (same_as(recs[3]), same_as(recs[7])) == ([7], [3])
    assert groups(run_dir)["groups"] == [[3, 7]]
