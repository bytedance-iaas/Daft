"""The three pre-start checks (D30): real read, real write probe, real minimal VLM call - and a
key that failed verification is saved but stopped here."""
from __future__ import annotations

import pytest

from daemon.errors import ApiError
from daemon.secrets import (
    InputTarget,
    OutputTarget,
    VlmSelection,
    prechecks_for_task,
    run_prechecks,
    task_needs_vlm,
)

from .conftest import API, JSON, add_access_key, add_backend, assert_schema, runtime, seed_task, service
from .fakes import AK, AK2, API_KEY, BAD_SK, SK, SK2

INPUT = "tos://datasets/droid_100"
OUTPUT = "tos://deliveries/droid-50"


def _setup(c, stub=None, *, in_sk=SK, selected=("timestamp_check",), effort=None, **task_kw):
    rt = runtime(c)
    inp = add_access_key(c, name="in", ak=AK, sk=in_sk)
    out = add_access_key(c, name="out", ak=AK2, sk=SK2)
    model_id = None
    if stub is not None:
        backend = add_backend(c, stub, kind="custom", api_key=API_KEY)
        r = c.post(f"{API}/vlm-backends/{backend['id']}/models",
                   json={"model_name": "doubao-seed-2-0-pro-260215", "reasoning_effort": effort},
                   headers=JSON)
        assert r.status_code == 201, r.text
        model_id = r.json()["id"]
    task = seed_task(rt.repo, selected=selected, input_uri=INPUT, delivery=OUTPUT,
                     input_cred_id=inp["id"], output_cred_id=out["id"], vlm_model_id=model_id,
                     state="created", **task_kw)
    return task, inp, out


def test_all_three_checks_run_for_real_and_pass(secret_client, fake_tos, vlm_stub):
    vlm_stub.keys = {API_KEY}
    c = secret_client()
    task, _, _ = _setup(c, vlm_stub, selected=("timestamp_check", "task_success"))
    fake_tos.calls.clear()
    chats = len(vlm_stub.chats())
    report = prechecks_for_task(service(c), task)
    assert report.ok, report.message()
    assert [r.id for r in report.results] == ["input", "output", "vlm"]
    assert all(r.code == "ok" for r in report.results)
    ops = sorted((o["op"], o["bucket"], o["key"], o["ak"]) for o in fake_tos.calls)
    assert ops == [("delete_object", "deliveries", "droid-50/.curator-write-probe", AK2),
                   ("get_object", "datasets", "droid_100/meta/info.json", AK),
                   ("put_object", "deliveries", "droid-50/.curator-write-probe", AK2)]
    assert len(vlm_stub.chats()) == chats + 1
    sent = vlm_stub.chats()[-1]
    assert sent["model"] == "doubao-seed-2-0-pro-260215" and "reasoning_effort" not in sent
    assert vlm_stub.requests[-1]["auth"] == f"Bearer {API_KEY}"
    report.raise_for_failure()                                   # nothing to raise


def test_a_failed_key_is_saved_and_marked_but_stops_the_task(secret_client, fake_tos):
    c = secret_client()
    task, inp, _ = _setup(c, in_sk=BAD_SK)
    assert inp["verify_state"] == "failed"                       # saved, marked (D30)
    report = prechecks_for_task(service(c), task)
    assert not report.ok
    failed = {r.id: r for r in report.failures}
    assert set(failed) == {"input"}
    assert failed["input"].code == "auth_failed" and "「in」" in failed["input"].reason
    with pytest.raises(ApiError) as err:
        report.raise_for_failure()
    e = err.value
    assert (e.code, e.status) == ("precheck_failed", 422)
    assert e.message.startswith("开始前的检查没有通过：输入数据集读不了，")
    assert [ch["id"] for ch in e.details["checks"]] == ["input", "output"]
    assert_schema("Error", {"error": {"code": e.code, "message": e.message, "details": e.details}})
    assert BAD_SK not in str(e.details) + e.message and AK not in str(e.details) + e.message


