"""C5 conformance suite: every Repository implementation must pass it unchanged.

Only the protocol is used here (``daemon.repo.protocol``); implementation details
(SQL, threads, files) are tested in ``test_repo_sqlite.py``. Implementations are
listed in ``repo_impls.py``.

Behaviour the protocol leaves open is pinned here so every implementation agrees:
``list_events`` is newest first; ``rebind_task_credentials`` treats ``None`` as
"unchanged"; ``soft_delete_task`` also refuses while a subtask is active;
``create_subtask`` checks the parent state table (``SUBTASK_PARENT_STATES``).
"""
from __future__ import annotations

import threading

import pytest

from daemon.pagination import CursorError
from daemon.repo import protocol as P

from .conftest import T0

OTHER = "someone-else"


def _module_rows(selected=("timestamp_check", "task_success")):
    return [P.TaskModule(task_id="", module_id=m, selected=m in selected, availability="available")
            for m in ("timestamp_check", "kinematic_limits", "task_success")]


def _spec(name="droid 前 50 条质检", *, state="queued", owner=P.DEFAULT_OWNER, delivery="tos://b/d",
          input_cred=None, output_cred=None, vlm_model=None):
    return P.TaskCreate(
        name=name, input_source="tos", input_uri="tos://bucket/datasets/droid_lerobot",
        output_uri=delivery, delivery_key=delivery, episode_selector={"mode": "head", "n": 50},
        params={"export": True}, modules=_module_rows(), state=state, owner_id=owner,
        input_cred_id=input_cred, output_cred_id=output_cred, vlm_model_id=vlm_model,
        input_region="cn-beijing")


def _cred(name="prod-tos", *, kind="tos", owner=P.DEFAULT_OWNER, key_version=1):
    return P.Credential(id="", name=name, kind=kind, payload_enc=b"\x00cipher", owner_id=owner,
                        key_version=key_version, payload_meta={"region": "cn-beijing"})


def _drive(repo, task_id, *states, clock_at=T0):
    """Walk a task through legal CAS steps, e.g. _drive(repo, id, 'running', 'succeeded')."""
    for to in states:
        cur = repo.get_task(task_id, include_deleted=True).state
        assert repo.update_task_state(task_id, {cur}, to, at=clock_at), f"{cur} -> {to}"


# ---------------------------------------------------------------------------
# schema and transactions
# ---------------------------------------------------------------------------

def test_schema_version_is_positive(repo):
    assert repo.schema_version() >= 1


def test_transaction_commits_all_or_nothing(repo):
    with repo.transaction():
        a = repo.create_task(_spec("a"))
        b = repo.create_task(_spec("b"))
    assert repo.get_task(a.id).name == "a" and repo.get_task(b.id).name == "b"

    created = []
    with pytest.raises(RuntimeError):
        with repo.transaction():
            created.append(repo.create_task(_spec("c")).id)
            repo.append_event(actor="t", action="x", resource=created[0], at=T0)
            raise RuntimeError("boom")
    with pytest.raises(P.NotFound):
        repo.get_task(created[0])
    assert repo.list_events(resource=created[0]).items == []


def test_nested_transaction_joins_the_outer_one(repo):
    with pytest.raises(RuntimeError):
        with repo.transaction():
            t = repo.create_task(_spec("outer"))
            with repo.transaction():
                repo.update_task_fields(t.id, if_updated_at=None, name="inner")
            assert repo.get_task(t.id).name == "inner"       # reads see the block's own writes
            raise RuntimeError("roll back everything")
    assert repo.list_tasks(page=1, page_size=10).total == 0


def test_uncommitted_writes_are_invisible_to_other_threads(repo):
    seen = {}
    inside = threading.Event()
    release = threading.Event()

    def other():
        inside.wait(10)
        try:
            repo.get_task(seen["id"])
            seen["visible"] = True
        except P.NotFound:
            seen["visible"] = False
        release.set()

    th = threading.Thread(target=other)
    th.start()
    with repo.transaction():
        seen["id"] = repo.create_task(_spec()).id
        inside.set()
        release.wait(10)
    th.join(10)
    assert seen["visible"] is False
    assert repo.get_task(seen["id"]).id == seen["id"]


def test_errors_inside_a_transaction_leave_it_usable(repo):
    t = repo.create_task(_spec())
    with repo.transaction():
        with pytest.raises(P.NotFound):
            repo.get_task("task_missing")
        repo.update_task_fields(t.id, if_updated_at=None, note="still committed")
    assert repo.get_task(t.id).note == "still committed"


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------

