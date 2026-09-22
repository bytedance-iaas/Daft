"""GET / POST /tasks/{id}/adjudication: the queue, recording decisions, F3.3 on the server."""
from __future__ import annotations

import shutil
import sqlite3

import pytest

from daemon.pagination import encode_cursor

from .conftest import (API, CAPTION, JSON, MIN, T0, TEXT, World, all_pages, assert_error,
                       assert_schema, make_task, read_csv)

LIST = ("openapi.yaml#/paths/~1tasks~1{id}~1adjudication/get/responses/200/content/"
        "application~1json/schema")


def _page(world, **q):
    r = world.get("/adjudication", **q)
    assert r.status_code == 200, r.text
    body = r.json()
    assert_schema(LIST, body)
    return body


def _cards(world, **q) -> dict[int, dict]:
    items, bodies = all_pages(lambda **p: world.get("/adjudication", **p), limit=50, **q)
    for body in bodies:
        assert_schema(LIST, body)
    return {c["episode_index"]: c for c in items}


def _ok(response) -> dict:
    assert response.status_code == 200, response.text
    body = response.json()
    assert_schema("AdjudicationCounts", body)
    return body


def _rows(world) -> int:
    with sqlite3.connect(world.rt.settings.db_path) as db:
        return db.execute("SELECT COUNT(*) FROM adjudication WHERE task_id=?",
                          (world.task_id,)).fetchone()[0]


def _apply(world, n: int, at: int) -> None:
    """What W5a's apply subtask does, then switches to the new revision."""
    sub = world.start_subtask(at=at)
    world.apply(sub)
    world.revision(n, subtask_id=sub.id)
    world.finish_subtask(sub, at=at + MIN)
    world.switch(n)


# ---------------------------------------------------------------------------
# the queue
# ---------------------------------------------------------------------------

def test_the_queue_of_the_first_revision(world):
    body = _page(world, status="all")
    assert [c["episode_index"] for c in body["items"]] == [3, 4, 5]
    assert body["counts"] == {"decided": 0, "pending": 3, "unapplied": 0}
    cards = {c["episode_index"]: c for c in body["items"]}
    assert all(c["status"] == "pending" for c in cards.values())
    assert [q["line"] for q in cards[5]["questions"]] == ["label", "task_verdict"]
    label = cards[4]["questions"][0]
    assert label == {"line": "label", "source_module": "skill_profile",
                     "reason": label["reason"], "annotation": TEXT[4], "caption": CAPTION[4],
                     "suggestion": CAPTION[4], "priority": "参考", "latest_decision": None}
    assert cards[5]["questions"][0]["priority"] == "重点"
    verdict = cards[3]["questions"][0]
    assert (verdict["line"], verdict["source_module"], verdict["annotation"]) == (
        "task_verdict", "task_success", TEXT[3])
    assert verdict["reason"] == "两层证据矛盾，进人工"
    # ep 8 (motion_quality could not score it) is no question for a person
    appeals = _page(world, tab="appeals", status="all")
    assert [c["episode_index"] for c in appeals["items"]] == [2, 7]
    q = appeals["items"][0]["questions"][0]
    assert (q["line"], q["source_module"], q["annotation"]) == ("reject_appeal", "task_success", TEXT[2])
    assert "3 路复核一致判未完成" in q["reason"]
    dup = appeals["items"][1]["questions"][0]            # D42: a duplicate can be restored
    assert (dup["line"], dup["source_module"]) == ("reject_appeal", "dedup")
    assert "与 ep000000 字节级完全重复" in dup["reason"]
    assert appeals["counts"] == body["counts"]            # counts are the whole task's


