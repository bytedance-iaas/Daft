"""C5 conformance suite: every Repository implementation must pass it unchanged.

Only the protocol is used here (``daemon.repo.protocol``); implementation details
(SQL, threads, files) are tested in ``test_repo_sqlite.py``. Implementations are
listed in ``repo_impls.py``.

Behaviour the protocol leaves open is pinned here so every implementation agrees:
``list_events`` is newest first; ``rebind_task_credentials`` treats ``None`` as
"unchanged"; ``soft_delete_task`` refuses with ``Conflict('subtask_active')`` while a
subtask is active; ``create_subtask`` checks the parent state table
(``SUBTASK_PARENT_STATES``). Beyond C5 1.2 (proposed for 1.3): a task that ends again
after a resume takes the new ``finished_at``; ``update_dataset`` takes the refreshed
preflight, both fingerprints and ``preflighted_at`` together and sets ``check_state``
back to ``ok``; a ``repreflight`` check leaves the dataset ``ok`` whatever it found;
a task may only point at a dataset of its own owner.
"""
from __future__ import annotations

import threading

import pytest

from daemon.pagination import CursorError
from daemon.repo import protocol as P

from .conftest import LISTING_DIGEST, META_DIGEST, T0, sample_preflight

OTHER = "someone-else"


def _module_rows(selected=("timestamp_check", "task_success")):
    return [P.TaskModule(task_id="", module_id=m, selected=m in selected, availability="available")
            for m in ("timestamp_check", "kinematic_limits", "task_success")]


def _spec(name="droid 前 50 条质检", *, state="queued", owner=P.DEFAULT_OWNER, delivery="tos://b/d",
          input_cred=None, output_cred=None, vlm_model=None, dataset=None, selected=None):
    modules = _module_rows() if selected is None else _module_rows(selected)
    return P.TaskCreate(
        name=name, input_source="tos", input_uri="tos://bucket/datasets/droid_lerobot",
        output_uri=delivery, delivery_key=delivery, episode_selector={"mode": "head", "n": 50},
        params={"export": True}, modules=modules, state=state, owner_id=owner,
        input_cred_id=input_cred, output_cred_id=output_cred, vlm_model_id=vlm_model,
        input_region="cn-beijing", dataset_id=dataset)


def _dataset(uri="tos://bucket/datasets/droid_lerobot", *, name=None, region="cn-beijing",
             source="tos", owner=P.DEFAULT_OWNER, cred=None, at=T0, note=None, **preflight):
    return P.Dataset(id="", name=name or uri.rsplit("/", 1)[-1], source=source, uri=uri,
                     region=region, credential_id=cred, owner_id=owner, note=note,
                     preflight=sample_preflight(**preflight), meta_fingerprint=META_DIGEST,
                     source_fingerprint={"objects": 204, "bytes": 1024, "digest": LISTING_DIGEST},
                     preflighted_at=at)


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


def test_a_method_failing_halfway_inside_a_transaction_leaves_nothing(repo):
    """Each method stays atomic in a caller's block, even when the caller catches its error."""
    repo.create_vlm_backend(_backend("taken"), None)
    t = repo.create_task(_spec())
    with repo.transaction():
        with pytest.raises(P.Conflict):
            repo.create_vlm_backend(_backend("taken"), _cred("new-key", kind="ark"))
        with pytest.raises(P.NotFound):
            repo.upsert_task_modules("task_missing", _module_rows())
        repo.update_task_fields(t.id, if_updated_at=None, note="kept")
    with pytest.raises(P.NotFound):
        repo.get_credential_by_name("new-key")                  # the key did not slip through
    assert repo.get_task(t.id).note == "kept"


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
# datasets (D36, D37)
# ---------------------------------------------------------------------------