def test_credential_crud_and_name_uniqueness(repo, clock):
    c = repo.create_credential(_cred())
    assert c.id and c.created_at == clock() and c.payload_enc == b"\x00cipher"
    assert repo.get_credential(c.id).name == "prod-tos"
    assert repo.get_credential_by_name("prod-tos").id == c.id
    with pytest.raises(P.Conflict) as err:
        repo.create_credential(_cred())
    assert err.value.code == "name_taken"
    repo.create_credential(_cred("same-name-other-owner", owner=OTHER))
    repo.create_credential(_cred("prod-tos", owner=OTHER))          # names are unique per owner
    assert [x.name for x in repo.list_credentials()] == ["prod-tos"]
    assert {x.name for x in repo.list_credentials(owner=OTHER)} == {"prod-tos",
                                                                    "same-name-other-owner"}

    clock.advance(10)
    u = repo.update_credential(c.id, name="renamed", payload_meta={"region": "cn-shanghai"})
    assert (u.name, u.payload_meta, u.payload_enc) == ("renamed", {"region": "cn-shanghai"},
                                                       b"\x00cipher")
    assert u.updated_at > c.updated_at
    u2 = repo.update_credential(c.id, payload_enc=b"new", key_version=2)
    assert (u2.payload_enc, u2.key_version, u2.name) == (b"new", 2, "renamed")
    repo.create_credential(_cred("taken"))
    with pytest.raises(P.Conflict) as err:
        repo.update_credential(c.id, name="taken")
    assert err.value.code == "name_taken"
    with pytest.raises(P.NotFound):
        repo.get_credential(c.id, owner=OTHER)
    with pytest.raises(P.NotFound):
        repo.update_credential("cred_missing", name="x")


def test_credential_kind_filter_and_verification(repo):
    tos = repo.create_credential(_cred("tos"))
    repo.create_credential(_cred("ark-key", kind="ark"))
    assert [c.name for c in repo.list_credentials(kind="tos")] == ["tos"]
    repo.set_credential_verification(tos.id, "failed", T0 + 5, "AccessDenied")
    got = repo.get_credential(tos.id)
    assert (got.verify_state, got.last_verified_at, got.last_verify_error) == \
        ("failed", T0 + 5, "AccessDenied")
    repo.set_credential_verification(tos.id, "ok", T0 + 6, None)
    assert repo.get_credential(tos.id).last_verify_error is None
    with pytest.raises(P.NotFound):
        repo.set_credential_verification("cred_missing", "ok", T0, None)


def test_credential_references_and_delete_rules(repo):
    c = repo.create_credential(_cred())
    running = repo.create_task(_spec("running", input_cred=c.id, output_cred=c.id))
    done = repo.create_task(_spec("done", input_cred=c.id))
    _drive(repo, done.id, "running", "succeeded")
    assert repo.credential_references(c.id) == (1, 1)
    with pytest.raises(P.Conflict) as err:
        repo.delete_credential(c.id)
    assert err.value.code == "credential_in_use"

    _drive(repo, running.id, "running", "failed")
    assert repo.credential_references(c.id) == (0, 2)
    repo.delete_credential(c.id)
    with pytest.raises(P.NotFound):
        repo.get_credential(c.id)
    assert repo.get_task(done.id).input_cred_id is None               # ON DELETE SET NULL
    assert repo.get_task(running.id).output_cred_id is None
    with pytest.raises(P.NotFound):
        repo.delete_credential(c.id)


def test_created_tasks_count_as_unfinished_references(repo):
    c = repo.create_credential(_cred())
    t = repo.create_task(_spec(state="created", input_cred=c.id))
    assert repo.credential_references(c.id) == (1, 0)
    repo.soft_delete_task(t.id, at=T0)
    assert repo.credential_references(c.id) == (0, 1)                # deleted records don't block
    repo.delete_credential(c.id)


def test_credentials_below_key_version_for_rotation(repo):
    old = [repo.create_credential(_cred(f"k{i}")) for i in range(3)]
    repo.create_credential(_cred("new", key_version=2))
    batch = repo.credentials_below_key_version(2, limit=2)
    assert len(batch) == 2 and all(c.key_version == 1 for c in batch)
    assert {c.id for c in repo.credentials_below_key_version(2)} == {c.id for c in old}


# ---------------------------------------------------------------------------
# VLM backends and models
# ---------------------------------------------------------------------------

def _backend(name="ark-prod", models=("doubao-seed-2-0-pro-260215",)):
    return P.VlmBackend(id="", name=name, kind="ark", endpoint="https://ark.example/api/v3",
                        credential_id=None,
                        models=[P.VlmModel(id="", backend_id="", model_name=m) for m in models])


