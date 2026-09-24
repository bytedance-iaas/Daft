"""The W4 routes end to end; every response body is validated against C4."""
from __future__ import annotations

import pytest

from curation.contracts import modules as registry
from daemon.repo import protocol as P
from daemon.transitions import change_task_state

from .conftest import T0, assert_error, assert_schema, seed_dataset, seed_task

BASES = ("", "/curation")
#: Every write says it is JSON, even without a body (routes/common.py).
JSON = {"Content-Type": "application/json"}


def _rt(client):
    return client.app.state.runtime


def _cred(repo, name="prod-tos"):
    return repo.create_credential(P.Credential(id="", name=name, kind="tos", payload_enc=b"x",
                                               key_version=1, payload_meta={"region": "cn-beijing"}))


def _finish(rt, task_id, to="succeeded"):
    for frm, nxt in (("queued", "running"), ("running", to)):
        assert change_task_state(rt.repo, rt.hub, task_id, {frm}, nxt, at=rt.clock())


# ---------------------------------------------------------------------------
# probes and prefix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base", BASES)
def test_probes_at_root_and_under_the_prefix(client_for, base):
    c = client_for(base_path=base)
    for prefix in {"", base}:
        r = c.get(f"{prefix}/healthz")
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        assert_schema("openapi.yaml#/paths/~1healthz/get/responses/200/content/application~1json/"
                      "schema", r.json())
        r = c.get(f"{prefix}/readyz")
        assert r.status_code == 200
        assert_schema("Readiness", r.json())
        assert r.json() == {"status": "ok", "checks": {
            "db_writable": True, "master_key": True, "workdir_writable": True,
            "scratch_writable": True, "reconciled": True}}
        assert c.head(f"{prefix}/healthz").status_code == 200
        assert c.head(f"{prefix}/readyz").status_code == 200


def test_readyz_turns_503_when_a_check_fails(client_for, tmp_path):
    (tmp_path / "file-not-dir").write_text("x")
    c = client_for(base_path="/curation", scratch_dir=tmp_path / "file-not-dir")
    r = c.get("/readyz")
    assert r.status_code == 503
    assert_schema("Readiness", r.json())
    assert r.json()["status"] == "not_ready" and r.json()["checks"]["scratch_writable"] is False


def test_readyz_reports_a_stuck_or_read_only_database(client_for, monkeypatch):
    import threading
    import time

    import daemon.app as app_mod

    c = client_for()
    rt = _rt(c)
    monkeypatch.setattr(app_mod, "PROBE_TIMEOUT_S", 0.2)
    release = threading.Event()
    real = rt.repo.purge_expired

    def stuck(*, now):
        release.wait(5)
        return real(now=now)

    monkeypatch.setattr(rt.repo, "purge_expired", stuck)
    started = time.monotonic()
    r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["db_writable"] is False
    assert time.monotonic() - started < 2                         # bounded, never hangs the probe
    assert c.get("/readyz").json()["checks"]["db_writable"] is False   # still stuck: no pile-up
    release.set()

    def read_only(*, now):
        import sqlite3
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(rt.repo, "purge_expired", read_only)
    time.sleep(0.1)
    assert c.get("/readyz").json()["checks"]["db_writable"] is False
    monkeypatch.setattr(rt.repo, "purge_expired", real)
    assert c.get("/readyz").status_code == 200


def test_everything_lives_under_the_prefix(client_for):
    c = client_for(base_path="/curation")
    assert c.get("/curation/api/v1/modules").status_code == 200
    assert c.get("/curation/api/v1/tasks").status_code == 200
    assert_error(c.get("/api/v1/tasks"), "not_found")          # the gateway never strips it
    assert_error(c.get("/api/v1/modules"), "not_found")


# ---------------------------------------------------------------------------
# modules
# ---------------------------------------------------------------------------

def test_modules_is_the_c1_registry(client_for):
    c = client_for(base_path="/curation")
    r = c.get("/curation/api/v1/modules")
    assert r.status_code == 200
    assert_schema("ModuleRegistry", r.json())
    assert r.json() == registry.export()


# ---------------------------------------------------------------------------
# task list: page numbers with a correct total (D21)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base", BASES)
def test_task_list_pages_and_total(client_for, clock, base):
    c = client_for(base_path=base)
    rt = _rt(c)
    ids = []
    for i in range(23):
        clock.advance(1000)
        ids.append(seed_task(rt.repo, f"task {i:02d}").id)
    newest = list(reversed(ids))
    seen = []
    for page in (1, 2, 3):
        r = c.get(f"{base}/api/v1/tasks", params={"page": page, "page_size": 10})
        assert r.status_code == 200
        body = r.json()
        assert_schema("openapi.yaml#/paths/~1tasks/get/responses/200/content/application~1json/schema",
                      body)
        for item in body["items"]:
            assert_schema("TaskListItem", item)
        assert (body["page"], body["page_size"], body["total"]) == (page, 10, 23)
        seen += [t["id"] for t in body["items"]]
    assert seen == newest
    empty = c.get(f"{base}/api/v1/tasks", params={"page": 9, "page_size": 10}).json()
    assert empty["items"] == [] and empty["total"] == 23