def test_the_output_check_is_a_write_probe(secret_client, fake_tos):
    c = secret_client()
    task, _, _ = _setup(c)
    fake_tos.grants[AK2].write = set()                           # can read, cannot write
    report = prechecks_for_task(service(c), task)
    out = next(r for r in report.results if r.id == "output")
    assert not out.ok and out.code == "forbidden" and "写入" in out.reason
    assert report.message().startswith("开始前的检查没有通过：交付目录写不进去，")
    assert fake_tos.ops("put_object")[-1]["key"] == "droid-50/.curator-write-probe"


def test_the_vlm_check_runs_only_when_a_vlm_module_is_selected(secret_client, vlm_stub):
    c = secret_client()
    task, _, _ = _setup(c, vlm_stub, selected=("timestamp_check", "dedup"))
    assert not task_needs_vlm(runtime(c).repo, task.id)
    chats = len(vlm_stub.chats())
    report = prechecks_for_task(service(c), task)
    assert [r.id for r in report.results] == ["input", "output"]
    assert len(vlm_stub.chats()) == chats
    rt = runtime(c)
    for module in ("task_success", "skill_profile"):
        other = seed_task(rt.repo, selected=(module,), input_uri=INPUT, delivery=OUTPUT,
                          input_cred_id=task.input_cred_id, output_cred_id=task.output_cred_id,
                          vlm_model_id=task.vlm_model_id)
        assert task_needs_vlm(rt.repo, other.id)
        report = prechecks_for_task(service(c), other)
        assert [r.id for r in report.results] == ["input", "output", "vlm"] and report.ok
    assert len(vlm_stub.chats()) == chats + 2
    forced = prechecks_for_task(service(c), task, need_vlm=True)
    assert [r.id for r in forced.results] == ["input", "output", "vlm"]


def test_the_effective_reasoning_effort_reaches_the_request(secret_client, vlm_stub):
    c = secret_client()
    task, _, _ = _setup(c, vlm_stub, selected=("task_success",), effort="low")
    prechecks_for_task(service(c), task)
    assert vlm_stub.chats()[-1]["reasoning_effort"] == "low"          # the model's setting
    rt = runtime(c)
    rt.repo.update_task_fields(task.id, if_updated_at=None, vlm_reasoning_effort="high")
    prechecks_for_task(service(c), rt.repo.get_task(task.id))
    assert vlm_stub.chats()[-1]["reasoning_effort"] == "high"         # the task's override


def test_a_started_task_checks_its_snapshot_with_the_current_key(secret_client, vlm_stub):
    vlm_stub.keys = {API_KEY, "ark-rotated-key-0003"}
    c = secret_client()
    task, _, _ = _setup(c, vlm_stub, selected=("task_success",))
    svc, rt = service(c), runtime(c)
    target = svc.vlm_target_for_task(task)
    snap = target.snapshot()
    assert API_KEY not in str(snap) and API_KEY not in repr(target)
    assert snap["model"] == "doubao-seed-2-0-pro-260215" and snap["reasoning_effort"] is None
    snap = {**snap, "reasoning_effort": "minimal"}                    # frozen at start (P17)
    rt.repo.freeze_task_inputs(task.id, run_id="r1", preflight={}, source_fingerprint={},
                               vlm_snapshot=snap)
    backend = rt.repo.get_vlm_backend(snap["backend_id"])
    c.put(f"{API}/vlm-backends/{backend.id}", json={"api_key": "ark-rotated-key-0003"},
          headers=JSON)
    c.patch(f"{API}/vlm-backends/{backend.id}/models/{snap['model_id']}",
            json={"reasoning_effort": "high"}, headers=JSON)          # a later edit
    report = prechecks_for_task(svc, rt.repo.get_task(task.id), checks=("vlm",))
    assert report.ok, report.message()
    assert vlm_stub.chats()[-1]["reasoning_effort"] == "minimal"      # the snapshot's
    assert vlm_stub.requests[-1]["auth"] == "Bearer ark-rotated-key-0003"   # the key: live


