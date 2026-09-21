"""The TOS and OpenAI-compatible call helpers on their own (fake TOS, stdlib HTTP stub)."""
from __future__ import annotations

import pytest

from daemon.secrets import tos as T
from daemon.secrets import vlm as V
from daemon.secrets.scrub import Scrubber

from .fakes import AK, AK2, API_KEY, BAD_SK, SK, SK2, FakeNetworkError, FakeTosError

NOW = 1_758_300_000_000


def _key(ak=AK, sk=SK, **kw) -> T.TosKey:
    kw.setdefault("name", "prod-tos")
    return T.TosKey(ak, sk, credential_id="cred_1", region="cn-beijing", **kw)


def _client(fake, key):
    return fake.factory("https://tos-cn-beijing.volces.com", "cn-beijing", key)


# ---------------------------------------------------------------------------
# TOS
# ---------------------------------------------------------------------------

def test_tos_key_repr_shows_no_secret():
    text = repr(_key(session_token="tok-secret-0001"))
    assert AK not in text and SK not in text and "tok-secret-0001" not in text


@pytest.mark.parametrize("raw, want", [
    ("tos-cn-beijing.volces.com", "https://tos-cn-beijing.volces.com"),
    ("https://TOS-cn-shanghai.ivolces.com/", "https://tos-cn-shanghai.ivolces.com"),
    ("http://10.0.0.8:9000", "http://10.0.0.8:9000"),
    ("", None), (None, None),
])
def test_tos_endpoint_normalization(raw, want):
    assert T.normalize_endpoint(raw) == want


@pytest.mark.parametrize("raw", ["https://ak:sk@tos-cn-beijing.volces.com",
                                 "https://tos-cn-beijing.volces.com/bucket",
                                 "https://tos-cn-beijing.volces.com?x=1", "ftp://host"])
def test_tos_endpoints_with_credentials_or_paths_are_refused(raw):
    with pytest.raises(ValueError):
        T.normalize_endpoint(raw)


def test_endpoints_server_side_internal_browser_side_public():
    internal = "https://tos-cn-beijing.ivolces.com"
    same = T.endpoints("cn-beijing", None, internal)
    assert same.server == internal and same.browser == "https://tos-cn-beijing.volces.com"
    other = T.endpoints("cn-shanghai", None, internal)            # another region: public
    assert other.server == other.browser == "https://tos-cn-shanghai.volces.com"
    custom = T.endpoints("cn-beijing", "https://tos-cn-beijing.ivolces.com", None)
    assert custom.server == "https://tos-cn-beijing.ivolces.com"
    assert custom.browser == "https://tos-cn-beijing.volces.com"   # never sign on internal
    assert T.endpoints(None, None, None).region == "cn-beijing"


def test_classify_splits_identity_permission_location_and_network():
    s = Scrubber([SK])
    assert T.classify(FakeTosError(403, "SignatureDoesNotMatch", f"x {SK}"), s).kind == "auth"
    assert SK not in T.classify(FakeTosError(403, "SignatureDoesNotMatch", f"x {SK}"), s).detail
    assert T.classify(FakeTosError(403, "AccessDenied"), s).kind == "forbidden"
    assert T.classify(FakeTosError(404, "NoSuchBucket"), s).kind == "no_bucket"
    assert T.classify(FakeTosError(404, "", ""), s, bucket_op=True).kind == "no_bucket"
    assert T.classify(FakeTosError(404, "NoSuchKey"), s).kind == "no_object"
    assert T.classify(FakeTosError(503, "ServiceUnavailable"), s).kind == "server"
    assert T.classify(FakeNetworkError("dns"), s).kind == "unreachable"
    assert T.classify(FakeTosError(400, "InvalidArgument", "bad"), s).kind == "other"


def test_identity_check_lists_buckets_and_touches_no_object(fake_tos):
    result = T.verify_identity(_client(fake_tos, _key()), _key(), region="cn-beijing",
                               endpoint="e", now=NOW)
    assert result == T.Verification("ok", None, NOW)
    assert [c["op"] for c in fake_tos.calls] == ["list_buckets"]


