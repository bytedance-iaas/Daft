"""``/credentials``: identity-only verification on save, marks, sealed storage, delete rules."""
from __future__ import annotations

import pytest

from daemon.repo import protocol as P
from daemon.transitions import change_task_state

from .conftest import API, JSON, add_access_key, assert_error, assert_schema, runtime, seed_task, service
from .fakes import AK, AK2, BAD_SK, SK, SK2


def _finish(rt, task_id, to="succeeded"):
    for frm, nxt in (("queued", "running"), ("running", to)):
        assert change_task_state(rt.repo, rt.hub, task_id, {frm}, nxt, at=rt.clock())


def test_saving_checks_identity_only_and_never_returns_the_key(secret_client, fake_tos):
    c = secret_client()
    body = add_access_key(c)
    assert_schema("Credential", body)
    assert body["verify_state"] == "ok" and body["last_verify_error"] is None
    assert body["meta"] == {"region": "cn-beijing", "access_key_id_hint": AK[-4:]}
    assert body["references"] == {"active_tasks": 0, "historical_tasks": 0}
    assert AK not in str(body) and SK not in str(body)
    # one signed request, no object read or written (permissions are checked per task)
    assert [op["op"] for op in fake_tos.calls] == ["list_buckets"]
    assert fake_tos.calls[0]["endpoint"] == "https://tos-cn-beijing.volces.com"
    stored = runtime(c).repo.get_credential(body["id"])
    assert stored.kind == "tos" and stored.key_version == 1
    assert SK.encode() not in stored.payload_enc and AK.encode() not in stored.payload_enc
    key = service(c).tos_key(body["id"])
    assert (key.access_key_id, key.secret_access_key) == (AK, SK)

    listed = c.get(f"{API}/credentials").json()["items"]
    assert [item["id"] for item in listed] == [body["id"]]
    for item in listed:
        assert_schema("Credential", item)


def test_a_key_that_fails_verification_is_saved_and_marked(secret_client):
    c = secret_client()
    body = add_access_key(c, name="typo", sk=BAD_SK)
    assert body["verify_state"] == "failed"
    assert "SignatureDoesNotMatch" in body["last_verify_error"]
    assert BAD_SK not in body["last_verify_error"] and AK not in body["last_verify_error"]
    assert runtime(c).repo.get_credential(body["id"]).verify_state == "failed"


def test_keys_without_list_permission_use_the_test_bucket(secret_client, fake_tos):
    fake_tos.add_key("AKLTscoped0001", "SKscoped0001", list_buckets=False, read={"datasets"})
    c = secret_client()
    plain = add_access_key(c, name="scoped", ak="AKLTscoped0001", sk="SKscoped0001")
    assert plain["verify_state"] == "unverified" and "测试用存储桶" in plain["last_verify_error"]
    with_bucket = add_access_key(c, name="scoped2", ak="AKLTscoped0001", sk="SKscoped0001",
                                 test_bucket="datasets")
    assert with_bucket["verify_state"] == "ok"
    assert with_bucket["meta"]["test_bucket"] == "datasets"
    assert fake_tos.ops("head_bucket")[-1]["bucket"] == "datasets"


def test_verify_again(secret_client, fake_tos):
    c = secret_client()
    cred = add_access_key(c)
    fake_tos.pairs[AK] = "rotated-on-the-console"            # the key was revoked meanwhile
    r = c.post(f"{API}/credentials/{cred['id']}/verify", headers=JSON)
    assert r.status_code == 200, r.text
    assert_schema("VerifyResult", r.json())
    assert r.json()["verify_state"] == "failed" and SK not in r.text
    assert runtime(c).repo.get_credential(cred["id"]).verify_state == "failed"


