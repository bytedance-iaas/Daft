"""``/deliveries/probe`` (a real write) and ``/media/sign`` (prefix checks, public endpoint)."""
from __future__ import annotations

import pytest

from daemon.repo import protocol as P

from .conftest import API, JSON, T0, add_access_key, assert_error, assert_schema, runtime, seed_task
from .fakes import AK, AK2, SK, SK2

PROBE_RESULT = "ProbeResult"


def _probe(c, uri, credential, **extra):
    return c.post(f"{API}/deliveries/probe", json={"uri": uri, "credential": credential, **extra},
                  headers=JSON)


def test_the_delivery_probe_writes_and_deletes_a_real_object(secret_client, fake_tos):
    c = secret_client()
    add_access_key(c, name="out", ak=AK2, sk=SK2)
    fake_tos.calls.clear()
    r = _probe(c, "tos://Deliveries//droid-50/", "out")
    assert r.status_code == 200, r.text
    assert_schema(PROBE_RESULT, r.json())
    assert r.json() == {"ok": True}
    assert [(o["op"], o["bucket"], o["key"], o["ak"]) for o in fake_tos.calls] == [
        ("put_object", "deliveries", "droid-50/.curator-write-probe", AK2),
        ("delete_object", "deliveries", "droid-50/.curator-write-probe", AK2)]
    assert not any(k == ("deliveries", "droid-50/.curator-write-probe") for k in fake_tos.objects)


def test_probe_failures_say_why(secret_client, fake_tos):
    c = secret_client()
    add_access_key(c, name="read-only", ak=AK, sk=SK)
    r = _probe(c, "tos://datasets/x", "read-only")
    assert_schema(PROBE_RESULT, r.json())
    assert r.json()["ok"] is False and r.json()["error"]["code"] == "forbidden"
    assert "写入" in r.json()["error"]["message"] and SK not in r.text
    r = _probe(c, "tos://no-such-bucket/x", "read-only")
    assert r.json()["error"]["code"] == "not_found"
    fake_tos.pairs[AK] = "changed"                            # the key no longer works
    r = _probe(c, "tos://datasets/x", "read-only")
    assert r.json()["error"]["code"] == "auth_failed" and SK not in r.text
    fake_tos.down = True
    r = _probe(c, "tos://datasets/x", "read-only", region="cn-shanghai")
    assert r.json()["error"]["code"] == "unreachable"
    assert "tos-cn-shanghai" in r.json()["error"]["message"]


def test_a_probe_that_cannot_clean_up_still_counts_as_writable(secret_client, fake_tos):
    c = secret_client()
    add_access_key(c, name="out", ak=AK2, sk=SK2)
    fake_tos.fail_delete = True
    r = _probe(c, "tos://deliveries/x", "out")
    assert r.json()["ok"] is True and r.json()["error"]["code"] == "leftover"


def test_probe_input_errors(secret_client):
    c = secret_client()
    assert_error(_probe(c, "tos://deliveries/x", "nope"), "validation_failed")
    add_access_key(c, name="out", ak=AK2, sk=SK2)
    assert_error(_probe(c, "tos://deliveries/../x", "out"), "validation_failed")
    assert_error(c.post(f"{API}/deliveries/probe", json={"uri": "s3://x", "credential": "out"},
                        headers=JSON), "validation_failed")


# ---------------------------------------------------------------------------
# media signing
# ---------------------------------------------------------------------------

def _started_task(c, *, input_source="tos", in_cred=None, out_cred=None, run_id="20260921-1200"):
    rt = runtime(c)
    task = seed_task(rt.repo, delivery="tos://deliveries/droid-50",
                     input_uri="tos://datasets/droid_100", input_cred_id=in_cred,
                     output_cred_id=out_cred)
    if input_source != "tos":
        rt.repo.update_task_fields(task.id, if_updated_at=None, input_source=input_source,
                                   input_uri="tos://public-mirror/lerobot/pusht",
                                   input_cred_id=None)
    if run_id:
        rt.repo.freeze_task_inputs(task.id, run_id=run_id, preflight={}, source_fingerprint={},
                                   vlm_snapshot=None)
    return rt.repo.get_task(task.id)


def _sign(c, task_id, scope, path, **params):
    return c.get(f"{API}/media/sign", params={"task": task_id, "scope": scope, "path": path,
                                              **params})


