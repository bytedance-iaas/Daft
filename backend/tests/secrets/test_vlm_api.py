"""``/vlm-backends``: listing when it works, hand-entered models when it does not, reasoning
effort limited to each model's levels (null = no field), and the delete rules."""
from __future__ import annotations

import re

from daemon.repo import protocol as P
from daemon.transitions import change_task_state

from .conftest import API, JSON, add_backend, assert_error, assert_schema, runtime, seed_task, service
from .fakes import API_KEY, API_KEY2

DOUBAO = ["minimal", "low", "medium", "high"]
ALL = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def _finish(rt, task_id, to="succeeded"):
    for frm, nxt in (("queued", "running"), ("running", to)):
        assert change_task_state(rt.repo, rt.hub, task_id, {frm}, nxt, at=rt.clock())


def test_a_custom_backend_lists_its_models(secret_client, vlm_stub):
    vlm_stub.keys = {API_KEY}
    vlm_stub.models = ["Qwen2.5-VL-72B-Instruct", "doubao-seed-2-0-pro-260215"]
    c = secret_client()
    body = add_backend(c, vlm_stub, name="vllm", kind="custom", api_key=API_KEY,
                       max_concurrency=32)
    assert_schema("VlmBackend", body)
    assert body["has_api_key"] is True and body["models_listed"] is True
    assert body["verify_state"] == "ok" and body["max_concurrency"] == 32
    assert [(m["model_name"], m["source"], m["reasoning_effort"]) for m in body["models"]] == [
        ("Qwen2.5-VL-72B-Instruct", "listed", None), ("doubao-seed-2-0-pro-260215", "listed", None)]
    assert body["models"][0]["capabilities"] == {"vision": None, "reasoning_effort_levels": ALL}
    assert body["models"][1]["capabilities"]["reasoning_effort_levels"] == DOUBAO
    assert API_KEY not in str(body)
    assert vlm_stub.requests[-1]["path"] == "/v1/models"
    assert vlm_stub.requests[-1]["auth"] == f"Bearer {API_KEY}"
    items = c.get(f"{API}/vlm-backends").json()["items"]
    assert [b["id"] for b in items] == [body["id"]]
    assert_schema("VlmBackend", items[0])
    # the key lives in its own sealed row, never in the backend or its meta
    rt = runtime(c)
    backend = rt.repo.get_vlm_backend(body["id"])
    key_row = rt.repo.get_credential(backend.credential_id)
    assert key_row.kind == "custom_vlm" and key_row.name == f"vlm-backend/{body['id']}"
    assert API_KEY.encode() not in key_row.payload_enc and API_KEY not in str(key_row.payload_meta)
    assert service(c).backend_api_key(backend) == API_KEY


def test_when_models_cannot_be_listed_they_are_entered_by_hand(secret_client, vlm_stub):
    vlm_stub.keys = {API_KEY}                  # no /models (Ark's OpenAI-compatible API)
    vlm_stub.served = {"ep-20260921-abcde": "doubao-seed-1-6-251015"}
    c = secret_client()
    body = add_backend(c, vlm_stub, api_key=API_KEY)
    assert body["models"] == [] and body["models_listed"] is False
    assert body["verify_state"] == "unverified" and "手动填写" in body["last_verify_error"]

    r = c.post(f"{API}/vlm-backends/{body['id']}/models", json={"model_name": "ep-20260921-abcde"},
               headers=JSON)
    assert r.status_code == 201, r.text
    model = r.json()
    assert_schema("VlmModel", model)
    assert model["source"] == "manual" and model["reasoning_effort"] is None
    # the server named the model behind the endpoint ID: its four levels, not all seven
    assert model["capabilities"]["reasoning_effort_levels"] == DOUBAO
    sent = vlm_stub.chats()[-1]
    assert sent["model"] == "ep-20260921-abcde" and "reasoning_effort" not in sent
    assert sent["max_tokens"] == 1
    listed = c.get(f"{API}/vlm-backends").json()["items"][0]
    assert listed["verify_state"] == "ok" and listed["last_verify_error"] is None
    assert [m["model_name"] for m in listed["models"]] == ["ep-20260921-abcde"]


