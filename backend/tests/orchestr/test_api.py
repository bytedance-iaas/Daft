"""The W5a routes without running a whole task: the state machine's 409s, pre-start checks
(422), validation, batches, purging, datasets, browsing and the episode preview.

Every response body is validated against C4.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import time

import pytest

from daemon.repo import protocol as P
from daemon.transitions import change_task_state

from ..secrets.fakes import AK, AK2, SK, SK2
from .conftest import (JSON, ListingTos, _seal_keys, assert_error, assert_schema, seed_dataset,
                       seed_task)

API = "/api/v1"


@pytest.fixture
def api(client_for, tmp_path, mini_source, monkeypatch):
    """``api(local_delivery=True)`` -> (client, fake TOS, dataset path)."""
    from daemon.util import now_ms

    def build(*, local_delivery: bool = True):
        root = tmp_path / "inputs"
        if not (root / "mini").exists():
            shutil.copytree(mini_source, root / "mini")
        if local_delivery:
            monkeypatch.setenv("CURATOR_LOCAL_DELIVERY_ROOT", str(tmp_path / "tos"))
        else:
            monkeypatch.delenv("CURATOR_LOCAL_DELIVERY_ROOT", raising=False)
        c = client_for(clock_fn=now_ms, local_data_root=root)
        tos = ListingTos()
        tos.add_key(AK, SK, read={"datasets"}, write=set())
        tos.add_key(AK2, SK2, read={"deliveries"}, write={"deliveries"})
        tos.put("datasets", "droid_100/meta/info.json", b'{"codebase_version": "v2.1"}')
        _seal_keys(c, tos)
        return c, tos, str(root / "mini")

    return build


def _rt(c):
    return c.app.state.runtime


def _cred(c, name):
    return _rt(c).repo.get_credential_by_name(name).id


def _seeded(c, state="created", **kw):
    rt = _rt(c)
    t = seed_task(rt.repo, state="created" if state == "created" else "queued",
                  input_cred_id=_cred(c, "in-key"), output_cred_id=_cred(c, "out-key"),
                  delivery="tos://deliveries/droid-50", input_uri="tos://datasets/droid_100", **kw)
    path = {"queued": [], "running": ["running"], "paused": ["running", "pausing", "paused"],
            "succeeded": ["running", "succeeded"], "stopped": ["running", "stopping", "stopped"],
            "completed_with_errors": ["running", "completed_with_errors"]}
    prev = t.state
    for nxt in path.get(state, []):
        kw2 = {"pause_reason": "user"} if nxt in ("pausing", "paused") else {}
        assert change_task_state(rt.repo, rt.hub, t.id, {prev}, nxt, at=rt.clock(), **kw2)
        prev = nxt
    return rt.repo.get_task(t.id)


# ---------------------------------------------------------------------------
# the state machine
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state,action", [
    ("created", "pause"), ("created", "resume"), ("created", "stop"),
    ("succeeded", "start"), ("succeeded", "pause"), ("succeeded", "resume"),
    ("succeeded", "stop"), ("stopped", "stop"), ("stopped", "start"), ("paused", "pause"),
    ("running", "start"), ("running", "resume")])
def test_illegal_transitions_answer_409_with_the_state(api, state, action):
    c, _, _ = api()
    t = _seeded(c, state)
    r = c.post(f"{API}/tasks/{t.id}/actions/{action}", headers=JSON)
    body = assert_error(r, "task_state_conflict")
    assert body["error"]["details"]["state"] == state
    assert c.get(f"{API}/tasks/{t.id}").json()["state"] == state


def test_unknown_action_and_task(api):
    c, _, _ = api()
    t = _seeded(c)
    assert_error(c.post(f"{API}/tasks/{t.id}/actions/jump", headers=JSON), "validation_failed")
    assert_error(c.post(f"{API}/tasks/task_nope/actions/start", headers=JSON), "not_found")


def test_legal_actions_on_tasks_nobody_runs(api):
    c, _, _ = api()
    queued = _seeded(c, "queued")
    r = c.post(f"{API}/tasks/{queued.id}/actions/stop", headers=JSON)
    assert r.status_code == 200 and r.json()["state"] == "stopped"
    assert_schema("Task", r.json())
    paused = _seeded(c, "paused")
    r = c.post(f"{API}/tasks/{paused.id}/actions/stop", headers=JSON)
    assert r.status_code == 200 and r.json()["state"] == "stopped", r.text
    paused2 = _seeded(c, "paused")
    r = c.post(f"{API}/tasks/{paused2.id}/actions/resume", headers=JSON)
    assert r.status_code == 200 and r.json()["state"] == "queued"
    kinds = [e["kind"] for e in c.get(f"{API}/tasks/{paused2.id}/timeline").json()["items"]]
    assert "user_resume" in kinds


@pytest.mark.parametrize("state,path,message", [
    ("succeeded", "retry", "错误"), ("succeeded", "continue", "已停止或失败"),
    ("running", "reexport", "已完成"), ("created", "adjudication/apply", "已完成"),
    ("stopped", "retry", "错误")])
def test_subtasks_need_their_parent_state(api, state, path, message):
    c, _, _ = api()
    t = _seeded(c, state)
    body = assert_error(c.post(f"{API}/tasks/{t.id}/{path}", headers=JSON, json={}),
                        "task_state_conflict")
    assert message in body["error"]["message"]


def test_one_subtask_at_a_time(api):
    c, _, _ = api()
    t = _seeded(c, "completed_with_errors")
    sub = _rt(c).repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="reexport", scope={},
                                               state="paused"))
    body = assert_error(c.post(f"{API}/tasks/{t.id}/retry", headers=JSON, json={}),
                        "subtask_active")
    assert body["error"]["details"]["active_subtask"] == sub.id
    assert re.fullmatch(r"sub-[a-z]{9}", sub.id)                          # D45


def test_subtask_bodies_are_validated(api):
    c, _, _ = api()
    t = _seeded(c, "completed_with_errors")
    assert_error(c.post(f"{API}/tasks/{t.id}/retry", headers=JSON, json={"modules": ["Bad!"]}),
                 "validation_failed")
    assert_error(c.post(f"{API}/tasks/{t.id}/adjudication/apply", headers=JSON,
                        json={"relabel_rerun": "v2"}), "validation_failed")
    assert_error(c.post(f"{API}/tasks/{t.id}/adjudication/apply", headers=JSON,
                        json={"extra": 1}), "validation_failed")
    assert_error(c.post(f"{API}/tasks/{t.id}/retry"), "validation_failed")      # no JSON type


def test_the_plan_is_404_before_the_task_ran(api):
    c, _, _ = api()
    t = _seeded(c)
    body = assert_error(c.get(f"{API}/tasks/{t.id}/plan"), "not_found")
    assert "还没开始" in body["error"]["message"]


# ---------------------------------------------------------------------------
# pre-start checks (D30, F2.6)
# ---------------------------------------------------------------------------

def _preflight_id(c, **kw) -> str:
    from ..daemon.conftest import sample_preflight

    return _rt(c).repo.put_preflight(request_hash="x", result=sample_preflight(**kw),
                                     at=_rt(c).clock())


def _body(c, ds_id, **extra):
    body = {"name": "droid", "input": {"dataset_id": ds_id},
            "output": {"uri": "tos://deliveries/droid-50", "credential": "out-key"},
            "preflight_id": _preflight_id(c), "episodes": {"mode": "head", "n": 5},
            "modules": ["timestamp_check"], "params": {"start_now": True}}
    body.update(extra)
    return body


def test_a_key_that_fails_the_write_probe_stops_the_start(api):
    c, tos, _ = api(local_delivery=False)
    ds = seed_dataset(_rt(c).repo, "tos://datasets/droid_100", credential_id=_cred(c, "in-key"))
    tos.pairs[AK2] = "revoked-secret"                     # the output key no longer works
    before = c.get(f"{API}/tasks").json()["total"]
    r = c.post(f"{API}/tasks", headers=JSON, json=_body(c, ds.id))
    body = assert_error(r, "precheck_failed")
    assert_schema("PrecheckDetails", body["error"]["details"])
    checks = {x["id"]: x for x in body["error"]["details"]["checks"]}
    assert checks["input"]["ok"] is True and checks["output"]["ok"] is False
    assert "vlm" not in checks                            # no VLM module selected
    assert SK2 not in r.text and "revoked-secret" not in r.text
    assert c.get(f"{API}/tasks").json()["total"] == before  # nothing was created

    r = c.post(f"{API}/tasks", headers=JSON,
               json=_body(c, ds.id, params={"start_now": False}))
    assert r.status_code == 201 and r.json()["state"] == "created"
    task_id = r.json()["id"]
    body = assert_error(c.post(f"{API}/tasks/{task_id}/actions/start", headers=JSON),
                        "precheck_failed")
    assert [x["id"] for x in body["error"]["details"]["checks"] if not x["ok"]] == ["output"]
    assert c.get(f"{API}/tasks/{task_id}").json()["state"] == "created"


def test_a_reasoning_effort_the_model_does_not_have_is_refused(api):
    from ..secrets.fakes import StubServer, VlmStub

    c, _, _ = api()
    with StubServer(VlmStub(models=["doubao-seed-2-0-pro-260215"])) as stub:
        r = c.post(f"{API}/vlm-backends", headers=JSON,
                   json={"name": "ark", "kind": "custom", "endpoint": stub.url})
        assert r.status_code == 201, r.text
        ds = seed_dataset(_rt(c).repo, "tos://datasets/droid_100",
                          credential_id=_cred(c, "in-key"))
        body = _body(c, ds.id, modules=["timestamp_check"],
                     vlm={"backend": "ark", "model": "doubao-seed-2-0-pro-260215",
                          "reasoning_effort": "max"}, params={"start_now": False})
        err = assert_error(c.post(f"{API}/tasks", headers=JSON, json=body), "validation_failed")
        assert "思考强度" in err["error"]["message"]


# ---------------------------------------------------------------------------
# creating
# ---------------------------------------------------------------------------

def test_create_validates_like_patch_and_a_batch_is_all_or_nothing(api):
    c, _, _ = api()
    ds = seed_dataset(_rt(c).repo, "tos://datasets/droid_100", credential_id=_cred(c, "in-key"))
    body = _body(c, ds.id, params={"start_now": False})
    assert_error(c.post(f"{API}/tasks", headers=JSON, json={**body, "modules": ["nope"]}),
                 "validation_failed")
    assert_error(c.post(f"{API}/tasks", headers=JSON, json={**body, "preflight_id": "pf_gone"}),
                 "preflight_expired")
    shared = {k: body[k] for k in ("episodes", "modules", "params")}
    items = [{"name": f"b{i}", "input": {"dataset_id": ds.id}, "output": body["output"],
              "preflight_id": body["preflight_id"]} for i in range(2)]
    r = c.post(f"{API}/tasks/batch", headers=JSON, json={"items": items, "shared": shared})
    assert r.status_code == 201, r.text
    out = r.json()
    assert_schema("openapi.yaml#/paths/~1tasks~1batch/post/responses/201/content/"
                  "application~1json/schema", out)
    assert [t["state"] for t in out["tasks"]] == ["created", "created"]
    total = c.get(f"{API}/tasks").json()["total"]
    bad = [items[0], {**items[1], "output": {"uri": "tos://deliveries/x", "credential": "no"}}]
    err = assert_error(c.post(f"{API}/tasks/batch", headers=JSON,
                              json={"items": bad, "shared": shared}), "validation_failed")
    assert err["error"]["details"]["item"] == 1
    assert c.get(f"{API}/tasks").json()["total"] == total


def test_idempotent_create_returns_the_first_task(api):
    c, _, _ = api()
    ds = seed_dataset(_rt(c).repo, "tos://datasets/droid_100", credential_id=_cred(c, "in-key"))
    body = _body(c, ds.id, params={"start_now": False})
    headers = {**JSON, "Idempotency-Key": "create-once-0001"}
    first = c.post(f"{API}/tasks", headers=headers, json=body)
    again = c.post(f"{API}/tasks", headers=headers, json=body)
    assert first.status_code == again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    assert again.headers.get("idempotent-replayed") == "true"


# ---------------------------------------------------------------------------
# purging a batch (D28)
# ---------------------------------------------------------------------------

def test_purge_needs_the_exact_path_and_removes_the_batch_and_latest(api, tmp_path):
    c, _, _ = api(local_delivery=True)
    rt = _rt(c)
    t = _seeded(c, "succeeded")
    rt.repo.freeze_task_inputs(t.id, run_id="20260921-120000", preflight={},
                               source_fingerprint={}, vlm_snapshot=None)
    rt.repo.set_export_fingerprint(t.id, "sha256:" + "c" * 64, False)
    batch = tmp_path / "tos" / "deliveries" / "droid-50" / "20260921-120000"
    (batch / "export").mkdir(parents=True)
    (batch / "export" / "manifest.json").write_text("{}")
    (batch / "report.md").write_text("x" * 100)
    (batch.parent / "latest").write_text("20260921-120000\n")
    other = batch.parent / "20260920-080000"
    other.mkdir()
    (other / "report.md").write_text("keep me")
    path = "tos://deliveries/droid-50/20260921-120000/"
    body = assert_error(c.post(f"{API}/tasks/{t.id}/purge-artifacts", headers=JSON,
                               json={"confirm_path": path.rstrip("/")}), "confirm_path_mismatch")
    assert body["error"]["details"]["expected"] == path
    r = c.post(f"{API}/tasks/{t.id}/purge-artifacts", headers=JSON, json={"confirm_path": path})
    assert r.status_code == 202, r.text
    assert r.json() == {"path": path, "bytes": 102, "latest_removed": True}
    # the purge runs in the background: the objects go first, the task is marked stale last
    # (a slow CI runner read the task in between, 2026-09-24)
    def stale() -> bool:
        return c.get(f"{API}/tasks/{t.id}").json()["delivery_stale"] is True

    deadline = time.monotonic() + 10
    while (batch.exists() or not stale()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not batch.exists() and not (batch.parent / "latest").exists()
    assert (other / "report.md").read_text() == "keep me"      # another batch is untouched
    assert stale()


def test_purge_refuses_running_tasks_and_tasks_without_a_batch(api):
    c, _, _ = api()
    running = _seeded(c, "running")
    assert_error(c.post(f"{API}/tasks/{running.id}/purge-artifacts", headers=JSON,
                        json={"confirm_path": "tos://x/y/"}), "task_state_conflict")
    done = _seeded(c, "succeeded")
    body = assert_error(c.post(f"{API}/tasks/{done.id}/purge-artifacts", headers=JSON,
                               json={"confirm_path": "tos://x/y/"}), "task_state_conflict")
    assert "还没有写过" in body["error"]["message"]


# ---------------------------------------------------------------------------
# preflight and datasets (CLI metadata commands, seconds)
# ---------------------------------------------------------------------------

def test_preflight_runs_the_cli_and_keeps_the_result(api):
    c, _, dataset = api()
    r = c.post(f"{API}/preflight", headers=JSON,
               json={"input": {"source": "local", "uri": dataset}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert_schema("PreflightResponse", body)
    assert body["result"]["dataset"]["episode_count"] == 8
    vlm = {m["id"]: m for m in body["result"]["modules"]}["task_success"]
    assert vlm["availability"] == "needs_input"            # no backend given
    assert _rt(c).repo.get_preflight(body["preflight_id"], max_age_ms=60_000,
                                     now=_rt(c).clock()) == body["result"]
    assert_error(c.post(f"{API}/preflight", headers=JSON,
                        json={"input": {"source": "local", "uri": dataset},
                              "vlm_backend": "missing"}), "validation_failed")
    assert_error(c.post(f"{API}/preflight", headers=JSON,
                        json={"input": {"source": "local", "uri": "/etc"}}), "validation_failed")


def test_register_recheck_and_repreflight_a_dataset(api):
    c, _, dataset = api()
    r = c.post(f"{API}/datasets", headers=JSON,
               json={"input": {"source": "local", "uri": dataset}, "note": "fixture"})
    assert r.status_code == 201, r.text
    ds = r.json()
    assert_schema("DatasetDetail", ds)
    assert ds["name"] == "mini" and ds["format"] == "lerobot_v2" and ds["episode_count"] == 8
    assert ds["listing"]["objects"] == 27 and [x["trigger"] for x in ds["checks"]] == ["add"]
    assert ds["meta_fingerprint"] == ds["preflight"]["meta_fingerprint"]
    again = c.post(f"{API}/datasets", headers=JSON,
                   json={"input": {"source": "local", "uri": dataset}})
    assert again.status_code == 200 and again.json()["id"] == ds["id"]
    same = c.post(f"{API}/datasets/{ds['id']}/recheck", headers=JSON)
    assert same.status_code == 200
    assert_schema("DatasetCheck", same.json())
    assert same.json()["result"] == "same" and same.json()["change"] is None
    target = pathlib.Path(dataset) / "data" / "chunk-000" / "episode_000001.parquet"
    os.utime(target, ns=(target.stat().st_atime_ns, target.stat().st_mtime_ns + 10**9))
    changed = c.post(f"{API}/datasets/{ds['id']}/recheck", headers=JSON).json()
    assert changed["result"] == "changed" and changed["change"]["modified"] == 1
    assert changed["change"]["meta_changed"] is False
    assert changed["change"]["sample_keys"] == ["data/chunk-000/episode_000001.parquet"]
    assert c.get(f"{API}/datasets/{ds['id']}").json()["check_state"] == "changed"
    r = c.post(f"{API}/datasets/{ds['id']}/repreflight", headers=JSON)
    assert r.status_code == 200, r.text
    assert_schema("DatasetDetail", r.json())
    assert r.json()["check_state"] == "ok" and r.json()["checks"][0]["trigger"] == "repreflight"
    assert_error(c.post(f"{API}/datasets/ds_missing/recheck", headers=JSON), "not_found")


# ---------------------------------------------------------------------------
# browsing and the episode preview (metadata in the Daemon)
# ---------------------------------------------------------------------------

def _page_schema(ref_item: str, body: dict) -> None:
    assert_schema("CursorPage", body)
    for item in body["items"]:
        assert_schema(ref_item, item)


def test_browse_local_tos_and_public(api):
    c, tos, dataset = api()
    r = c.get(f"{API}/datasets/browse", params={"source": "local"})
    assert r.status_code == 200, r.text
    _page_schema("BrowsedDataset", r.json())
    assert r.json()["items"] == [{"name": "mini", "uri": dataset, "format_hint": "lerobot_v2",
                                  "episodes": 8}]
    for name in ("droid_100", "pusht", "umi"):
        tos.put("datasets", f"lerobot/{name}/meta/info.json",
                json.dumps({"codebase_version": "v3.0" if name == "umi" else "v2.1",
                            "total_episodes": 10}).encode())
    tos.put("datasets", "lerobot/notes.txt", b"x")
    first = c.get(f"{API}/datasets/browse", params={
        "source": "tos", "uri": "tos://datasets/lerobot", "credential": "in-key", "limit": 2})
    assert first.status_code == 200, first.text
    _page_schema("BrowsedDataset", first.json())
    assert [i["name"] for i in first.json()["items"]] == ["droid_100", "pusht"]
    assert first.json()["has_more"] is True
    rest = c.get(f"{API}/datasets/browse", params={
        "source": "tos", "uri": "tos://datasets/lerobot", "credential": "in-key", "limit": 2,
        "cursor": first.json()["next_cursor"]}).json()
    assert rest["items"] == [{"name": "umi", "uri": "tos://datasets/lerobot/umi",
                              "format_hint": "lerobot_v3", "episodes": 10}]
    assert rest["has_more"] is False
    assert_error(c.get(f"{API}/datasets/browse", params={"source": "tos",
                                                         "uri": "tos://datasets/lerobot"}),
                 "validation_failed")                        # a private prefix needs a key
    public = c.get(f"{API}/datasets/browse", params={"source": "public"})
    assert public.status_code == 200 and public.json()["items"] == []   # not configured
    assert_error(c.get(f"{API}/datasets/browse", params={"source": "ftp"}), "validation_failed")


def test_episode_preview_pages_with_a_cursor(api):
    c, _, dataset = api()
    seen, cursor = [], None
    while True:
        params = {"source": "local", "uri": dataset, "limit": 3}
        if cursor:
            params["cursor"] = cursor
        r = c.get(f"{API}/datasets/episodes", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        _page_schema("EpisodePreview", body)
        seen += body["items"]
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
    assert [e["index"] for e in seen] == list(range(8))
    by = {e["index"]: e for e in seen}
    assert by[4]["task"] == "" and by[4]["task_source"] == "无"
    assert by[0]["task_source"] == "原始标注" and by[0]["length_s"] == 5.0
    ds = c.post(f"{API}/datasets", headers=JSON,
                json={"input": {"source": "local", "uri": dataset}}).json()
    r = c.get(f"{API}/datasets/episodes", params={"dataset_id": ds["id"], "limit": 50})
    assert [e["index"] for e in r.json()["items"]] == list(range(8))
    assert_error(c.get(f"{API}/datasets/episodes"), "validation_failed")


def test_episode_preview_on_tos_signs_every_camera(api):
    c, tos, _ = api()
    info = {"codebase_version": "v2.1", "fps": 10, "chunks_size": 1000,
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
            "features": {"observation.images.wrist": {"dtype": "video"},
                         "observation.state": {"dtype": "float32"}}}
    tos.put("datasets", "droid_100/meta/info.json", json.dumps(info).encode())
    tos.put("datasets", "droid_100/meta/episodes.jsonl", b"".join(
        json.dumps({"episode_index": i, "tasks": ["pick"], "length": 20}).encode() + b"\n"
        for i in range(3)))
    r = c.get(f"{API}/datasets/episodes", params={
        "source": "tos", "uri": "tos://datasets/droid_100", "credential": "in-key"})
    assert r.status_code == 200, r.text
    body = r.json()
    _page_schema("EpisodePreview", body)
    cam = body["items"][1]["cameras"][0]
    assert cam["name"] == "wrist" and "X-Tos-Signature" in cam["url"]
    assert "videos/chunk-000/observation.images.wrist/episode_000001.mp4" in cam["url"]
    assert body["items"][0]["length_s"] == 2.0


def test_the_summary_says_how_many_episodes_were_skipped(api):
    c, _, _ = api()
    t = _seeded(c, "succeeded")
    summary = {"total": 7, "passed": 5, "rejected": 2, "held": 0, "review": 0,
               "pass_rate": 0.714, "pending_adjudication": 0}
    _rt(c).repo.set_task_summary(t.id, {**summary, "skipped": 1})
    body = c.get(f"{API}/tasks/{t.id}").json()
    assert_schema("Task", body)
    assert body["summary"]["skipped"] == 1 and body["summary"]["total"] == 7
    _rt(c).repo.set_task_summary(t.id, summary)
    assert "skipped" not in c.get(f"{API}/tasks/{t.id}").json()["summary"]   # none: left out