def test_backend_with_key_and_models(repo):
    b = repo.create_vlm_backend(_backend(), _cred("ark-prod-key", kind="ark"))
    assert b.id and b.credential_id and [m.model_name for m in b.models] == \
        ["doubao-seed-2-0-pro-260215"]
    assert repo.get_credential(b.credential_id).kind == "ark"
    assert repo.get_vlm_backend(b.id).name == "ark-prod"
    assert repo.get_vlm_backend_by_name("ark-prod").id == b.id
    assert [x.id for x in repo.list_vlm_backends()] == [b.id]
    assert repo.list_vlm_backends(owner=OTHER) == []
    with pytest.raises(P.Conflict) as err:
        repo.create_vlm_backend(_backend(), None)
    assert err.value.code == "name_taken"
    with pytest.raises(P.NotFound):
        repo.get_vlm_backend("vb_missing")


def test_backend_updates_and_verification(repo):
    b = repo.create_vlm_backend(_backend(), None)
    u = repo.update_vlm_backend(b.id, endpoint="https://vllm.local/v1", max_concurrency=16)
    assert (u.endpoint, u.max_concurrency, u.name) == ("https://vllm.local/v1", 16, "ark-prod")
    repo.create_vlm_backend(_backend("other"), None)
    with pytest.raises(P.Conflict) as err:
        repo.update_vlm_backend(b.id, name="other")
    assert err.value.code == "name_taken"
    repo.set_vlm_backend_verification(b.id, "ok", T0 + 1, None)
    assert repo.get_vlm_backend(b.id).verify_state == "ok"


def test_backend_and_model_edits_are_whitelisted(repo):
    b = repo.create_vlm_backend(_backend(models=("a", "b")), None)
    with pytest.raises(ValueError):
        repo.update_vlm_backend(b.id, owner_id="someone-else")
    with pytest.raises(ValueError):
        repo.update_vlm_model(b.models[0].id, backend_id="vb_other")
    with pytest.raises(P.Conflict) as err:
        repo.update_vlm_model(b.models[0].id, model_name="b")     # unique per backend
    assert err.value.code == "name_taken"
    with pytest.raises(P.NotFound):
        repo.update_vlm_backend("vb_missing", endpoint="x")
    with pytest.raises(P.NotFound):
        repo.update_vlm_backend(b.id, owner=OTHER, endpoint="x")


def test_model_upsert_update_delete(repo):
    b = repo.create_vlm_backend(_backend(models=()), None)
    m = repo.upsert_vlm_model(P.VlmModel(id="", backend_id=b.id, model_name="ep-2026", source="manual"))
    again = repo.upsert_vlm_model(P.VlmModel(id="", backend_id=b.id, model_name="ep-2026",
                                             reasoning_effort="low", source="listed"))
    assert again.id == m.id and again.reasoning_effort == "low" and again.source == "listed"
    u = repo.update_vlm_model(m.id, reasoning_effort=None, max_concurrency=8,
                              capabilities={"vision": True})
    assert (u.reasoning_effort, u.max_concurrency, u.capabilities) == (None, 8, {"vision": True})
    assert [x.id for x in repo.get_vlm_backend(b.id).models] == [m.id]
    repo.delete_vlm_model(m.id)
    assert repo.get_vlm_backend(b.id).models == []
    with pytest.raises(P.NotFound):
        repo.update_vlm_model(m.id, max_concurrency=1)
    with pytest.raises(P.NotFound):
        repo.upsert_vlm_model(P.VlmModel(id="", backend_id="vb_missing", model_name="x"))


def test_backend_and_model_in_use_rules(repo):
    b = repo.create_vlm_backend(_backend(), _cred("key", kind="ark"))
    model = b.models[0]
    t = repo.create_task(_spec(vlm_model=model.id))
    for call in (lambda: repo.delete_vlm_backend(b.id), lambda: repo.delete_vlm_model(model.id)):
        with pytest.raises(P.Conflict) as err:
            call()
        assert err.value.code == "backend_in_use"
    _drive(repo, t.id, "running", "completed_with_errors")
    repo.delete_vlm_backend(b.id)
    with pytest.raises(P.NotFound):
        repo.get_vlm_backend(b.id)
    with pytest.raises(P.NotFound):
        repo.get_credential(b.credential_id)                       # the key goes with its backend
    assert repo.get_task(t.id).vlm_model_id is None                  # finished task survives


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