def test_task_list_filters_and_item_fields(client_for, clock):
    c = client_for(base_path="/curation")
    rt = _rt(c)
    a = seed_task(rt.repo, "droid 抽检", delivery="tos://deliveries/droid")
    clock.advance(1)
    b = seed_task(rt.repo, "umi 全量", delivery="tos://deliveries/umi",
                  selected=("timestamp_check", "dedup"))
    _finish(rt, b.id, "completed_with_errors")
    rt.repo.set_task_summary(b.id, {"total": 50, "passed": 41, "rejected": 7, "held": 2,
                                    "review": 10, "pass_rate": 0.82})
    rt.repo.update_module_state(b.id, "dedup", {"pending"}, "failed")
    rt.repo.add_usage([P.UsageDelta(task_id=b.id, ledger="actual", module_id="task_success",
                                    call_kind="probe", model_name="m", prompt_tokens=100,
                                    requests=2)], at=T0)

    def ids(**params):
        body = c.get("/curation/api/v1/tasks", params=params).json()
        assert body["total"] == len(body["items"])
        return [t["id"] for t in body["items"]], body["items"]

    assert ids(state="completed_with_errors")[0] == [b.id]
    assert ids(q="droid")[0] == [a.id]
    assert ids(delivery="tos://Deliveries/droid/")[0] == [a.id]        # normalized like storage
    _, items = ids(q="umi")
    item = items[0]
    assert item["dataset"] == "droid_100" and item["summary"]["pass_rate"] == 0.82
    assert item["pending_adjudication"] == 10
    assert item["module_counts"] == {"selected": 2, "pending": 1, "failed": 1}
    assert item["usage"]["prompt_tokens"] == 100 and item["usage"]["requests"] == 2
    assert item["active_subtask"] is None and item["deleted_at"] is None
    assert item["modules"] == [m for m in registry.ids() if m in ("timestamp_check", "dedup")]
    assert item["dataset_id"] is None

    assert ids(module="dedup")[0] == [b.id]                          # C4 1.1: every listed module
    assert ids(module="timestamp_check,dedup")[0] == [b.id]
    assert ids(module=" timestamp_check ,")[0] == [b.id, a.id]
    assert ids(module="")[0] == [b.id, a.id]
    assert ids(module="dedup", q="droid")[0] == []
    assert ids(dataset_id="ds_unknown")[0] == []
    body = assert_error(c.get("/curation/api/v1/tasks", params={"module": "dedup,nope"}),
                        "validation_failed")
    assert "nope" in body["error"]["message"]

    assert_error(c.get("/curation/api/v1/tasks", params={"page_size": 7}), "validation_failed")
    far = c.get("/curation/api/v1/tasks", params={"page": 10**20}).json()
    assert far["items"] == [] and far["total"] == 2                  # far away: empty, not a 500
    assert_error(c.get("/curation/api/v1/tasks", params={"page": 0}), "validation_failed")
    assert_error(c.get("/curation/api/v1/tasks", params={"state": "exploded"}), "validation_failed")
    assert_error(c.get("/curation/api/v1/tasks", params={"delivery": "s3://x/y"}), "validation_failed")


def test_task_list_filters_by_result_and_pending_adjudication(client_for, clock):
    """C4 1.14: ?has_result=true for 质检报告, ?pending_adjudication=true for 人工裁决; false or
    absent does not filter."""
    c = client_for(base_path="/curation")
    rt = _rt(c)
    a = seed_task(rt.repo, "no result yet")
    clock.advance(1)
    b = seed_task(rt.repo, "reported")
    _finish(rt, b.id)
    assert rt.repo.switch_result_rev(b.id, 0, 1)
    rt.repo.set_task_summary(b.id, {"total": 50, "passed": 50, "pending_adjudication": 0})
    clock.advance(1)
    d = seed_task(rt.repo, "to adjudicate")
    _finish(rt, d.id, "completed_with_errors")
    assert rt.repo.switch_result_rev(d.id, 0, 1)
    rt.repo.set_task_summary(d.id, {"total": 50, "passed": 41, "review": 10,
                                    "pending_adjudication": 6})

    def ids(**params):
        body = c.get("/curation/api/v1/tasks", params=params).json()
        assert body["total"] == len(body["items"])
        return [t["id"] for t in body["items"]]

    assert ids() == [d.id, b.id, a.id]
    assert ids(has_result="true") == [d.id, b.id]
    assert ids(pending_adjudication="true") == [d.id]
    assert ids(has_result="false", pending_adjudication="false") == [d.id, b.id, a.id]
    assert_error(c.get("/curation/api/v1/tasks", params={"has_result": "maybe"}),
                 "validation_failed")


def test_running_filter_includes_tasks_whose_subtask_runs(client_for, clock):
    """D46: a retry (resume, adjudication run, re-export) that is queued or running shows its
    finished task as running, so ?state=running lists it; the item keeps the task's own state
    and names the subtask. The task's other filters do not change."""
    from daemon.transitions import change_subtask_state

    c = client_for()
    rt = _rt(c)
    main = seed_task(rt.repo, "main run")
    change_task_state(rt.repo, rt.hub, main.id, {"queued"}, "running", at=rt.clock())
    clock.advance(1)
    retried = seed_task(rt.repo, "retried")
    _finish(rt, retried.id, "completed_with_errors")
    sub = rt.repo.create_subtask(P.Subtask(id="", task_id=retried.id, kind="retry", scope={},
                                           state="queued"))
    clock.advance(1)
    exported = seed_task(rt.repo, "export paused")
    _finish(rt, exported.id)
    paused = rt.repo.create_subtask(P.Subtask(id="", task_id=exported.id, kind="reexport",
                                              scope={}, state="queued"))
    for frm, to, kw in (("queued", "running", {}), ("running", "pausing", {"pause_reason": "user"}),
                        ("pausing", "paused", {})):
        change_subtask_state(rt.repo, rt.hub, paused.id, {frm}, to, at=T0, **kw)

    def listed(**params):
        body = c.get("/api/v1/tasks", params=params).json()
        assert body["total"] == len(body["items"])
        for item in body["items"]:
            assert_schema("TaskListItem", item)
        return {t["id"]: t for t in body["items"]}

    running = listed(state="running")
    assert list(running) == [retried.id, main.id]                     # newest first
    assert (running[retried.id]["state"], running[retried.id]["active_subtask"]) == \
        ("completed_with_errors", sub.id)
    change_subtask_state(rt.repo, rt.hub, sub.id, {"queued"}, "running", at=T0)
    assert list(listed(state="running")) == [retried.id, main.id]     # queued, then running
    assert list(listed(state="completed_with_errors")) == [retried.id]
    assert list(listed(state="succeeded")) == [exported.id]           # a paused subtask: not running
    change_subtask_state(rt.repo, rt.hub, sub.id, {"running"}, "succeeded", at=T0)
    assert list(listed(state="running")) == [main.id]                 # the retry ended
    # the overview's counts stay per piece of work: one main run, one paused subtask
    running = c.get("/api/v1/overview").json()["running"]
    assert (running["running"], running["paused"]) == (1, 1)