def test_a_model_that_fails_its_minimal_call_is_not_saved(secret_client, vlm_stub):
    vlm_stub.keys = {API_KEY}
    vlm_stub.chat_models = {"doubao-seed-2-0-pro-260215"}
    c = secret_client()
    backend = add_backend(c, vlm_stub, api_key=API_KEY)
    r = c.post(f"{API}/vlm-backends/{backend['id']}/models",
               json={"model_name": "doubao-not-opened"}, headers=JSON)
    body = assert_error(r, "model_check_failed")
    assert "doubao-not-opened" in body["error"]["message"]
    assert body["error"]["details"]["http_status"] == 404
    assert runtime(c).repo.get_vlm_backend(backend["id"]).models == []

    r = c.post(f"{API}/vlm-backends/{backend['id']}/models",
               json={"model_name": "doubao-seed-2-0-pro-260215"}, headers=JSON)
    assert r.status_code == 201
    assert_error(c.post(f"{API}/vlm-backends/{backend['id']}/models",
                        json={"model_name": "doubao-seed-2-0-pro-260215"}, headers=JSON),
                 "name_taken")


def test_reasoning_effort_is_limited_to_the_models_levels(secret_client, vlm_stub):
    c = secret_client()
    backend = add_backend(c, vlm_stub, kind="custom")
    before = len(vlm_stub.chats())
    r = c.post(f"{API}/vlm-backends/{backend['id']}/models",
               json={"model_name": "doubao-seed-2-0-pro-260215", "reasoning_effort": "xhigh"},
               headers=JSON)
    assert "等同于 high" in assert_error(r, "validation_failed")["error"]["message"]
    assert len(vlm_stub.chats()) == before                 # refused before any call

    r = c.post(f"{API}/vlm-backends/{backend['id']}/models",
               json={"model_name": "doubao-seed-2-0-pro-260215", "reasoning_effort": "high",
                     "max_concurrency": 16}, headers=JSON)
    assert r.status_code == 201, r.text
    model = r.json()
    assert model["reasoning_effort"] == "high" and model["max_concurrency"] == 16
    assert vlm_stub.chats()[-1]["reasoning_effort"] == "high"   # the check carried it

    url = f"{API}/vlm-backends/{backend['id']}/models/{model['id']}"
    assert_error(c.patch(url, json={"reasoning_effort": "max"}, headers=JSON), "validation_failed")
    assert_error(c.patch(url, json={"reasoning_effort": "turbo"}, headers=JSON),
                 "validation_failed")
    r = c.patch(url, json={"reasoning_effort": "minimal"}, headers=JSON)
    assert r.status_code == 200 and r.json()["reasoning_effort"] == "minimal"
    assert_schema("VlmModel", r.json())
    r = c.patch(url, json={"reasoning_effort": None, "max_concurrency": None}, headers=JSON)
    assert r.json()["reasoning_effort"] is None and r.json()["max_concurrency"] is None
    assert_error(c.patch(url, json={}, headers=JSON), "validation_failed")

    other = c.post(f"{API}/vlm-backends/{backend['id']}/models",
                   json={"model_name": "glm-4.5v", "reasoning_effort": "max"}, headers=JSON)
    assert other.status_code == 201                        # unknown model: all seven allowed
    assert other.json()["capabilities"]["reasoning_effort_levels"] == ALL


def test_a_task_level_override_is_checked_against_the_model(secret_client, vlm_stub):
    import pytest

    from daemon.secrets.effort import EffortNotAllowed

    c = secret_client()
    backend = add_backend(c, vlm_stub, kind="custom")
    model = c.post(f"{API}/vlm-backends/{backend['id']}/models",
                   json={"model_name": "doubao-seed-2-0-pro-260215"}, headers=JSON).json()
    svc = service(c)
    for effort in (None, "minimal", "high"):
        svc.check_task_effort(model["id"], effort)
    svc.check_task_effort(None, "max")                      # no model: nothing to check against
    with pytest.raises(EffortNotAllowed) as err:
        svc.check_task_effort(model["id"], "max")
    assert "等同于 high" in err.value.message_zh