def test_create_and_get_task(repo, clock):
    t = repo.create_task(_spec(state="created"))
    assert t.id.startswith("task_") and t.state == "created" and t.created_at == clock()
    assert t.updated_at == t.created_at and t.result_rev == 0 and t.deleted_at is None
    assert t.episode_selector == {"mode": "head", "n": 50} and t.params == {"export": True}
    assert t.owner_id == P.DEFAULT_OWNER and t.delivery_stale is False
    mods = {m.module_id: m for m in repo.get_task_modules(t.id)}
    assert set(mods) == {"timestamp_check", "kinematic_limits", "task_success"}
    assert mods["timestamp_check"].selected and not mods["kinematic_limits"].selected
    assert mods["task_success"].state == "pending"
    with pytest.raises(P.NotFound):
        repo.get_task(t.id, owner=OTHER)
    with pytest.raises(P.NotFound):
        repo.get_task("task_missing")
    with pytest.raises(ValueError):
        repo.create_task(_spec(state="running"))


def test_list_tasks_page_numbers_and_total(repo, clock):
    ids = []
    for i in range(23):
        clock.advance(1000)
        ids.append(repo.create_task(_spec(f"task {i:02d}")).id)
    repo.create_task(_spec("not mine", owner=OTHER))
    newest_first = list(reversed(ids))

    pages = [repo.list_tasks(page=p, page_size=10) for p in (1, 2, 3, 4)]
    assert [pg.total for pg in pages] == [23, 23, 23, 23]
    assert [len(pg.items) for pg in pages] == [10, 10, 3, 0]
    assert [t.id for pg in pages for t in pg.items] == newest_first
    assert (pages[1].page, pages[1].page_size) == (2, 10)
    with pytest.raises(ValueError):
        repo.list_tasks(page=0, page_size=10)


def test_list_tasks_filters_keep_total_consistent(repo, clock):
    a = repo.create_task(_spec("droid 前 50 条", delivery="tos://b/droid"))
    clock.advance(1)
    b = repo.create_task(_spec("umi_640 全量", delivery="tos://b/umi"))
    clock.advance(1)
    c = repo.create_task(_spec("DROID 抽检", state="created", delivery="tos://b/droid"))
    _drive(repo, b.id, "running")

    def ids(**kw):
        page = repo.list_tasks(page=1, page_size=20, **kw)
        assert page.total == len(page.items)
        return [t.id for t in page.items]

    assert ids(state="running") == [b.id]
    assert ids(state="created") == [c.id]
    assert ids(q="droid") == [c.id, a.id]                       # name, case-insensitive
    assert ids(q=a.id[-8:]) == [a.id]                            # or a piece of the id
    assert ids(q="%") == [] and ids(q="d_o") == []                # LIKE wildcards are literal
    assert ids(q="umi_6") == [b.id]
    assert ids(delivery_key="tos://b/droid") == [c.id, a.id]
    assert ids(delivery_key="tos://b/droid", state="queued") == [a.id]
    repo.soft_delete_task(c.id, at=T0)
    assert ids(q="droid") == [a.id]
    assert ids(state="deleted") == [c.id]


def test_update_task_fields_if_match(repo, clock):
    t = repo.create_task(_spec(state="created"))
    with pytest.raises(P.PreconditionFailed):
        repo.update_task_fields(t.id, if_updated_at=t.updated_at - 1, name="x")
    u = repo.update_task_fields(t.id, if_updated_at=t.updated_at, name="renamed",
                                params={"export": False}, episode_selector={"mode": "all"})
    assert (u.name, u.params, u.episode_selector) == ("renamed", {"export": False}, {"mode": "all"})
    assert u.updated_at > t.updated_at                                # even within one millisecond
    with pytest.raises(P.PreconditionFailed):
        repo.update_task_fields(t.id, if_updated_at=t.updated_at, note="stale window")
    u2 = repo.update_task_fields(t.id, if_updated_at=None, note=None, preflight={"x": 1})
    assert u2.note is None and u2.preflight == {"x": 1}
    with pytest.raises(ValueError):
        repo.update_task_fields(t.id, if_updated_at=None, state="queued")
    with pytest.raises(P.NotFound):
        repo.update_task_fields(t.id, if_updated_at=None, owner=OTHER, name="x")