def test_tasks_made_before_d45_keep_their_links(client_for, monkeypatch, clock):
    """A task whose id predates D45 (task_<26 Crockford characters>) still opens, lists,
    changes and deletes; newer ones are task-<9 letters>; the list orders them by creation."""
    import re

    from daemon.repo import sqlite as S

    c = client_for(base_path="/curation")
    rt = _rt(c)
    legacy, fresh = "task_01HXR2D8QZ7N4Y0M5K3J2H1G0F", S.new_id
    with monkeypatch.context() as m:
        m.setattr(S, "new_id", lambda prefix: legacy if prefix == "task" else fresh(prefix))
        old = seed_task(rt.repo, "made before D45", state="created")
    clock.advance(1)
    new = seed_task(rt.repo, "made after", state="created")
    assert old.id == legacy and re.fullmatch(r"task-[a-z]{9}", new.id)
    for t in (old, new):
        r = c.get(f"/curation/api/v1/tasks/{t.id}")
        assert r.status_code == 200, r.text
        assert_schema("Task", r.json())
        assert r.json()["links"][0]["url"].endswith(f"/curation/tasks/{t.id}")
        assert _patch(c, t.id, {"note": "still here"}, base="/curation").status_code == 200
    assert [x["id"] for x in c.get("/curation/api/v1/tasks").json()["items"]] == [new.id, old.id]
    assert c.get("/curation/api/v1/tasks", params={"q": legacy[-8:]}).json()["total"] == 1
    assert c.delete(f"/curation/api/v1/tasks/{legacy}", headers=JSON).status_code == 204
    assert c.post(f"/curation/api/v1/tasks/{legacy}/restore", headers=JSON).status_code == 200


def test_deleted_filter_lists_soft_deleted_tasks(client_for):
    c = client_for()
    rt = _rt(c)
    t = seed_task(rt.repo, state="created")
    assert c.delete(f"/api/v1/tasks/{t.id}", headers=JSON).status_code == 204
    assert c.get("/api/v1/tasks").json()["total"] == 0
    body = c.get("/api/v1/tasks", params={"state": "deleted"}).json()
    assert [x["id"] for x in body["items"]] == [t.id] and body["items"][0]["deleted_at"] is not None
    for item in body["items"]:
        assert_schema("TaskListItem", item)


# ---------------------------------------------------------------------------
# task detail
# ---------------------------------------------------------------------------

def test_task_detail_fits_the_contract(client_for):
    c = client_for(base_path="/curation", public_base_url="https://kit.example.com")
    rt = _rt(c)
    cred = _cred(rt.repo)
    backend = rt.repo.create_vlm_backend(P.VlmBackend(
        id="", name="ark-prod", kind="ark", endpoint="https://ark.example/api/v3", credential_id=None,
        models=[P.VlmModel(id="", backend_id="", model_name="doubao-seed-2-0-pro-260215")]), None)
    t = seed_task(rt.repo, input_cred_id=cred.id, output_cred_id=cred.id,
                  vlm_model_id=backend.models[0].id, selected=("timestamp_check", "task_success"))
    _finish(rt, t.id, "completed_with_errors")
    rt.repo.set_task_progress(t.id, {"stages": [
        {"id": "numeric", "state": "succeeded", "done": 50, "total": 50, "elapsed_s": 1.0},
        {"id": "vlm", "state": "succeeded", "done": 49, "total": 49, "elapsed_s": 408}]})
    rt.repo.set_task_summary(t.id, {"total": 50, "passed": 41, "rejected": 7, "held": 2,
                                    "review": 10, "pass_rate": 0.82, "pending_adjudication": 4})
    rt.repo.switch_result_rev(t.id, 0, 1)
    snapshot = {"backend": "ark-prod", "model": "doubao-seed-2-0-pro-260215",
                "endpoint": "https://ark.example/api/v3", "reasoning_effort": None,
                "timeouts_s": {"probe": 60}, "parallelism": 64}
    rt.repo.freeze_task_inputs(t.id, run_id="20260920-130514", preflight={},
                               source_fingerprint={"objects": 204, "bytes": 1520331122,
                                                   "digest": "sha256:abc"},
                               vlm_snapshot=snapshot)
    r = c.get(f"/curation/api/v1/tasks/{t.id}")
    assert r.status_code == 200
    body = r.json()
    assert_schema("Task", body)
    assert r.headers["etag"] == f'"{body["updated_at"]}"'
    assert body["input"] == {"source": "tos", "uri": "tos://bucket/datasets/droid_100",
                             "region": "cn-beijing", "credential": "prod-tos"}
    assert body["vlm"] == {"backend": "ark-prod", "model": "doubao-seed-2-0-pro-260215",
                           "reasoning_effort": None, "snapshot": snapshot}
    assert body["source"] == {"objects": 204, "bytes": 1520331122, "digest": "sha256:abc"}
    assert [m["id"] for m in body["modules"]] == list(registry.ids())
    assert body["modules"][0]["name"] == "时间戳检查"
    assert body["pending_adjudication"] == 4 and body["result_rev"] == 1
    assert {link["rel"]: link["url"] for link in body["links"]} == {
        "task": f"https://kit.example.com/curation/tasks/{t.id}",
        "report": f"https://kit.example.com/curation/tasks/{t.id}/report",
        "adjudication": f"https://kit.example.com/curation/tasks/{t.id}/adjudication?status=pending"}

    rt.repo.delete_credential(cred.id)                       # finished task keeps its report
    rt.repo.delete_vlm_backend(backend.id)
    body = c.get(f"/curation/api/v1/tasks/{t.id}").json()
    assert_schema("Task", body)
    assert body["input"]["credential"] is None and body["output"]["credential"] is None
    assert body["vlm"] == {"backend": "ark-prod", "model": "doubao-seed-2-0-pro-260215",
                           "reasoning_effort": None, "snapshot": snapshot}   # names from start


def test_links_are_relative_without_public_base_url(client_for):
    c = client_for(base_path="/curation")
    t = seed_task(_rt(c).repo)
    links = c.get(f"/curation/api/v1/tasks/{t.id}").json()["links"]
    assert links == [{"rel": "task", "title": "Open task", "url": f"/curation/tasks/{t.id}",
                      "absolute": False}]


