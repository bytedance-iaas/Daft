"""P3.3 验收:exceed_limits 注入 100% 检出+定位关节帧号;干净集 0 误报。"""
from __future__ import annotations

import os

import numpy as np
import pytest

from curation.core.checks.kinematics import kinematic_limits
from curation.registry.registry import EmbodimentRegistry
from curation.tests import corrupt

DATA = "/data03/hao/data"
N = 20


@pytest.fixture(scope="module")
def reg():
    return EmbodimentRegistry()


def _clean_franka(seed: int, t: int = 150) -> dict:
    """限内的 franka 关节轨迹(±80% 极限,速度远低于极限)。"""
    rng = np.random.default_rng(seed)
    reg = EmbodimentRegistry()
    prof = reg.get("franka")
    tt = np.linspace(0, np.pi, t)[:, None]
    mid = np.array([(lo + hi) / 2 for lo, hi in prof.joint_limits])
    amp = np.array([(hi - lo) / 2 * 0.6 for lo, hi in prof.joint_limits])
    q = mid + amp * np.sin(tt + rng.uniform(0, 6, 7)) * rng.uniform(0.3, 1.0, 7)
    return {"episode_id": f"fr{seed}", "action": q.astype(np.float32),
            "proprio_state": q.astype(np.float32),
            "timestamps": np.arange(t) / 15.0, "video": {}, "fps": 15.0}


def test_clean_zero_false_positive(reg):
    prof = reg.get("franka")
    results = [kinematic_limits(_clean_franka(s)["action"], prof, fps=15.0) for s in range(N)]
    assert all(r.passed for r in results), \
        f"干净集出现误报: {[r.detail for r in results if not r.passed][:2]}"


def test_exceed_limits_100pct_detected_and_localized(reg):
    prof = reg.get("franka")
    for s in range(N):
        row = _clean_franka(s)
        joint = s % 7
        lo, hi = prof.joint_limits[joint]
        beyond = hi + 0.3 * (hi - lo)   # 超出上限 30% 量程(franka 关节4 是全负区间,×1.5 会落回区间内)
        bad, inj = corrupt.exceed_limits(row, joint=joint, frame=75, limit_value=beyond, factor=1.0)
        r = kinematic_limits(bad["action"], prof, fps=15.0)
        assert r.passed is False, f"seed{s}: 超限未检出"
        hits = [v for v in r.detail["violations"] if v["type"] == "joint_limit"]
        assert any(v["joint"] == joint and v["frame"] == 75 for v in hits), \
            f"seed{s}: 定位失败 {hits[:3]}"


def test_velocity_violation_detected(reg):
    prof = reg.get("franka")
    row = _clean_franka(0)
    row["action"][80, 0] = row["action"][79, 0] + 0.5   # 一帧跳 0.5rad@15fps = 7.5rad/s >> 2.175
    r = kinematic_limits(row["action"], prof, fps=15.0)
    assert r.passed is False
    assert any(v["type"] == "velocity_limit" and v["joint"] == 0 and v["frame"] == 79
               for v in r.detail["violations"])


def test_dim_mismatch_is_format_violation(reg):
    r = kinematic_limits(np.zeros((10, 6)), reg.get("franka"), fps=15.0)
    # 理由说人话:关节数对不上,而不是 "维度 7 != dof 6"
    assert r.passed is False and "关节数" in r.detail["reason"]
    assert "!=" not in r.detail["reason"]


def test_nan_is_violation(reg):
    q = _clean_franka(1)["action"]
    q[5, 2] = np.nan
    r = kinematic_limits(q, reg.get("franka"), fps=15.0)
    assert r.passed is False and "无效值" in r.detail["reason"]


def test_draft_profile_is_undecidable(reg):
    r = kinematic_limits(np.zeros((10, 14)), reg.get("agibot"), fps=30.0)
    assert r.passed is None                      # 不可判 ≠ 判坏(诚实缺省)


@pytest.mark.skipif(not os.path.exists(f"{DATA}/aloha_sim_insertion_human/meta/info.json"),
                    reason="aloha_sim 未下载")