def test_task_state_cas(repo):
    t = repo.create_task(_spec())
    assert not repo.update_task_state(t.id, {"running"}, "pausing", at=T0)   # state is queued
    assert repo.get_task(t.id).state == "queued"
    assert repo.update_task_state(t.id, {"queued"}, "running", at=T0 + 1)
    got = repo.get_task(t.id)
    assert got.started_at == T0 + 1 and got.finished_at is None and got.updated_at >= T0 + 1
    assert repo.update_task_state(t.id, {"running"}, "pausing", pause_reason="user", at=T0 + 2)
    assert repo.update_task_state(t.id, {"pausing"}, "paused", at=T0 + 3)
    got = repo.get_task(t.id)
    assert (got.state, got.pause_reason) == ("paused", "user")        # kept through pausing -> paused
    assert repo.update_task_state(t.id, {"paused"}, "queued", at=T0 + 4)
    assert repo.get_task(t.id).pause_reason is None                    # only set while pausing/paused
    _drive(repo, t.id, "running")
    assert repo.update_task_state(t.id, {"running", "pausing"}, "stopping", reason="用户停止", at=T0 + 5)
    assert repo.update_task_state(t.id, {"stopping"}, "stopped", reason="用户停止", at=T0 + 6)
    got = repo.get_task(t.id)
    assert (got.state, got.state_reason, got.finished_at, got.started_at) == \
        ("stopped", "用户停止", T0 + 6, T0 + 1)


def test_illegal_transitions_are_programming_errors(repo):
    t = repo.create_task(_spec())
    for frm, to in (({"running"}, "queued"), ({"failed"}, "queued"), ({"queued", "running"}, "paused"),
                    ({"stopped"}, "succeeded")):
        with pytest.raises(ValueError):
            repo.update_task_state(t.id, frm, to, at=T0)


def test_terminal_recompute_keeps_first_finish_time(repo):
    t = repo.create_task(_spec())
    _drive(repo, t.id, "running")
    assert repo.update_task_state(t.id, {"running"}, "completed_with_errors", at=T0 + 10)
    assert repo.update_task_state(t.id, {"completed_with_errors"}, "succeeded", at=T0 + 99)
    got = repo.get_task(t.id)
    assert (got.state, got.finished_at) == ("succeeded", T0 + 10)


def test_concurrent_cas_has_exactly_one_winner(repo):
    t = repo.create_task(_spec())
    wins = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait(10)
        wins.append(repo.update_task_state(t.id, {"queued"}, "running", at=T0))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    assert sorted(wins) == [False] * 7 + [True]


def test_progress_summary_revision_export_freeze(repo):
    t = repo.create_task(_spec())
    before = repo.get_task(t.id).updated_at
    repo.set_task_progress(t.id, {"stages": [{"id": "numeric", "state": "running", "done": 3,
                                              "total": 50}]})
    repo.set_task_summary(t.id, {"total": 50, "passed": 41})
    got = repo.get_task(t.id)
    assert got.progress["stages"][0]["done"] == 3 and got.summary["passed"] == 41
    assert got.updated_at == before                  # runtime data does not break If-Match

    assert repo.switch_result_rev(t.id, 0, 1)
    assert not repo.switch_result_rev(t.id, 0, 1)
    assert repo.get_task(t.id).result_rev == 1
    with pytest.raises(ValueError):
        repo.switch_result_rev(t.id, 1, 1)

    repo.set_export_fingerprint(t.id, "sha256:abc", delivery_stale=True)
    got = repo.get_task(t.id)
    assert (got.export_fingerprint, got.delivery_stale) == ("sha256:abc", True)

    repo.freeze_task_inputs(t.id, run_id="20260920-130514", preflight={"format": "v2"},
                            source_fingerprint={"objects": 204}, vlm_snapshot=None)
    got = repo.get_task(t.id)
    assert (got.run_id, got.preflight, got.source_fingerprint, got.vlm_snapshot) == \
        ("20260920-130514", {"format": "v2"}, {"objects": 204}, None)
    for call in (lambda: repo.set_task_progress("task_missing", {}),
                 lambda: repo.switch_result_rev("task_missing", 0, 1)):
        with pytest.raises(P.NotFound):
            call()


def test_rebind_credentials_one_side_at_a_time(repo):
    old = repo.create_credential(_cred("old"))
    new = repo.create_credential(_cred("new"))
    t = repo.create_task(_spec(input_cred=old.id, output_cred=old.id))
    got = repo.rebind_task_credentials(t.id, input_cred_id=None, output_cred_id=new.id)
    assert (got.input_cred_id, got.output_cred_id) == (old.id, new.id)
    got = repo.rebind_task_credentials(t.id, input_cred_id=new.id, output_cred_id=None)
    assert (got.input_cred_id, got.output_cred_id) == (new.id, new.id)


