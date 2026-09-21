"""Conformance suite for the queries beyond C5 1.2 (``daemon.repo.extras``, proposed for 1.3).

Runs on every implementation in ``repo_impls.py``, like ``test_repo_conformance.py``.
"""
from __future__ import annotations

from daemon.repo import protocol as P
from daemon.repo.extras import TOKEN_SLOT_MS, FinishedResults, dataset_format, token_slot

from .conftest import T0, sample_preflight, seed_dataset, seed_task

OTHER = "someone-else"
DAY = 24 * 60 * 60 * 1000


def _finish(repo, task_id, to="succeeded", *, at=T0, owner=P.DEFAULT_OWNER):
    for frm, nxt in (("queued", "running"), ("running", to)):
        assert repo.update_task_state(task_id, {frm}, nxt, at=at)


def test_dataset_format_reads_the_preflight():
    assert dataset_format(sample_preflight(version="v2")) == "lerobot_v2"
    assert dataset_format(sample_preflight(version="v3")) == "lerobot_v3"
    assert dataset_format(sample_preflight(version="v3", supported=False)) == "unsupported"
    rrd = sample_preflight()
    rrd["format"] = {"kind": "rrd", "version": None, "supported": False, "detail": "Rerun"}
    assert dataset_format(rrd) == "unsupported"
    assert dataset_format({}) == dataset_format(None) == "unsupported"


def test_find_dataset_by_address(repo):
    ds = seed_dataset(repo)
    public = seed_dataset(repo, "tos://hf-cache/lerobot/aloha_sim", source="public", region=None)
    assert repo.find_dataset(source="tos", uri=ds.uri, region="cn-beijing").id == ds.id
    assert repo.find_dataset(source="public", uri=public.uri, region=None).id == public.id
    assert repo.find_dataset(source="public", uri=public.uri, region="").id == public.id
    assert repo.find_dataset(source="tos", uri=ds.uri, region=None) is None
    assert repo.find_dataset(source="public", uri=ds.uri, region="cn-beijing") is None
    assert repo.find_dataset(source="tos", uri=ds.uri + "/", region="cn-beijing") is None
    assert repo.find_dataset(source="tos", uri=ds.uri, region="cn-beijing", owner=OTHER) is None


def test_adjudication_backlog(repo):
    a, b, c, d, e = (seed_task(repo, n) for n in "abcde")
    theirs = seed_task(repo, "theirs", owner=OTHER)
    summary = {"total": 50, "passed": 40, "rejected": 5, "held": 0, "review": 5, "pass_rate": 0.8}
    repo.set_task_summary(a.id, {**summary, "pending_adjudication": 3})
    repo.set_task_summary(b.id, summary)                       # no pending key yet: review counts
    repo.set_task_summary(c.id, {**summary, "pending_adjudication": 0})  # all judged
    repo.set_task_summary(d.id, {**summary, "pending_adjudication": True, "review": 2})
    repo.set_task_summary(theirs.id, {**summary, "pending_adjudication": 9})
    assert repo.adjudication_backlog() == (3, 10)
    for t in (a, b, d):
        _finish(repo, t.id)
    repo.soft_delete_task(a.id, at=T0)
    assert repo.adjudication_backlog() == (2, 7)
    assert repo.adjudication_backlog(owner=OTHER) == (1, 9)
    assert e.summary is None


def test_delivery_pending_count(repo):
    never = seed_task(repo, "never exported")
    stale = seed_task(repo, "stale")
    fresh = seed_task(repo, "exported")
    no_result = seed_task(repo, "no result yet")
    running = seed_task(repo, "running")
    for t in (never, stale, fresh, no_result):
        _finish(repo, t.id, "completed_with_errors")
    for t in (never, stale, fresh, running):
        assert repo.switch_result_rev(t.id, 0, 1)
    repo.set_export_fingerprint(stale.id, "sha256:old", delivery_stale=True)
    repo.set_export_fingerprint(fresh.id, "sha256:now", delivery_stale=False)
    repo.update_task_state(running.id, {"queued"}, "running", at=T0)
    assert repo.delivery_pending_count() == 2                          # never + stale
    repo.soft_delete_task(never.id, at=T0)
    assert repo.delivery_pending_count() == 1
    assert repo.delivery_pending_count(owner=OTHER) == 0


