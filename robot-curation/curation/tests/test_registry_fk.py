"""P2.3 验收:M2 规格库(profiles/registry/FK)。"""
from __future__ import annotations

import os
import textwrap

import numpy as np
import pytest

from curation.registry.registry import (
    EmbodimentRegistry,
    UnknownEmbodimentError,
)

PANDA_URDF = "/data03/hao/data/urdf/panda/panda_kinematics.urdf"


@pytest.fixture(scope="module")
def reg():
    return EmbodimentRegistry()


def test_all_profiles_load(reg):
    ids = reg.ids()
    assert len(ids) >= 8
    for expected in ("franka", "so100", "widowx", "ur5", "aloha", "google_robot", "agibot", "pusht"):
        assert expected in ids


def test_franka_limits_authoritative(reg):
    p = reg.get("franka")
    assert p.dof == 7 and len(p.joint_limits) == 7 and len(p.velocity_limits) == 7
    assert p.joint_limits[0] == (-2.8973, 2.8973)   # 官方 FCI 文档值
    assert p.joint_limits[3] == (-3.0718, -0.0698)  # 关节4 不对称极限(易错点)
    assert p.velocity_limits[4] == 2.61
    assert p.quality == "authoritative"
    assert p.has_limits


def test_dataset_robot_types_resolve(reg):
    """手上 5 个数据集的 robot_type(除 pusht 的 unknown)都能查到。"""
    assert reg.get("franka").embodiment_id == "franka"    # droid
    assert reg.get("widowx").embodiment_id == "widowx"    # bridge
    assert reg.get("so100").embodiment_id == "so100"      # svla
    assert reg.get("aloha").dof == 14                     # aloha_sim
    assert reg.get("pusht").action_space == "ee_position_2d"  # 人工指定用


def test_aliases_and_case_insensitive(reg):
    assert reg.get("panda").embodiment_id == "franka"
    assert reg.get("FRANKA_PANDA").embodiment_id == "franka"
    assert reg.get("wx250s").embodiment_id == "widowx"


def test_unknown_rejected_with_helpful_message(reg):
    """查不到报错不放行(M2 铁律),且消息给出已注册清单。"""
    with pytest.raises(UnknownEmbodimentError, match="已注册"):
        reg.get("unknown")
    with pytest.raises(UnknownEmbodimentError):
        reg.get("")


def test_draft_profile_has_no_limits_but_loads(reg):
    p = reg.get("agibot")
    assert not p.has_limits          # 空极限=只能格式校验;kinematics 检查须显式跳过
    assert p.quality == "draft"


def test_dof_mismatch_yaml_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(textwrap.dedent("""
        embodiment_id: bad_bot
        dof: 3
        action_space: joint_position
        joint_limits: [[-1, 1], [-1, 1]]
    """))
    with pytest.raises(ValueError, match="joint_limits"):
        EmbodimentRegistry(profiles_dir=str(tmp_path))


# ---------- FK ----------

@pytest.mark.skipif(not os.path.exists(PANDA_URDF), reason="panda URDF 未准备(见 fk.py)")
def test_fk_franka_zero_pose_matches_known():
    """P2 验收:FK 对 Franka 零位姿输出与已知值对齐(官方运动学 0.088/0/0.926)。"""
    from curation.registry.fk import FKSolver

    T = FKSolver(PANDA_URDF, target_frame="panda_link8").ee_pose(np.zeros(9))
    np.testing.assert_allclose(T[:3, 3], [0.088, 0.0, 0.926], atol=1e-3)
    np.testing.assert_allclose(T[:3, :3] @ T[:3, :3].T, np.eye(3), atol=1e-9)  # 合法旋转


def test_fk_without_urdf_is_explicit(reg):
    from curation.registry.fk import FKUnavailableError, fk_for_profile

    with pytest.raises(FKUnavailableError, match="urdf_path"):
        fk_for_profile(reg.get("so100"), target_frame="gripper")