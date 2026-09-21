"""Deep-link parsing, ported with v1's test cases (``curation/tests/test_ui_datasources.py``)."""
from __future__ import annotations

import re

from daemon import deeplink as dl


class QP(dict):
    """A stand-in for Starlette's QueryParams (``getlist``)."""

    def getlist(self, k):
        v = self.get(k)
        return v if isinstance(v, list) else ([v] if v is not None else [])


def test_deeplink_values_reads_all_three_keys_and_merges():
    assert dl.deeplink_values(QP({"dataset": "a,b"})) == (["a", "b"], True)
    assert dl.deeplink_values(QP({"dataset_url": "tos://x/datasets/c"})) == \
        (["tos://x/datasets/c"], True)
    assert dl.deeplink_values(QP({"url": "tos://x/d"})) == (["tos://x/d"], True)
    assert dl.deeplink_values(QP({"dataset": "a", "url": "a,b", "dataset_url": "c"})) == \
        (["a", "c", "b"], True)                                   # key order, de-duplicated
    assert dl.deeplink_values(QP({"dataset": ""})) == ([], True)   # present but empty
    assert dl.deeplink_values(QP({"foo": "bar"})) == ([], False)
    assert dl.deeplink_values({"dataset": "x"}) == (["x"], True)   # plain dict works
    assert dl.deeplink_values(QP({"dataset": ["a", "b,a"]})) == (["a", "b"], True)


def test_parse_dataset_ref_is_deterministic_on_odd_inputs():
    assert dl.parse_dataset_ref("demo_v2") == {"bucket": None, "prefix": None, "dataset": "demo_v2"}
    assert dl.parse_dataset_ref("tos://BucketA/datasets/demo_v2/") == \
        {"bucket": "bucketa", "prefix": "datasets", "dataset": "demo_v2"}
    got = dl.parse_dataset_ref("tos://b/DS/Demo")
    assert (got["prefix"], got["dataset"]) == ("DS", "Demo")
    assert dl.parse_dataset_ref("tos://b/raw/2026/x")["prefix"] == "raw/2026"
    assert dl.parse_dataset_ref("tos://b/x")["prefix"] == ""
    for bad in ("tos://bucketa", "tos://", "tos:///datasets/x", "https://host/datasets/x",
                "tos://b/datasets/demo%20v2", "tos://b/datasets/a%2Fb"):
        got = dl.parse_dataset_ref(bad)
        assert got.get("error"), bad
        assert "dataset" not in got, bad


def test_region_takes_the_first_valid_value():
    assert dl.deeplink_region(QP({"region": "cn-beijing"})) == ("cn-beijing", True)
    assert dl.deeplink_region(QP({"region": ["<x>", "AP-SOUTHEAST-1"]})) == ("ap-southeast-1", True)
    assert dl.deeplink_region(QP({"region": "../../etc"})) == (None, True)
    assert dl.deeplink_region(QP({})) == (None, False)


def test_deeplink_endpoint_two_key_names_both_recognized():
    host = "tos-cn-beijing.volces.com"
    assert dl.deeplink_endpoint({"endpoint": host}) == (host, True)
    assert dl.deeplink_endpoint({"tos_endpoint": f"https://{host}"}) == (host, True)
    assert dl.deeplink_endpoint({"endpoint": "a.volces.com", "tos_endpoint": "b.volces.com"}) == \
        ("a.volces.com", True)
    assert dl.deeplink_endpoint({"endpoint": "<img onerror=x>", "tos_endpoint": "b.volces.com"}) == \
        ("b.volces.com", True)
    assert dl.deeplink_endpoint({"endpoint": "<img onerror=x>"}) == (None, True)
    assert dl.deeplink_endpoint({}) == (None, False)
    assert dl.deeplink_endpoint(QP({"endpoint": [host]})) == (host, True)


def test_endpoint_sanitizer_strips_or_rejects_untrusted_input():
    san = dl.sanitize_endpoint
    assert san("https://u:p@tos-cn-beijing.volces.com/a/b?c=1") == "tos-cn-beijing.volces.com"
    assert san("tos-cn-beijing.ivolces.com:443/bucket") == "tos-cn-beijing.ivolces.com"
    assert san("TOS-CN-BEIJING.VOLCES.COM") == "tos-cn-beijing.volces.com"
    evils = ["<img onerror=alert(1)>", "[x](javascript:alert(1))", "[x](https://evil.com/a)",
             "a" * 2000, "b" * 300 + ".volces.com", "host name with spaces", ""]
    for evil in evils:
        got = san(evil)
        assert got is None or re.fullmatch(r"[a-z0-9.-]{1,253}", got), evil
        assert got != evil.lower(), evil


def test_endpoint_region_recognizes_or_abstains():
    assert dl.endpoint_region("tos-cn-beijing.volces.com") == "cn-beijing"
    assert dl.endpoint_region("tos-ap-southeast-1.ivolces.com") == "ap-southeast-1"
    assert dl.endpoint_region("tos-s3-cn-north-1.volces.com") == "cn-north-1"
    assert dl.endpoint_region("example.com") is None
    assert dl.endpoint_region(None) is None


def test_entry_detection_covers_every_v1_key():
    for key in ("dataset", "dataset_url", "url", "region", "endpoint", "tos_endpoint", "source"):
        assert dl.is_entry(QP({key: ""})), key
    assert not dl.is_entry(QP({"utm_source": "x"}))
