"""Record normalization, D33 incident detection and exact comparison."""
from __future__ import annotations

import json
import math

import jsonschema
import pytest

from parity import records as R

from .conftest import load_schema

SCHEMA = load_schema("cli", "result-record.schema.json")


@pytest.mark.parametrize("value,expected", [("ep000034", 34), (34, 34), ("ep7", 7)])
def test_episode_index(value, expected):
    assert R.episode_index(value) == expected


@pytest.mark.parametrize("value", ["episode", True, None])
def test_episode_index_rejects_garbage(value):
    with pytest.raises(ValueError):
        R.episode_index(value)


@pytest.mark.parametrize("passed,score,error,verdict", [
    (True, None, None, "pass"), (False, None, None, "fail"), (None, None, None, "abstain"),
    (None, 0.8, None, "scored"), (True, None, {"kind": "execution"}, "error")])
def test_derive_verdict(passed, score, error, verdict):
    assert R.derive_verdict(passed, score, error) == verdict


def _ts(detail: dict, passed=None) -> dict:
    return {"passed": passed, "score": None, "detail": json.dumps(detail, ensure_ascii=False)}


@pytest.mark.parametrize("detail,step", [
    ({"reason": "VLM 调用/解析失败: Timeout", "rules": ["vlm_call_failed"]}, "probe"),
    ({"reason": "internal_error: X", "rules": ["internal_error"], "internal_error": True},
     "internal"),
    ({"reason": "所有相机解码失败,无帧可判"}, "decode"),
    ({"verdict": "success", "cam_votes": {"wrist": "yes", "ext": "unavail"}}, "endstate"),
    ({"label_check": {"outcome": "error:Timeout"}}, "label_guard"),
    ({"arbitration": {"applied": True, "error": "ReadTimeout"}}, "arbitration"),
    ({"arbitration": {"lines": [{"votes": ["yes", "error", "yes"]}]}}, "arbitration"),
])
def test_degraded_task_success_is_an_error_under_d33(detail, step):
    rec = R.record_from_v1_struct("task_success", "ep000003", _ts(detail, passed=True))
    assert rec["verdict"] == "error"
    assert step in [i["step"] for i in rec["error"]["incidents"]]
    jsonschema.validate(rec, SCHEMA)


def test_normal_abstain_is_not_an_error():
    rec = R.record_from_v1_struct("task_success", "ep000003",
                                  _ts({"reason": "灰区", "rules": ["abstain_kept_review_not_done"],
                                       "cam_votes": {"wrist": "unclear"}}))
    assert rec["verdict"] == "abstain" and rec["error"] is None
    jsonschema.validate(rec, SCHEMA)


def test_soft_module_record_is_scored_and_valid():
    rec = R.record_from_v1_struct("visual_quality", "ep000001",
                                  {"passed": None, "score": 0.93, "detail": "{\"x\": 1}"})
    assert rec["verdict"] == "scored" and rec["gate"] == "soft"
    jsonschema.validate(rec, SCHEMA)


def test_schema_rejects_error_verdict_without_incidents():
    rec = R.make_record("task_success", 1, passed=None, score=None, details={})
    rec["verdict"] = "error"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(rec, SCHEMA)


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
    rec = R.make_record("motion_quality", 1, passed=None, score=0.5, details={},
                        elapsed_s=3.2, evidence=["x.jpg"])
    assert "elapsed_s" not in R.comparable(rec) and "evidence" not in R.comparable(rec)