def test_register_dataset_gets_or_creates_by_address(repo, clock):
    ds, created = repo.register_dataset(_dataset(note="抽检用"))
    assert created and ds.id.startswith("ds_") and ds.created_at == clock() == ds.updated_at
    assert (ds.name, ds.note, ds.check_state, ds.checked_at, ds.region) == \
        ("droid_lerobot", "抽检用", "ok", None, "cn-beijing")
    assert ds.preflight["dataset"]["episode_count"] == 200 and ds.meta_fingerprint == META_DIGEST
    assert ds.source_fingerprint == {"objects": 204, "bytes": 1024, "digest": LISTING_DIGEST}
    assert repo.get_dataset(ds.id).uri == "tos://bucket/datasets/droid_lerobot"

    clock.advance(10)
    again, created = repo.register_dataset(_dataset(name="another name", version="v3"))
    assert not created and again.id == ds.id and again.name == "droid_lerobot"   # unchanged
    assert again.preflight["format"]["version"] == "v2"
    other_region, created = repo.register_dataset(_dataset(region="cn-shanghai"))
    assert created and other_region.id != ds.id
    public, created = repo.register_dataset(_dataset(source="public", region=None))
    assert created
    same, created = repo.register_dataset(_dataset(source="public", region=""))   # "" == no region
    assert not created and same.id == public.id and same.region is None
    theirs, created = repo.register_dataset(_dataset(owner=OTHER))
    assert created and theirs.id != ds.id
    with pytest.raises(P.NotFound):
        repo.get_dataset(theirs.id)
    with pytest.raises(P.NotFound):
        repo.get_dataset("ds_missing")


def test_list_datasets_pages_filters_and_total(repo, clock):
    ids = []
    for i in range(12):
        clock.advance(1000)
        ids.append(repo.register_dataset(_dataset(f"tos://bucket/sets/set_{i:02d}"))[0].id)
    repo.register_dataset(_dataset("tos://bucket/sets/theirs", owner=OTHER))
    pages = [repo.list_datasets(page=p, page_size=5) for p in (1, 2, 3, 4)]
    assert [pg.total for pg in pages] == [12] * 4
    assert [d.id for pg in pages for d in pg.items] == ids[::-1]            # newest first
    assert pages[3].items == []
    with pytest.raises(ValueError):
        repo.list_datasets(page=0, page_size=5)

    clock.advance(1000)
    v3 = repo.register_dataset(_dataset("tos://bucket/other/umi_640", name="UMI 640",
                                        version="v3"))[0].id
    clock.advance(1000)
    bad = repo.register_dataset(_dataset("tos://bucket/other/broken", version="v3",
                                         supported=False))[0].id

    def ids_of(**kw):
        page = repo.list_datasets(page=1, page_size=50, **kw)
        assert page.total == len(page.items)
        return [d.id for d in page.items]

    assert ids_of(fmt="lerobot_v3") == [v3]
    assert ids_of(fmt="unsupported") == [bad]
    assert len(ids_of(fmt="lerobot_v2")) == 12
    assert ids_of(q="umi") == [v3]                                   # name, case-insensitive
    assert ids_of(q="other/br") == [bad]                             # or a piece of the address
    assert ids_of(q="set_1") == [ids[11], ids[10]]
    assert ids_of(q="%") == [] and ids_of(q="set_0_") == []            # wildcards are literal
    repo.record_dataset_check(P.DatasetCheck(dataset_id=ids[3], at=T0, trigger="recheck",
                                             result="changed", change={"meta_changed": True}))
    assert ids_of(check_state="changed") == [ids[3]]
    assert ids_of(check_state="changed", q="set_1") == []
    assert len(ids_of(check_state="ok")) == 13


def test_update_dataset_renames_or_refreshes(repo, clock):
    ds, _ = repo.register_dataset(_dataset(note="old note"))
    clock.advance(5)
    u = repo.update_dataset(ds.id, name="DROID 全量", note=None)
    assert (u.name, u.note, u.updated_at > ds.updated_at) == ("DROID 全量", None, True)
    assert repo.update_dataset(ds.id).updated_at == u.updated_at        # nothing to change
    repo.record_dataset_check(P.DatasetCheck(dataset_id=ds.id, at=T0 + 1, trigger="recheck",
                                             result="changed"))
    assert repo.get_dataset(ds.id).check_state == "changed"

    fresh = sample_preflight(version="v3", episodes=210)
    r = repo.update_dataset(ds.id, preflight=fresh, meta_fingerprint="sha256:" + "c" * 64,
                            source_fingerprint={"objects": 210, "bytes": 2048, "digest": "d"},
                            preflighted_at=T0 + 2, manifest_path="datasets/ds/manifest.json")
    assert (r.check_state, r.preflighted_at, r.manifest_path) == ("ok", T0 + 2,
                                                                  "datasets/ds/manifest.json")
    assert r.preflight["dataset"]["episode_count"] == 210 and r.source_fingerprint["objects"] == 210
    assert repo.list_datasets(page=1, page_size=5, fmt="lerobot_v3").total == 1   # format follows
    assert r.name == "DROID 全量"

    for bad in ({"preflight": fresh}, {"meta_fingerprint": "x", "source_fingerprint": {}},
                {"manifest_path": "x"}, {"check_state": "ok"}, {"owner_id": OTHER},
                {"uri": "tos://b/other"}):
        with pytest.raises(ValueError):
            repo.update_dataset(ds.id, **bad)
    with pytest.raises(P.NotFound):
        repo.update_dataset(ds.id, owner=OTHER, name="x")
    with pytest.raises(P.NotFound):
        repo.update_dataset("ds_missing", name="x")