def test_soft_delete_restore_purge(repo, clock):
    running = repo.create_task(_spec("running"))
    _drive(repo, running.id, "running")
    with pytest.raises(P.StateConflict):
        repo.soft_delete_task(running.id, at=T0)

    created = repo.create_task(_spec("created", state="created"))
    repo.soft_delete_task(created.id, at=T0 + 1)
    with pytest.raises(P.NotFound):
        repo.get_task(created.id)
    assert repo.get_task(created.id, include_deleted=True).deleted_at == T0 + 1
    with pytest.raises(P.NotFound):
        repo.soft_delete_task(created.id, at=T0 + 2)                  # already gone
    assert repo.restore_task(created.id).deleted_at is None
    assert repo.get_task(created.id).state == "created"

    done = repo.create_task(_spec("done"))
    _drive(repo, done.id, "running", "succeeded")
    repo.create_subtask(P.Subtask(id="", task_id=done.id, kind="reexport", scope={}, state="queued"))
    with pytest.raises(P.StateConflict):
        repo.soft_delete_task(done.id, at=T0)                           # its subtask is still active
    sub = repo.active_subtask(done.id)
    _sub_drive(repo, sub.id, "running", "succeeded")
    repo.append_adjudication([P.AdjudicationCreate(task_id=done.id, episode_index=3, line="label",
                                                   decision="keep_label", decided_by="alice")], at=T0)
    repo.add_usage([P.UsageDelta(task_id=done.id, ledger="actual", module_id="task_success",
                                 call_kind="probe", model_name="m", requests=1)], at=T0)
    repo.soft_delete_task(done.id, at=T0 + 10)
    repo.soft_delete_task(created.id, at=T0 + 50)

    assert repo.purge_deleted_tasks(before=T0 + 20) == 1               # only the older one
    with pytest.raises(P.NotFound):
        repo.get_task(done.id, include_deleted=True)
    assert repo.list_subtasks(done.id) == []
    assert repo.latest_adjudications(done.id) == []
    assert repo.usage_buckets(done.id, ledger="actual") == []
    assert repo.get_task(created.id, include_deleted=True).deleted_at == T0 + 50
    with pytest.raises(P.NotFound):
        repo.restore_task(done.id)


def test_tasks_in_states_for_reconciliation(repo, clock):
    a = repo.create_task(_spec("a"))
    clock.advance(1)
    b = repo.create_task(_spec("b"))
    clock.advance(1)
    repo.create_task(_spec("c", state="created"))
    _drive(repo, b.id, "running")
    assert [t.id for t in repo.tasks_in_states({"queued", "running"})] == [a.id, b.id]
    assert repo.tasks_in_states([]) == []


# ---------------------------------------------------------------------------
# task modules
# ---------------------------------------------------------------------------

def test_modules_upsert_cas_and_stale(repo):
    t = repo.create_task(_spec())
    repo.upsert_task_modules(t.id, [
        P.TaskModule(task_id=t.id, module_id="task_success", selected=True, availability="available",
                     params={"evidence_frames": "all"}),
        P.TaskModule(task_id=t.id, module_id="dedup", selected=True, availability="available")])
    mods = {m.module_id: m for m in repo.get_task_modules(t.id)}
    assert len(mods) == 4 and mods["task_success"].params == {"evidence_frames": "all"}

    assert repo.update_module_state(t.id, "task_success", {"pending"}, "running", started_at=T0)
    assert not repo.update_module_state(t.id, "task_success", {"pending"}, "running")
    assert repo.update_module_state(t.id, "task_success", {"running"}, "completed_with_errors",
                                    episodes_total=49, episodes_error=2, finished_at=T0 + 9,
                                    input_digest="sha256:keep")
    assert repo.update_module_state(t.id, "dedup", {"pending"}, "succeeded")
    repo.mark_modules_stale(t.id, ["task_success", "dedup", "timestamp_check"])
    mods = {m.module_id: m for m in repo.get_task_modules(t.id)}
    assert mods["task_success"].state == "stale" and mods["dedup"].state == "stale"
    assert mods["timestamp_check"].state == "pending"                  # nothing to go stale
    assert (mods["task_success"].episodes_total, mods["task_success"].episodes_error) == (49, 2)
    with pytest.raises(ValueError):
        repo.update_module_state(t.id, "dedup", {"stale"}, "running", state_reason="nope")
    with pytest.raises(P.NotFound):
        repo.upsert_task_modules("task_missing", [])


# ---------------------------------------------------------------------------
# subtasks
# ---------------------------------------------------------------------------

def _sub_drive(repo, sub_id, *states):
    for to in states:
        cur = repo.get_subtask(sub_id).state
        assert repo.update_subtask_state(sub_id, {cur}, to, at=T0), f"{cur} -> {to}"


