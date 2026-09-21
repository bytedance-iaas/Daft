"""Registered datasets and the overview (C4 1.1, D36, D37); every body is validated against C4."""
from __future__ import annotations

from curation.contracts import schemas
from daemon.repo import protocol as P
from daemon.transitions import change_task_state

from .conftest import (
    LISTING_DIGEST,
    META_DIGEST,
    T0,
    assert_error,
    assert_schema,
    seed_dataset,
    seed_task,
)

JSON = {"Content-Type": "application/json"}
DAY = 24 * 60 * 60 * 1000
LIST_SCHEMA = "openapi.yaml#/paths/~1datasets/get/responses/200/content/application~1json/schema"
UNKNOWN = "这个接口不存在（或还没有实现）"

_SPEC = schemas.load("openapi.yaml")["components"]["schemas"]
ITEM_KEYS = set(_SPEC["DatasetItem"]["properties"])
DETAIL_KEYS = ITEM_KEYS | set(_SPEC["DatasetDetail"]["allOf"][1]["properties"])


def assert_dataset_detail(body: dict) -> None:
    """C4 1.2 ``DatasetDetail`` is ``allOf`` [``DatasetItem``, the extra fields], and
    ``DatasetItem`` forbids extra keys, so no document satisfies both branches (a
    contract gap, see the W4 report): check each half and the exact key set instead."""
    assert_schema("DatasetItem", {k: v for k, v in body.items() if k in ITEM_KEYS})
    assert_schema("openapi.yaml#/components/schemas/DatasetDetail/allOf/1", body)
    assert set(body) == DETAIL_KEYS


def _rt(client):
    return client.app.state.runtime


def _cred(repo, name="prod-tos"):
    return repo.create_credential(P.Credential(id="", name=name, kind="tos", payload_enc=b"x",
                                               key_version=1, payload_meta={}))


def _to(rt, task_id, *states, at=None):
    for to in states:
        cur = rt.repo.get_task(task_id).state
        assert change_task_state(rt.repo, rt.hub, task_id, {cur}, to, at=at or rt.clock())


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_dataset_list_pages_filters_and_last_task(client_for, clock):
    c = client_for(base_path="/curation")
    rt = _rt(c)
    ids = []
    for i in range(12):
        clock.advance(1000)
        ids.append(seed_dataset(rt.repo, f"tos://bucket/sets/set_{i:02d}").id)
    clock.advance(1000)
    umi = seed_dataset(rt.repo, "tos://bucket/other/umi_640", name="UMI 640", version="v3",
                       robot_type=None, episodes=640)
    clock.advance(1000)
    broken = seed_dataset(rt.repo, "tos://bucket/other/broken", supported=False)
    rt.repo.record_dataset_check(P.DatasetCheck(dataset_id=ids[0], at=T0 + 5, trigger="recheck",
                                                result="changed", change=None))
    older = seed_task(rt.repo, "first run", dataset_id=umi.id)
    clock.advance(1000)
    newer = seed_task(rt.repo, "second run", dataset_id=umi.id, state="created")

    r = c.get("/curation/api/v1/datasets", params={"page_size": 10})
    assert r.status_code == 200
    body = r.json()
    assert_schema(LIST_SCHEMA, body)
    for item in body["items"]:
        assert_schema("DatasetItem", item)
    assert (body["page"], body["page_size"], body["total"]) == (1, 10, 14)
    assert [x["id"] for x in body["items"]] == [broken.id, umi.id] + ids[::-1][:8]
    item = body["items"][1]
    assert item == {"id": umi.id, "name": "UMI 640", "source": "tos",
                    "uri": "tos://bucket/other/umi_640", "region": "cn-beijing",
                    "format": "lerobot_v3", "episode_count": 640, "robot_type": None,
                    "check_state": "ok", "checked_at": None, "preflighted_at": T0,
                    "created_at": umi.created_at,
                    "last_task": {"id": newer.id, "name": "second run", "state": "created",
                                  "created_at": newer.created_at}}
    assert body["items"][0]["format"] == "unsupported" and body["items"][2]["last_task"] is None
    assert older.id != newer.id

    def ids_of(**params):
        got = c.get("/curation/api/v1/datasets", params={"page_size": 50, **params}).json()
        assert got["total"] == len(got["items"])
        return [x["id"] for x in got["items"]]

    assert ids_of(q="umi") == [umi.id]                                  # name
    assert ids_of(q="other/") == [broken.id, umi.id]                    # or address
    assert ids_of(format="lerobot_v3") == [umi.id]
    assert ids_of(format="unsupported") == [broken.id]
    assert ids_of(check_state="changed") == [ids[0]]
    page2 = c.get("/curation/api/v1/datasets", params={"page": 2, "page_size": 10}).json()
    assert [x["id"] for x in page2["items"]] == ids[::-1][8:] and page2["total"] == 14
    for params in ({"page_size": 7}, {"page": 0}, {"format": "rrd"}, {"check_state": "unknown"}):
        assert_error(c.get("/curation/api/v1/datasets", params=params), "validation_failed")


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------