def test_missing_keys_and_backends_are_reported(secret_client, vlm_stub):
    c = secret_client()
    task, _, out = _setup(c, vlm_stub, selected=("task_success",))
    rt = runtime(c)
    rt.repo.update_task_fields(task.id, if_updated_at=None, output_cred_id=None,
                               vlm_model_id=None)
    report = prechecks_for_task(service(c), rt.repo.get_task(task.id))
    codes = {r.id: r.code for r in report.results}
    assert codes == {"input": "ok", "output": "credential_missing", "vlm": "model_missing"}
    snap_task = seed_task(rt.repo, selected=("task_success",))
    rt.repo.freeze_task_inputs(snap_task.id, run_id="r", preflight={}, source_fingerprint={},
                               vlm_snapshot={"backend_id": "vb_gone", "backend": "old-ark",
                                             "model": "m"})
    report = prechecks_for_task(service(c), rt.repo.get_task(snap_task.id), checks=("vlm",))
    assert report.results[0].code == "backend_missing" and "old-ark" in report.results[0].reason


def test_checks_can_be_narrowed_for_subtasks(secret_client, fake_tos, vlm_stub):
    c = secret_client()
    task, _, _ = _setup(c, vlm_stub, selected=("task_success",))
    report = prechecks_for_task(service(c), task, checks=("input", "output"))   # re-export
    assert [r.id for r in report.results] == ["input", "output"]
    with pytest.raises(ValueError):
        prechecks_for_task(service(c), task, checks=("bogus",))


def test_public_and_local_inputs(secret_client, fake_tos, tmp_path):
    local_root = tmp_path / "local"
    (local_root / "ds" / "meta").mkdir(parents=True)
    (local_root / "ds" / "meta" / "info.json").write_text("{}")
    c = secret_client(local_data_root=local_root)
    svc = service(c)
    report = run_prechecks(svc, input=InputTarget("public", "tos://public-mirror/lerobot/pusht"))
    assert report.ok and fake_tos.ops("get_object")[-1]["ak"] is None      # anonymous
    report = run_prechecks(svc, input=InputTarget("public", "tos://public-mirror/lerobot/none"))
    assert report.results[0].code == "not_found" and "meta/info.json" in report.results[0].reason
    assert run_prechecks(svc, input=InputTarget("local", str(local_root / "ds"))).ok
    outside = run_prechecks(svc, input=InputTarget("local", str(tmp_path)))
    assert outside.results[0].code == "forbidden"
    missing = run_prechecks(svc, input=InputTarget("local", str(local_root / "nope")))
    assert missing.results[0].code == "not_found"


def test_unreachable_tos_and_crashing_checks(secret_client, fake_tos, monkeypatch):
    c = secret_client()
    task, inp, out = _setup(c)
    svc = service(c)
    fake_tos.down = True
    report = prechecks_for_task(svc, task)
    assert {r.code for r in report.results} == {"unreachable"}
    fake_tos.down = False

    def boom(*a, **k):
        raise RuntimeError(f"surprise with {SK}")

    monkeypatch.setattr(svc, "tos_client", boom)
    report = run_prechecks(svc, input=InputTarget("tos", INPUT, credential_id=inp["id"]),
                           output=OutputTarget(OUTPUT, credential_id=out["id"]))
    assert not report.ok and SK not in report.message() and SK not in str(report.details())


def test_run_prechecks_without_a_stored_task(secret_client, vlm_stub):
    c = secret_client()
    task, inp, out = _setup(c, vlm_stub, selected=("task_success",))
    report = run_prechecks(service(c), input=InputTarget("tos", INPUT, "cn-beijing", inp["id"]),
                           output=OutputTarget(OUTPUT, "cn-beijing", out["id"]),
                           vlm=VlmSelection(model_id=task.vlm_model_id))
    assert report.ok and [r.id for r in report.results] == ["input", "output", "vlm"]
    assert all(r.elapsed_ms >= 0 for r in report.results)