def test_dataset_checks_are_the_change_history(repo):
    ds, _ = repo.register_dataset(_dataset())
    change = {"meta_changed": False, "added": 12, "removed": 0, "modified": 1,
              "sample_keys": ["data/chunk-000/episode_000200.parquet"], "preflighted_at": T0}
    first = repo.record_dataset_check(P.DatasetCheck(dataset_id=ds.id, at=T0 + 1, trigger="add",
                                                     result="same"))
    assert first.id is not None and first.change is None
    got = repo.get_dataset(ds.id)
    assert (got.check_state, got.checked_at) == ("ok", T0 + 1)
    repo.record_dataset_check(P.DatasetCheck(dataset_id=ds.id, at=T0 + 2, trigger="task_start",
                                             result="changed", change=change))
    got = repo.get_dataset(ds.id)
    assert (got.check_state, got.checked_at) == ("changed", T0 + 2)
    repo.record_dataset_check(P.DatasetCheck(dataset_id=ds.id, at=T0 + 3, trigger="repreflight",
                                             result="changed", change=change))
    assert repo.get_dataset(ds.id).check_state == "ok"         # a repreflight is the new baseline
    repo.record_dataset_check(P.DatasetCheck(dataset_id=ds.id, at=T0 + 4, trigger="recheck",
                                             result="same"))

    history = repo.list_dataset_checks(ds.id)
    assert [(c.trigger, c.result) for c in history] == [
        ("recheck", "same"), ("repreflight", "changed"), ("task_start", "changed"), ("add", "same")]
    assert history[2].change == change and history[2].dataset_id == ds.id
    assert [c.at for c in repo.list_dataset_checks(ds.id, limit=2)] == [T0 + 4, T0 + 3]
    assert repo.list_dataset_checks("ds_missing") == []
    late = repo.record_dataset_check(P.DatasetCheck(dataset_id=ds.id, at=T0 + 1, trigger="recheck",
                                                    result="changed"))       # arrives out of order
    got = repo.get_dataset(ds.id)
    assert (got.check_state, got.checked_at) == ("ok", T0 + 4)        # the newer check stands
    assert late.id in {c.id for c in repo.list_dataset_checks(ds.id)}  # but it is in the history
    with pytest.raises(P.NotFound):
        repo.record_dataset_check(P.DatasetCheck(dataset_id="ds_missing", at=T0, trigger="add",
                                                 result="same"))
    with pytest.raises(ValueError):
        repo.record_dataset_check(P.DatasetCheck(dataset_id=ds.id, at=T0, trigger="add",
                                                 result="different"))
    with pytest.raises(ValueError):
        repo.list_dataset_checks(ds.id, limit=0)


def test_delete_dataset_rules(repo):
    ds, _ = repo.register_dataset(_dataset())
    created = repo.create_task(_spec("created", state="created", dataset=ds.id))
    queued = repo.create_task(_spec("queued", dataset=ds.id))
    done = repo.create_task(_spec("done", dataset=ds.id))
    _drive(repo, done.id, "running", "succeeded")
    repo.record_dataset_check(P.DatasetCheck(dataset_id=ds.id, at=T0, trigger="add", result="same"))

    with pytest.raises(P.Conflict) as err:
        repo.delete_dataset(ds.id)
    assert err.value.code == "dataset_in_use"
    _drive(repo, queued.id, "stopped")
    with pytest.raises(P.Conflict):
        repo.delete_dataset(ds.id)                                # the created one still counts
    repo.soft_delete_task(created.id, at=T0)
    with pytest.raises(P.NotFound):
        repo.delete_dataset(ds.id, owner=OTHER)
    repo.delete_dataset(ds.id)
    with pytest.raises(P.NotFound):
        repo.get_dataset(ds.id)
    assert repo.list_dataset_checks(ds.id) == []
    for t in (created, queued, done):                             # the tasks keep their input
        got = repo.get_task(t.id, include_deleted=True)
        assert got.dataset_id is None and got.input_uri == "tos://bucket/datasets/droid_lerobot"
    with pytest.raises(P.NotFound):
        repo.delete_dataset(ds.id)