def test_dataset_detail_with_checks_tasks_and_listing(client_for, clock):
    c = client_for()
    rt = _rt(c)
    key = _cred(rt.repo, "readonly-tos")
    ds = seed_dataset(rt.repo, credential_id=key.id)
    rt.repo.update_dataset(ds.id, note="DROID 抽检用")
    change = {"meta_changed": True, "added": 12, "removed": 0, "modified": 1,
              "sample_keys": ["data/chunk-000/episode_000200.parquet", "meta/info.json"],
              "preflighted_at": T0}
    for i in range(22):
        rt.repo.record_dataset_check(P.DatasetCheck(
            dataset_id=ds.id, at=T0 + i, trigger="recheck" if i else "add",
            result="changed" if i == 21 else "same", change=change if i == 21 else None))
    tasks = []
    for i in range(22):
        clock.advance(1000)
        tasks.append(seed_task(rt.repo, f"run {i}", dataset_id=ds.id))
    gone = seed_task(rt.repo, "deleted run", dataset_id=ds.id, state="created")
    rt.repo.soft_delete_task(gone.id, at=T0)

    r = c.get(f"/api/v1/datasets/{ds.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert_dataset_detail(body)
    assert (body["note"], body["credential"], body["check_state"]) == \
        ("DROID 抽检用", "readonly-tos", "changed")
    assert body["meta_fingerprint"] == META_DIGEST
    assert body["listing"] == {"objects": 204, "bytes": 1_234_567, "digest": LISTING_DIGEST}
    assert body["preflight"]["dataset"]["episode_count"] == 200
    assert (body["episode_count"], body["robot_type"], body["format"]) == (200, "franka",
                                                                          "lerobot_v2")
    assert len(body["checks"]) == 20 and body["checks"][0] == {
        "at": T0 + 21, "trigger": "recheck", "result": "changed", "change": change}
    assert [x["at"] for x in body["checks"]] == [T0 + i for i in range(21, 1, -1)]  # newest first
    assert [x["id"] for x in body["tasks"]] == [t.id for t in tasks[::-1][:20]]
    assert body["last_task"] == body["tasks"][0]
    assert body["links"] == []                                  # Link.rel has no "dataset" yet

    rt.repo.delete_credential(key.id)                           # only the registration used it
    body = c.get(f"/api/v1/datasets/{ds.id}").json()
    assert_dataset_detail(body)
    assert body["credential"] is None

    public = seed_dataset(rt.repo, "tos://hf-cache/lerobot/aloha_sim", source="public", region=None)
    body = c.get(f"/api/v1/datasets/{public.id}").json()
    assert_dataset_detail(body)
    assert (body["credential"], body["region"], body["checks"], body["tasks"],
            body["last_task"], body["checked_at"]) == (None, None, [], [], None, None)


def test_dataset_listing_takes_the_source_manifest_summary_too(client_for):
    """A registration may keep C2 ``source-manifest`` ``summary`` as is (``count``, not ``objects``)."""
    c = client_for()
    rt = _rt(c)
    ds, _ = rt.repo.register_dataset(P.Dataset(
        id="", name="x", source="tos", uri="tos://b/x", preflight={}, meta_fingerprint=META_DIGEST,
        source_fingerprint={"count": 9, "bytes": 100, "digest": LISTING_DIGEST}, preflighted_at=T0))
    body = c.get(f"/api/v1/datasets/{ds.id}").json()
    assert body["listing"] == {"objects": 9, "bytes": 100, "digest": LISTING_DIGEST}
    assert (body["format"], body["episode_count"], body["robot_type"]) == ("unsupported", None, None)


def test_unknown_datasets_and_neighbouring_paths(client_for):
    c = client_for()
    body = assert_error(c.get("/api/v1/datasets/ds_missing"), "not_found")
    assert body["error"]["message"] != UNKNOWN                 # the handler said so
    for path in ("/api/v1/datasets/browse", "/api/v1/datasets/episodes",
                 "/api/v1/datasets/task_x"):                  # W3's routes, not an id (yet)
        assert assert_error(c.get(path), "not_found")["error"]["message"] == UNKNOWN
    assert_error(c.patch("/api/v1/datasets/ds_missing", json={"name": "x"}), "not_found")
    assert_error(c.delete("/api/v1/datasets/ds_missing", headers=JSON), "not_found")


# ---------------------------------------------------------------------------
# rename, note, delete
# ---------------------------------------------------------------------------

def test_rename_and_note(client_for):
    c = client_for(base_path="/curation")
    rt = _rt(c)
    ds = seed_dataset(rt.repo)
    r = c.patch(f"/curation/api/v1/datasets/{ds.id}", json={"name": "DROID 全量", "note": "第二批"})
    assert r.status_code == 200, r.text
    assert_dataset_detail(r.json())
    assert (r.json()["name"], r.json()["note"]) == ("DROID 全量", "第二批")
    r = c.patch(f"/curation/api/v1/datasets/{ds.id}", json={"note": None})
    assert r.status_code == 200 and r.json()["note"] is None and r.json()["name"] == "DROID 全量"
    events = rt.repo.list_events(resource=ds.id).items
    assert [(e.action, e.detail["fields"]) for e in events] == [
        ("dataset.update", ["note"]), ("dataset.update", ["name", "note"])]

    for bad in ({}, {"name": ""}, {"uri": "tos://b/other"}, {"note": "x" * 2001}, [1]):
        assert_error(c.patch(f"/curation/api/v1/datasets/{ds.id}", json=bad), "validation_failed")
    r = c.patch(f"/curation/api/v1/datasets/{ds.id}", content=b'{"name": "x"}',
                headers={"Content-Type": "text/plain"})
    assert "JSON" in assert_error(r, "validation_failed")["error"]["message"]

    key = {"Idempotency-Key": "rename-0001"}
    first = c.patch(f"/curation/api/v1/datasets/{ds.id}", json={"name": "again"}, headers=key)
    replay = c.patch(f"/curation/api/v1/datasets/{ds.id}", json={"name": "again"}, headers=key)
    assert replay.headers["idempotent-replayed"] == "true" and replay.json() == first.json()
    assert_error(c.patch(f"/curation/api/v1/datasets/{ds.id}", json={"name": "other"},
                         headers=key), "idempotency_conflict")


def test_delete_waits_for_unfinished_tasks(client_for, clock):
    c = client_for()
    rt = _rt(c)
    ds = seed_dataset(rt.repo)
    created = seed_task(rt.repo, "created", dataset_id=ds.id, state="created")
    clock.advance(1)
    running = seed_task(rt.repo, "running", dataset_id=ds.id)
    _to(rt, running.id, "running")
    clock.advance(1)
    done = seed_task(rt.repo, "done", dataset_id=ds.id)
    _to(rt, done.id, "running", "succeeded")

    assert_error(c.delete(f"/api/v1/datasets/{ds.id}"), "validation_failed")    # JSON header
    body = assert_error(c.delete(f"/api/v1/datasets/{ds.id}", headers=JSON), "dataset_in_use")
    assert "2 个未结束的任务" in body["error"]["message"]
    assert body["error"]["details"]["count"] == 2
    assert [t["id"] for t in body["error"]["details"]["tasks"]] == [running.id, created.id]

    _to(rt, running.id, "stopping", "stopped")
    assert c.delete(f"/api/v1/tasks/{created.id}", headers=JSON).status_code == 204
    r = c.delete(f"/api/v1/datasets/{ds.id}", headers=JSON)
    assert r.status_code == 204 and r.content == b""
    assert_error(c.get(f"/api/v1/datasets/{ds.id}"), "not_found")
    task = c.get(f"/api/v1/tasks/{done.id}").json()
    assert_schema("Task", task)
    assert task["dataset_id"] is None and task["input"]["uri"] == ds.uri   # the input stays
    ev = rt.repo.list_events(resource=ds.id).items[0]
    assert (ev.action, ev.detail["uri"]) == ("dataset.delete", ds.uri)
    assert_error(c.delete(f"/api/v1/datasets/{ds.id}", headers=JSON), "not_found")


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------

OVERVIEW = "/api/v1/overview"


def test_overview_of_an_empty_site(client_for):
    c = client_for()
    r = c.get(OVERVIEW)
    assert r.status_code == 200
    body = r.json()
    assert_schema("Overview", body)
    assert body["todo"] == {"error_tasks": 0, "adjudication": {"tasks": 0, "episodes": 0},
                            "delivery_pending": 0, "datasets_changed": 0,
                            "credentials_failed": 0, "backends_failed": 0}
    assert body["running"] == {"running": 0, "queued": 0, "paused": 0, "active": []}
    assert body["recent"]["pass_rate"] is None and body["recent"]["days"] == 7
    assert [d["tokens"] for d in body["recent"]["tokens_per_day"]] == [0] * 7
    # T0 is 2025-09-19 16:40 UTC, already the 20th at +08:00 (the default site offset)
    assert [d["date"] for d in body["recent"]["tokens_per_day"]] == [
        f"2025-09-{d}" for d in range(14, 21)]
    assert body["datasets"] == {"total": 0, "changed": 0} and body["generated_at"] == T0


def _usage(rt, task_id, tokens, at):
    rt.repo.add_usage([P.UsageDelta(task_id=task_id, ledger="actual", module_id="task_success",
                                    call_kind="probe", model_name="m", prompt_tokens=tokens - 10,
                                    completion_tokens=10, requests=1),
                       P.UsageDelta(task_id=task_id, ledger="attributed", module_id="task_success",
                                    call_kind="probe", model_name="m", prompt_tokens=999,
                                    requests=1)], at=at)


def test_overview_counts(client_for, clock):
    c = client_for()
    rt = _rt(c)
    summary = {"total": 50, "passed": 40, "rejected": 5, "held": 5, "review": 3, "pass_rate": 0.8}

    running = seed_task(rt.repo, "running")
    _to(rt, running.id, "running")
    rt.repo.set_task_progress(running.id, {"stages": [
        {"id": "numeric", "state": "succeeded", "done": 50, "total": 50},
        {"id": "vlm", "state": "running", "done": 31, "total": 49}]})
    clock.advance(1)
    pausing = seed_task(rt.repo, "pausing")
    _to(rt, pausing.id, "running", "pausing")
    for name in ("q1", "q2"):
        seed_task(rt.repo, name)
    paused = seed_task(rt.repo, "paused")
    _to(rt, paused.id, "running")
    change_task_state(rt.repo, None, paused.id, {"running"}, "pausing", at=T0, pause_reason="user")
    _to(rt, paused.id, "paused")

    errors = seed_task(rt.repo, "with errors")                 # finished today, never exported
    _to(rt, errors.id, "running", "completed_with_errors", at=T0)
    rt.repo.set_task_summary(errors.id, {**summary, "pending_adjudication": 3})
    rt.repo.switch_result_rev(errors.id, 0, 1)
    ok = seed_task(rt.repo, "ok")                              # yesterday, exported
    _to(rt, ok.id, "running", "succeeded", at=T0 - DAY)
    rt.repo.set_task_summary(ok.id, {**summary, "total": 30, "passed": 30, "review": 0})
    rt.repo.switch_result_rev(ok.id, 0, 1)
    rt.repo.set_export_fingerprint(ok.id, "sha256:x", delivery_stale=False)
    old = seed_task(rt.repo, "last week")                      # outside the 7 days, stale
    _to(rt, old.id, "running", "succeeded", at=T0 - 8 * DAY)
    rt.repo.set_task_summary(old.id, {**summary, "pending_adjudication": 0})   # all judged
    rt.repo.switch_result_rev(old.id, 0, 1)
    rt.repo.set_export_fingerprint(old.id, "sha256:y", delivery_stale=True)
    failed = seed_task(rt.repo, "failed")
    _to(rt, failed.id, "running", "failed", at=T0)
    gone = seed_task(rt.repo, "deleted")
    _to(rt, gone.id, "running", "completed_with_errors", at=T0)
    rt.repo.set_task_summary(gone.id, {**summary, "pending_adjudication": 9})
    rt.repo.soft_delete_task(gone.id, at=T0)

    changed = seed_dataset(rt.repo, "tos://b/changed")
    seed_dataset(rt.repo, "tos://b/fine")
    rt.repo.record_dataset_check(P.DatasetCheck(dataset_id=changed.id, at=T0, trigger="recheck",
                                                result="changed"))
    bad_key = _cred(rt.repo, "bad")
    _cred(rt.repo, "good")
    rt.repo.set_credential_verification(bad_key.id, "failed", T0, "AccessDenied")
    for name, state in (("ark-bad", "failed"), ("ark-ok", "ok")):
        b = rt.repo.create_vlm_backend(P.VlmBackend(
            id="", name=name, kind="ark", endpoint="https://ark.example", credential_id=None),
            P.Credential(id="", name=f"{name}-key", kind="ark", payload_enc=b"x", key_version=1,
                         payload_meta={}))
        rt.repo.set_vlm_backend_verification(b.id, state, T0, None)
        rt.repo.set_credential_verification(b.credential_id, "failed", T0, None)  # not counted

    _usage(rt, running.id, 1000, T0)                           # today (+08:00)
    _usage(rt, errors.id, 500, T0 - 41 * 60 * 1000)            # 23:59 yesterday, local time
    _usage(rt, ok.id, 70, T0 - 8 * DAY)                        # too old

    body = c.get(OVERVIEW).json()
    assert_schema("Overview", body)
    assert body["todo"] == {"error_tasks": 1, "adjudication": {"tasks": 1, "episodes": 3},
                            "delivery_pending": 2, "datasets_changed": 1,
                            "credentials_failed": 1, "backends_failed": 1}
    assert body["running"] == {
        "running": 2, "queued": 2, "paused": 1,
        "active": [{"task": {"id": pausing.id, "name": "pausing", "state": "pausing",
                             "created_at": pausing.created_at},
                    "stage": None, "done": 0, "total": 0},
                   {"task": {"id": running.id, "name": "running", "state": "running",
                             "created_at": running.created_at},
                    "stage": "vlm", "done": 31, "total": 49}]}
    recent = body["recent"]
    assert (recent["tasks_finished"], recent["episodes_checked"], recent["pass_rate"]) == \
        (2, 80, 0.875)
    assert recent["tokens_per_day"][-2:] == [{"date": "2025-09-19", "tokens": 500},
                                             {"date": "2025-09-20", "tokens": 1000}]
    assert sum(d["tokens"] for d in recent["tokens_per_day"]) == 1500
    assert body["datasets"] == {"total": 2, "changed": 1}


def test_overview_days_follow_the_site_offset(client_for):
    c = client_for(tz_offset_minutes=0)
    rt = _rt(c)
    t = seed_task(rt.repo)
    _usage(rt, t.id, 1000, T0)
    _usage(rt, t.id, 500, T0 - 41 * 60 * 1000)
    days = c.get(OVERVIEW).json()["recent"]["tokens_per_day"]
    assert days[-1] == {"date": "2025-09-19", "tokens": 1500}   # one UTC day
    assert days[0]["date"] == "2025-09-13"