def test_clean_real_aloha_zero_false_positive(reg):
    """真数据 + 真 profile:干净 aloha 0 误报(P3.3 验收主判据)。"""
    from curation.ingest.lerobot_reader import read_lerobot_rows

    rows = read_lerobot_rows(f"{DATA}/aloha_sim_insertion_human", max_episodes=20)
    prof = reg.get("aloha")
    fails = []
    for r in rows:
        res = kinematic_limits(r["action"], prof, fps=r["fps"])
        if res.passed is not True:
            fails.append((r["episode_id"], res.detail.get("violations", res.detail)[:2]))
    assert not fails, f"干净 aloha 误报 {len(fails)}/20: {fails[:2]}"


@pytest.mark.skipif(not os.path.exists(f"{DATA}/pusht/meta/info.json"),
                    reason="pusht 未下载")
def test_clean_real_pusht_zero_false_positive(reg):
    from curation.ingest.lerobot_reader import read_lerobot_rows

    rows = read_lerobot_rows(f"{DATA}/pusht", max_episodes=20)
    prof = reg.get("pusht")
    results = [kinematic_limits(r["action"], prof, fps=r["fps"]) for r in rows]
    assert all(r.passed for r in results), \
        f"干净 pusht 误报: {[r.detail['violations'][:2] for r in results if not r.passed][:2]}"


def test_extreme_spike_killed_not_unit_mismatch():
    """单帧极端毛刺(≈20×限值)必须硬杀并给证据,不得被单位错配守卫弃权。

    2026-07-14 注入实测抓出的 bug:B1 守卫用 max 做量级比,单帧 9999 被误判成
    "单位错配"弃权,放走了坏数据。守卫改用 p95(单位错配=整条缩放,毛刺≠错配)。
    """
    import copy
    import os

    import pytest

    if not os.path.exists("/data03/hao/data/pusht/meta/info.json"):
        pytest.skip("无 pusht 数据")
    from curation.ingest.lerobot_reader import read_lerobot_rows, rows_to_daft
    from curation.pipeline.config import apply_check_selection, load_config
    from curation.pipeline.funnel import run_funnel
    from curation.registry.registry import EmbodimentRegistry

    r = copy.deepcopy(read_lerobot_rows("/data03/hao/data/pusht", max_episodes=1,
                                        embodiment_id="pusht")[0])
    r["action"][50, 0] = 9999.0                     # 单帧毛刺(限 [0,512])
    cfg = apply_check_selection(load_config(), only="kinematic_limits")
    _, stats = run_funnel(rows_to_daft([r]), cfg, EmbodimentRegistry())
    killed = {k["episode_id"]: k for k in stats["hard_killed"]}
    assert r["episode_id"] in killed                # 硬杀,不是弃权
    assert "joint_limit" in killed[r["episode_id"]]["detail"]   # 证据带违规定位


# ---------------- EE 规格检查(2026-07-14,不做 IK) ----------------

def _franka():
    from curation.registry.registry import EmbodimentRegistry
    return EmbodimentRegistry().get("franka")


def _clean_ee(T=120):
    """基座前方 0.5m 附近画小圈的合法轨迹(米制,15fps 量级速度)。"""
    import numpy as np
    t = np.linspace(0, 2 * np.pi, T)
    pose = np.zeros((T, 6))
    pose[:, 0] = 0.45 + 0.1 * np.cos(t)
    pose[:, 1] = 0.1 * np.sin(t)
    pose[:, 2] = 0.35 + 0.05 * np.sin(2 * t)
    pose[:, 3] = 3.0 + 0.1 * np.sin(t)          # roll 贴近 π(考验回绕)
    pose[:, 4] = 0.2 * np.sin(t)
    pose[:, 5] = 0.3 * np.cos(t)
    return pose


def test_ee_clean_passes():
    from curation.core.checks.kinematics import ee_limits
    res = ee_limits(_clean_ee(), _franka(), 15.0)
    assert res.passed is True and res.detail["mode"] == "ee"


