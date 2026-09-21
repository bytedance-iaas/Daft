"""W8 acceptance: no API response, log line, audit event or database byte contains a secret.

One scenario drives every W8 route - successes, failures and malformed requests - against
fakes that behave like careless providers: the fake TOS quotes the key pair in its error
messages and the OpenAI-compatible stub echoes the ``Authorization`` header. Then everything
the Daemon produced is searched for the planted values.
"""
from __future__ import annotations

import logging

from daemon.errors import error_body
from daemon.logconfig import JsonFormatter
from daemon.secrets import prechecks_for_task

from .conftest import API, JSON, runtime, seed_task, service
from .fakes import AK, AK2, API_KEY, API_KEY2, BAD_SK, KEY_IDS, SECRETS, SK, SK2, FakeTosError

WRONG_API_KEY = "ark-planted-wrong-key-00000009"
ALL_SECRETS = (*SECRETS, WRONG_API_KEY)


def test_the_fakes_really_echo_secrets(fake_tos, vlm_stub):
    """Without this the scenario below would prove little: the providers it talks to do put
    the secrets into their answers, so anything clean on our side was cleaned by us."""
    import pytest
    import requests

    from daemon.secrets.tos import TosKey

    with pytest.raises(Exception) as err:
        fake_tos.factory("https://e", "cn-beijing", TosKey(AK, BAD_SK)).list_buckets()
    assert BAD_SK in str(err.value) and AK in str(err.value)
    vlm_stub.keys = {API_KEY}
    r = requests.get(f"{vlm_stub.url}/models", headers={"Authorization": f"Bearer {API_KEY2}"},
                     timeout=3)
    assert r.status_code == 401 and API_KEY2 in r.text


class Recorder:
    """Every response the scenario got, as text (status line, headers, body)."""

    def __init__(self, client):
        self.client = client
        self.seen: list[tuple[str, str]] = []            # (what, text)

    def __call__(self, method: str, path: str, **kw):
        r = self.client.request(method, f"{API}{path}", **kw)
        headers = "\n".join(f"{k}: {v}" for k, v in r.headers.items())
        self.seen.append((f"{method} {path}", f"{r.status_code}\n{headers}\n{r.text}"))
        return r


