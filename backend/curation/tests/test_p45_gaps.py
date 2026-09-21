"""P4.5 缺口:EE 空间分派(DROID/Bridge 过漏斗)——修掉'关节极限卡笛卡尔坐标'隐患。"""
from __future__ import annotations

import json
import os

import pytest

DATA = "/data03/hao/data"


def _need(name):
    return pytest.mark.skipif(
        not os.path.exists(f"{DATA}/{name}/meta/info.json"), reason=f"{name} 未下载")


@_need("droid_lerobot")
def test_action_space_inferred():
    from curation.ingest.lerobot_reader import read_lerobot_rows

    assert read_lerobot_rows(f"{DATA}/droid_lerobot", max_episodes=1)[0]["action_space"] == "ee"
    assert read_lerobot_rows(f"{DATA}/aloha_sim_insertion_human",
                             max_episodes=1)[0]["action_space"] == "joint"
    assert read_lerobot_rows(f"{DATA}/pusht", max_episodes=1)[0]["action_space"] == "joint"


def _run_funnel(dataset, n):
    from curation.ingest.lerobot_reader import read_lerobot_rows, rows_to_daft
    from curation.pipeline.config import load_config
    from curation.pipeline.funnel import run_funnel
    from curation.registry.registry import EmbodimentRegistry

    rows = read_lerobot_rows(f"{DATA}/{dataset}", max_episodes=n)
    df, stats = run_funnel(rows_to_daft(rows), load_config(), EmbodimentRegistry())
    out = df.select("episode_id", "verdict", "check_kinematic_limits").to_pydict()
    return stats, out


@_need("droid_lerobot")
def test_droid_ee_dispatch_no_false_kill():
    """DROID:EE 7 维 vs Franka dof 7 维度巧合——绝不拿关节极限硬卡;
    2026-07-14 起有 EE 规格 → 走 EE 真判(mode=ee),干净数据应 pass。"""
    stats, out = _run_funnel("droid_lerobot", 5)
    assert stats["after_numeric_gates"] == 5, f"EE 数据被数值段错杀: {stats}"
    for kin in out["check_kinematic_limits"]:
        assert kin["passed"] is True, f"干净 droid 应过 EE 检查,got {kin}"
        assert json.loads(kin["detail"])["mode"] == "ee"


@_need("bridge_orig_lerobot")
def test_bridge_ee_dispatch_no_dim_mismatch_kill():
    """Bridge:EE 7 维 vs WidowX dof 6——修复前会全灭于'维度不匹配'。"""
    stats, out = _run_funnel("bridge_orig_lerobot", 5)
    assert stats["after_numeric_gates"] == 5, f"Bridge 被维度不匹配错杀: {stats}"
    # 2026-07-14 起:EE 规格真判(mode=ee),干净数据 pass 而非弃权
    assert all(k["passed"] is True for k in out["check_kinematic_limits"])


@_need("aloha_sim_insertion_human")
def test_joint_dataset_still_checked():
    """关节空间数据集(aloha)不受分派影响,运动学照常硬校验。"""
    _, out = _run_funnel("aloha_sim_insertion_human", 3)
    assert all(k["passed"] is True for k in out["check_kinematic_limits"])