def test_unknown_task_and_other_owners(client_for):
    c = client_for()
    rt = _rt(c)
    other = seed_task(rt.repo, owner="someone-else")
    for path in (f"/api/v1/tasks/{other.id}", "/api/v1/tasks/task_nope",
                 f"/api/v1/tasks/{other.id}/subtasks", f"/api/v1/tasks/{other.id}/timeline",
                 f"/api/v1/tasks/{other.id}/logs", f"/api/v1/tasks/{other.id}/usage",
                 f"/events/tasks/{other.id}"):
        assert_error(c.get(path), "not_found")


# ---------------------------------------------------------------------------
# PATCH (D20) and If-Match
# ---------------------------------------------------------------------------

def _patch(c, task_id, body, etag=None, base="", **headers):
    if etag is None:
        etag = c.get(f"{base}/api/v1/tasks/{task_id}").headers["etag"]
    return c.patch(f"{base}/api/v1/tasks/{task_id}", json=body, headers={"If-Match": etag, **headers})


def test_patch_name_and_note_in_any_state(client_for):
    c = client_for()
    rt = _rt(c)
    t = seed_task(rt.repo)
    _finish(rt, t.id)
    r = _patch(c, t.id, {"name": "改个名字", "note": "第二轮"})
    assert r.status_code == 200, r.text
    assert_schema("Task", r.json())
    assert (r.json()["name"], r.json()["note"]) == ("改个名字", "第二轮")
    body = assert_error(_patch(c, t.id, {"episodes": {"mode": "all"}}), "task_state_conflict")
    assert body["error"]["details"] == {"state": "succeeded", "fields": ["episodes"]}
    assert "复制为新任务" in body["error"]["message"]


def test_patch_if_match_rules(client_for, clock):
    c = client_for()
    t = seed_task(_rt(c).repo)
    etag = c.get(f"/api/v1/tasks/{t.id}").headers["etag"]
    assert _patch(c, t.id, {"name": "a"}, etag=etag).status_code == 200
    body = assert_error(_patch(c, t.id, {"name": "b"}, etag=etag), "precondition_failed")
    assert "刷新" in body["error"]["message"]
    r = c.patch(f"/api/v1/tasks/{t.id}", json={"name": "c"})
    assert_error(r, "validation_failed")
    assert_error(_patch(c, t.id, {"name": "c"}, etag='"yesterday"'), "validation_failed")
    fresh = c.get(f"/api/v1/tasks/{t.id}").json()["updated_at"]
    assert _patch(c, t.id, {"name": "d"}, etag=str(fresh)).status_code == 200      # bare value works


def test_patch_body_is_validated_against_the_contract(client_for):
    c = client_for()
    t = seed_task(_rt(c).repo, state="created")
    body = assert_error(_patch(c, t.id, {"nmae": "typo"}), "validation_failed")
    assert "nmae" in body["error"]["message"]
    assert_error(_patch(c, t.id, {}), "validation_failed")
    assert_error(_patch(c, t.id, {"name": ""}), "validation_failed")
    r = c.patch(f"/api/v1/tasks/{t.id}", content=b"name=x",
                headers={"If-Match": '"1"', "Content-Type": "application/x-www-form-urlencoded"})
    assert "JSON" in assert_error(r, "validation_failed")["error"]["message"]
    r = c.patch(f"/api/v1/tasks/{t.id}", content=b"{not json",
                headers={"If-Match": '"1"', "Content-Type": "application/json"})
    assert_error(r, "validation_failed")


def _preflight(rt, *, episodes=200, kin="needs_input", vlm_available=True):
    modules = [{"id": "timestamp_check", "availability": "available"},
               {"id": "motion_quality", "availability": "unsupported",
                "reason": "数据集缺少 observation.state 列"},
               {"id": "kinematic_limits", "availability": kin,
                **({"reason": "robot_type not found", "input_hint": {
                    "field": "embodiment_id", "options": ["franka", "ur5"]}}
                   if kin == "needs_input" else {})},
               {"id": "task_success", "availability": "available" if vlm_available else "needs_input",
                **({} if vlm_available else {"reason": "no VLM backend",
                                             "input_hint": {"field": "vlm"}})}]
    result = {"schema_version": "1.0",
              "format": {"kind": "lerobot", "version": "v2", "supported": True, "detail": "v2"},
              "validation": [], "dataset": {"episode_count": episodes, "cameras": ["wrist"],
                                            "fps": 15.0, "robot_type": None, "total_frames": 1,
                                            "labels": {"with_task": 1, "without_task": 0},
                                            "profile": None},
              "modules": modules, "meta_fingerprint": "sha256:" + "0" * 64, "warnings": []}
    return rt.repo.put_preflight(request_hash="h", result=result, at=rt.clock())


def test_patch_a_created_task_resolves_every_field(client_for, tmp_path):
    c = client_for(local_data_root=tmp_path)
    rt = _rt(c)
    _cred(rt.repo, "src")
    _cred(rt.repo, "dst")
    rt.repo.create_vlm_backend(P.VlmBackend(
        id="", name="ark-prod", kind="ark", endpoint="https://ark.example", credential_id=None,
        models=[P.VlmModel(id="", backend_id="", model_name="doubao")]), None)
    t = seed_task(rt.repo, state="created")
    pf = _preflight(rt)
    body = {"name": "新配置", "input": {"source": "tos", "uri": "tos://Bucket//datasets/new/",
                                     "region": "cn-shanghai", "credential": "src"},
            "output": {"uri": "tos://out/deliveries/x/", "credential": "dst"}, "preflight_id": pf,
            "episodes": {"mode": "explicit", "expr": "3,10-12"},
            "modules": ["timestamp_check", {"id": "kinematic_limits"},
                        {"id": "task_success", "params": {"evidence_frames": "all"}}],
            "embodiment_id": "franka", "vlm": {"backend": "ark-prod", "model": "doubao",
                                              "reasoning_effort": "low"},
            "params": {"start_now": True, "export": False, "limits": {"cpu_concurrency": 4}}}
    r = _patch(c, t.id, body)
    assert r.status_code == 200, r.text
    got = r.json()
    assert_schema("Task", got)
    assert got["input"] == {"source": "tos", "uri": "tos://bucket/datasets/new",
                            "region": "cn-shanghai", "credential": "src"}
    assert got["output"] == {"uri": "tos://out/deliveries/x", "credential": "dst"}
    assert got["episodes"] == {"mode": "explicit", "expr": "3,10-12", "indices": [3, 10, 11, 12]}
    assert got["vlm"] == {"backend": "ark-prod", "model": "doubao", "reasoning_effort": "low",
                          "snapshot": None}                     # frozen only at start
    assert got["params"] == {"export": False, "limits": {"cpu_concurrency": 4}}
    mods = {m["id"]: m for m in got["modules"]}
    assert [m for m in mods if mods[m]["selected"]] == ["timestamp_check", "kinematic_limits",
                                                       "task_success"]
    assert mods["motion_quality"]["availability"] == "unsupported"
    assert mods["motion_quality"]["unavailable_reason"] == "数据集缺少 observation.state 列"
    stored = rt.repo.get_task(t.id)
    assert stored.delivery_key == "tos://out/deliveries/x" and stored.preflight["dataset"]
    assert {m.module_id: m.params for m in rt.repo.get_task_modules(t.id)}["task_success"] == \
        {"evidence_frames": "all"}
    events = rt.repo.list_events(resource=t.id).items
    assert events[0].action == "task.update" and "modules" in events[0].detail["fields"]


