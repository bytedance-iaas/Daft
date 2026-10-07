"""Exact dedup as a streaming segment (D70, design doc 17 §3.2).

The whole-set pass is gone: an episode is hashed when it arrives, the video content is read
only where the action bytes collide, and a later call groups against the records already in
the run directory. The synthetic fixture's episode 7 is a byte copy of episode 3.
"""
from __future__ import annotations

import json
import os

import pytest


def records(run_dir: str) -> dict[int, dict]:
    from curation.pipeline.records import latest_results

    return latest_results(run_dir, "dedup")


def groups(run_dir: str) -> dict:
    with open(os.path.join(run_dir, "checks", "dedup", "groups.json"), encoding="utf-8") as fh:
        return json.load(fh)


def details(rec: dict) -> dict:
    return rec.get("details") or {}


@pytest.fixture
def run_dir(tmp_path) -> str:
    return str(tmp_path / "run")


def dedup(cli, dataset: str, run_dir: str, episodes: str, *extra):
    res = cli("check", "--modules", "dedup", "--input", dataset, "--run-dir", run_dir,
              "--episodes", episodes, *extra)
    assert res.rc == 0, res.err
    return res


def test_the_copy_is_the_duplicate_and_every_record_carries_its_action_hash(cli, dataset, run_dir):
    dedup(cli, dataset, run_dir, "0-7")
    recs = records(run_dir)
    assert sorted(recs) == list(range(8))
    assert all(details(recs[e]).get("action_hash") for e in recs), "resume reads the hashes back"
    assert details(recs[7])["duplicate_of"] == 3                      # 7 is the copy
    assert [f["code"] for f in recs[7]["findings"]] == ["duplicate"]
    assert not recs[3]["findings"], "the member seen first is clean; aggregate picks the keeper"
    g = groups(run_dir)
    assert g["action_collisions"] == [[3, 7]]
    assert g["dropped"] == [{"episode_index": 7, "duplicate_of": 3}]
    assert sorted(g["fingerprints"]) == ["3", "7"], "only the collision reads video content"
    assert g["order"] == list(range(8))


def test_a_second_call_groups_against_the_records_already_written(cli, dataset, run_dir):
    """A retry or a resumed run judges some episodes only: the group still forms, because the
    segment seeds its state from the records in the run directory."""
    dedup(cli, dataset, run_dir, "0-3")
    assert not records(run_dir)[3]["findings"]
    assert groups(run_dir)["action_collisions"] == []
    res = dedup(cli, dataset, run_dir, "4-7")
    assert res.doc["modules"]["dedup"]["episodes"]["total"] == 4
    recs = records(run_dir)
    assert details(recs[7])["duplicate_of"] == 3
    g = groups(run_dir)
    assert g["action_collisions"] == [[3, 7]] and g["dropped"] == [{"episode_index": 7, "duplicate_of": 3}]
    assert g["order"] == list(range(8)), "the episodes judged earlier come first"


def test_resume_keeps_the_duplicate_and_does_not_judge_it_again(cli, dataset, run_dir):
    dedup(cli, dataset, run_dir, "0-7")
    before = records(run_dir)[7]
    res = dedup(cli, dataset, run_dir, "0-7", "--resume")
    assert res.doc["modules"]["dedup"]["skipped_existing"] == 8
    after = records(run_dir)[7]
    assert details(after)["duplicate_of"] == 3
    assert {k: v for k, v in after.items() if k != "elapsed_s"} \
        == {k: v for k, v in before.items() if k != "elapsed_s"}, "the record is the one already there"
    assert groups(run_dir)["dropped"] == [{"episode_index": 7, "duplicate_of": 3}]


def test_without_the_copy_no_video_content_is_read(cli, dataset, run_dir):
    """Pass two is the expensive one (it hashes video files); nothing collides here, so it
    never runs and no fingerprint is computed."""
    dedup(cli, dataset, run_dir, "0-6")
    recs = records(run_dir)
    assert not any(r["findings"] for r in recs.values())
    assert all("fingerprint" not in details(r) for r in recs.values())
    assert groups(run_dir) == {"order": list(range(7)), "action_collisions": [],
                               "fingerprints": {}, "dropped": []}


def test_the_survivors_leave_the_copy_out(cli, dataset, run_dir, tmp_path):
    """What the segment hands the next step (a funnel run): the duplicate does not go on."""
    out = tmp_path / "survivors.txt"
    dedup(cli, dataset, run_dir, "0-7", "--survivors-out", str(out))
    assert [int(ln) for ln in out.read_text().split()] == [0, 1, 2, 3, 4, 5, 6]


def test_the_group_keeps_the_lowest_index_whatever_order_it_saw_them(cli, dataset, run_dir):
    """The Daemon hands episodes over as the earlier segments finish them, so the segment can meet
    the copy before the original. Once it has seen both it settles the group on the lowest index and
    appends a corrected record for the one that said otherwise (D70)."""
    dedup(cli, dataset, run_dir, "7")                 # the copy arrives first and looks clean
    assert not records(run_dir)[7]["findings"]
    dedup(cli, dataset, run_dir, "3")                 # its original follows
    recs = records(run_dir)
    assert not recs[3]["findings"], "the lowest index keeps"
    assert details(recs[7])["duplicate_of"] == 3 and [f["code"] for f in recs[7]["findings"]] == ["duplicate"]
    assert details(recs[7])["action_hash"] == details(recs[3])["action_hash"]
    g = groups(run_dir)
    assert g["dropped"] == [{"episode_index": 7, "duplicate_of": 3}]
    assert g["action_collisions"] == [[3, 7]]


def test_a_settled_group_stays_settled_when_the_rest_arrives(cli, dataset, run_dir):
    """The correction is not undone by the episodes that come after it."""
    dedup(cli, dataset, run_dir, "7")
    dedup(cli, dataset, run_dir, "3")
    dedup(cli, dataset, run_dir, "0-6", "--resume")
    recs = records(run_dir)
    assert sorted(recs) == list(range(8))
    assert details(recs[7])["duplicate_of"] == 3 and not recs[3]["findings"]
    assert groups(run_dir)["dropped"] == [{"episode_index": 7, "duplicate_of": 3}]