def test_source_and_status_filters(world):
    assert list(_cards(world, status="all", source="skill_profile")) == [4, 5]
    assert list(_cards(world, status="all", source="task_success")) == [3, 5]
    assert list(_cards(world, status="all", source="timestamp_check")) == []
    assert list(_cards(world, tab="appeals", status="all", source="task_success")) == [2]
    assert list(_cards(world, tab="appeals", status="all", source="dedup")) == [7]
    assert_error(world.get("/adjudication", source="nope"), "validation_failed")
    assert_error(world.get("/adjudication", status="done"), "validation_failed")
    assert_error(world.get("/adjudication", tab="other"), "validation_failed")
    assert_error(world.get("/adjudication", limit=201), "validation_failed")

    _ok(world.decide((3, "task_verdict", "unsure"), (4, "label", "keep_label")))
    cards = _cards(world, status="all")
    assert (cards[3]["status"], cards[4]["status"], cards[5]["status"]) == \
        ("unsure", "decided", "pending")
    assert list(_cards(world)) == [3, 5]                   # pending (default): unsure included
    assert list(_cards(world, status="decided")) == [4]
    assert list(_cards(world, status="unapplied")) == [4]  # unsure is not something to apply


def test_paging_has_no_duplicates_or_gaps_even_while_people_decide(world):
    first = _page(world, limit=1)
    assert [c["episode_index"] for c in first["items"]] == [3] and first["has_more"]
    # the first card is decided between two pages: it leaves the pending filter
    _ok(world.decide((3, "task_verdict", "success")))
    second = _page(world, limit=1, cursor=first["next_cursor"])
    assert [c["episode_index"] for c in second["items"]] == [4]
    third = _page(world, limit=1, cursor=second["next_cursor"])
    assert [c["episode_index"] for c in third["items"]] == [5]
    assert third["has_more"] is False and third["next_cursor"] is None
    items, _ = all_pages(lambda **p: world.get("/adjudication", **p), limit=2, status="all")
    assert [c["episode_index"] for c in items] == [3, 4, 5]


def test_a_revision_switch_while_paging_answers_result_changed(world):
    first = _page(world, limit=1, status="all")
    _ok(world.decide((3, "task_verdict", "success")))
    _apply(world, 2, T0 + 10 * MIN)
    body = assert_error(world.get("/adjudication", limit=1, status="all",
                                  cursor=first["next_cursor"]), "result_changed")
    assert body["error"]["details"] == {"cursor_revision": 1, "revision": 2}
    # a cursor of another filter or another task is a client bug
    fresh = _page(world, limit=1, status="all")
    assert_error(world.get("/adjudication", limit=1, status="pending", cursor=fresh["next_cursor"]),
                 "validation_failed")
    other = encode_cursor("adjudication", [2, 3], scope={"task": "task_other", "tab": "review",
                                                         "status": "all", "source": None})
    assert_error(world.get("/adjudication", status="all", cursor=other), "validation_failed")


# ---------------------------------------------------------------------------
# recording decisions
# ---------------------------------------------------------------------------

def test_decisions_are_appended_and_the_latest_wins(world):
    counts = _ok(world.decide((3, "task_verdict", "success")))
    assert counts == {"decided": 1, "pending": 2, "unapplied": 1}
    counts = _ok(world.decide((3, "task_verdict", "failure")))
    assert counts == {"decided": 1, "pending": 2, "unapplied": 1}
    assert _rows(world) == 2                                # append only
    q = _cards(world, status="all")[3]["questions"][0]
    d = q["latest_decision"]
    assert_schema("Decision", d)
    assert (d["decision"], d["applied"], d["decided_by"], d["episode_index"], d["line"]) == (
        "failure", False, "anonymous", 3, "task_verdict")
    assert d["decided_at"] == T0 and d["new_label"] is None and d["note"] is None