def test_a_deleted_access_key_leaves_the_dataset_without_one(repo):
    """Registrations never block deleting an access key (only unfinished tasks do);
    like a finished task, the registration then has no key until one is bound again."""
    c = repo.create_credential(_cred())
    ds, _ = repo.register_dataset(_dataset(cred=c.id))
    assert ds.credential_id == c.id
    draft = repo.create_task(_spec("draft on the dataset", state="created", dataset=ds.id))
    assert repo.credential_references(c.id) == (0, 0)                # datasets are not counted
    repo.delete_credential(c.id)
    assert repo.get_dataset(ds.id).credential_id is None
    assert repo.get_task(draft.id).dataset_id == ds.id              # the task keeps its link
    other = repo.create_credential(_cred("new-key"))
    assert repo.update_dataset(ds.id, credential_id=other.id).credential_id == other.id
    with pytest.raises(P.NotFound):
        repo.update_dataset(ds.id, credential_id="cred_missing")
    assert repo.get_dataset(ds.id).credential_id == other.id
    assert repo.update_dataset(ds.id, credential_id=None).credential_id is None


def test_a_registration_reads_with_an_access_key_of_its_owner(repo):
    ark = repo.create_vlm_backend(_backend("ark"), _cred("vlm-backend/vb_ark", kind="ark"))
    theirs = repo.create_credential(_cred("their-key", owner=OTHER))
    mine = repo.create_credential(_cred("my-key"))
    ds, _ = repo.register_dataset(_dataset("tos://bucket/x", cred=mine.id))
    for cred in (ark.credential_id, theirs.id, "cred_missing"):
        with pytest.raises(P.NotFound):
            repo.register_dataset(_dataset(cred=cred))
        with pytest.raises(P.NotFound):
            repo.update_dataset(ds.id, credential_id=cred)
    assert [d.id for d in repo.list_datasets(page=1, page_size=10).items] == [ds.id]
    assert repo.get_dataset(ds.id).credential_id == mine.id


def test_register_and_update_refuse_malformed_rows(repo):
    for bad in ({"id": "custom-id"}, {"id": "ds_with-dash"}, {"name": None},
                {"meta_fingerprint": None}):
        spec = _dataset()
        for k, v in bad.items():
            setattr(spec, k, v)
        with pytest.raises(ValueError):
            repo.register_dataset(spec)
    given = _dataset()
    given.id = "ds_01GIVEN"
    ds, created = repo.register_dataset(given)
    assert created and ds.id == "ds_01GIVEN"
    other = _dataset("tos://bucket/elsewhere")
    other.id = "ds_01GIVEN"
    with pytest.raises(ValueError):                                     # the id is taken
        repo.register_dataset(other)
    for bad in ({"name": None}, {"preflight": None, "meta_fingerprint": "m",
                                 "source_fingerprint": {}, "preflighted_at": T0}):
        with pytest.raises(ValueError):
            repo.update_dataset(ds.id, **bad)
    assert repo.get_dataset(ds.id).name == "droid_lerobot"


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


def test_list_tasks_by_dataset_and_selected_modules(repo, clock):
    ds, _ = repo.register_dataset(_dataset())
    both = repo.create_task(_spec("both", dataset=ds.id, selected=("timestamp_check", "task_success")))
    clock.advance(1)
    ts_only = repo.create_task(_spec("ts only", selected=("timestamp_check",)))
    clock.advance(1)
    all_three = repo.create_task(_spec("all", dataset=ds.id,
                                       selected=("timestamp_check", "kinematic_limits", "task_success")))

    def ids(**kw):
        page = repo.list_tasks(page=1, page_size=20, **kw)
        assert page.total == len(page.items)
        return [t.id for t in page.items]

    assert ids(dataset_id=ds.id) == [all_three.id, both.id]
    assert ids(dataset_id="ds_missing") == []
    assert ids(modules=["task_success"]) == [all_three.id, both.id]
    assert ids(modules=["task_success", "timestamp_check"]) == [all_three.id, both.id]
    assert ids(modules=["kinematic_limits", "task_success"]) == [all_three.id]  # every one of them
    assert ids(modules=["kinematic_limits", "kinematic_limits"]) == [all_three.id]
    assert ids(modules=["dedup"]) == []                                 # no row at all
    assert ids(modules=[]) == [all_three.id, ts_only.id, both.id]        # no filter
    assert ids(modules=["timestamp_check"], dataset_id=ds.id, q="both") == [both.id]
    assert repo.list_tasks(page=1, page_size=1, modules=["timestamp_check"]).total == 3
    far = repo.list_tasks(page=10**20, page_size=100)                   # far away: empty, no error
    assert (far.items, far.total) == ([], 3)
    far = repo.list_datasets(page=10**20, page_size=100)
    assert (far.items, far.total) == ([], 1)


