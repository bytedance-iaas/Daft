"""运动学极限查不到规格表 → 整项跳过,不拖垮整批(2026-09-16 用户定)。

此前漏斗里 registry.get 查不到型号就抛 UnknownEmbodimentError,daft 一行抛
= 整个任务失败;报告却写着"按弃权处理"。现在:
  ① info.json 读到型号且规格库支持 → 照常查;
  ② 读到了但不支持 → 运动学极限整项跳过,其余模块照常;
  ③ 读不到 → UI/CLI 开跑前追问;没问到的(跑全部/非交互)同样整项跳过。
"""
from __future__ import annotations

import json
import os

import pytest

from curation.tests.test_lerobot_v2_export import _write_v2_dataset


def _dataset(tmp_path, robot_type):
    src = _write_v2_dataset(str(tmp_path / "ds"), n_episodes=2)
    p = os.path.join(src, "meta", "info.json")
    info = json.load(open(p))
    if robot_type is None:
        info.pop("robot_type", None)
    else:
        info["robot_type"] = robot_type
    json.dump(info, open(p, "w"))
    return src


def _run(src, tmp_path, **kw):
    from curation.pipeline.run import run_pipeline
    return run_pipeline(None, src, str(tmp_path / "out"), lite=True,
                        report_only=True, **kw)


@pytest.mark.parametrize("robot_type,why", [
    ("testarm", "机器人型号 testarm 不在规格库"),      # 读到了但不支持
    (None, "info.json 未声明 robot_type"),            # 读不到(没被追问到)
])
def test_kinematics_skipped_instead_of_failing_the_run(tmp_path, capsys,
                                                       robot_type, why):
    src = _dataset(tmp_path, robot_type)
    summary = _run(src, tmp_path, only_checks="timestamp_check,kinematic_limits")
    out = capsys.readouterr().out
    assert why in out and "运动学极限整项跳过,其余模块照常" in out
    # 其余模块照常:时间戳检查真跑了(合成数据全长不足 1 秒,被它硬杀)
    assert {k["check"] for k in summary["stats"]["hard_killed"]} == {"timestamp_check"}
    report = open(summary["deliverables"]["report_md"], encoding="utf-8").read()
    assert "整项跳过" in report, "报告里要留痕,说法与实际行为一致"


def test_only_kinematics_selected_and_unsupported_still_finishes(tmp_path, capsys):
    """只勾了运动学极限、型号又不支持:跳过后一个检查都没有,也要正常出报告。"""
    src = _dataset(tmp_path, "testarm")
    summary = _run(src, tmp_path, only_checks="kinematic_limits")
    assert "运动学极限整项跳过" in capsys.readouterr().out
    assert os.path.exists(summary["deliverables"]["passed_json"])


def test_supported_embodiment_still_runs_kinematics(tmp_path, capsys):
    """规格库支持的型号(这里人工指定 so101):照常查,不打跳过警告。"""
    src = _dataset(tmp_path, "testarm")
    _run(src, tmp_path, only_checks="kinematic_limits", embodiment_id="so101")
    assert "运动学极限整项跳过" not in capsys.readouterr().out