def test_each_line_accepts_only_its_own_decisions(world):
    cases = [
        ((4, "label", "success"), "标注分歧不能选 success"),
        ((3, "task_verdict", "restore"), "任务成败不能选 restore"),
        ((2, "reject_appeal", "discard"), "被拒复议不能选 discard"),
        ((2, "reject_appeal", "failure"), "被拒复议不能选 failure"),
        ((3, "label", "keep_label"), "没有待裁决的标注分歧"),
        ((1, "reject_appeal", "restore"), "不能复议"),          # timestamp fragment: final
        ((0, "reject_appeal", "restore"), "不能复议"),          # not rejected at all
        ((99, "task_verdict", "success"), "不在这个任务的待裁决队列里"),
        ((6, "task_verdict", "success"), "不在这个任务的待裁决队列里"),   # held: nothing to judge
        ((4, "label", "custom_label"), "要填写新标注"),
        ((4, "label", "custom_label", "   "), "要填写新标注"),
        ((4, "label", "keep_label", "a label"), "可以带 new_label"),
        ((4, "task_verdict", "success"), "才能直接判成败"),      # label-only card, no relabel yet
    ]
    for item, words in cases:
        body = assert_error(world.decide(item), "validation_failed")
        assert words in body["error"]["message"], (item, body)
        assert body["error"]["details"]["errors"][0]["field"].startswith("decisions.0.")
    assert _rows(world) == 0


def test_request_body_is_checked_against_c4(world):
    url = f"{API}/tasks/{world.task_id}/adjudication"
    for body in ({}, {"decisions": []}, {"decisions": [{"episode_index": 3, "line": "task_verdict"}]},
                 {"decisions": [{"episode_index": 3, "line": "task_verdict", "decision": "yes"}]},
                 {"decisions": [{"episode_index": 3, "line": "verdict", "decision": "success"}]},
                 {"decisions": [{"episode_index": -1, "line": "task_verdict", "decision": "success"}]},
                 {"decisions": [{"episode_index": 3, "line": "task_verdict", "decision": "success",
                                 "decided_by": "mallory"}]},
                 {"decisions": [], "extra": 1}):
        assert_error(world.client.post(url, json=body, headers=JSON), "validation_failed")
    assert_error(world.client.post(url, content=b'{"decisions": [}', headers=JSON),
                 "validation_failed")
    good = b'{"decisions": [{"episode_index": 3, "line": "task_verdict", "decision": "success"}]}'
    assert_error(world.client.post(url, content=good, headers={"Content-Type": "text/plain"}),
                 "validation_failed")                       # writes are JSON only (C4 1.2)
    assert_error(world.client.post(url, content=good, headers={**JSON,
                                                               "Sec-Fetch-Site": "cross-site"}),
                 "validation_failed")                       # no cross-site writes
    assert _rows(world) == 0


def test_a_submission_is_all_or_nothing(world):
    body = assert_error(world.decide((3, "task_verdict", "success"), (1, "reject_appeal", "restore")),
                        "validation_failed")
    assert body["error"]["details"]["errors"][0]["field"] == "decisions.1.episode_index"
    assert _rows(world) == 0
    assert _page(world)["counts"] == {"decided": 0, "pending": 3, "unapplied": 0}


def test_adopting_the_suggestion_takes_its_text_and_opens_the_optional_verdict(world):
    _ok(world.decide((4, "label", "adopt_suggestion")))
    card = _cards(world, status="all")[4]
    assert card["status"] == "decided"                     # executing re-judges the new label
    assert card["questions"][0]["latest_decision"]["new_label"] == CAPTION[4]
    _ok(world.decide((5, "label", "adopt_suggestion", "open the top drawer")))
    card = _cards(world, status="all")[5]
    assert card["status"] == "decided"                     # verdict open, relabel re-judges it
    assert card["questions"][0]["latest_decision"]["new_label"] == "open the top drawer"
    # a label-only card takes a verdict once the label changed (v1's optional verdict)
    _ok(world.decide((4, "task_verdict", "failure")))
    card = _cards(world, status="all")[4]
    assert [q["line"] for q in card["questions"]] == ["label", "task_verdict"]
    assert card["questions"][1]["source_module"] == "task_success"
    assert card["questions"][1]["latest_decision"]["decision"] == "failure"
    # the label kept after all: the recorded verdict stays visible (it would still apply)
    # and can be changed or withdrawn; once withdrawn, a kept label takes no verdict
    _ok(world.decide((4, "label", "keep_label")))
    card = _cards(world, status="all")[4]
    assert [q["line"] for q in card["questions"]] == ["label", "task_verdict"]
    assert card["status"] == "decided"
    _ok(world.decide((4, "task_verdict", "unsure")))
    card = _cards(world, status="all")[4]
    assert [q["line"] for q in card["questions"]] == ["label"] and card["status"] == "decided"
    assert_error(world.decide((4, "task_verdict", "success")), "validation_failed")
    # in one submission, a relabel then its verdict
    _ok(world.decide((4, "label", "custom_label", "wipe it"), (4, "task_verdict", "success")))
    card = _cards(world, status="all")[4]
    assert card["questions"][0]["latest_decision"]["new_label"] == "wipe it"
    assert card["questions"][1]["latest_decision"]["decision"] == "success"