def test_identity_check_marks_a_wrong_secret_and_scrubs_the_echo(fake_tos):
    key = _key(sk=BAD_SK)
    result = T.verify_identity(_client(fake_tos, key), key, region="cn-beijing",
                               endpoint="e", now=NOW)
    assert result.state == "failed" and "SignatureDoesNotMatch" in result.error
    assert BAD_SK not in result.error and AK not in result.error


def test_identity_check_without_list_permission(fake_tos):
    fake_tos.add_key("AKLTnolist0000", "SKnolist0000", list_buckets=False, read={"datasets"})
    no_bucket = _key("AKLTnolist0000", "SKnolist0000")
    r = T.verify_identity(_client(fake_tos, no_bucket), no_bucket, region="cn-beijing",
                          endpoint="e", now=NOW)
    assert r.state == "unverified" and "测试用存储桶" in r.error
    with_bucket = _key("AKLTnolist0000", "SKnolist0000", test_bucket="datasets")
    r = T.verify_identity(_client(fake_tos, with_bucket), with_bucket, region="cn-beijing",
                          endpoint="e", now=NOW)
    assert r.state == "ok"
    assert [c["op"] for c in fake_tos.calls][-2:] == ["list_buckets", "head_bucket"]
    wrong = _key("AKLTnolist0000", "SKnolist0000", test_bucket="nope-bucket")
    r = T.verify_identity(_client(fake_tos, wrong), wrong, region="cn-beijing", endpoint="e",
                          now=NOW)
    assert r.state == "failed" and "nope-bucket" in r.error and "cn-beijing" in r.error
    other = _key("AKLTnolist0000", "SKnolist0000", test_bucket="deliveries")
    r = T.verify_identity(_client(fake_tos, other), other, region="cn-beijing", endpoint="e",
                          now=NOW)
    assert r.state == "failed" and "没有访问权限" in r.error


def test_identity_check_when_tos_cannot_be_reached(fake_tos):
    fake_tos.down = True
    r = T.verify_identity(_client(fake_tos, _key()), _key(), region="cn-beijing",
                          endpoint="https://tos-cn-beijing.volces.com", now=NOW)
    assert r.state == "failed" and "连不上" in r.error


def test_write_probe_writes_and_deletes(fake_tos):
    key = _key(AK2, SK2)
    out = T.write_probe(_client(fake_tos, key), "tos://deliveries/droid-50", key=key,
                        region="cn-beijing", endpoint="e")
    assert out == T.ProbeOutcome(True, "ok", "")
    ops = [(c["op"], c["key"]) for c in fake_tos.calls]
    assert ops == [("put_object", "droid-50/.curator-write-probe"),
                   ("delete_object", "droid-50/.curator-write-probe")]
    assert ("deliveries", "droid-50/.curator-write-probe") not in fake_tos.objects


def test_write_probe_failures(fake_tos):
    key = _key()                                                   # reads datasets only
    out = T.write_probe(_client(fake_tos, key), "tos://datasets/x", key=key,
                        region="cn-beijing", endpoint="e")
    assert not out.ok and out.kind == "forbidden" and "写入" in out.reason
    out = T.write_probe(_client(fake_tos, key), "tos://missing-bucket/x", key=key,
                        region="cn-beijing", endpoint="e")
    assert not out.ok and out.kind == "no_bucket"
    fake_tos.fail_delete = True
    key2 = _key(AK2, SK2)
    out = T.write_probe(_client(fake_tos, key2), "tos://deliveries", key=key2,
                        region="cn-beijing", endpoint="e")
    assert out.ok and out.kind == "leftover" and T.PROBE_OBJECT in out.reason