def test_task_points_at_a_dataset_of_its_own_owner(repo):
    mine, _ = repo.register_dataset(_dataset())
    theirs, _ = repo.register_dataset(_dataset(owner=OTHER))
    t = repo.create_task(_spec(state="created", dataset=mine.id))
    assert repo.get_task(t.id).dataset_id == mine.id
    with pytest.raises(P.NotFound):
        repo.create_task(_spec(dataset=theirs.id))
    with pytest.raises(P.NotFound):
        repo.create_task(_spec(dataset="ds_missing"))
    assert repo.list_tasks(page=1, page_size=10).total == 1              # nothing half-created
    with pytest.raises(P.NotFound):
        repo.update_task_fields(t.id, if_updated_at=None, dataset_id=theirs.id)
    assert repo.update_task_fields(t.id, if_updated_at=None, dataset_id=None).dataset_id is None
    assert repo.update_task_fields(t.id, if_updated_at=None, dataset_id=mine.id).dataset_id == mine.id


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
                    ({"stopped"}, "queued")):
        with pytest.raises(ValueError):
            repo.update_task_state(t.id, frm, to, at=T0)


def test_terminal_recompute_keeps_first_finish_time(repo):
    t = repo.create_task(_spec())
    _drive(repo, t.id, "running")
    assert repo.update_task_state(t.id, {"running"}, "completed_with_errors", at=T0 + 10)
    assert repo.update_task_state(t.id, {"completed_with_errors"}, "succeeded", at=T0 + 99)
    got = repo.get_task(t.id)
    assert (got.state, got.finished_at) == ("succeeded", T0 + 10)


def test_a_finished_resume_ends_the_task_again(repo):
    """stopped / failed -> succeeded / completed_with_errors, for tasks only (C5 1.2)."""
    stopped = repo.create_task(_spec("stopped"))
    _drive(repo, stopped.id, "running", "stopping")
    assert repo.update_task_state(stopped.id, {"stopping"}, "stopped", at=T0 + 10)
    assert repo.update_task_state(stopped.id, {"stopped"}, "succeeded", at=T0 + 500)
    got = repo.get_task(stopped.id)
    assert (got.state, got.finished_at, got.started_at) == ("succeeded", T0 + 500, T0)

    failed = repo.create_task(_spec("failed"))
    _drive(repo, failed.id, "running")
    assert repo.update_task_state(failed.id, {"running"}, "failed", reason="密钥失效", at=T0 + 10)
    assert repo.update_task_state(failed.id, {"failed"}, "completed_with_errors", at=T0 + 600)
    got = repo.get_task(failed.id)
    assert (got.state, got.finished_at, got.state_reason) == ("completed_with_errors", T0 + 600, None)

    _drive(repo, failed.id, "succeeded")
    assert repo.get_task(failed.id).finished_at == T0 + 600            # a recompute keeps it
    for frm, to in (("stopped", "queued"), ("failed", "running"), ("stopped", "failed")):
        with pytest.raises(ValueError):
            repo.update_task_state(stopped.id, {frm}, to, at=T0)

    parent = repo.create_task(_spec("parent"))
    _drive(repo, parent.id, "running", "failed")
    sub = repo.create_subtask(P.Subtask(id="", task_id=parent.id, kind="resume", scope={},
                                        state="queued"))
    _sub_drive(repo, sub.id, "running", "failed")
    for to in ("succeeded", "completed_with_errors"):
        with pytest.raises(ValueError):                              # subtasks stay final
            repo.update_subtask_state(sub.id, {"failed"}, to, at=T0)


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
    with pytest.raises(P.Conflict) as err:
        repo.soft_delete_task(done.id, at=T0)                           # its subtask is still active
    assert err.value.code == "subtask_active"
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