def test_ee_out_of_reach_killed_with_location():
    import numpy as np
    from curation.core.checks.kinematics import ee_limits
    pose = _clean_ee()
    pose[60, :3] = [1.5, 0.0, 0.5]              # 手掌出现在 1.58m 处(臂展 0.855)
    res = ee_limits(pose, _franka(), 15.0)
    assert res.passed is False
    v = [x for x in res.detail["violations"] if x["type"] == "ee_reach"]
    assert v and v[0]["frame"] == 60


def test_ee_teleport_velocity_killed():
    from curation.core.checks.kinematics import ee_limits
    pose = _clean_ee()
    pose[60:, 1] += 0.4                          # 单帧横跳 0.4m@15fps = 6m/s(限1.7)
    res = ee_limits(pose, _franka(), 15.0)
    assert res.passed is False
    assert any(x["type"] == "ee_translation_velocity" and x["frame"] == 59
               for x in res.detail["violations"])


def test_ee_pi_wrap_not_false_positive():
    """roll 跨 ±π 的表示法跳变(-3.1→+3.1)不是真旋转,测地步长必须不误杀。"""
    import numpy as np
    from curation.core.checks.kinematics import ee_limits
    pose = _clean_ee()
    pose[:, 3] = np.where(np.arange(len(pose)) < 60, 3.10, -3.10)  # 裸差分=6.2rad/帧!
    res = ee_limits(pose, _franka(), 15.0)
    rot_v = [x for x in res.detail.get("violations", [])
             if x["type"] == "ee_rotation_velocity"]
    assert not rot_v, f"±π 回绕被误判为旋转超速: {rot_v[:2]}"


def test_ee_real_fast_rotation_killed():
    import numpy as np
    from curation.core.checks.kinematics import ee_limits
    pose = _clean_ee()
    pose[60:, 4] += 1.2                          # pitch 单帧真转 1.2rad@15fps=18rad/s(限2.5)
    res = ee_limits(pose, _franka(), 15.0)
    assert any(x["type"] == "ee_rotation_velocity" for x in res.detail["violations"])


def test_ee_unit_mismatch_abstains():
    """毫米记的数据(数值×1000)→ 单位守卫弃权,不硬比。"""
    from curation.core.checks.kinematics import ee_limits
    pose = _clean_ee()
    pose[:, :3] *= 1000.0
    res = ee_limits(pose, _franka(), 15.0)
    assert res.passed is None and res.detail.get("unit_mismatch")


def test_ee_sustained_out_abstains():
    """整条都在 ~2m 外(<3倍臂展,过单位守卫)= 基座系约定错位嫌疑 → 弃权不误杀。"""
    from curation.core.checks.kinematics import ee_limits
    pose = _clean_ee()
    pose[:, 0] += 1.6                            # 全程 ~2.1m(阈 1.19×1.25=1.49)
    res = ee_limits(pose, _franka(), 15.0)
    assert res.passed is None and "错位嫌疑" in res.detail["reason"]


def test_ee_funnel_dispatch_droid():
    """漏斗分派:droid(EE 数据 × franka 关节表)→ 走 EE 规格真判,不再弃权。"""
    import os

    import pytest
    if not os.path.exists("/data03/hao/data/droid_lerobot/meta"):
        pytest.skip("无 droid 数据")
    import json
    from curation.ingest.lerobot_reader import read_lerobot_rows, rows_to_daft
    from curation.pipeline.config import apply_check_selection, load_config
    from curation.pipeline.funnel import run_funnel
    from curation.registry.registry import EmbodimentRegistry
    rows = read_lerobot_rows("/data03/hao/data/droid_lerobot", max_episodes=2,
                             skip_missing=True)
    cfg = apply_check_selection(load_config(), only="kinematic_limits")
    df, _ = run_funnel(rows_to_daft(rows), cfg, EmbodimentRegistry())
    out = df.select("check_kinematic_limits").to_pydict()["check_kinematic_limits"]
    for c in out:
        assert c["passed"] is True
        assert json.loads(c["detail"])["mode"] == "ee"