@pytest.mark.parametrize("body, field, words", [
    ({"modules": ["motion_quality"]}, "modules", "不可用"),
    ({"modules": ["kinematic_limits"]}, "embodiment_id", "机器人型号"),
    ({"modules": ["kinematic_limits"], "embodiment_id": "koch"}, "embodiment_id", "规格库"),
    ({"modules": ["task_success"]}, "vlm", "VLM"),
    ({"modules": ["timestamp_check", "timestamp_check"]}, "modules.1", "重复"),
    ({"modules": ["no_such_module"]}, "modules.0", "没有叫"),
    ({"modules": [{"id": "video_action_sync", "params": {"sync_plots": "none"}}]},
     "modules.0.params", "参数"),
    ({"episodes": {"mode": "explicit", "expr": "500-600"}}, "episodes.expr", "不存在"),
    ({"episodes": {"mode": "explicit", "expr": "5-3"}}, "episodes.expr", "颠倒"),
    ({"episodes": {"mode": "explicit", "expr": "@list.txt"}}, "episodes.expr", "命令行"),
    ({"input": {"source": "tos", "uri": "tos://bucket/x", "credential": "nope"}}, "input.credential",
     "不存在"),
    ({"input": {"source": "public", "uri": "tos://hf/dataset/x", "credential": "src"}},
     "input.credential", "不需要"),
    ({"input": {"source": "local", "uri": "/etc"}}, "input.source", "没有开启"),
    ({"output": {"uri": "tos://A_B/x", "credential": "src"}}, "output.uri", "存储桶"),
    ({"vlm": {"backend": "nope", "model": "m"}}, "vlm.backend", "不存在"),
])
def test_patch_rejections_say_what_to_fix(client_for, body, field, words):
    c = client_for()
    rt = _rt(c)
    _cred(rt.repo, "src")
    t = seed_task(rt.repo, state="created")
    pf = _preflight(rt)
    assert _patch(c, t.id, {"preflight_id": pf}).status_code == 200
    err = assert_error(_patch(c, t.id, body), "validation_failed")["error"]
    assert words in err["message"], err
    assert err["details"]["errors"][0]["field"] == field


def test_patch_input_change_needs_a_new_preflight_and_expiry(client_for, clock):
    c = client_for()
    rt = _rt(c)
    _cred(rt.repo, "src")
    t = seed_task(rt.repo, state="created")
    err = assert_error(_patch(c, t.id, {"input": {"source": "tos", "uri": "tos://bucket/other",
                                                  "credential": "src"}}), "validation_failed")
    assert "重新预检" in err["error"]["message"]
    pf = _preflight(rt)
    clock.advance(31 * 60 * 1000)
    assert_error(_patch(c, t.id, {"preflight_id": pf}), "preflight_expired")
    assert_error(_patch(c, t.id, {"preflight_id": "pf_unknown"}), "preflight_expired")


def test_local_input_stays_under_its_root(client_for, tmp_path):
    root = tmp_path / "datasets"
    root.mkdir()
    c = client_for(local_data_root=root)
    rt = _rt(c)
    t = seed_task(rt.repo, state="created")
    pf = _preflight(rt)
    ok = _patch(c, t.id, {"input": {"source": "local", "uri": str(root / "droid")},
                          "preflight_id": pf})
    assert ok.status_code == 200, ok.text
    assert ok.json()["input"] == {"source": "local", "uri": str(root / "droid")}
    pf = _preflight(rt)
    err = assert_error(_patch(c, t.id, {"input": {"source": "local", "uri": str(root / "../..")},
                                        "preflight_id": pf}), "validation_failed")
    assert "之下" in err["error"]["message"]


def test_patch_input_may_name_a_registered_dataset(client_for):
    """C4 1.1: ``input: {dataset_id}`` is the same as giving that dataset's address in full."""
    c = client_for()
    rt = _rt(c)
    key = _cred(rt.repo, "ds-key")
    _cred(rt.repo, "src")
    ds = seed_dataset(rt.repo, "tos://bucket/datasets/umi_640", region="cn-shanghai",
                      credential_id=key.id)
    t = seed_task(rt.repo, state="created")
    assert rt.repo.get_task(t.id).dataset_id is None

    err = assert_error(_patch(c, t.id, {"input": {"dataset_id": ds.id}}), "validation_failed")
    assert "重新预检" in err["error"]["message"]                     # a new input, a new preflight
    r = _patch(c, t.id, {"input": {"dataset_id": ds.id}, "preflight_id": _preflight(rt)})
    assert r.status_code == 200, r.text
    assert_schema("Task", r.json())
    assert r.json()["dataset_id"] == ds.id
    assert r.json()["input"] == {"source": "tos", "uri": "tos://bucket/datasets/umi_640",
                                 "region": "cn-shanghai", "credential": "ds-key"}
    listed = c.get("/api/v1/tasks", params={"dataset_id": ds.id}).json()
    assert [x["id"] for x in listed["items"]] == [t.id] and listed["items"][0]["dataset_id"] == ds.id

    # the same address in full links the registration; another address drops the link
    r = _patch(c, t.id, {"input": {"source": "tos", "uri": "tos://bucket/datasets/umi_640/",
                                   "region": "cn-shanghai", "credential": "src"}})
    assert r.status_code == 200 and r.json()["dataset_id"] == ds.id
    r = _patch(c, t.id, {"input": {"source": "tos", "uri": "tos://bucket/datasets/other",
                                   "credential": "src"}, "preflight_id": _preflight(rt)})
    assert r.status_code == 200 and r.json()["dataset_id"] is None

    err = assert_error(_patch(c, t.id, {"input": {"dataset_id": "ds_missing"}}),
                       "validation_failed")["error"]
    assert "不存在" in err["message"] and err["details"]["errors"][0]["field"] == "input.dataset_id"
    rt.repo.delete_credential(key.id)                         # the registration loses its key
    assert rt.repo.get_dataset(ds.id).credential_id is None
    err = assert_error(_patch(c, t.id, {"input": {"dataset_id": ds.id},
                                        "preflight_id": _preflight(rt)}), "validation_failed")
    assert "访问密钥已被删除" in err["error"]["message"]
    public = seed_dataset(rt.repo, "tos://hf-cache/lerobot/aloha_sim", source="public", region=None)
    r = _patch(c, t.id, {"input": {"dataset_id": public.id}, "preflight_id": _preflight(rt)})
    assert r.status_code == 200, r.text
    assert r.json()["input"] == {"source": "public", "uri": "tos://hf-cache/lerobot/aloha_sim"}


