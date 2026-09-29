"""``POST /datasets/{id}/sign``: the ReRun viewer's reads of a registered dataset (design doc 15)."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from daemon.repo import protocol as P
from daemon.secrets.sigv4 import presign

from .conftest import (
    API,
    JSON,
    T0,
    add_access_key,
    assert_error,
    assert_schema,
    runtime,
    service,
)
from .fakes import AK, AK2, SK, SK2

URI = "tos://datasets/lerobot/droid_100"
BASE = "lerobot/droid_100"
HOST = "datasets.tos-s3-cn-beijing.volces.com"


def _register(c, cred_id, *, uri=URI, source="tos", region="cn-beijing", owner=P.DEFAULT_OWNER):
    ds, _ = runtime(c).repo.register_dataset(P.Dataset(
        id="", name=uri.rsplit("/", 1)[-1] or "bucket", source=source, uri=uri, region=region,
        credential_id=cred_id, preflight={}, meta_fingerprint="m",
        source_fingerprint={"count": 1, "bytes": 1, "digest": "d"}, preflighted_at=T0,
        owner_id=owner))
    return ds


def _sign(c, ds_id, requests, headers=JSON, **extra):
    return c.post(f"{API}/datasets/{ds_id}/sign", json={"requests": requests, **extra},
                  headers=headers)


def _get(key):
    return {"op": "get", "key": key}


def _events(c, action="dataset.viewer_sign"):
    return [e for e in runtime(c).repo.list_events(limit=200).items if e.action == action]


@pytest.fixture
def signer(secret_client):
    c = secret_client()
    cred = add_access_key(c, name="in")
    return c, cred, _register(c, cred["id"])


def test_reads_and_listings_are_signed_in_order_on_the_s3_endpoint(signer, fake_tos):
    c, _, ds = signer
    fake_tos.calls.clear()
    r = _sign(c, ds.id, [
        _get(f"{BASE}/meta/info.json"),
        {"op": "list", "prefix": f"{BASE}/", "delimiter": "/"},
        {"op": "list", "prefix": f"{BASE}/data/", "continuation_token": "1/ab+c==", "max_keys": 7},
    ])
    assert r.status_code == 200, r.text
    assert_schema("DatasetSignResponse", r.json())
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["expires_at"] == T0 + 1800 * 1000
    get, listing, page = (urlsplit(u) for u in r.json()["urls"])
    assert {get.netloc, listing.netloc, page.netloc} == {HOST}
    assert get.path == f"/{BASE}/meta/info.json" and listing.path == page.path == "/"
    q = parse_qs(listing.query)
    assert (q["list-type"], q["max-keys"], q["prefix"], q["delimiter"]) == (
        ["2"], ["1000"], [f"{BASE}/"], ["/"])
    q = parse_qs(page.query)
    assert (q["max-keys"], q["prefix"], q["continuation-token"]) == (
        ["7"], [f"{BASE}/data/"], ["1/ab+c=="])
    assert "delimiter" not in q
    assert q["X-Amz-Credential"] == [f"{AK}/20250919/cn-beijing/s3/aws4_request"]
    assert q["X-Amz-Expires"] == ["1800"]
    assert fake_tos.calls == []                        # local arithmetic, TOS is not asked


def test_the_urls_are_exactly_what_the_signer_makes(signer):
    c, _, ds = signer
    r = _sign(c, ds.id, [_get(f"{BASE}/videos/chunk-000/front/episode_000001.mp4")], ttl=600)
    expected = presign("GET", "https://tos-s3-cn-beijing.volces.com", "datasets",
                       f"{BASE}/videos/chunk-000/front/episode_000001.mp4", {},
                       access_key_id=AK, secret_access_key=SK, region="cn-beijing", ttl_s=600,
                       at_ms=T0)
    assert r.json() == {"expires_at": T0 + 600 * 1000, "urls": [expected]}


def test_signing_uses_the_public_s3_endpoint_even_when_the_pod_uses_the_internal_one(
        secret_client, monkeypatch):
    monkeypatch.setenv("TOS_ENDPOINT", "https://tos-cn-beijing.ivolces.com")
    c = secret_client()
    ds = _register(c, add_access_key(c, name="in")["id"])
    r = _sign(c, ds.id, [_get(f"{BASE}/meta/info.json")])
    assert urlsplit(r.json()["urls"][0]).netloc == HOST


def test_the_region_is_the_registrations_then_the_keys(secret_client):
    c = secret_client()
    cred = add_access_key(c, name="in")                           # the key says cn-beijing
    ds = _register(c, cred["id"], region="cn-shanghai")
    url = _sign(c, ds.id, [_get(f"{BASE}/a")]).json()["urls"][0]
    assert urlsplit(url).netloc == "datasets.tos-s3-cn-shanghai.volces.com"
    assert "%2Fcn-shanghai%2Fs3%2F" in url
    ds = _register(c, cred["id"], uri="tos://datasets/lerobot/other", region=None)
    url = _sign(c, ds.id, [_get("lerobot/other/a")]).json()["urls"][0]
    assert urlsplit(url).netloc == HOST


def test_a_session_token_is_signed_into_the_url(secret_client):
    from daemon.secrets.service import tos_payload

    c = secret_client()
    cred = add_access_key(c, name="sts")
    svc = service(c)                                  # sealed as temporary credentials would be
    blob, version = svc.sealer.seal(cred["id"], tos_payload(AK, SK, "STS-token/planted+0001"))
    runtime(c).repo.update_credential(cred["id"], payload_enc=blob, key_version=version)
    ds = _register(c, cred["id"])
    q = parse_qs(urlsplit(_sign(c, ds.id, [_get(f"{BASE}/a")]).json()["urls"][0]).query)
    assert q["X-Amz-Security-Token"] == ["STS-token/planted+0001"]


@pytest.mark.parametrize("item", [
    _get("lerobot/droid_100_v2/meta/info.json"),               # a sibling that shares the prefix
    _get("lerobot/other/meta/info.json"),
    _get(BASE),                                                 # the prefix itself
    _get(f"{BASE}/"),                                           # nothing after it
    _get(f"/{BASE}/meta/info.json"),                            # a leading slash
    _get(f"{BASE}/../droid_100_v2/x"),
    _get(f"{BASE}/./meta/info.json"),
    _get(f"{BASE}//meta/info.json"),                            # a literal key, not normalized
    _get(f"{BASE}/meta\\info.json"),
    _get(f"{BASE}/meta/info.json\x00"),
    _get(f"{BASE}/%2e%2e/droid_100_v2/x"),
    _get(f"{BASE}/a%2Fb"),
    _get(f"{BASE}/%2E/x"),
    {"op": "list", "prefix": "lerobot/droid_100"},             # would list droid_100_v2 too
    {"op": "list", "prefix": "lerobot/"},
    {"op": "list", "prefix": f"{BASE}/../"},
    {"op": "list", "prefix": f"{BASE}//"},
    {"op": "list", "prefix": f"/{BASE}/"},
])
def test_what_could_leave_the_dataset_is_refused(signer, item):
    c, _, ds = signer
    body = assert_error(_sign(c, ds.id, [_get(f"{BASE}/ok.json"), item]), "validation_failed")
    field = "key" if item["op"] == "get" else "prefix"
    assert body["error"]["details"]["errors"][0]["field"] == f"requests.1.{field}"
    assert body["error"]["message"].startswith("第 2 项：")
    assert "urls" not in body


def test_every_bad_item_is_reported_and_nothing_is_signed(signer):
    c, _, ds = signer
    body = assert_error(_sign(c, ds.id, [_get("x"), _get(f"{BASE}/ok"), _get("../y")]),
                        "validation_failed")
    assert [e["field"] for e in body["error"]["details"]["errors"]] == [
        "requests.0.key", "requests.2.key"]
    assert "另有 1 项" in body["error"]["message"]


@pytest.mark.parametrize("extra, requests", [
    ({"ttl": 30}, [_get(f"{BASE}/a")]),
    ({"ttl": 7200}, [_get(f"{BASE}/a")]),
    ({}, []),
    ({}, [_get(f"{BASE}/a")] * 101),
    ({}, [{"op": "put", "key": f"{BASE}/a"}]),                   # only reads
    ({}, [{"op": "get", "key": f"{BASE}/a", "bucket": "other"}]),  # the caller picks no bucket
    ({"region": "cn-shanghai"}, [_get(f"{BASE}/a")]),
    ({"credential": "other"}, [_get(f"{BASE}/a")]),
    ({}, [{"op": "list", "prefix": f"{BASE}/", "delimiter": ","}]),
    ({}, [{"op": "list", "prefix": f"{BASE}/", "max_keys": 1001}]),
    ({}, [_get("")]),
])
def test_the_request_shape_is_checked(signer, extra, requests):
    c, _, ds = signer
    assert_error(_sign(c, ds.id, requests, **extra), "validation_failed")


def test_limits_that_pass(signer):
    c, _, ds = signer
    for ttl in (60, 3600):
        assert _sign(c, ds.id, [_get(f"{BASE}/a")], ttl=ttl).status_code == 200
    r = _sign(c, ds.id, [_get(f"{BASE}/e{i}.mp4") for i in range(100)])
    assert r.status_code == 200 and len(r.json()["urls"]) == 100


def test_public_local_and_whole_bucket_registrations_are_not_signed(secret_client):
    c = secret_client()
    cred = add_access_key(c, name="in")
    public = _register(c, None, uri="tos://public-mirror/lerobot/pusht", source="public")
    local = _register(c, None, uri="/data/local/pusht", source="local", region=None)
    whole = _register(c, cred["id"], uri="tos://datasets")
    for ds, words in ((public, "公开"), (local, "本地"), (whole, "整个存储桶")):
        body = assert_error(_sign(c, ds.id, [_get("lerobot/pusht/a")]), "validation_failed")
        assert words in body["error"]["message"]


def test_missing_datasets_keys_and_custom_endpoints(secret_client):
    c = secret_client()
    assert_error(_sign(c, "ds-nosuchone", [_get(f"{BASE}/a")]), "not_found")
    theirs = _register(c, None, owner="someone-else")
    assert_error(_sign(c, theirs.id, [_get(f"{BASE}/a")]), "not_found")

    cred = add_access_key(c, name="in")
    ds = _register(c, cred["id"])
    r = c.delete(f"{API}/credentials/{cred['id']}", params={"confirm": "true"}, headers=JSON)
    assert r.status_code == 204, r.text
    body = assert_error(_sign(c, ds.id, [_get(f"{BASE}/a")]), "not_found")
    assert body["error"]["details"] == {"reason": "credential_missing"}
    assert "重新添加这个数据集" in body["error"]["message"]

    custom = add_access_key(c, name="custom", ak=AK2, sk=SK2,
                            endpoint="https://tos-cn-beijing.volces.com")
    ds = _register(c, custom["id"], uri="tos://datasets/lerobot/custom")
    body = assert_error(_sign(c, ds.id, [_get("lerobot/custom/a")]), "validation_failed")
    assert "自定义 endpoint" in body["error"]["message"]


def test_an_unreadable_key_is_a_server_error(signer, monkeypatch):
    from daemon.secrets.service import SecretsService, Unavailable

    c, _, ds = signer

    def broken(self, cred):
        raise Unavailable("secret_unreadable", "访问密钥解不开")

    monkeypatch.setattr(SecretsService, "tos_key_from", broken)
    body = assert_error(_sign(c, ds.id, [_get(f"{BASE}/a")]), "internal")
    assert body["error"]["details"] == {"reason": "secret_unreadable"}


def test_only_same_origin_json_requests(signer):
    c, _, ds = signer
    url = f"{API}/datasets/{ds.id}/sign"
    body = {"requests": [_get(f"{BASE}/a")]}
    assert_error(c.post(url, content=b'{"requests": []}', headers={"Content-Type": "text/plain"}),
                 "validation_failed")
    for site in ("cross-site", "same-site"):
        assert_error(c.post(url, json=body, headers={**JSON, "Sec-Fetch-Site": site}),
                     "validation_failed")
    assert c.post(url, json=body, headers={**JSON, "Sec-Fetch-Site": "same-origin"}) \
        .status_code == 200
    assert_error(c.get(url), "method_not_allowed")


def test_one_audit_event_an_hour_per_person_and_dataset(signer, clock):
    c, cred, ds = signer
    for _ in range(3):
        assert _sign(c, ds.id, [_get(f"{BASE}/a")]).status_code == 200
    assert_error(_sign(c, ds.id, [_get("x")]), "validation_failed")      # failures are not audited
    events = _events(c)
    assert len(events) == 1
    assert (events[0].resource, events[0].detail) == (ds.id, {"name": "droid_100",
                                                               "credential": "in"})
    other = _register(c, cred["id"], uri="tos://datasets/lerobot/other")
    _sign(c, other.id, [_get("lerobot/other/a")])
    assert len(_events(c)) == 2
    clock.advance(3600 * 1000 - 1)
    _sign(c, ds.id, [_get(f"{BASE}/a")])
    assert len(_events(c)) == 2
    clock.advance(1)
    _sign(c, ds.id, [_get(f"{BASE}/a")])
    assert len(_events(c)) == 3


def test_the_secret_key_is_never_in_the_answer(signer):
    c, _, ds = signer
    r = _sign(c, ds.id, [_get(f"{BASE}/a"), {"op": "list", "prefix": f"{BASE}/"}])
    for text in (r.text, *r.headers.values()):
        assert SK not in text and SK.replace("/", "%2F").replace("+", "%2B") not in text