def test_subtask_rules(repo, clock):
    t = repo.create_task(_spec())
    with pytest.raises(P.StateConflict):                                 # parent still queued
        repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="retry", scope={}, state="queued"))
    _drive(repo, t.id, "running", "completed_with_errors")
    s1 = repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="retry", state="queued",
                                       scope={"modules": ["task_success"], "episodes": "errors"}))
    assert s1.id.startswith("sub_") and s1.created_at == clock() and s1.scope["episodes"] == "errors"
    with pytest.raises(P.Conflict) as err:
        repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="reexport", scope={},
                                      state="queued"))
    assert err.value.code == "subtask_active"
    assert repo.active_subtask(t.id).id == s1.id
    assert not repo.update_subtask_state(s1.id, {"running"}, "succeeded", at=T0)
    _sub_drive(repo, s1.id, "running")
    assert repo.get_subtask(s1.id).started_at == T0
    repo.set_subtask_progress(s1.id, {"stages": []})
    _sub_drive(repo, s1.id, "succeeded")
    assert repo.active_subtask(t.id) is None and repo.get_subtask(s1.id).finished_at == T0

    clock.advance(5)
    s2 = repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="apply_adjudication", scope={},
                                       state="queued"))
    assert [s.id for s in repo.list_subtasks(t.id)] == [s1.id, s2.id]
    with pytest.raises(ValueError):
        repo.update_subtask_state(s2.id, {"queued"}, "created", at=T0)
    with pytest.raises(ValueError):
        repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="retry", scope={}, state="created"))
    with pytest.raises(P.NotFound):
        repo.get_subtask("sub_missing")


def test_resume_needs_a_stopped_or_failed_parent(repo):
    t = repo.create_task(_spec())
    _drive(repo, t.id, "running", "failed")
    s = repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="resume", scope={}, state="queued"))
    assert s.kind == "resume"


# ---------------------------------------------------------------------------
# token usage
# ---------------------------------------------------------------------------

def test_usage_accumulates_per_bucket_and_ledger(repo):
    t = repo.create_task(_spec())

    def delta(ledger="actual", **kw):
        base = dict(task_id=t.id, ledger=ledger, module_id="task_success", call_kind="probe",
                    model_name="doubao", prompt_tokens=100, completion_tokens=10, requests=1)
        base.update(kw)
        return P.UsageDelta(**base)

    repo.add_usage([delta(), delta(cached_tokens=50)], at=T0)
    repo.add_usage([delta(requests=0, requests_unknown_usage=2, prompt_tokens=0,
                          completion_tokens=0)], at=T0 + 5)
    repo.add_usage([delta(subtask_id="sub_1"), delta(ledger="attributed", call_kind="merged")],
                   at=T0 + 6)
    actual = repo.usage_buckets(t.id, ledger="actual")
    assert [(b.subtask_id, b.requests) for b in actual] == [("", 2), ("sub_1", 1)]
    main = actual[0]
    assert (main.prompt_tokens, main.cached_tokens, main.requests_unknown_usage, main.updated_at) == \
        (200, 50, 2, T0 + 5)
    attributed = repo.usage_buckets(t.id, ledger="attributed")
    assert [(b.call_kind, b.prompt_tokens) for b in attributed] == [("merged", 100)]
    repo.add_usage([], at=T0)


# ---------------------------------------------------------------------------
# adjudication (append only, the latest row wins)
# ---------------------------------------------------------------------------

def test_adjudication_last_writer_wins(repo):
    t = repo.create_task(_spec())

    def row(ep, line, decision, **kw):
        return P.AdjudicationCreate(task_id=t.id, episode_index=ep, line=line, decision=decision,
                                    decided_by="alice", **kw)

    first = repo.append_adjudication([row(29, "label", "adopt_suggestion"),
                                      row(29, "task_verdict", "unsure"),
                                      row(7, "task_verdict", "success", note="看清楚了")], at=T0)
    assert [a.decided_at for a in first] == [T0] * 3 and first[0].id < first[1].id
    later = repo.append_adjudication([row(29, "label", "custom_label", new_label="pour rice")],
                                     at=T0 + 1)
    latest = repo.latest_adjudications(t.id)
    assert [(a.episode_index, a.line, a.decision) for a in latest] == [
        (7, "task_verdict", "success"), (29, "label", "custom_label"), (29, "task_verdict", "unsure")]
    assert repo.latest_adjudications(t.id, line="label")[0].new_label == "pour rice"

    sub_ids = [a.id for a in latest]
    _drive(repo, t.id, "running", "succeeded")
    s = repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="apply_adjudication", scope={},
                                      state="queued"))
    assert repo.mark_adjudications_applied(sub_ids, s.id) == 3
    assert repo.mark_adjudications_applied(sub_ids, s.id) == 0          # already applied
    assert repo.latest_adjudications(t.id, unapplied_only=True) == []
    repo.append_adjudication([row(7, "task_verdict", "failure")], at=T0 + 2)   # a change of mind
    pending = repo.latest_adjudications(t.id, unapplied_only=True)
    assert [(a.episode_index, a.decision) for a in pending] == [(7, "failure")]
    assert later[0].id in {a.id for a in repo.latest_adjudications(t.id)}


