"""Exact comparison of result records."""
from __future__ import annotations

import math

from parity import records as R


def test_diff_is_exact_for_floats_and_types():
    assert R.diff_values({"a": 0.1}, {"a": 0.1}) == []
    assert R.diff_values({"a": 0.30000000000000004}, {"a": 0.3})[0]["path"] == "a"
    assert R.diff_values({"a": 1}, {"a": 1.0})[0]["path"] == "a"
    assert R.diff_values({"a": math.nan}, {"a": math.nan}) == []
    assert R.diff_values({"a": True}, {"a": 1}) != []


def test_diff_ignores_key_order_but_not_list_order():
    assert R.diff_values({"a": 1, "b": 2}, {"b": 2, "a": 1}) == []
    d = R.diff_values({"l": [1, 2]}, {"l": [2, 1]})
    assert [x["path"] for x in d] == ["l[0]", "l[1]"]
    assert R.diff_values({"l": [1]}, {"l": [1, 2]})[0]["path"] == "l.length"


def test_diff_reports_missing_keys():
    d = R.diff_values({"a": 1}, {"b": 1})
    assert {x["path"] for x in d} == {"a", "b"}


def test_volatile_fields_are_not_compared():
    rec = {"episode_index": 1, "module": "motion_quality", "status": "ok", "findings": [],
           "details": {}, "elapsed_s": 3.2, "evidence": ["x.jpg"]}
    assert R.comparable(rec) == {"episode_index": 1, "module": "motion_quality", "status": "ok",
                                 "findings": [], "details": {}}


def test_an_error_record_is_an_error():
    assert R.is_error({"status": "error"}) and not R.is_error({"status": "ok"})