def test_anonymous_urls_are_public_and_quoted():
    assert T.anonymous_url("public-mirror", "a b/c.mp4", "cn-beijing") == \
        "https://public-mirror.tos-cn-beijing.volces.com/a%20b/c.mp4"


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("https://ark.cn-beijing.volces.com/api/v3", "https://ark.cn-beijing.volces.com/api/v3"),
    ("https://ark.cn-beijing.volces.com/api/v3/", "https://ark.cn-beijing.volces.com/api/v3"),
    ("https://ark.cn-beijing.volces.com/api/v3/chat/completions",
     "https://ark.cn-beijing.volces.com/api/v3"),
    ("http://10.0.0.5:8000/v1/models", "http://10.0.0.5:8000/v1"),
])
def test_vlm_endpoint_normalization(raw, want):
    assert V.normalize_endpoint(raw) == want


@pytest.mark.parametrize("raw", ["", "ark.cn-beijing.volces.com/api/v3",
                                 f"https://user:{API_KEY}@host/v1",
                                 f"https://host/v1?api_key={API_KEY}", "https://host/v1#frag"])
def test_vlm_endpoints_that_could_carry_a_key_are_refused(raw):
    with pytest.raises(ValueError) as err:
        V.normalize_endpoint(raw)
    assert API_KEY not in str(err.value)


def test_minimal_body_has_no_reasoning_effort_field_when_null():
    body = V.minimal_request_body("m", None)
    assert "reasoning_effort" not in body
    assert body == {"model": "m", "max_tokens": 1, "temperature": 0,
                    "messages": [{"role": "user", "content": "ping"}]}
    assert V.minimal_request_body("m", "minimal")["reasoning_effort"] == "minimal"


def test_listing_and_minimal_call_against_the_stub(vlm_stub):
    vlm_stub.keys = {API_KEY}
    vlm_stub.models = ["qwen2.5-vl-72b", "qwen2.5-vl-7b", "qwen2.5-vl-72b"]
    vlm_stub.served = {"ep-20260921-abc": "doubao-seed-1-6-251015"}
    c = V.VlmConnector(list_timeout_s=3, call_timeout_s=3)
    listing = c.list_models(vlm_stub.url, API_KEY)
    assert listing.ok and listing.models == ("qwen2.5-vl-72b", "qwen2.5-vl-7b")
    call = c.minimal_call(vlm_stub.url, API_KEY, "ep-20260921-abc")
    assert call.ok and call.served_model == "doubao-seed-1-6-251015"
    assert vlm_stub.requests[-1]["auth"] == f"Bearer {API_KEY}"
    assert "reasoning_effort" not in vlm_stub.chats()[-1]
    c.minimal_call(vlm_stub.url, API_KEY, "m", "high")
    assert vlm_stub.chats()[-1]["reasoning_effort"] == "high"


def test_failures_are_classified_and_scrubbed(vlm_stub):
    vlm_stub.keys = {"another-key-0000"}
    c = V.VlmConnector(list_timeout_s=3, call_timeout_s=3)
    listing = c.list_models(vlm_stub.url, API_KEY)                 # the stub echoes the header
    assert not listing.ok and listing.failure.kind == "auth"
    assert API_KEY not in listing.failure.detail + listing.failure.reason
    vlm_stub.keys = None
    listing = c.list_models(vlm_stub.url, API_KEY)                 # no /models at all
    assert not listing.ok and listing.failure.kind == "unsupported"
    assert "手动填写" in listing.failure.reason
    vlm_stub.chat_models = {"known"}
    call = c.minimal_call(vlm_stub.url, API_KEY, "unknown-model")
    assert not call.ok and call.failure.kind == "not_found" and "unknown-model" in call.failure.reason
    vlm_stub.reject_efforts = {"max"}
    call = c.minimal_call(vlm_stub.url, API_KEY, "known", "max")
    assert not call.ok and call.failure.kind == "rejected" and call.failure.status == 400


def test_unreachable_endpoints():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                      # nothing listens there now
    c = V.VlmConnector(list_timeout_s=1, call_timeout_s=1)
    listing = c.list_models(f"http://127.0.0.1:{port}/v1", API_KEY)
    assert not listing.ok and listing.failure.kind == "unreachable"
    call = c.minimal_call(f"http://127.0.0.1:{port}/v1", API_KEY, "m")
    assert not call.ok and call.failure.kind == "unreachable" and "连不上" in call.failure.reason
