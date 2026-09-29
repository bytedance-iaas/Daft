"""SigV4 query-string presigning for the ReRun viewer (design doc 15 §2.4, K1)."""
from __future__ import annotations

import calendar
import datetime
from urllib.parse import parse_qsl, quote, urlsplit

import pytest

from daemon.secrets import tos as T
from daemon.secrets.sigv4 import presign

from .fakes import AK, SK

ENDPOINT = "https://tos-s3-cn-beijing.volces.com"
WHEN = calendar.timegm(datetime.datetime(2026, 9, 28, 12, 34, 56).timetuple()) * 1000


def _sign(key="", query=None, **kw):
    kw.setdefault("access_key_id", "AKLTexample0001")
    kw.setdefault("secret_access_key", "SKexample/Secret+Value==")
    kw.setdefault("region", "cn-beijing")
    kw.setdefault("ttl_s", 1800)
    kw.setdefault("at_ms", WHEN)
    return presign("GET", kw.pop("endpoint", ENDPOINT), kw.pop("bucket", "datasets"), key,
                   query or {}, **kw)


def _names(url):
    return [name for name, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)]


def test_the_aws_documentation_example():
    # "Authenticating Requests: Using Query Parameters (AWS Signature Version 4)", GET test.txt
    url = presign("GET", "https://s3.amazonaws.com", "examplebucket", "test.txt", {},
                  access_key_id="AKIAIOSFODNN7EXAMPLE",
                  secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                  region="us-east-1", ttl_s=86400,
                  at_ms=calendar.timegm(datetime.datetime(2013, 5, 24).timetuple()) * 1000)
    assert url == (
        "https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
        "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")


def test_an_awkward_object_key_matches_botocore():
    # expected signature computed once with botocore 1.43 (S3SigV4QueryAuth), which the
    # image does not ship; spaces, CJK, '+', '%' and '~' in the key
    url = _sign("lerobot/droid 100/中文/x+y%z~a.json")
    parts = urlsplit(url)
    assert parts.netloc == "datasets.tos-s3-cn-beijing.volces.com"
    assert parts.path == "/lerobot/droid%20100/%E4%B8%AD%E6%96%87/x%2By%25z~a.json"
    assert url.endswith(
        "&X-Amz-Signature=1d22178364b571ff19e0737dfc938201dfabdda9638d162edcea5a9c4ee1f761")


def test_a_listing_page_with_a_session_token_matches_botocore():
    url = _sign("", {"list-type": "2", "max-keys": "1000", "prefix": "lerobot/droid 100/",
                     "delimiter": "/", "continuation-token": "1/abc+def=="},
                session_token="STS/token+x==")
    parts = urlsplit(url)
    assert parts.path == "/"
    assert "prefix=lerobot%2Fdroid%20100%2F" in parts.query       # '/' in a value is encoded
    assert "delimiter=%2F" in parts.query
    assert "continuation-token=1%2Fabc%2Bdef%3D%3D" in parts.query
    assert "X-Amz-Security-Token=STS%2Ftoken%2Bx%3D%3D" in parts.query
    assert url.endswith(
        "&X-Amz-Signature=946db9a675c114678bd5059123ac7b78433b08f873bb676fea2ebc67d34cc709")


def test_parameters_are_sorted_by_name_and_the_signature_comes_last():
    url = _sign("", {"prefix": "a/", "list-type": "2", "max-keys": "7", "delimiter": "/"})
    names = _names(url)
    assert names[-1] == "X-Amz-Signature"
    assert names[:-1] == sorted(names[:-1])
    assert names[:-1] == ["X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date", "X-Amz-Expires",
                          "X-Amz-SignedHeaders", "delimiter", "list-type", "max-keys", "prefix"]


def test_the_token_is_signed_only_when_there_is_one():
    plain, with_token = _sign("a/b.json"), _sign("a/b.json", session_token="tok")
    assert "X-Amz-Security-Token" not in plain
    assert "X-Amz-Security-Token=tok" in with_token
    assert plain.rsplit("=", 1)[1] != with_token.rsplit("=", 1)[1]


def test_what_changes_the_signature():
    base = _sign("a/b.json")
    sig = base.rsplit("=", 1)[1]
    assert _sign("a/b.json").rsplit("=", 1)[1] == sig                           # deterministic
    for changed in (_sign("a/c.json"), _sign("a/b.json", ttl_s=60),
                    _sign("a/b.json", at_ms=WHEN + 1000), _sign("a/b.json", region="cn-shanghai"),
                    _sign("a/b.json", bucket="other"), _sign("a/b.json", secret_access_key="x")):
        assert changed.rsplit("=", 1)[1] != sig


def test_the_url_never_carries_the_secret_key():
    url = _sign("a/b.json", access_key_id=AK, secret_access_key=SK, session_token="tok")
    assert SK not in url and quote(SK, safe="") not in url and quote(SK) not in url
    assert f"X-Amz-Credential={AK}%2F20260928%2Fcn-beijing%2Fs3%2Faws4_request" in url


@pytest.mark.parametrize("kw, match", [
    ({"endpoint": "https://tos-s3-cn-beijing.volces.com/bucket"}, "endpoint"),
    ({"endpoint": "tos-s3-cn-beijing.volces.com"}, "endpoint"),
    ({"bucket": ""}, "bucket"),
    ({"bucket": "a/b"}, "bucket"),
    ({"ttl_s": 0}, "ttl_s"),
    ({"ttl_s": 7 * 24 * 3600 + 1}, "ttl_s"),
    ({"secret_access_key": ""}, "secret"),
])
def test_bad_arguments_are_refused(kw, match):
    with pytest.raises(ValueError, match=match):
        _sign("a", **kw)


def test_the_caller_cannot_smuggle_auth_parameters():
    with pytest.raises(ValueError, match="X-Amz-Expires"):
        _sign("a", {"X-Amz-Expires": "604800"})


def test_the_s3_public_endpoint():
    assert T.s3_public_endpoint("cn-beijing") == "https://tos-s3-cn-beijing.volces.com"
    assert T.s3_public_endpoint("cn-shanghai") == "https://tos-s3-cn-shanghai.volces.com"