def test_refresh_adds_listed_models_and_keeps_their_settings(secret_client, vlm_stub):
    c = secret_client()
    backend = add_backend(c, vlm_stub, kind="custom")
    model = c.post(f"{API}/vlm-backends/{backend['id']}/models",
                   json={"model_name": "doubao-seed-2-0-pro-260215", "reasoning_effort": "low"},
                   headers=JSON).json()
    r = c.post(f"{API}/vlm-backends/{backend['id']}/refresh-models", headers=JSON)
    assert r.status_code == 200 and r.json()["listed"] is False and r.json()["models"] == []
    assert "手动填写" in r.json()["note"]

    vlm_stub.models = ["doubao-seed-2-0-pro-260215", "qwen-vl"]
    r = c.post(f"{API}/vlm-backends/{backend['id']}/refresh-models", headers=JSON)
    assert r.status_code == 200, r.text
    assert_schema("openapi.yaml#/paths/~1vlm-backends~1{id}~1refresh-models/post/responses/200/"
                  "content/application~1json/schema", r.json())
    got = {m["model_name"]: m for m in r.json()["models"]}
    assert got["doubao-seed-2-0-pro-260215"]["id"] == model["id"]
    assert got["doubao-seed-2-0-pro-260215"]["reasoning_effort"] == "low"      # kept
    assert got["doubao-seed-2-0-pro-260215"]["source"] == "manual"
    assert got["qwen-vl"]["source"] == "listed"
    listed = c.get(f"{API}/vlm-backends").json()["items"][0]
    assert listed["models_listed"] is True and listed["verify_state"] == "ok"
    assert len(listed["models"]) == 2


def test_verify_tries_models_then_a_minimal_call(secret_client, vlm_stub):
    vlm_stub.keys = {API_KEY}
    c = secret_client()
    backend = add_backend(c, vlm_stub, api_key=API_KEY)
    url = f"{API}/vlm-backends/{backend['id']}/verify"
    r = c.post(url, headers=JSON)                          # no /models, no model to call
    assert_schema("VerifyResult", r.json())
    assert r.json()["verify_state"] == "unverified" and "添加一个模型" in r.json()["error"]
    c.post(f"{API}/vlm-backends/{backend['id']}/models", json={"model_name": "m1"}, headers=JSON)
    r = c.post(url, headers=JSON)
    assert r.json() == {"verify_state": "ok", "last_verified_at": r.json()["last_verified_at"],
                        "error": None}
    assert vlm_stub.chats()[-1]["model"] == "m1"
    vlm_stub.keys = {"a-newer-key-000"}                    # the key was revoked
    r = c.post(url, headers=JSON)
    assert r.json()["verify_state"] == "failed" and "HTTP 401" in r.json()["error"]
    assert API_KEY not in r.text                           # the stub echoed it; we did not
    assert c.get(f"{API}/vlm-backends").json()["items"][0]["models_listed"] is False
    vlm_stub.keys, vlm_stub.models = {API_KEY}, ["m1"]     # the server learned /models
    assert c.post(url, headers=JSON).json()["verify_state"] == "ok"
    assert c.get(f"{API}/vlm-backends").json()["items"][0]["models_listed"] is True


def test_update_keeps_an_empty_key_and_reverifies_a_new_one(secret_client, vlm_stub):
    vlm_stub.keys = {API_KEY}
    vlm_stub.models = ["m1"]
    c = secret_client()
    backend = add_backend(c, vlm_stub, api_key=API_KEY)
    requests = len(vlm_stub.requests)
    r = c.put(f"{API}/vlm-backends/{backend['id']}",
              json={"name": "ark-2", "api_key": "", "max_concurrency": 8}, headers=JSON)
    assert r.status_code == 200, r.text
    assert_schema("VlmBackend", r.json())
    assert r.json()["name"] == "ark-2" and r.json()["max_concurrency"] == 8
    assert len(vlm_stub.requests) == requests              # nothing to re-verify
    rt = runtime(c)
    assert service(c).backend_api_key(rt.repo.get_vlm_backend(backend["id"])) == API_KEY

    r = c.put(f"{API}/vlm-backends/{backend['id']}", json={"api_key": API_KEY2}, headers=JSON)
    assert r.json()["verify_state"] == "failed"            # the stub does not know the new key
    assert API_KEY2 not in r.text
    assert service(c).backend_api_key(rt.repo.get_vlm_backend(backend["id"])) == API_KEY2
    vlm_stub.keys.add(API_KEY2)
    requests = len(vlm_stub.requests)
    r = c.put(f"{API}/vlm-backends/{backend['id']}",
              json={"endpoint": vlm_stub.url + "/chat/completions"}, headers=JSON)
    assert r.json()["endpoint"] == vlm_stub.url            # a pasted full URL is the same base
    assert len(vlm_stub.requests) == requests and r.json()["verify_state"] == "failed"
    r = c.post(f"{API}/vlm-backends/{backend['id']}/verify", headers=JSON)
    assert r.json()["verify_state"] == "ok"