# ---------------------------------------------------------------------------
# delete / restore (D28, P12) and rebind
# ---------------------------------------------------------------------------

def test_delete_and_restore(client_for, clock):
    c = client_for(base_path="/curation")
    rt = _rt(c)
    running = seed_task(rt.repo, "running")
    change_task_state(rt.repo, None, running.id, {"queued"}, "running", at=T0)
    body = assert_error(c.delete(f"/curation/api/v1/tasks/{running.id}", headers=JSON), "task_state_conflict")
    assert "先停止" in body["error"]["message"] and body["error"]["details"]["state"] == "running"

    done = seed_task(rt.repo, "done")
    _finish(rt, done.id)
    r = c.delete(f"/curation/api/v1/tasks/{done.id}", headers=JSON)
    assert r.status_code == 204 and r.content == b""
    assert_error(c.get(f"/curation/api/v1/tasks/{done.id}"), "not_found")
    assert_error(c.delete(f"/curation/api/v1/tasks/{done.id}", headers=JSON), "not_found")
    r = c.post(f"/curation/api/v1/tasks/{done.id}/restore", headers=JSON)
    assert r.status_code == 200
    assert_schema("Task", r.json())
    assert r.json()["deleted_at"] is None
    actions = [e.action for e in rt.repo.list_events(resource=done.id).items]
    assert actions[:2] == ["task.restore", "task.delete"]

    c.delete(f"/curation/api/v1/tasks/{done.id}", headers=JSON)
    clock.advance(31 * 24 * 3600 * 1000)
    body = assert_error(c.post(f"/curation/api/v1/tasks/{done.id}/restore", headers=JSON), "not_found")
    assert "30 天" in body["error"]["message"]


def test_delete_waits_for_an_active_subtask(client_for):
    c = client_for()
    rt = _rt(c)
    t = seed_task(rt.repo)
    _finish(rt, t.id)
    rt.repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="reexport", scope={}, state="queued"))
    body = assert_error(c.delete(f"/api/v1/tasks/{t.id}", headers=JSON), "subtask_active")
    assert "子任务" in body["error"]["message"] and body["error"]["details"]["active_subtask"]


def test_rebind_credentials(client_for):
    c = client_for()
    rt = _rt(c)
    old = _cred(rt.repo, "old")
    _cred(rt.repo, "new")
    t = seed_task(rt.repo, input_cred_id=old.id, output_cred_id=old.id)
    body = assert_error(c.post(f"/api/v1/tasks/{t.id}/rebind-credentials",
                               json={"output_credential": "new"}), "task_state_conflict")
    assert "编辑" in body["error"]["message"]
    _finish(rt, t.id)
    rt.repo.delete_credential(old.id)
    r = c.post(f"/api/v1/tasks/{t.id}/rebind-credentials", json={"output_credential": "new"})
    assert r.status_code == 200, r.text
    assert_schema("Task", r.json())
    assert r.json()["output"]["credential"] == "new" and r.json()["input"]["credential"] is None
    assert_error(c.post(f"/api/v1/tasks/{t.id}/rebind-credentials", json={}), "validation_failed")
    assert_error(c.post(f"/api/v1/tasks/{t.id}/rebind-credentials",
                        json={"input_credential": "missing"}), "validation_failed")


# ---------------------------------------------------------------------------
# subtasks, usage, timeline
# ---------------------------------------------------------------------------

def test_subtasks_and_active_subtask(client_for, clock):
    c = client_for()
    rt = _rt(c)
    t = seed_task(rt.repo)
    _finish(rt, t.id, "completed_with_errors")
    s1 = rt.repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="retry", state="queued",
                                          scope={"modules": ["task_success"], "episodes": "errors"}))
    body = c.get(f"/api/v1/tasks/{t.id}/subtasks").json()
    assert_schema("openapi.yaml#/paths/~1tasks~1{id}~1subtasks/get/responses/200/content/"
                  "application~1json/schema", body)
    assert [s["id"] for s in body["items"]] == [s1.id]
    detail = c.get(f"/api/v1/tasks/{t.id}").json()
    assert_schema("Task", detail)
    assert detail["active_subtask"]["id"] == s1.id and detail["active_subtask"]["kind"] == "retry"
    item = c.get("/api/v1/tasks").json()["items"][0]
    assert item["active_subtask"] == s1.id