def test_no_secret_reaches_a_response_a_log_line_or_the_database(secret_client, fake_tos,
                                                                  vlm_stub, caplog):
    caplog.set_level(logging.DEBUG)
    vlm_stub.keys = {API_KEY}
    vlm_stub.served = {"ep-20260921-x": "doubao-seed-2-0-pro-260215"}
    c = secret_client()
    rt, svc = runtime(c), service(c)
    call = Recorder(c)

    # -- access keys ---------------------------------------------------------------
    good = call("POST", "/credentials", headers=JSON, json={
        "name": "in", "access_key_id": AK, "secret_access_key": SK, "region": "cn-beijing"}).json()
    out = call("POST", "/credentials", headers={**JSON, "Idempotency-Key": "leak-test-0001"},
               json={"name": "out", "access_key_id": AK2, "secret_access_key": SK2,
                     "region": "cn-beijing", "test_bucket": "deliveries"}).json()
    call("POST", "/credentials", headers={**JSON, "Idempotency-Key": "leak-test-0001"},
         json={"name": "out", "access_key_id": AK2, "secret_access_key": SK2,
               "region": "cn-beijing", "test_bucket": "deliveries"})            # replay
    bad = call("POST", "/credentials", headers=JSON, json={
        "name": "typo", "access_key_id": AK, "secret_access_key": BAD_SK,
        "region": "cn-beijing"}).json()
    assert bad["verify_state"] == "failed"
    fake_tos.list_error = FakeTosError(400, "InvalidArgument", f"cannot parse {AK} / {SK}")
    odd = call("POST", f"/credentials/{good['id']}/verify", headers=JSON).json()
    assert odd["verify_state"] == "failed" and "cannot parse *** / ***" in odd["error"]
    fake_tos.list_error = None
    for body in ({"name": "x", "access_key_id": AK, "secret_access_key": 12345678901234,
                  "region": "cn-beijing"},
                 {"name": "x", "access_key_id": AK, "secret_access_key": SK, "region": "bad region"},
                 {"name": "x", "access_key_id": AK, "secret_access_key": SK, "region": "cn-beijing",
                  "endpoint": f"https://{AK}:{SK}@tos-cn-beijing.volces.com"},
                 {"name": "x", "access_key_id": AK, "secret_access_key": SK, "extra": SK}):
        assert call("POST", "/credentials", headers=JSON, json=body).status_code == 400
    call("GET", "/credentials")
    call("PUT", f"/credentials/{bad['id']}", headers=JSON, json={"secret_access_key": SK2})
    call("PUT", f"/credentials/{bad['id']}", headers=JSON, json={"access_key_id": AK2})   # 400
    call("PUT", f"/credentials/{bad['id']}", headers=JSON, json={"name": "typo-2",
                                                                "secret_access_key": ""})
    fake_tos.pairs[AK] = "revoked"
    call("POST", f"/credentials/{good['id']}/verify", headers=JSON)
    fake_tos.pairs[AK] = SK
    call("POST", f"/credentials/{good['id']}/verify", headers=JSON)

    # -- VLM backends and models ------------------------------------------------------
    ark = call("POST", "/vlm-backends", headers=JSON, json={
        "name": "ark", "kind": "ark", "endpoint": vlm_stub.url, "api_key": WRONG_API_KEY}).json()
    assert ark["verify_state"] == "failed"                     # the stub echoed the header
    call("PUT", f"/vlm-backends/{ark['id']}", headers=JSON, json={"api_key": API_KEY})
    call("POST", f"/vlm-backends/{ark['id']}/models", headers=JSON,
         json={"model_name": "ep-20260921-x", "reasoning_effort": "high"})
    vlm_stub.models = ["qwen-vl"]
    custom = call("POST", "/vlm-backends", headers=JSON, json={
        "name": "vllm", "kind": "custom", "endpoint": vlm_stub.url, "api_key": API_KEY}).json()
    call("POST", f"/vlm-backends/{custom['id']}/refresh-models", headers=JSON)
    model = custom["models"][0]
    call("PATCH", f"/vlm-backends/{custom['id']}/models/{model['id']}", headers=JSON,
         json={"reasoning_effort": "low", "max_concurrency": 8})
    call("PUT", f"/vlm-backends/{custom['id']}", headers=JSON, json={"api_key": API_KEY2})
    call("POST", f"/vlm-backends/{custom['id']}/verify", headers=JSON)           # 401 echo
    call("POST", f"/vlm-backends/{custom['id']}/models", headers=JSON,
         json={"model_name": "not-there"})                                        # 422 echo
    call("POST", "/vlm-backends", headers=JSON, json={
        "name": "leaky", "kind": "custom", "endpoint": f"{vlm_stub.url}?key={API_KEY}"})  # 400
    call("POST", "/vlm-backends", headers=JSON, json={
        "name": "leaky", "kind": "ark", "endpoint": vlm_stub.url, "api_key": 42424242424242})
    call("GET", "/vlm-backends")

    # -- probe and media -----------------------------------------------------------------
    call("POST", "/deliveries/probe", headers=JSON,
         json={"uri": "tos://deliveries/droid-50", "credential": "out"})
    call("POST", "/deliveries/probe", headers=JSON,
         json={"uri": "tos://datasets/droid-50", "credential": "in"})             # forbidden
    call("POST", "/deliveries/probe", headers=JSON,
         json={"uri": "tos://datasets/droid-50", "credential": "typo-2"})         # bad key
    task = seed_task(rt.repo, selected=("task_success",), input_cred_id=good["id"],
                     output_cred_id=out["id"], vlm_model_id=model["id"],
                     input_uri="tos://datasets/droid_100", delivery="tos://deliveries/droid-50")
    rt.repo.freeze_task_inputs(task.id, run_id="r1", preflight={}, source_fingerprint={},
                               vlm_snapshot=None)
    media = [call("GET", "/media/sign", params={"task": task.id, "scope": scope, "path": p})
             for scope, p in (("delivery", "details/a.mp4"), ("input", "videos/b.mp4"))]
    call("GET", "/media/sign", params={"task": task.id, "scope": "delivery", "path": "../x"})

    # -- prechecks: a precheck_failed body as W5 will return it ------------------------------
    fake_tos.pairs[AK] = "revoked-again"
    report = prechecks_for_task(svc, rt.repo.get_task(task.id))
    assert not report.ok
    err = report.error()
    call.seen.append(("precheck_failed", str(error_body(err.code, err.message, err.details))))

    # -- deletes ---------------------------------------------------------------------------------
    call("DELETE", f"/credentials/{good['id']}", headers=JSON)                     # 409
    call("DELETE", f"/vlm-backends/{ark['id']}", headers=JSON)
    call("DELETE", f"/credentials/{bad['id']}", headers=JSON)

    # -- search everything the Daemon produced ------------------------------------------------
    formatter = JsonFormatter()
    logs = caplog.text + "\n".join(formatter.format(r) for r in caplog.records)
    events = []
    cursor = None
    while True:
        page = rt.repo.list_events(cursor=cursor, limit=200)
        events += [f"{e.action} {e.resource} {e.detail}" for e in page.items]
        if not page.has_more:
            break
        cursor = page.next_cursor
    db_bytes = b"".join(p.read_bytes() for p in rt.settings.db_path.parent.glob("curator.db*")
                        if p.is_file())
    places = {"logs": logs, "events": "\n".join(events)}
    for what, text in call.seen:
        places[f"response {what}"] = text

    for secret in ALL_SECRETS:
        for where, text in places.items():
            assert secret not in text, f"{secret!r} leaked into {where}"
        assert secret.encode() not in db_bytes, f"{secret!r} is in the database file"
    signed = {text for what, text in call.seen if what.startswith("GET /media/sign")}
    for key_id in KEY_IDS:                       # key ids: only inside presigned URLs
        for where, text in places.items():
            if text in signed:
                continue
            assert key_id not in text, f"{key_id!r} leaked into {where}"
        assert key_id.encode() not in db_bytes
    assert any(AK2 in r.text for r in media[:1])                   # a presigned URL has to
    assert len(call.seen) > 35 and "SignatureDoesNotMatch" in places["events"] + logs + \
        "".join(text for _, text in call.seen)