def test_a_keyless_custom_backend_gets_a_key_later(secret_client, vlm_stub):
    c = secret_client()
    backend = add_backend(c, vlm_stub, kind="custom")
    assert backend["has_api_key"] is False
    assert all(r["auth"] == "" for r in vlm_stub.requests)
    r = c.put(f"{API}/vlm-backends/{backend['id']}", json={"api_key": API_KEY}, headers=JSON)
    assert r.json()["has_api_key"] is True
    assert vlm_stub.requests[-1]["auth"] == f"Bearer {API_KEY}"


def test_colliding_ids_are_drawn_again(secret_client, vlm_stub, monkeypatch):
    """D45: the backend's and its key row's ids are random. When one is taken the save draws
    both again (the key row is named after the backend and sealed for its own id)."""
    from daemon.routes import vlm as routes

    vlm_stub.keys = {API_KEY, API_KEY2}
    vlm_stub.models = ["Qwen2.5-VL-72B-Instruct"]
    c = secret_client()
    rt = runtime(c)
    first = add_backend(c, vlm_stub, name="first", kind="custom", api_key=API_KEY)
    first_key = rt.repo.get_vlm_backend(first["id"]).credential_id
    fresh = routes.new_id
    draws = {"vb": [first["id"]], "cred": [first_key]}
    monkeypatch.setattr(routes, "new_id",
                        lambda prefix: draws[prefix].pop() if draws.get(prefix) else fresh(prefix))
    second = add_backend(c, vlm_stub, name="second", kind="custom", api_key=API_KEY2)
    assert draws == {"vb": [], "cred": []}
    assert re.fullmatch(r"vb-[a-z]{9}", second["id"]) and second["id"] != first["id"]
    backend = rt.repo.get_vlm_backend(second["id"])
    key_row = rt.repo.get_credential(backend.credential_id)
    assert key_row.id != first_key and key_row.name == f"vlm-backend/{second['id']}"
    assert service(c).backend_api_key(backend) == API_KEY2          # sealed for its own id
    assert service(c).backend_api_key(rt.repo.get_vlm_backend(first["id"])) == API_KEY
    assert [m["model_name"] for m in second["models"]] == ["Qwen2.5-VL-72B-Instruct"]

    # a backend without a key row gets one on update: that id may collide too
    bare = rt.repo.create_vlm_backend(P.VlmBackend(
        id="", name="bare", kind="custom", endpoint=first["endpoint"], credential_id=None), None)
    draws["cred"] = [first_key]
    r = c.put(f"{API}/vlm-backends/{bare.id}", json={"api_key": API_KEY2}, headers=JSON)
    assert r.status_code == 200, r.text
    assert r.json()["has_api_key"] is True and draws["cred"] == []
    updated = rt.repo.get_vlm_backend(bare.id)
    assert updated.credential_id not in (None, first_key)
    assert service(c).backend_api_key(updated) == API_KEY2


def test_backend_bodies_are_validated_without_echoing_the_key(secret_client, vlm_stub):
    c = secret_client()
    for body in ({"name": "a", "kind": "ark", "endpoint": vlm_stub.url},            # ark: key
                 {"name": "a", "kind": "ark", "endpoint": vlm_stub.url, "api_key": ""},
                 {"name": "a", "kind": "ark", "endpoint": vlm_stub.url, "api_key": 123456789},
                 {"name": "a", "kind": "custom", "endpoint": f"{vlm_stub.url}?key={API_KEY}"},
                 {"name": "a", "kind": "custom", "endpoint": "ftp://x"},
                 {"name": "a", "kind": "other", "endpoint": vlm_stub.url, "api_key": API_KEY}):
        r = c.post(f"{API}/vlm-backends", json=body, headers=JSON)
        assert_error(r, "validation_failed")
        assert API_KEY not in r.text and "123456789" not in r.text
    add_backend(c, vlm_stub, name="dup", kind="custom")
    assert_error(c.post(f"{API}/vlm-backends", json={"name": "dup", "kind": "custom",
                                                    "endpoint": vlm_stub.url}, headers=JSON),
                 "name_taken")


