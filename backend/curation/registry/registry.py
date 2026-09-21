"""EmbodimentRegistry:机器人规格只读库(文档中的 M2)。

只提供事实(规格/FK),不校验 —— 校验在 checks/motion_quality.py 与 checks/kinematics.py。
profiles/*.yaml 预置 8 个;国产臂逐个收集=壁垒。
查不到 embodiment_id → UnknownEmbodimentError,**报错不放行**(绝不给默认值)。
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from typing import Any

import yaml

_DEFAULT_PROFILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")


@dataclass(frozen=True)
class EmbodimentProfile:
    embodiment_id: str
    dof: int
    action_space: str                        # joint_position / ee_position_2d / ...
    joint_limits: tuple = ()                 # ((min,max),...) 长度=dof;空=该项未收集
    velocity_limits: tuple = ()
    gripper_dims: tuple = ()             # action 向量里的夹爪列下标(数据布局约定,同单位制)
    unit: str = "rad"
    gripper_range: tuple | None = None
    cameras: dict[str, str] = field(default_factory=dict)   # cam_name -> wrist/external
    urdf_path: str | None = None
    # EE(笛卡尔)规格(2026-07-14 用户定,不做 IK):EE 数据集(droid/bridge)用
    # 这套查 state——可达半径(基座系,米)+ 平移/旋转速度极限。弱于关节检查
    # (手掌在空间内≠关节没超限),定位是"能查多少查多少"的增量。
    ee_reach_m: float | None = None
    ee_velocity_limits: dict | None = None   # {"translation_m_s": x, "rotation_rad_s": y}
    control_freq_hz: float | None = None
    aliases: tuple = ()
    source: str = ""                         # 规格出处溯源
    quality: str = "draft"                   # authoritative / approximate / draft

    @property
    def has_limits(self) -> bool:
        return len(self.joint_limits) > 0

    @property
    def has_ee_limits(self) -> bool:
        return self.ee_reach_m is not None


class UnknownEmbodimentError(KeyError):
    """查不到型号:报错不放行,不用默认值。"""


class EmbodimentRegistry:
    def __init__(self, profiles_dir: str | None = None):
        self._profiles: dict[str, EmbodimentProfile] = {}
        self._alias: dict[str, str] = {}
        profiles_dir = profiles_dir or _DEFAULT_PROFILES_DIR
        paths = sorted(glob.glob(os.path.join(profiles_dir, "*.yaml")))
        if not paths:
            raise FileNotFoundError(f"profiles 目录为空: {profiles_dir}")
        for p in paths:
            self._load_one(p)

    def _load_one(self, path: str) -> None:
        with open(path) as f:
            raw: dict[str, Any] = yaml.safe_load(f)
        pid = raw["embodiment_id"]
        limits = tuple(tuple(x) for x in (raw.get("joint_limits") or []))
        if limits and len(limits) != raw["dof"]:
            raise ValueError(
                f"{path}: joint_limits 数量({len(limits)}) != dof({raw['dof']})")
        vel = tuple(raw.get("velocity_limits") or [])
        if vel and len(vel) != raw["dof"]:
            raise ValueError(
                f"{path}: velocity_limits 数量({len(vel)}) != dof({raw['dof']})")
        prof = EmbodimentProfile(
            embodiment_id=pid,
            dof=int(raw["dof"]),
            action_space=raw.get("action_space", ""),
            joint_limits=limits,
            velocity_limits=vel,
            gripper_dims=tuple(raw.get("gripper_dims", [])),
            unit=raw.get("unit", "rad"),
            gripper_range=tuple(raw["gripper_range"]) if raw.get("gripper_range") else None,
            cameras=raw.get("cameras") or {},
            urdf_path=raw.get("urdf_path"),
            ee_reach_m=(float(raw["ee_reach_m"]) if raw.get("ee_reach_m") else None),
            ee_velocity_limits=raw.get("ee_velocity_limits"),
            control_freq_hz=raw.get("control_freq_hz"),
            aliases=tuple(raw.get("aliases") or []),
            source=raw.get("source", ""),
            quality=raw.get("quality", "draft"),
        )
        if pid in self._profiles:
            raise ValueError(f"embodiment_id 重复: {pid}({path})")
        self._profiles[pid] = prof
        for a in (pid, *prof.aliases):
            key = a.lower()
            if key in self._alias and self._alias[key] != pid:
                raise ValueError(f"别名冲突: {a} → {self._alias[key]} 与 {pid}")
            self._alias[key] = pid

    def get(self, embodiment_id: str) -> EmbodimentProfile:
        key = (embodiment_id or "").lower()
        if key not in self._alias:
            raise UnknownEmbodimentError(
                f"未知 embodiment_id: {embodiment_id!r}。已注册: {sorted(self._profiles)}。"
                f"请提供 profile YAML 或在摄入时用 embodiment_id= 参数指定已注册型号")
        return self._profiles[self._alias[key]]

    def ids(self) -> list[str]:
        return sorted(self._profiles)