def test_discard_wins_over_the_task_verdict(world):
    """F3.3 rule 1, on the server: the card is decided by the discard, a verdict next to a
    discard on the label is refused, and executing rejects the episode."""
    _ok(world.decide((5, "task_verdict", "success"), (5, "label", "discard")))
    card = _cards(world, status="all")[5]
    assert card["status"] == "decided"
    assert {q["line"]: q["latest_decision"]["decision"] for q in card["questions"]} == {
        "label": "discard", "task_verdict": "success"}
    body = assert_error(world.decide((5, "task_verdict", "failure")), "validation_failed")
    assert "整条弃用" in body["error"]["message"]
    # a discard on a verdict-only card goes on the verdict line
    _ok(world.decide((3, "task_verdict", "discard")))
    assert _cards(world, status="all")[3]["status"] == "decided"
    _apply(world, 2, T0 + 10 * MIN)
    for ep in (3, 5):
        view = world.get(f"/episodes/{ep}").json()
        assert view["list"] == "reject"
        assert view["reasons"] == [{"module": view["reasons"][0]["module"], "kind": "human",
                                    "text": "人工裁决弃用"}]
    cards = _cards(world, status="all")
    assert cards[5]["status"] == "applied" and cards[3]["status"] == "applied"
    # undoing the discard and judging again is possible afterwards
    _ok(world.decide((5, "label", "unsure")))
    _ok(world.decide((5, "task_verdict", "success")))


def test_appeals_only_for_rejects_of_appealable_modules(world):
    """F3.3 rule 2: only rejects attributed to an appealable module - task_success, and
    dedup since D42 - can be appealed; the physical and structural gates are final."""
    assert_error(world.decide((1, "reject_appeal", "restore")), "validation_failed")
    assert _ok(world.decide((7, "reject_appeal", "keep_rejected")))["unapplied"] == 1
    assert _cards(world, tab="appeals", status="all")[7]["status"] == "decided"
    assert _ok(world.decide((7, "reject_appeal", "unsure")))["unapplied"] == 0
    assert _cards(world, tab="appeals", status="all")[7]["status"] == "unsure"
    assert _ok(world.decide((2, "reject_appeal", "restore")))["unapplied"] == 1
    card = _cards(world, tab="appeals", status="all")[2]
    assert card["status"] == "decided"
    _apply(world, 2, T0 + 10 * MIN)
    assert world.get("/episodes/2").json()["list"] == "passed"
    # the restored episode left the appeals list of r2; its card stays, applied
    appeals = _cards(world, tab="appeals", status="all")
    assert appeals[2]["status"] == "applied"
    assert appeals[2]["questions"][0]["latest_decision"]["applied"] is True
    # and the decision can still be changed
    assert _ok(world.decide((2, "reject_appeal", "keep_rejected")))["unapplied"] == 1


