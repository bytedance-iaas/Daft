"""GET /tasks/{id}/episodes: the episodes of a revision for the report's Episode tab (F6.2)."""
from __future__ import annotations

from .conftest import T0, all_pages, assert_error, assert_schema

PAGE = "TaskEpisodePage"
LISTS = {0: "passed", 1: "reject", 2: "reject", 3: "passed", 4: "passed", 5: "passed",
         6: "held", 7: "reject", 8: "passed"}


def _page(world, **q) -> dict:
    r = world.get("/episodes", **q)
    assert r.status_code == 200, r.text
    body = r.json()
    assert_schema(PAGE, body)
    return body


def _eps(body) -> list[int]:
    return [i["episode_index"] for i in body["items"]]


def test_every_episode_in_order_with_its_list_and_reasons(world):
    body = _page(world)
    assert _eps(body) == list(range(9)) and body["total"] == 9 and body["revision"] == 1
    assert body["has_more"] is False and body["next_cursor"] is None
    assert body["counts"] == {"all": 9, "passed": 5, "reject": 3, "held": 1, "review": 3}
    by_ep = {i["episode_index"]: i for i in body["items"]}
    assert {ep: i["list"] for ep, i in by_ep.items()} == LISTS
    assert by_ep[1]["reason_modules"] == ["timestamp_check"]
    assert by_ep[2]["reason_modules"] == ["task_success"]
    assert by_ep[7]["reason_modules"] == ["dedup"]
    assert by_ep[6]["reason_modules"] == ["task_success"]               # held: the failed module
    assert by_ep[0]["reason_modules"] == [] and by_ep[0]["review"] is False
    # 待裁: the cards still to decide (the appeal candidates ep 2 and ep 7 are optional)
    assert [ep for ep, i in by_ep.items() if i["review"]] == [3, 4, 5]
    assert by_ep[3]["review_modules"] == ["task_success"]
    assert by_ep[4]["review_modules"] == ["skill_profile"]
    assert sorted(by_ep[5]["review_modules"]) == ["skill_profile", "task_success"]


def test_filters_by_list_and_open_questions(world):
    assert _eps(_page(world, list="reject")) == [1, 2, 7]
    assert _eps(_page(world, list="held")) == [6]
    assert _eps(_page(world, review="true")) == [3, 4, 5]
    assert _eps(_page(world, review="false")) == [0, 1, 2, 6, 7, 8]
    body = _page(world, list="passed", review="true")
    assert _eps(body) == [3, 4, 5] and body["total"] == 3
    assert body["counts"]["all"] == 9                                  # counts ignore filters
    assert _eps(_page(world, list="held", review="true")) == []
    # a decision takes the card off 待裁 at once, before it is applied
    assert world.decide((3, "task_verdict", "success")).status_code == 200
    assert _eps(_page(world, review="true")) == [4, 5]
    assert _page(world)["counts"]["review"] == 2


def test_q_finds_an_episode_by_number(world):
    for q in ("8", "ep8", "ep 8", "EP 8", " ep08 ", "000008"):
        assert _eps(_page(world, q=q)) == [8], q
    assert _eps(_page(world, q="0")) == [0]
    assert _eps(_page(world, q="")) == list(range(9))
    assert _eps(_page(world, q="ep")) == list(range(9))
    assert _eps(_page(world, q="42")) == []
    assert _eps(_page(world, q="1", list="reject")) == [1]
    body = assert_error(world.get("/episodes", q="wipe"), "validation_failed", 400)
    assert "episode 编号" in body["error"]["message"]
    assert_error(world.get("/episodes", q="ep-3"), "validation_failed", 400)


def test_q_is_a_substring_of_the_index(world, monkeypatch):
    """ep 12 is found by 12 and by 2 (it contains it), like every index containing the digits."""
    from daemon.results import episode_list as L
    from daemon.results.revision import Revision

    many = {ep: ("passed", {"episode_index": ep}) for ep in (2, 12, 20, 112, 121, 300)}
    monkeypatch.setattr(Revision, "entries", lambda self: many)
    monkeypatch.setattr(L, "open_questions", lambda *a: {})
    assert _eps(_page(world, q="12")) == [12, 112, 121]
    assert _eps(_page(world, q="ep 2")) == [2, 12, 20, 112, 121]
    assert _eps(_page(world, q="ep000300")) == [300]


def test_cursor_pages_cover_the_filtered_set_once(world):
    items, bodies = all_pages(lambda **q: world.get("/episodes", **q), limit=2)
    assert [i["episode_index"] for i in items] == list(range(9)) and len(bodies) == 5
    assert all(b["total"] == 9 for b in bodies)
    items, _ = all_pages(lambda **q: world.get("/episodes", list="reject", **q), limit=1)
    assert [i["episode_index"] for i in items] == [1, 2, 7]
    first = _page(world, limit=2)
    # the cursor is bound to the filters it was issued under
    assert_error(world.get("/episodes", cursor=first["next_cursor"], list="reject"),
                 "validation_failed", 400)
    assert_error(world.get("/episodes", cursor="not-a-cursor"), "validation_failed", 400)
    for bad in ({"limit": 0}, {"limit": 501}, {"list": "review"}, {"review": "maybe"}, {"rev": 0}):
        assert_error(world.get("/episodes", **bad), "validation_failed", 400)
    assert _page(world, limit=500)["total"] == 9


def test_a_new_revision_sends_old_cursors_back_to_the_start(world):
    first = _page(world, limit=2)
    world.decide((3, "task_verdict", "success"))
    sub = world.start_subtask(at=T0 + 10 * 60_000)
    world.apply(sub)
    world.revision(2, subtask_id=sub.id)
    world.finish_subtask(sub, at=T0 + 20 * 60_000)
    world.switch(2)
    body = assert_error(world.get("/episodes", cursor=first["next_cursor"], limit=2),
                        "result_changed", 409)
    assert body["error"]["details"] == {"cursor_revision": 1, "revision": 2}
    current = _page(world)
    assert current["revision"] == 2 and 3 not in [i["episode_index"] for i in current["items"]
                                                  if i["review"]]
    # a history revision shows the questions it asked (nobody adjudicates it any more)
    old = _page(world, rev=1)
    assert old["revision"] == 1 and _eps(_page(world, rev=1, review="true")) == [3, 4, 5]
    assert_error(world.get("/episodes", rev=3), "not_found", 404)


def test_a_task_without_results_has_no_episode_list(world):
    from .conftest import API, make_task

    task = make_task(world.repo, world.dataset, name="还没跑")
    assert_error(world.client.get(f"{API}/tasks/{task.id}/episodes"), "not_found", 404)
    assert_error(world.client.get(f"{API}/tasks/nope/episodes"), "not_found", 404)