def test_usage_totals_use_the_actual_ledger_only(client_for):
    c = client_for()
    rt = _rt(c)
    t = seed_task(rt.repo)
    rt.repo.add_usage([
        P.UsageDelta(task_id=t.id, ledger="actual", module_id="task_success", call_kind="merged",
                     model_name="m", prompt_tokens=1000, completion_tokens=10, requests=1),
        P.UsageDelta(task_id=t.id, ledger="attributed", module_id="task_success",
                     call_kind="merged", model_name="m", prompt_tokens=600, requests=1),
        P.UsageDelta(task_id=t.id, ledger="attributed", module_id="skill_profile",
                     call_kind="merged", model_name="m", prompt_tokens=400, requests=1),
        P.UsageDelta(task_id=t.id, ledger="actual", module_id="autolabel", call_kind="caption",
                     model_name="m", subtask_id="sub_x", requests_unknown_usage=2)], at=T0)
    body = c.get(f"/api/v1/tasks/{t.id}/usage").json()
    assert_schema("UsageReport", body)
    assert body["totals"] == {"prompt_tokens": 1000, "completion_tokens": 10, "reasoning_tokens": 0,
                              "cached_tokens": 0, "requests": 1, "requests_unknown_usage": 2}
    assert sum(r["prompt_tokens"] for r in body["attributed"]) == 1000     # never added to totals
    assert c.get(f"/api/v1/tasks/{t.id}").json()["usage"] == body["totals"]


def test_timeline_from_events_and_subtasks(client_for, clock):
    c = client_for()
    rt = _rt(c)
    t = seed_task(rt.repo)
    for frm, to, kw in (("queued", "running", {}),
                        ("running", "pausing", {"pause_reason": "system"}),
                        ("pausing", "paused", {"pause_reason": "system"}),
                        ("paused", "queued", {}), ("queued", "running", {}),
                        ("running", "pausing", {"pause_reason": "user"}),
                        ("pausing", "paused", {}), ("paused", "queued", {}),
                        ("queued", "running", {}), ("running", "completed_with_errors", {})):
        clock.advance(1000)
        assert change_task_state(rt.repo, rt.hub, t.id, {frm}, to, at=clock(), **kw)
    clock.advance(1000)
    sub = rt.repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="retry", scope={},
                                           state="queued"))
    rt.repo.update_subtask_state(sub.id, {"queued"}, "running", at=clock.advance(1000))
    rt.repo.update_subtask_state(sub.id, {"running"}, "succeeded", at=clock.advance(1000))
    rt.repo.switch_result_rev(t.id, 0, 2)
    from daemon.transitions import record_revision
    record_revision(rt.repo, t.id, 2, at=clock.advance(1), subtask_id=sub.id)
    body = c.get(f"/api/v1/tasks/{t.id}/timeline").json()
    assert_schema("openapi.yaml#/paths/~1tasks~1{id}~1timeline/get/responses/200/content/"
                  "application~1json/schema", body)
    kinds = [e["kind"] for e in body["items"]]
    assert kinds == ["created", "started", "system_pause", "system_resume", "user_pause",
                     "user_resume", "finished", "subtask_started", "subtask_finished", "revision"]
    assert [e["at"] for e in body["items"]] == sorted(e["at"] for e in body["items"])
    assert body["items"][-1]["revision"] == 2 and body["items"][-2]["subtask_id"] == sub.id


def test_timeline_of_a_resumed_task(client_for, clock):
    """C5 1.2: a resume subtask that finishes ends the main run - the timeline says so."""
    from daemon.transitions import change_subtask_state, record_revision

    c = client_for()
    rt = _rt(c)
    t = seed_task(rt.repo)
    for frm, to, kw in (("queued", "running", {}), ("running", "stopping", {}),
                        ("stopping", "stopped", {"reason": "用户停止"})):
        clock.advance(1000)
        assert change_task_state(rt.repo, rt.hub, t.id, {frm}, to, at=clock(), **kw)
    sub = rt.repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="resume", scope={},
                                           state="queued"))
    assert change_subtask_state(rt.repo, rt.hub, sub.id, {"queued"}, "running",
                                at=clock.advance(1000))
    rt.repo.switch_result_rev(t.id, 0, 1)
    rt.repo.set_subtask_result_rev(sub.id, 1)
    record_revision(rt.repo, t.id, 1, at=clock.advance(1000), subtask_id=sub.id)
    assert change_task_state(rt.repo, rt.hub, t.id, {"stopped"}, "succeeded",
                             at=clock.advance(1000), publish_done=False)   # the parent first,
    assert change_subtask_state(rt.repo, rt.hub, sub.id, {"running"}, "succeeded",
                                at=clock.advance(1000))                    # then the subtask
    tail = [(e.event, e.data["state"], e.data["subtask_id"]) for e in rt.hub.buffered(t.id)][-3:]
    assert tail == [("state", "succeeded", None), ("state", "succeeded", sub.id),
                    ("done", "succeeded", sub.id)]          # one done, after both changes
    body = c.get(f"/api/v1/tasks/{t.id}/timeline").json()
    assert_schema("openapi.yaml#/paths/~1tasks~1{id}~1timeline/get/responses/200/content/"
                  "application~1json/schema", body)
    assert [(e["kind"], e["state"]) for e in body["items"]] == [
        ("created", None), ("started", "running"), ("stopped", "stopped"),
        ("subtask_started", "running"), ("revision", None), ("finished", "succeeded"),
        ("subtask_finished", "succeeded")]
    assert body["items"][5]["text"] == "继续运行后主流程结束：已完成"
    assert body["items"][6]["revision"] == 1 and body["items"][6]["subtask_id"] == sub.id
    task = rt.repo.get_task(t.id)
    assert task.finished_at == body["items"][5]["at"]              # the resume's end


def test_timeline_mixes_row_and_events(client_for, clock):
    """Started without an event (e.g. purged), then paused by the startup reconciliation."""
    from daemon.reconcile import reconcile

    c = client_for()
    rt = _rt(c)
    t = seed_task(rt.repo)
    rt.repo.update_task_state(t.id, {"queued"}, "running", at=T0 + 10)
    clock.advance(60_000)
    reconcile(rt.repo, rt.hub, rt.clock)
    items = c.get(f"/api/v1/tasks/{t.id}/timeline").json()["items"]
    assert [e["kind"] for e in items] == ["created", "started", "system_pause", "system_resume"]
    assert items[2]["text"] == "任务被系统暂停：Daemon 重启时任务还在运行，将自动恢复"


