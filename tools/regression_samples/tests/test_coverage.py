"""platform_status of the sample set's taxonomy is generated from the module registry (design doc 17 §6.2)."""
from __future__ import annotations

import json

from regression_samples import coverage_from_registry as C


def _items():
    with open(C.TAXONOMY, encoding="utf-8") as f:
        return {i["id"]: i for i in json.load(f)["items"]}


def test_the_committed_notes_are_what_the_registry_says():
    """A registry change that moves an item fails here until `python -m regression_samples.coverage_from_registry`."""
    assert C.main(["--check"]) == 0


def test_the_rules():
    items = _items()
    # a code that rejects or asks a person under the default policy
    assert items["FILE-4"]["platform_status"] == "能判"
    assert items["FILE-4"]["platform_codes"] == ["data_integrity.decode_failed", "data_integrity.decode_concealed"]
    # reported only: the retired soft scores and the camera defects (registry 1.14 rides on task_success)
    for item, code in (("IMG-1", "visual_quality.frozen"), ("IMG-5", "camera_defects.glitch"),
                       ("IMG-6", "camera_defects.shake"), ("IMG-7", "camera_defects.contamination")):
        assert (items[item]["platform_status"], items[item]["platform_codes"]) == ("有读数", [code]), item
    # every module that covers it has a precondition
    assert items["MV-5"]["platform_status"] == "部分"
    assert items["MV-5"]["platform_conditions"] == ["要上传 trajectory.json"]
    assert items["ACT-4"]["platform_conditions"] == ["数据集要有状态量", "机器人型号要在规格库里"]
    # nothing covers it: a gap
    assert (items["MV-1"]["platform_status"], items["MV-1"]["platform_codes"]) == ("没有", [])
    # controls are handled, never named by a code; the preflight judges the input format
    assert {i["platform_status"] for i in items.values() if i["kind"] == "control"} == {"能处理"}
    assert (items["SET-4"]["platform_status"], items["SET-4"]["platform_codes"]) == ("能判", ["preflight"])
    assert all("platform_conditions" not in i for i in items.values() if i["platform_status"] != "部分")


def test_the_side_of_one_item():
    registry = {"taxonomy_version": "1.1", "modules": [
        {"id": "a", "needs": ["video"], "covers": ["X-1"], "codes": [{"code": "c", "item": "X-1", "level": "info"}]},
        {"id": "b", "needs": ["state"], "covers": ["X-1", "X-2"], "codes": [{"code": "d", "item": "X-1", "level": "blocking"},
                                                                         {"code": "e", "item": "X-2", "level": "info"}]}]}
    side = C.platform_side
    assert side({"id": "X-1", "kind": "defect"}, registry["modules"]) == {"platform_status": "能判",
                                                                           "platform_codes": ["a.c", "b.d"]}
    assert side({"id": "X-2", "kind": "defect"}, registry["modules"]) == {
        "platform_status": "部分", "platform_codes": ["b.e"], "platform_conditions": ["数据集要有状态量"]}
    assert side({"id": "X-3", "kind": "defect"}, registry["modules"]) == {"platform_status": "没有", "platform_codes": []}
    out = C.generate({"taxonomy_version": "1.1", "items": [{"id": "X-1", "kind": "defect", "platform": "notes",
                                                            "platform_status": "未测", "level": "episode"}]}, registry)
    assert list(out["items"][0]) == ["id", "kind", "platform", "platform_status", "platform_codes", "level"]
    assert "platform_status" in out["legend"]
