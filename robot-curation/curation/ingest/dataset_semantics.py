"""数据集语义解析层(2026-07-09):action/state 的"含义"从这里统一解析,可扩展。

设计目标(用户要求):未来出现带新规则的数据集,只需**新增一个 profile YAML**(数据,
非代码),系统即能正确解释它,且**不影响已有数据集**。未知数据集自动回退到数值指纹推断。

一条数据的 action 是"关节角/EE位姿"、"绝对/增量/速度"、"度/弧度"、"哪列是夹爪"——
这些语义此前散落在各处的指纹推断里。本层把它们收敛为一个 DatasetSemantics 对象,
来源分两档:
  ① profile 命中(dataset_profiles/*.yaml 按 robot_type+字段名+版本匹配)→ 权威声明;
  ② 未命中 → 数值指纹推断(现有启发式)→ 标记 source="inferred"。
下游(reader/funnel/checks)只读这个对象,不再各自猜。
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from typing import Any

import numpy as np

_EE_NAMES = {"x", "y", "z", "roll", "pitch", "yaw"}
_PROFILE_DIR = os.path.join(os.path.dirname(__file__), "dataset_profiles")


@dataclass
class DatasetSemantics:
    """一个数据集的 action/state 语义。下游据此正确处理,不再猜。"""
    action_space: str = "joint"          # joint / ee
    proprio_space: str = "joint"         # joint / ee
    control_mode: str = "absolute"       # absolute / delta / velocity / unknown
    unit: str = "unknown"                # rad / deg / normalized / pixel / meter+rad ...
    gripper_dims: tuple = ()             # action 里夹爪列下标
    angle_dims: tuple = ()               # 姿态角/关节角列下标(供差分解绕/测地)
    euler_triplet: bool = False          # angle_dims 是 EE 的 rpy 三元组(测地里程表)
    stuck_strategy: str = "auto"         # cmd_delta_vs_pos / increment_vs_pos /
                                         #   velocity_dual_scale / abstain / auto
    source: str = "inferred"             # profile / inferred
    profile_name: str = ""
    cameras: dict = field(default_factory=dict)   # {相机短名: view}(front/rear/wrist/side/unknown)
    extras: dict = field(default_factory=dict)


def _infer_control_mode(action: np.ndarray) -> str:
    a = np.asarray(action, dtype=np.float64)
    if a.ndim != 2 or a.shape[0] < 3:
        return "unknown"
    scale = np.abs(a).mean()
    if scale < 1e-9:
        return "unknown"
    step = np.abs(np.diff(a, axis=0)).mean()
    return "absolute" if scale > 10.0 * (step + 1e-9) else "delta"


def _load_profiles() -> list[dict]:
    profs = []
    for p in sorted(glob.glob(os.path.join(_PROFILE_DIR, "*.yaml"))):
        try:
            import yaml
            profs.append(yaml.safe_load(open(p)) | {"_file": os.path.basename(p)})
        except Exception:  # noqa: BLE001
            continue
    return profs


def _action_names(info: dict, key: str = "action") -> list[str]:
    names = info.get("features", {}).get(key, {}).get("names") or []
    if isinstance(names, dict):
        names = next(iter(names.values()), [])
    return [str(n).lower() for n in names]


#: 测试/回测开关:设为 1 时忽略所有 profile,强制走指纹+预检(拿 profile 声明当标准答案对分)。
IGNORE_PROFILES_ENV = "CURATION_SEMANTICS_IGNORE_PROFILES"


def _match_profile(info: dict, profiles: list[dict],
                   dataset_name: str = "") -> dict | None:
    if os.environ.get(IGNORE_PROFILES_ENV, "").strip() in ("1", "true", "yes"):
        return None
    """按 match 段匹配:robot_type / action_names / codebase_version /
    dataset_name 全命中才算。dataset_name(2026-08-27):官方 lerobot/droid_100
    的 robot_type=unknown、names=motor_*,前两把钥匙全废 —— 目录名是这类
    元数据残缺数据集唯一可靠的身份。"""
    robot = str(info.get("robot_type", "")).lower()
    anames = _action_names(info)
    version = str(info.get("codebase_version", ""))
    for prof in profiles:
        m = prof.get("match", {})
        if "dataset_name" in m and str(m["dataset_name"]).lower() \
                != str(dataset_name or "").lower():
            continue
        if "robot_type" in m and str(m["robot_type"]).lower() != robot:
            continue
        if "action_names" in m and [str(x).lower() for x in m["action_names"]] != anames:
            continue
        if "codebase_version_prefix" in m and not version.startswith(
                str(m["codebase_version_prefix"])):
            continue
        if m:                             # 至少有一条匹配条件且全通过
            return prof
    return None


def resolve_semantics(info: dict, sample_action: np.ndarray | None = None,
                      dataset_name: str = "") -> DatasetSemantics:
    """解析数据集语义:先 profile 命中,否则数值指纹推断。

    info: meta/info.json;sample_action: 一条 action(用于指纹推断,可空)。
    """
    anames = _action_names(info)
    pnames = _action_names(info, "observation.state")
    is_ee = bool(set(anames) & _EE_NAMES)
    prop_ee = bool(set(pnames) & _EE_NAMES)

    prof = _match_profile(info, _load_profiles(), dataset_name)
    if prof is not None:
        act = prof.get("action", {})
        st = prof.get("state", {})
        return DatasetSemantics(
            action_space=act.get("space", "ee" if is_ee else "joint"),
            proprio_space=st.get("space", "ee" if prop_ee else "joint"),
            control_mode=act.get("control_mode", "unknown"),
            unit=act.get("unit", "unknown"),
            gripper_dims=tuple(act.get("gripper_dims", [])),
            angle_dims=tuple(act.get("angle_dims", [])),
            euler_triplet=bool(act.get("euler_triplet", False)),
            stuck_strategy=act.get("stuck_strategy", "auto"),
            source="profile", profile_name=prof.get("_file", ""),
            cameras={str(k): (v.get("view") if isinstance(v, dict) else str(v))
                     for k, v in (prof.get("cameras") or {}).items()},
            extras=prof.get("extras", {}) or {})

    # —— 回退:数值指纹推断(未知数据集也能工作)——
    cmode = _infer_control_mode(sample_action) if sample_action is not None else "unknown"
    sem = DatasetSemantics(
        action_space="ee" if is_ee else "joint",
        proprio_space="ee" if prop_ee else "joint",
        control_mode=cmode, source="inferred")
    if is_ee and sample_action is not None and np.asarray(sample_action).shape[1] >= 6:
        sem.angle_dims = (3, 4, 5)
        sem.euler_triplet = True
    return sem


def needs_velocity_calibration(sem: DatasetSemantics) -> bool:
    """末端速度/增量指令 × 末端位姿读数:执行器饱和要靠速度域标定才算得了。"""
    return (sem.action_space == "ee" and sem.proprio_space == "ee"
            and sem.control_mode in ("velocity", "delta"))


def attach_velocity_calibration(sem: DatasetSemantics, sample_rows: list[dict]) -> DatasetSemantics:
    """末端速度/增量指令 × 末端位姿读数的数据集 → 用样本行标定速度域增益/延迟/基线,
    塞进 extras["velocity_calibration"](随 semantics_extras 列流到运动质量检查;执行器饱和
    据此在速度域计算,见 core/checks/velocity_calibration.py)。其它数据集原样返回。"""
    if not needs_velocity_calibration(sem):
        return sem
    if not any(r.get("proprio_state") is not None for r in sample_rows):
        return sem
    from ..core.checks.velocity_calibration import fit_velocity_gain
    try:
        calib = fit_velocity_gain(sample_rows)
    except Exception:  # noqa: BLE001  标定失败=退回"不适用",不拖垮摄入
        calib = None
    if calib:
        sem.extras = dict(sem.extras or {})
        sem.extras["velocity_calibration"] = calib
    return sem


def infer_control_mode_majority(rows: list[dict]) -> str:
    """数据集级多数票(控制模式是本体约定,非单条属性;短片单向漂移会骗过单条指纹)。"""
    from collections import Counter
    votes = [_infer_control_mode(r["action"]) for r in rows if r.get("action") is not None]
    known = [v for v in votes if v != "unknown"]
    if not known:
        return "unknown"
    top, cnt = Counter(known).most_common(1)[0]
    return top if cnt / len(known) >= 0.8 else "unknown"