def test_update_keeps_an_empty_secret_and_reverifies_identity_changes(secret_client, fake_tos):
    c = secret_client()
    cred = add_access_key(c)
    calls = len(fake_tos.calls)
    r = c.put(f"{API}/credentials/{cred['id']}",
              json={"name": "prod-tos-2", "secret_access_key": ""}, headers=JSON)
    assert r.status_code == 200, r.text
    assert_schema("Credential", r.json())
    assert r.json()["name"] == "prod-tos-2"
    assert len(fake_tos.calls) == calls                       # a rename is not re-verified
    key = service(c).tos_key(cred["id"])
    assert (key.access_key_id, key.secret_access_key) == (AK, SK)

    r = c.put(f"{API}/credentials/{cred['id']}", json={"access_key_id": AK2}, headers=JSON)
    assert_error(r, "validation_failed")                      # a new id needs its own secret
    assert AK2 not in r.text

    r = c.put(f"{API}/credentials/{cred['id']}",
              json={"access_key_id": AK2, "secret_access_key": SK2}, headers=JSON)
    assert r.status_code == 200 and r.json()["verify_state"] == "ok"
    assert r.json()["meta"]["access_key_id_hint"] == AK2[-4:]
    assert fake_tos.calls[-1]["ak"] == AK2
    key = service(c).tos_key(cred["id"])
    assert (key.access_key_id, key.secret_access_key) == (AK2, SK2)

    r = c.put(f"{API}/credentials/{cred['id']}", json={"secret_access_key": BAD_SK}, headers=JSON)
    assert r.json()["verify_state"] == "failed" and BAD_SK not in r.text

    r = c.put(f"{API}/credentials/{cred['id']}",
              json={"endpoint": "tos-cn-beijing.ivolces.com", "test_bucket": "deliveries"},
              headers=JSON)
    assert r.json()["meta"]["endpoint"] == "https://tos-cn-beijing.ivolces.com"
    assert fake_tos.calls[-1]["endpoint"] == "https://tos-cn-beijing.ivolces.com"
    r = c.put(f"{API}/credentials/{cred['id']}", json={"endpoint": "", "test_bucket": ""},
              headers=JSON)
    assert "endpoint" not in r.json()["meta"] and "test_bucket" not in r.json()["meta"]


def test_names_are_unique_and_the_backend_prefix_is_reserved(secret_client):
    c = secret_client()
    a = add_access_key(c, name="a")
    add_access_key(c, name="b")
    r = c.post(f"{API}/credentials", json={"name": "a", "access_key_id": AK,
                                           "secret_access_key": SK, "region": "cn-beijing"},
               headers=JSON)
    assert_error(r, "name_taken")
    assert_error(c.put(f"{API}/credentials/{a['id']}", json={"name": "b"}, headers=JSON),
                 "name_taken")
    r = c.post(f"{API}/credentials", json={"name": "vlm-backend/x", "access_key_id": AK,
                                           "secret_access_key": SK, "region": "cn-beijing"},
               headers=JSON)
    assert_error(r, "validation_failed")


@pytest.mark.parametrize("body", [
    {"name": "x", "access_key_id": AK, "secret_access_key": 1234567890123, "region": "cn-beijing"},
    {"name": "x", "access_key_id": AK, "secret_access_key": [SK], "region": "cn-beijing"},
    {"name": "x", "access_key_id": AK, "secret_access_key": SK, "region": "Not A Region"},
    {"name": "x", "access_key_id": AK, "secret_access_key": SK, "region": "cn-beijing",
     "endpoint": f"https://{AK}:{SK}@tos-cn-beijing.volces.com"},
    {"name": "x", "access_key_id": AK, "secret_access_key": SK, "region": "cn-beijing",
     "test_bucket": "Bad_Bucket"},
    {"name": "x", "access_key_id": "  ", "secret_access_key": SK, "region": "cn-beijing"},
    {"name": "x", "access_key_id": AK, "secret_access_key": SK, "region": "cn-beijing",
     "extra": SK},
])
def test_invalid_bodies_never_echo_the_secret(secret_client, body):
    c = secret_client()
    r = c.post(f"{API}/credentials", json=body, headers=JSON)
    assert_error(r, "validation_failed")
    assert SK not in r.text and "1234567890123" not in r.text and AK not in r.text


