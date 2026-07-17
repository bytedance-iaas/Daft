"""框架中立契约层(DESIGN.md §11.2)。

⚠️ 纪律:core/ 包内禁止 import daft —— 质检逻辑全部是纯函数,写在本契约上;
Daft 壳 / CuratorStage 壳 / LAS 算子壳都只是 adapters/ 里的薄适配器。
客户插件也写在这层契约上,从不 import 框架。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import numpy as np


@dataclass
class Episode:
    """统一 Episode 表示 —— UDF/检查函数看到的一条数据的行视图。

    约定(CLAUDE.md):
    - action = 指令(command);proprio = 实际(achieved)。分开存,不混。
    - 小数据存值(action 几 KB 直接放数组),大数据存指针(video_paths 只是路径,
      视频字节推迟到需要的检查里才读、即用即弃)。
    - 统一的是容器骨架:不统一维度(7 关节就存 7),不统一语义(夹爪含义照搬)。
    """

    episode_id: str
    embodiment_id: str = ""
    instruction: str = ""
    action: np.ndarray | None = None  # [T, dim] 变长
    proprio: dict[str, np.ndarray] = field(default_factory=dict)
    video_paths: dict[str, str] = field(default_factory=dict)  # cam_name -> 路径(指针)
    timestamps: dict[str, np.ndarray] = field(default_factory=dict)  # 通道名 -> 时间戳
    fps: float | None = None
    raw_extras: dict[str, Any] = field(default_factory=dict)  # 看不懂的字段原样收留
    provenance: dict[str, Any] = field(default_factory=dict)  # 字段来源溯源
    scores: dict[str, "CheckResult"] = field(default_factory=dict)  # 下游累积


@dataclass
class CheckResult:
    """一次检查的产出:硬门给 passed,软分给 score,detail 放定位信息(帧号/关节等)。"""

    name: str
    passed: bool | None = None  # 硬门:False = 短路判废
    score: float | None = None  # 软分:0-1,加权可补偿
    detail: dict[str, Any] = field(default_factory=dict)


Gate = Literal["hard", "soft"]

CheckFn = Callable[[Episode], CheckResult]


@dataclass(frozen=True)
class CheckSpec:
    """注册表里的一条检查:纯函数 + 元信息(门类型/依赖/资源声明)。"""

    fn: CheckFn
    name: str
    gate: Gate = "soft"
    needs: tuple[str, ...] = ()  # 声明依赖:"video" / "action" / "profile"
    gpus: float = 0.0  # 资源声明,适配器转译给框架(如 daft 的 gpus=)


_CHECK_REGISTRY: dict[str, CheckSpec] = {}


def register_check(
    fn: CheckFn,
    *,
    name: str,
    gate: Gate = "soft",
    needs: tuple[str, ...] = (),
    gpus: float = 0.0,
) -> CheckSpec:
    """把一个检查函数挂进注册表(自研检查与客户插件同一入口)。"""
    if gate not in ("hard", "soft"):
        raise ValueError(f"gate 必须是 hard/soft,got {gate!r}")
    if name in _CHECK_REGISTRY:
        raise ValueError(f"检查 {name!r} 已注册,不允许覆盖")
    spec = CheckSpec(fn=fn, name=name, gate=gate, needs=tuple(needs), gpus=gpus)
    _CHECK_REGISTRY[name] = spec
    return spec


def get_check(name: str) -> CheckSpec:
    if name not in _CHECK_REGISTRY:
        raise KeyError(f"检查 {name!r} 未注册;已有: {sorted(_CHECK_REGISTRY)}")
    return _CHECK_REGISTRY[name]


def all_checks() -> dict[str, CheckSpec]:
    return dict(_CHECK_REGISTRY)


def clear_registry() -> None:
    """仅测试用:清空注册表。"""
    _CHECK_REGISTRY.clear()
