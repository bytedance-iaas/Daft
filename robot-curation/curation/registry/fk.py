"""FK 包装:lerobot RobotKinematics(placo→pinocchio)(文档中的 M2 的 FK 部分)。

提供"关节角 → 末端位姿"这个中性事实;只有自碰撞检测(V1)用得到。
有 URDF 才可用;MVP 只 Franka(已验:零位姿 EE=(0.088,0,0.926) 与官方运动学一致)。
⚠️ 很多公开 URDF 引用的 mesh 文件缺失(package:// 解析不到)→ FK 只需运动学链,
用 strip_geometry() 生成纯运动学 URDF 再加载。
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import numpy as np


class FKUnavailableError(RuntimeError):
    """该 embodiment 无可用 URDF,FK 不可用(profile.urdf_path 为空)。"""


def strip_geometry(urdf_in: str, urdf_out: str | None = None) -> str:
    """剥掉 visual/collision 节点 → 纯运动学 URDF(mesh 缺失也能加载)。"""
    urdf_out = urdf_out or urdf_in.replace(".urdf", "_kinematics.urdf")
    tree = ET.parse(urdf_in)
    for link in tree.getroot().iter("link"):
        for tag in ("visual", "collision"):
            for node in link.findall(tag):
                link.remove(node)
    tree.write(urdf_out)
    return urdf_out


class FKSolver:
    """一个 URDF 一个 solver(加载一次,重复用;daft 侧用 @daft.cls 持有)。"""

    def __init__(self, urdf_path: str, target_frame: str):
        from lerobot.model.kinematics import RobotKinematics  # 惰性:placo 仅 FK 用到

        if not os.path.exists(urdf_path):
            raise FileNotFoundError(f"URDF 不存在: {urdf_path}")
        self._rk = RobotKinematics(urdf_path, target_frame_name=target_frame)

    def ee_pose(self, q: np.ndarray) -> np.ndarray:
        """关节角向量 → 4x4 齐次位姿(基座系)。q 长度须为 URDF 全部驱动关节数。"""
        return self._rk.forward_kinematics(np.asarray(q, dtype=np.float64))


def fk_for_profile(profile, target_frame: str) -> FKSolver:
    """从 EmbodimentProfile 建 FK;无 urdf_path 明确报错(不静默降级)。"""
    if not profile.urdf_path:
        raise FKUnavailableError(
            f"{profile.embodiment_id}: profile 无 urdf_path,FK 不可用"
            f"(自碰撞等 FK 功能对该 embodiment 关闭)")
    return FKSolver(profile.urdf_path, target_frame)