def test_timeline_falls_back_to_the_row_without_events(client_for):
    c = client_for()
    rt = _rt(c)
    t = seed_task(rt.repo)
    rt.repo.update_task_state(t.id, {"queued"}, "running", at=T0 + 10)
    rt.repo.update_task_state(t.id, {"running"}, "failed", reason="源数据变了", at=T0 + 20)
    items = c.get(f"/api/v1/tasks/{t.id}/timeline").json()["items"]
    assert [(e["kind"], e["at"]) for e in items] == [("created", T0), ("started", T0 + 10),
                                                    ("failed", T0 + 20)]
    assert "源数据变了" in items[-1]["text"]


# ---------------------------------------------------------------------------
# errors, unknown routes, pending operations
# ---------------------------------------------------------------------------

def test_unknown_routes_and_methods_answer_with_the_error_body(client_for):
    c = client_for(base_path="/curation")
    assert_error(c.get("/curation/api/v1/nope"), "not_found")
    assert_error(c.post("/curation/api/v1/tasks", json={}), "validation_failed")    # W5a: served
    assert_error(c.get("/curation/api/v1/tasks/x/report"), "not_found")             # W5, pending
    r = c.put("/curation/api/v1/tasks/x", json={})           # the path exists, the method not
    body = assert_error(r, "method_not_allowed", status=405)
    assert r.headers["allow"] == "DELETE, GET, HEAD, PATCH"
    assert body["error"]["details"]["allow"] == ["DELETE", "GET", "HEAD", "PATCH"]
    assert_error(c.delete("/curation/api/v1/overview", headers=JSON), "method_not_allowed",
                 status=405)
    assert_error(c.post("/curation/api/v1/datasets", json={}), "validation_failed")  # W5a
    assert_error(c.put("/curation/api/v1/datasets/browse", json={}), "method_not_allowed",
                 status=405)                                  # GET only; not an id either
    r = c.post("/curation/healthz")
    assert_error(r, "method_not_allowed", status=405)


def test_cross_site_writes_are_refused(client_for):
    c = client_for()
    t = seed_task(_rt(c).repo, state="created")
    for site in ("cross-site", "same-site"):                         # sibling subdomains too
        r = c.delete(f"/api/v1/tasks/{t.id}", headers={**JSON, "Sec-Fetch-Site": site})
        assert "其它站点" in assert_error(r, "validation_failed")["error"]["message"]
    r = c.post(f"/api/v1/tasks/{t.id}/restore", content=b"a=b",
               headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert_error(r, "validation_failed")
    for bodyless in (c.delete(f"/api/v1/tasks/{t.id}"),              # no Content-Type at all:
                     c.post(f"/api/v1/tasks/{t.id}/restore")):        # a CORS "simple request"
        assert "Content-Type" in assert_error(bodyless, "validation_failed")["error"]["message"]
    assert c.delete(f"/api/v1/tasks/{t.id}",
                    headers={**JSON, "Sec-Fetch-Site": "same-origin"}).status_code == 204


def test_oversized_bodies_are_refused(client_for):
    c = client_for()
    t = seed_task(_rt(c).repo)
    big = b'{"name": "' + b"x" * (1024 * 1024) + b'"}'
    r = c.patch(f"/api/v1/tasks/{t.id}", content=big,
                headers={"Content-Type": "application/json", "If-Match": '"1"'})
    assert "太大" in assert_error(r, "validation_failed")["error"]["message"]


def test_unexpected_errors_become_internal(client_for, monkeypatch):
    c = client_for()
    rt = _rt(c)

    def boom(*a, **kw):
        raise RuntimeError("secret=hunter2 leaked?")

    monkeypatch.setattr(rt.repo, "list_tasks", boom)
    body = assert_error(c.get("/api/v1/tasks"), "internal")
    assert "hunter2" not in str(body)


# ---------------------------------------------------------------------------
# Idempotency-Key
# ---------------------------------------------------------------------------

def test_idempotent_writes_replay_the_first_response(client_for):
    c = client_for()
    rt = _rt(c)
    t = seed_task(rt.repo)
    _finish(rt, t.id)
    key = {"Idempotency-Key": "agent-retry-0001"}
    first = c.delete(f"/api/v1/tasks/{t.id}", headers={**JSON, **key})
    again = c.delete(f"/api/v1/tasks/{t.id}", headers={**JSON, **key})
    assert first.status_code == again.status_code == 204
    assert again.headers.get("idempotent-replayed") == "true"
    assert_error(c.delete(f"/api/v1/tasks/{t.id}", headers=JSON), "not_found")           # without the key

    other = seed_task(rt.repo)
    _finish(rt, other.id)
    body = assert_error(c.delete(f"/api/v1/tasks/{other.id}", headers={**JSON, **key}), "idempotency_conflict")
    assert body["error"]["details"] == {"operation": "deleteTask"}
    assert_error(c.delete(f"/api/v1/tasks/{other.id}", headers={**JSON, "Idempotency-Key": "short"}),
                 "validation_failed")


def test_idempotency_does_not_record_failures(client_for):
    c = client_for()
    rt = _rt(c)
    t = seed_task(rt.repo)
    change_task_state(rt.repo, None, t.id, {"queued"}, "running", at=T0)
    key = {"Idempotency-Key": "retry-after-fix"}
    assert_error(c.delete(f"/api/v1/tasks/{t.id}", headers={**JSON, **key}), "task_state_conflict")
    change_task_state(rt.repo, None, t.id, {"running"}, "failed", at=T0)
    assert c.delete(f"/api/v1/tasks/{t.id}", headers={**JSON, **key}).status_code == 204


def test_idempotent_patch_replays_even_after_the_task_moved_on(client_for):
    c = client_for()
    t = seed_task(_rt(c).repo)
    etag = c.get(f"/api/v1/tasks/{t.id}").headers["etag"]
    headers = {"If-Match": etag, "Idempotency-Key": "rename-12345678"}
    first = c.patch(f"/api/v1/tasks/{t.id}", json={"name": "x"}, headers=headers)
    again = c.patch(f"/api/v1/tasks/{t.id}", json={"name": "x"}, headers=headers)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json() and again.headers["idempotent-replayed"] == "true"
    assert again.headers["etag"] == first.headers["etag"]
    assert_error(c.patch(f"/api/v1/tasks/{t.id}", json={"name": "y"}, headers=headers),
                 "idempotency_conflict")