def test_backend_keys_are_not_managed_here(secret_client):
    c = secret_client()
    rt = runtime(c)
    backend = rt.repo.create_vlm_backend(
        P.VlmBackend(id="", name="b", kind="custom", endpoint="http://h/v1", credential_id=None),
        P.Credential(id="cred_vlm", name="vlm-backend/x", kind="custom_vlm", payload_enc=b"x" * 40,
                     key_version=1, payload_meta={}))
    assert backend.credential_id == "cred_vlm"
    assert c.get(f"{API}/credentials").json()["items"] == []
    assert_error(c.put(f"{API}/credentials/cred_vlm", json={"name": "y"}, headers=JSON),
                 "not_found")
    assert_error(c.delete(f"{API}/credentials/cred_vlm", headers=JSON), "not_found")
    assert_error(c.post(f"{API}/credentials/cred_vlm/verify", headers=JSON), "not_found")


def test_delete_is_refused_while_an_unfinished_task_uses_the_key(secret_client):
    c = secret_client()
    rt = runtime(c)
    cred = add_access_key(c)
    task = seed_task(rt.repo, input_cred_id=cred["id"], output_cred_id=cred["id"])
    listed = c.get(f"{API}/credentials").json()["items"][0]
    assert listed["references"] == {"active_tasks": 1, "historical_tasks": 0}
    body = assert_error(c.delete(f"{API}/credentials/{cred['id']}", headers=JSON),
                        "credential_in_use")
    assert body["error"]["details"] == {"active_tasks": 1, "historical_tasks": 0}

    _finish(rt, task.id)                                   # now only a finished task uses it
    body = assert_error(c.delete(f"{API}/credentials/{cred['id']}", headers=JSON),
                        "credential_in_use")
    assert body["error"]["details"]["confirm_required"] is True
    assert "confirm=true" in body["error"]["message"]
    r = c.delete(f"{API}/credentials/{cred['id']}?confirm=true", headers=JSON)
    assert r.status_code == 204, r.text
    after = rt.repo.get_task(task.id)
    assert after.input_cred_id is None and after.output_cred_id is None   # rebind later
    assert_error(c.delete(f"{API}/credentials/{cred['id']}", headers=JSON), "not_found")
    actions = [e.action for e in rt.repo.list_events(resource=cred["id"]).items]
    assert actions == ["credential.delete", "credential.create"]


def test_writes_accept_an_idempotency_key(secret_client, fake_tos):
    c = secret_client()
    body = {"name": "k", "access_key_id": AK, "secret_access_key": SK, "region": "cn-beijing"}
    headers = {**JSON, "Idempotency-Key": "create-k-000001"}
    first = c.post(f"{API}/credentials", json=body, headers=headers)
    again = c.post(f"{API}/credentials", json=body, headers=headers)
    assert first.status_code == again.status_code == 201
    assert again.headers.get("idempotent-replayed") == "true" and again.json() == first.json()
    assert len(c.get(f"{API}/credentials").json()["items"]) == 1
    assert len(fake_tos.ops("list_buckets")) == 1
    other = c.post(f"{API}/credentials", json={**body, "secret_access_key": SK2}, headers=headers)
    assert_error(other, "idempotency_conflict")                # same key, another secret
    record = runtime(c).repo.get_idempotent(key="create-k-000001", route="createCredential")
    assert SK not in str(record.response) and SK not in record.response["fingerprint"]


def test_audit_events_carry_names_not_values(secret_client):
    c = secret_client()
    cred = add_access_key(c)
    c.put(f"{API}/credentials/{cred['id']}", json={"secret_access_key": SK2}, headers=JSON)
    events = runtime(c).repo.list_events(resource=cred["id"]).items
    assert [e.action for e in events] == ["credential.update", "credential.create"]
    assert events[0].detail["fields"] == ["secret_access_key"]
    assert SK not in str([e.detail for e in events]) and SK2 not in str([e.detail for e in events])
