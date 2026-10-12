"""数据包完整性(容器体检):缺什么、按什么补的,必须写进报告留档。

钉住 container_findings 纯函数:每种缺失/补救组合给对状态和说明;LeRobot 全正常时整节不占位。
mcap 的几种情形在 tests/cli/test_containers.py 里随报告一起核对。
"""
from __future__ import annotations

from curation.export.report import container_findings

_ROBOT_HIT = {"robot_type": "so101", "embodiment_id": "so101",
              "registry_profile": "so101", "quality": "verified"}
_ROBOT_MISS = {"robot_type": "unknown", "embodiment_id": "unknown",
               "registry_profile": "(未注册)", "quality": None}


def _by_item(findings: list[dict]) -> dict:
    return {f["项"]: f for f in findings}


def test_lerobot_all_normal_is_silent():
    """info.json 齐全 + 规格库命中:零 findings,报告整节不占位。"""
    assert container_findings("lerobot", {"fps": 30}, _ROBOT_HIT) == []


def test_lerobot_unregistered_robot_degrades():
    robot = {"robot_type": "ur5e", "embodiment_id": "ur5e",
             "registry_profile": "(未注册)", "quality": None}
    got = _by_item(container_findings("lerobot", {"fps": 30}, robot))
    assert got["机器人型号"]["状态"] == "已跳过"
    assert "ur5e" in got["机器人型号"]["说明"]
    assert "整项跳过" in got["机器人型号"]["说明"]


def test_lerobot_missing_robot_type():
    got = _by_item(container_findings("lerobot", {"fps": 30}, _ROBOT_MISS))
    assert got["机器人型号"]["状态"] == "缺失"
    assert "--embodiment" in got["机器人型号"]["说明"]
    assert "整项跳过" in got["机器人型号"]["说明"]


def test_lerobot_missing_robot_type_but_embodiment_given():
    """info.json 没写 robot_type,但追问框/--embodiment 给了已注册型号:照常查,
    报告说「已补」,不能再说"无规格可查/跳过"。"""
    robot = {"robot_type": "unknown", "embodiment_id": "so101",
             "registry_profile": "so101", "quality": "verified"}
    got = _by_item(container_findings("lerobot", {"fps": 30}, robot))
    assert got["机器人型号"]["状态"] == "缺失(已补)"
    assert "so101" in got["机器人型号"]["说明"]
    assert "跳过" not in got["机器人型号"]["说明"]


def test_lerobot_given_embodiment_not_in_registry_names_it():
    """人工指定的型号不在规格库:说的是指定的那个型号,不是 robot_type。"""
    robot = {"robot_type": "unknown", "embodiment_id": "koch",
             "registry_profile": "(未注册)", "quality": None}
    got = _by_item(container_findings("lerobot", {"fps": 30}, robot))
    assert got["机器人型号"]["状态"] == "已跳过"
    assert "koch" in got["机器人型号"]["说明"]
    assert "整项跳过" in got["机器人型号"]["说明"]