def test_delete_rules_for_backends_and_models(secret_client, vlm_stub):
    vlm_stub.models = ["m1", "m2"]
    c = secret_client()
    rt = runtime(c)
    backend = add_backend(c, vlm_stub, kind="custom", api_key=API_KEY)
    m1, m2 = backend["models"]
    task = seed_task(rt.repo, vlm_model_id=m1["id"])
    body = assert_error(c.delete(f"{API}/vlm-backends/{backend['id']}", headers=JSON),
                        "backend_in_use")
    assert body["error"]["details"] == {"active_tasks": 1, "historical_tasks": 0}
    assert_error(c.delete(f"{API}/vlm-backends/{backend['id']}/models/{m1['id']}", headers=JSON),
                 "backend_in_use")
    r = c.delete(f"{API}/vlm-backends/{backend['id']}/models/{m2['id']}", headers=JSON)
    assert r.status_code == 204
    assert_error(c.delete(f"{API}/vlm-backends/{backend['id']}/models/{m2['id']}", headers=JSON),
                 "not_found")

    _finish(rt, task.id)
    body = assert_error(c.delete(f"{API}/vlm-backends/{backend['id']}", headers=JSON),
                        "backend_in_use")
    assert body["error"]["details"]["confirm_required"] is True
    key_id = rt.repo.get_vlm_backend(backend["id"]).credential_id
    r = c.delete(f"{API}/vlm-backends/{backend['id']}?confirm=true", headers=JSON)
    assert r.status_code == 204, r.text
    assert c.get(f"{API}/vlm-backends").json()["items"] == []
    assert rt.repo.get_task(task.id).vlm_model_id is None
    try:
        rt.repo.get_credential(key_id)
        raise AssertionError("the backend's key row should be gone")
    except P.NotFound:
        pass


def test_another_owners_backend_is_invisible(secret_client, vlm_stub):
    c = secret_client()
    rt = runtime(c)
    other = rt.repo.create_vlm_backend(P.VlmBackend(id="", name="theirs", kind="custom",
                                                    endpoint=vlm_stub.url, credential_id=None,
                                                    owner_id="someone-else"), None)
    assert c.get(f"{API}/vlm-backends").json()["items"] == []
    assert_error(c.post(f"{API}/vlm-backends/{other.id}/verify", headers=JSON), "not_found")
    assert_error(c.post(f"{API}/vlm-backends/{other.id}/models", json={"model_name": "x"},
                        headers=JSON), "not_found")


def test_one_model_is_the_default_a_new_task_starts_with(secret_client, vlm_stub):
    """C4 1.6: at most one default across every backend; deleting it leaves none."""
    vlm_stub.models = ["Qwen2.5-VL-72B-Instruct", "glm-4.5v"]
    c = secret_client()
    first = add_backend(c, vlm_stub, name="vllm", kind="custom")
    second = add_backend(c, vlm_stub, name="vllm-2", kind="custom")
    a, b = first["models"][0], first["models"][1]
    other = second["models"][0]
    assert [m["is_default"] for m in first["models"] + second["models"]] == [False] * 4

    def url(backend, model):
        return f"{API}/vlm-backends/{backend['id']}/models/{model['id']}"

    def defaults():
        return [(m["model_name"], b_["name"]) for b_ in c.get(f"{API}/vlm-backends").json()["items"]
                for m in b_["models"] if m["is_default"]]

    r = c.patch(url(first, a), json={"is_default": True}, headers=JSON)
    assert r.status_code == 200 and r.json()["is_default"] is True
    assert_schema("VlmModel", r.json())
    assert defaults() == [(a["model_name"], "vllm")]

    # a second one takes the flag from the first, across backends
    assert c.patch(url(second, other), json={"is_default": True}, headers=JSON).status_code == 200
    assert defaults() == [(other["model_name"], "vllm-2")]

    # other fields still patch, and leave the flag where it is
    assert c.patch(url(first, b), json={"max_concurrency": 4}, headers=JSON).status_code == 200
    assert defaults() == [(other["model_name"], "vllm-2")]

    # clearing leaves none, and deleting the default takes it with the model
    assert c.patch(url(second, other), json={"is_default": False}, headers=JSON).json()[
        "is_default"] is False
    assert defaults() == []
    assert c.patch(url(first, a), json={"is_default": True}, headers=JSON).status_code == 200
    assert c.delete(url(first, a), headers=JSON).status_code == 204
    assert defaults() == []