def test_finished_results_since(repo):
    summary = {"total": 50, "passed": 40, "rejected": 5, "held": 5, "review": 0, "pass_rate": 0.8}
    old = seed_task(repo, "old")
    _finish(repo, old.id, at=T0 - 8 * DAY)
    repo.set_task_summary(old.id, summary)
    ok = seed_task(repo, "ok")
    _finish(repo, ok.id, at=T0)
    repo.set_task_summary(ok.id, summary)
    cwe = seed_task(repo, "errors")
    _finish(repo, cwe.id, "completed_with_errors", at=T0 + 1)
    repo.set_task_summary(cwe.id, {**summary, "total": 30, "passed": 10})
    bare = seed_task(repo, "no summary")
    _finish(repo, bare.id, at=T0 + 2)
    failed = seed_task(repo, "failed")
    _finish(repo, failed.id, "failed", at=T0 + 3)
    repo.set_task_summary(failed.id, summary)
    theirs = seed_task(repo, "theirs", owner=OTHER)
    _finish(repo, theirs.id, at=T0)
    since = T0 - 7 * DAY
    assert repo.finished_results(since=since) == FinishedResults(tasks=3, episodes=80, passed=50)
    assert repo.finished_results(since=T0 + 1) == FinishedResults(tasks=2, episodes=30, passed=10)
    repo.soft_delete_task(cwe.id, at=T0)
    assert repo.finished_results(since=since) == FinishedResults(tasks=2, episodes=50, passed=40)
    assert repo.finished_results(since=since, owner=OTHER).tasks == 1


def test_token_timeline_follows_the_actual_ledger(repo):
    a = seed_task(repo, "a")
    b = seed_task(repo, "b")
    theirs = seed_task(repo, "theirs", owner=OTHER)

    def usage(task, ledger="actual", **kw):
        base = dict(task_id=task.id, ledger=ledger, module_id="task_success", call_kind="probe",
                    model_name="m", prompt_tokens=100, completion_tokens=20, reasoning_tokens=15,
                    cached_tokens=50, requests=1)
        return P.UsageDelta(**{**base, **kw})

    slot0 = token_slot(T0)
    repo.add_usage([usage(a), usage(b), usage(a, ledger="attributed")], at=T0)
    repo.add_usage([usage(a, completion_tokens=0, prompt_tokens=0, requests=0,
                          requests_unknown_usage=1)], at=T0 + 1)          # nothing spent
    repo.add_usage([usage(a, subtask_id="sub_1")], at=slot0 + TOKEN_SLOT_MS)
    repo.add_usage([usage(theirs, prompt_tokens=7, completion_tokens=0)], at=T0)
    repo.add_usage([usage(b)], at=slot0 + 3 * DAY)

    assert repo.token_timeline(since=slot0, until=slot0 + DAY) == [
        (slot0, 240), (slot0 + TOKEN_SLOT_MS, 120)]
    assert repo.token_timeline(since=slot0 + 1, until=slot0 + 4 * DAY) == [
        (slot0 + TOKEN_SLOT_MS, 120), (slot0 + 3 * DAY, 120)]
    assert repo.token_timeline(since=slot0, until=slot0 + 1, owner=OTHER) == [(slot0, 7)]
    assert repo.token_timeline(since=0, until=slot0) == []

    repo.purge_expired(now=slot0 + 91 * DAY)                             # kept 90 days, like events
    assert repo.token_timeline(since=0, until=slot0 + 100 * DAY) == [(slot0 + 3 * DAY, 120)]