# ---------------------------------------------------------------------------
# events: cursor pagination
# ---------------------------------------------------------------------------

def test_events_newest_first_with_cursor(repo):
    ids = [repo.append_event(actor="alice", action="task.create", resource=f"task_{i % 2}", at=T0 + i,
                             detail={"i": i}).id for i in range(7)]
    repo.append_event(actor="bob", action="x", resource="task_0", at=T0, owner=OTHER)
    page = repo.list_events(limit=3)
    assert [e.id for e in page.items] == ids[::-1][:3] and page.has_more and page.next_cursor
    rest = repo.list_events(limit=3, cursor=page.next_cursor)
    last = repo.list_events(limit=3, cursor=rest.next_cursor)
    assert [e.id for e in rest.items + last.items] == ids[::-1][3:]
    assert not last.has_more and last.next_cursor is None
    only0 = repo.list_events(resource="task_0", limit=50)
    assert [e.detail["i"] for e in only0.items] == [6, 4, 2, 0]
    with pytest.raises(CursorError):                                   # issued for other filters
        repo.list_events(resource="task_1", cursor=page.next_cursor)
    with pytest.raises(CursorError):
        repo.list_events(cursor="not-a-cursor")
    with pytest.raises(ValueError):
        repo.list_events(limit=0)
    assert repo.list_events(owner=OTHER).items[0].actor == "bob"


def test_event_cursor_has_no_duplicates_or_gaps_under_concurrent_inserts(repo):
    existing = {repo.append_event(actor="a", action="seed", resource="r", at=T0).id for _ in range(60)}
    stop = threading.Event()
    added = set()

    def writer():
        while not stop.is_set():
            added.add(repo.append_event(actor="w", action="live", resource="r", at=T0).id)

    th = threading.Thread(target=writer)
    th.start()
    try:
        seen, cursor = [], None
        while True:
            page = repo.list_events(resource="r", limit=7, cursor=cursor)
            seen += [e.id for e in page.items]
            if not page.has_more:
                break
            cursor = page.next_cursor
    finally:
        stop.set()
        th.join(10)
    assert len(seen) == len(set(seen)), "a row came back twice"
    assert existing <= set(seen), "a row that existed before paging started was skipped"
    assert set(seen) - existing <= added
    assert seen == sorted(seen, reverse=True)


def test_purge_events(repo):
    repo.append_event(actor="a", action="old", resource="r", at=T0)
    keep = repo.append_event(actor="a", action="new", resource="r", at=T0 + 100)
    assert repo.purge_events(before=T0 + 50) == 1
    assert [e.id for e in repo.list_events().items] == [keep.id]


# ---------------------------------------------------------------------------
# preflight cache and idempotency keys
# ---------------------------------------------------------------------------

def test_preflight_cache_max_age_and_owner(repo):
    pf = repo.put_preflight(request_hash="sha256:req", result={"schema_version": "1.0"}, at=T0)
    assert pf.startswith("pf_")
    assert repo.get_preflight(pf, max_age_ms=30 * 60_000, now=T0 + 60_000) == {"schema_version": "1.0"}
    assert repo.get_preflight(pf, max_age_ms=30 * 60_000, now=T0 + 31 * 60_000) is None
    assert repo.get_preflight(pf, max_age_ms=30 * 60_000, now=T0, owner=OTHER) is None
    assert repo.get_preflight("pf_missing", max_age_ms=1, now=T0) is None


def test_idempotency_records_and_expiry(repo):
    rec = P.IdempotencyRecord(key="k-12345678", route="createTask",
                              response={"status": 201, "body": {"id": "task_1"}}, created_at=T0)
    repo.put_idempotent(rec)
    got = repo.get_idempotent(key="k-12345678", route="createTask")
    assert got.response == rec.response and got.created_at == T0
    assert repo.get_idempotent(key="k-12345678", route="taskAction") is None
    assert repo.get_idempotent(key="k-12345678", route="createTask", owner=OTHER) is None
    repo.put_idempotent(P.IdempotencyRecord(key="k-12345678", route="createTask",
                                            response={"status": 201, "body": {"id": "task_2"}},
                                            created_at=T0 + 1))
    assert repo.get_idempotent(key="k-12345678", route="createTask").response["body"]["id"] == "task_2"

    repo.put_preflight(request_hash="h", result={}, at=T0)
    assert repo.purge_expired(now=T0 + 25 * 3600 * 1000) == 2
    assert repo.get_idempotent(key="k-12345678", route="createTask") is None