def test_unsure_keeps_the_episode_in_the_queue(world):
    """F3.3 rule 3: an unsure answer is recorded, counts as pending, and executing it changes
    nothing - the card is still there in the next revision."""
    counts = _ok(world.decide((3, "task_verdict", "unsure")))
    assert counts == {"decided": 0, "pending": 3, "unapplied": 0}
    assert _cards(world)[3]["status"] == "unsure"
    _ok(world.decide((4, "label", "keep_label")))
    _apply(world, 2, T0 + 10 * MIN)
    cards = _cards(world)                                   # pending
    assert 3 in cards and cards[3]["status"] == "unsure"
    assert cards[3]["questions"][0]["latest_decision"]["applied"] is True
    assert world.get("/episodes/3").json()["list"] == "passed"
    counts = _page(world)["counts"]
    assert counts == {"decided": 1, "pending": 2, "unapplied": 0}


def test_decided_cards_stay_after_their_answers_were_applied(world):
    _ok(world.decide((3, "task_verdict", "success"), (4, "label", "keep_label"),
                     (5, "label", "discard")))
    _apply(world, 2, T0 + 10 * MIN)
    review = world.run_dir / "revisions" / "r0002" / "review.json"
    assert '"episode_index": 3' not in review.read_text(encoding="utf-8")
    cards = _cards(world, status="all")
    assert {ep: c["status"] for ep, c in cards.items()} == {3: "applied", 4: "applied", 5: "applied"}
    assert _page(world)["counts"] == {"decided": 3, "pending": 0, "unapplied": 0}
    assert list(_cards(world, status="decided")) == [3, 4, 5]
    assert list(_cards(world)) == []


# ---------------------------------------------------------------------------
# side effects: CSV copy, summary, idempotency, task isolation
# ---------------------------------------------------------------------------

def test_csv_copy_in_v1_columns_and_words(world):
    _ok(world.decide((4, "label", "adopt_suggestion"), (3, "task_verdict", "success"),
                     (2, "reject_appeal", "restore"), (5, "label", "unsure")))
    hd = world.run_dir / "human-decisions"
    labels = read_csv(hd / "label_decisions.csv")
    assert [(r["episode_id"], r["decision"], r["new_label"]) for r in labels] == [
        ("ep000004", "采纳建议改标", CAPTION[4]), ("ep000005", "拿不准", "")]
    assert list(labels[0]) == ["episode_id", "decision", "new_label", "note", "at"]
    verdicts = read_csv(hd / "task_verdicts.csv")
    assert [(r["episode_id"], r["verdict"]) for r in verdicts] == [("ep000003", "判成功")]
    assert list(verdicts[0]) == ["episode_id", "verdict", "note", "at"]
    appeals = read_csv(hd / "reject_appeals.csv")
    assert [(r["episode_id"], r["appeal"]) for r in appeals] == [("ep000002", "捞回")]
    # a changed decision rewrites the copy: the latest per episode and line
    _ok(world.decide((3, "task_verdict", "failure")))
    assert [(r["episode_id"], r["verdict"]) for r in read_csv(hd / "task_verdicts.csv")] == [
        ("ep000003", "判失败")]


def test_pending_adjudication_follows_every_submission(world):
    task_url = f"{API}/tasks/{world.task_id}"
    task = world.client.get(task_url).json()
    assert_schema("Task", task)
    assert task["pending_adjudication"] == 3
    _ok(world.decide((3, "task_verdict", "success"), (4, "label", "keep_label")))
    task = world.client.get(task_url).json()
    assert task["pending_adjudication"] == 1
    assert task["summary"] == {"total": 9, "passed": 5, "rejected": 3, "held": 1, "review": 5,
                               "pass_rate": 0.5556}
    link = [ln for ln in task["links"] if ln["rel"] == "adjudication"][0]
    assert link["title"] == "1 episode needs human judgement"
    item = world.client.get(f"{API}/tasks").json()["items"][0]
    assert item["pending_adjudication"] == 1
    overview = world.client.get(f"{API}/overview").json()
    assert overview["todo"]["adjudication"] == {"tasks": 1, "episodes": 1}
    _ok(world.decide((5, "label", "discard")))
    assert world.client.get(task_url).json()["pending_adjudication"] == 0
    assert world.client.get(f"{API}/overview").json()["todo"]["adjudication"] == {
        "tasks": 0, "episodes": 0}