def test_subtask_pause_reason_is_kept_while_pausing_and_paused(repo):
    t = repo.create_task(_spec())
    _drive(repo, t.id, "running", "completed_with_errors")
    s = repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="retry", scope={}, state="queued"))
    assert s.pause_reason is None
    _sub_drive(repo, s.id, "running")
    assert repo.update_subtask_state(s.id, {"running"}, "pausing", pause_reason="user", at=T0 + 1)
    assert repo.update_subtask_state(s.id, {"pausing"}, "paused", at=T0 + 2)
    assert repo.get_subtask(s.id).pause_reason == "user"               # kept through paused
    assert [x.pause_reason for x in repo.list_subtasks(t.id)] == ["user"]
    assert repo.active_subtask(t.id).pause_reason == "user"
    assert repo.update_subtask_state(s.id, {"paused"}, "queued", at=T0 + 3)
    assert repo.get_subtask(s.id).pause_reason is None                  # only while pausing/paused
    _sub_drive(repo, s.id, "running")
    assert repo.update_subtask_state(s.id, {"running"}, "pausing", pause_reason="system",
                                     reason="Daemon 重启时任务还在运行", at=T0 + 4)
    got = repo.get_subtask(s.id)
    assert (got.pause_reason, got.state_reason) == ("system", "Daemon 重启时任务还在运行")
    assert repo.update_subtask_state(s.id, {"pausing"}, "stopping", pause_reason="user", at=T0 + 5)
    assert repo.get_subtask(s.id).pause_reason is None


def test_subtask_result_revision(repo):
    t = repo.create_task(_spec())
    _drive(repo, t.id, "running", "completed_with_errors")
    s = repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="retry", scope={}, state="queued"))
    assert s.result_rev is None
    repo.set_subtask_result_rev(s.id, 2)
    assert repo.get_subtask(s.id).result_rev == 2
    with pytest.raises(P.NotFound):
        repo.set_subtask_result_rev("sub_missing", 2)


def test_subtasks_in_states_across_tasks(repo, clock):
    subs = []
    for name in ("a", "b", "c"):
        t = repo.create_task(_spec(name))
        _drive(repo, t.id, "running", "completed_with_errors")
        clock.advance(1)
        subs.append(repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="retry", scope={},
                                                  state="queued")))
    _sub_drive(repo, subs[0].id, "running")
    _sub_drive(repo, subs[2].id, "running", "succeeded")
    assert [s.id for s in repo.subtasks_in_states({"queued", "running"})] == [subs[0].id, subs[1].id]
    assert [s.id for s in repo.subtasks_in_states(["succeeded"])] == [subs[2].id]
    assert repo.subtasks_in_states([]) == []


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
    from daemon.pagination import encode_cursor
    with pytest.raises(CursorError):                                   # right listing, wrong shape
        repo.list_events(cursor=encode_cursor("events", "7", scope={"owner": P.DEFAULT_OWNER,
                                                                     "resource": None}))
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


# ---------------------------------------------------------------------------
# C5 1.3: reference counts for the delete prompts
# ---------------------------------------------------------------------------

def test_vlm_backend_references(repo):
    b = repo.create_vlm_backend(_backend(), _cred("ark-refs-key", kind="ark"))
    model = b.models[0]
    assert repo.vlm_backend_references(b.id) == (0, 0)
    running = repo.create_task(_spec("running", vlm_model=model.id))
    done = repo.create_task(_spec("done", vlm_model=model.id))
    _drive(repo, done.id, "running", "succeeded")
    assert repo.vlm_backend_references(b.id) == (1, 1)
    repo.soft_delete_task(done.id, at=T0)
    assert repo.vlm_backend_references(b.id) == (1, 1)               # deleted counts as finished
    _drive(repo, running.id, "running", "failed")
    assert repo.vlm_backend_references(b.id) == (0, 2)
    assert repo.vlm_backend_references("vb_missing") == (0, 0)


def test_credential_dataset_references(repo):
    c = repo.create_credential(_cred())
    assert repo.credential_dataset_references(c.id) == 0
    repo.register_dataset(_dataset(cred=c.id))
    repo.register_dataset(_dataset("tos://bucket/datasets/other", cred=c.id))
    assert repo.credential_dataset_references(c.id) == 2
    repo.delete_credential(c.id)                                       # registrations never block
    assert repo.credential_dataset_references(c.id) == 0