def test_delivery_urls_are_signed_under_the_run_directory_on_the_public_endpoint(
        secret_client, fake_tos, clock):
    c = secret_client()
    inp = add_access_key(c, name="in")
    out = add_access_key(c, name="out", ak=AK2, sk=SK2)
    task = _started_task(c, in_cred=inp["id"], out_cred=out["id"])
    r = _sign(c, task.id, "delivery", "details/clips/ep000034.mp4")
    assert r.status_code == 200, r.text
    assert_schema("SignedUrl", r.json())
    assert r.json()["expires_at"] == T0 + 1800 * 1000
    assert r.headers["cache-control"] == "no-store"
    op = fake_tos.ops("presign")[-1]
    assert (op["bucket"], op["key"], op["ak"], op["expires"]) == (
        "deliveries", "droid-50/20260921-1200/details/clips/ep000034.mp4", AK2, 1800)
    assert op["endpoint"] == "https://tos-cn-beijing.volces.com"
    assert SK2 not in r.text and SK not in r.text

    r = _sign(c, task.id, "input", "videos/chunk-000/front/episode_000034.mp4", ttl=600)
    assert r.status_code == 200 and r.json()["expires_at"] == T0 + 600 * 1000
    op = fake_tos.ops("presign")[-1]
    assert (op["bucket"], op["key"], op["ak"]) == (
        "datasets", "droid_100/videos/chunk-000/front/episode_000034.mp4", AK)


def test_signing_uses_the_public_endpoint_even_when_the_pod_uses_the_internal_one(
        secret_client, fake_tos, monkeypatch):
    monkeypatch.setenv("TOS_ENDPOINT", "https://tos-cn-beijing.ivolces.com")
    c = secret_client()
    out = add_access_key(c, name="out", ak=AK2, sk=SK2)
    assert fake_tos.calls[-1]["endpoint"] == "https://tos-cn-beijing.ivolces.com"   # own calls
    task = _started_task(c, out_cred=out["id"])
    assert _sign(c, task.id, "delivery", "report.md").status_code == 200
    assert fake_tos.ops("presign")[-1]["endpoint"] == "https://tos-cn-beijing.volces.com"


def test_the_public_cache_bucket_is_not_signed(secret_client, fake_tos):
    c = secret_client()
    task = _started_task(c, input_source="public")
    r = _sign(c, task.id, "input", "videos/episode_000001.mp4")
    assert r.status_code == 200
    assert r.json()["url"] == ("https://public-mirror.tos-cn-beijing.volces.com/"
                               "lerobot/pusht/videos/episode_000001.mp4")
    assert fake_tos.ops("presign") == []


@pytest.mark.parametrize("path", [
    "../other-run/secret.mp4", "a/../../b.mp4", "..", "%2e%2e/x.mp4", "a/%2E%2E/x",
    "a\\..\\b", "a/b\x00.mp4", "", "/", "./.", "a/%2f/b",
])
def test_paths_that_could_leave_the_prefix_are_refused(secret_client, fake_tos, path):
    c = secret_client()
    out = add_access_key(c, name="out", ak=AK2, sk=SK2)
    task = _started_task(c, out_cred=out["id"])
    for scope in ("delivery", "input"):
        assert_error(_sign(c, task.id, scope, path), "validation_failed")
    assert fake_tos.ops("presign") == []


def test_redundant_slashes_and_dots_are_normalized(secret_client, fake_tos):
    c = secret_client()
    out = add_access_key(c, name="out", ak=AK2, sk=SK2)
    task = _started_task(c, out_cred=out["id"])
    assert _sign(c, task.id, "delivery", "/details//./clips/ep1.mp4").status_code == 200
    assert fake_tos.ops("presign")[-1]["key"] == "droid-50/20260921-1200/details/clips/ep1.mp4"


def test_sign_errors(secret_client):
    c = secret_client()
    out = add_access_key(c, name="out", ak=AK2, sk=SK2)
    not_started = _started_task(c, out_cred=out["id"], run_id=None)
    assert_error(_sign(c, not_started.id, "delivery", "x.mp4"), "not_found")
    task = _started_task(c, out_cred=None)                     # the key was deleted
    body = assert_error(_sign(c, task.id, "delivery", "x.mp4"), "not_found")
    assert "重新绑定" in body["error"]["message"]
    assert_error(_sign(c, "task_nope", "delivery", "x.mp4"), "not_found")
    ok = _started_task(c, out_cred=out["id"])
    for ttl in (30, 7200):
        assert_error(_sign(c, ok.id, "delivery", "x.mp4", ttl=ttl), "validation_failed")
    assert_error(_sign(c, ok.id, "elsewhere", "x.mp4"), "validation_failed")
    rt = runtime(c)
    local = seed_task(rt.repo)
    rt.repo.update_task_fields(local.id, if_updated_at=None, input_source="local",
                               input_uri="/data/local/ds")
    assert_error(_sign(c, local.id, "input", "x.mp4"), "validation_failed")
    theirs = seed_task(rt.repo, owner="someone-else")
    assert_error(_sign(c, theirs.id, "delivery", "x.mp4"), "not_found")
    assert isinstance(rt.repo.get_task(theirs.id, owner="someone-else"), P.Task)