def test_listing_repairs_a_stale_pending_count(world):
    summary = dict(world.task().summary)
    world.repo.set_task_summary(world.task_id, {**summary, "pending_adjudication": 99})
    assert world.client.get(f"{API}/tasks/{world.task_id}").json()["pending_adjudication"] == 99
    _page(world)
    assert world.client.get(f"{API}/tasks/{world.task_id}").json()["pending_adjudication"] == 3


def test_idempotency_key_replays_the_first_answer(world):
    key = {"Idempotency-Key": "adj-0001-abcdef"}
    first = world.decide((3, "task_verdict", "success"), headers=key)
    again = world.decide((3, "task_verdict", "success"), headers=key)
    assert _ok(first) == _ok(again)
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert _rows(world) == 1
    assert_error(world.decide((3, "task_verdict", "failure"), headers=key), "idempotency_conflict")


def test_no_result_yet_means_nothing_to_decide(client_for, tmp_path):
    from .conftest import make_dataset

    c = client_for(base_path="/curation")
    rt = c.app.state.runtime
    task = make_task(rt.repo, make_dataset(tmp_path / "ds"))
    r = c.post(f"{API}/tasks/{task.id}/adjudication", headers=JSON, json={
        "decisions": [{"episode_index": 3, "line": "task_verdict", "decision": "success"}]})
    body = assert_error(r, "task_state_conflict")
    assert body["error"]["details"] == {"state": "queued", "result_rev": 0}


def test_decisions_never_cross_tasks(world, tmp_path):
    """D32: a second task over the same dataset, with the same episodes in its queue."""
    other = make_task(world.repo, world.dataset, name="same data, another task")
    from .conftest import finish_main_run

    finish_main_run(world.repo, other.id)
    other_dir = world.rt.settings.work_dir / other.id
    shutil.copytree(world.run_dir, other_dir)
    b = World(client=world.client, rt=world.rt, task_id=other.id, run_dir=other_dir,
              dataset=world.dataset, clock=world.clock)
    b.switch(1)
    _ok(world.decide((3, "task_verdict", "success"), (4, "label", "discard")))
    assert _page(b)["counts"] == {"decided": 0, "pending": 3, "unapplied": 0}
    assert all(q["latest_decision"] is None
               for c in _cards(b, status="all").values() for q in c["questions"])
    assert not (other_dir / "human-decisions").exists()
    assert world.client.get(f"{API}/tasks/{other.id}").json()["pending_adjudication"] == 3
    _ok(b.decide((3, "task_verdict", "failure")))
    assert _cards(world, status="all")[3]["questions"][0]["latest_decision"]["decision"] == "success"
    assert _cards(b, status="all")[3]["questions"][0]["latest_decision"]["decision"] == "failure"
    # an episode only in this task's queue is refused by the other one
    shutil.rmtree(other_dir / "revisions" / "r0001")
    shutil.copytree(world.run_dir / "revisions" / "r0001", other_dir / "revisions" / "r0001")
    review = other_dir / "revisions" / "r0001" / "review.json"
    review.write_text(review.read_text(encoding="utf-8").replace('"episode_index": 4,',
                                                                 '"episode_index": 14,'),
                      encoding="utf-8")
    assert_error(b.decide((4, "label", "keep_label")), "validation_failed")


@pytest.mark.parametrize("line,decision", [("task_verdict", "success"), ("label", "keep_label")])
def test_submission_reaches_only_the_path_task(world, line, decision):
    """The task comes from the path only; the body has no task field to point elsewhere."""
    url = f"{API}/tasks/{world.task_id}/adjudication"
    r = world.client.post(url, headers=JSON, json={"decisions": [
        {"episode_index": 5, "line": line, "decision": decision, "task_id": "task_other"}]})
    assert_error(r, "validation_failed")
