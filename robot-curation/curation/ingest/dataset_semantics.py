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


#: 末端(EE)动作/状态的**列布局识别**(2026-09-16,umi 教训):此前 EE 数据一律写死
#: "第 3-5 列是 rpy 三元组",umi 的每手是 xyz + rot6d(6 列) + 夹爪宽度 = 10 列、双手 20 列,
#: 六维旋转被当欧拉角做里程表,指令与读数第 3 列差出十几倍量程 → 执行器饱和整批归零。
#: 现在按 info.json 的 names 逐列分类(位置/旋转/夹爪/其它),旋转列按**连续块长**认表示法:
#: 3=rpy、4=四元数、6=rot6d、9=旋转矩阵;没有 names 时只对经典 6/7 维(xyz+rpy[+夹爪])
#: 保留老规则,其余维数不把任何列当角度(宁可少做解绕,也不把别的量当角度算)。
_ROT_REPR_BY_LEN = {3: "rpy", 4: "quat", 6: "rot6d", 9: "rotmat"}
_ROT_TOKENS = ("rot6d", "rot_6d", "6d", "quat", "qx", "qy", "qz", "qw", "roll", "pitch", "yaw",
               "rx", "ry", "rz", "rotvec", "axis_angle", "axisangle", "rot", "orient", "euler",
               "wx", "wy", "wz")
_POS_TOKENS = ("pos_x", "pos_y", "pos_z", "x", "y", "z", "pos", "position", "trans", "tx", "ty", "tz")
_GRIP_TOKENS = ("gripper", "grip", "width", "open", "finger", "claw")


def _name_kind(name: str) -> str:
    """一列的名字 → pos / rot / grip / other。按下划线/点/空格切成词元后逐词判,
    夹爪最先(robot0_gripper_width 里也有 x 之类的短词元不能抢)。"""
    toks = [t for t in str(name).lower().replace(".", "_").replace(" ", "_").split("_") if t]
    if any(t in _GRIP_TOKENS for t in toks):
        return "grip"
    if any(t in _ROT_TOKENS for t in toks) or any(t.startswith("rot6d") for t in toks):
        return "rot"
    if any(t in _POS_TOKENS for t in toks):
        return "pos"
    return "other"


def detect_ee_layout(names: list | None, dim: int) -> dict:
    """EE 布局:{angle_dims, rotation_blocks:[(起点,长度,表示法)], euler_triplet, gripper_dims,
    translation_dims, source}。names 缺失/对不上维数 → 只对 6/7 维保留 xyz+rpy 老规则。"""
    names = [str(n) for n in (names or [])]
    if names and len(names) == dim:
        kinds = [_name_kind(n) for n in names]
        rot_idx = [i for i, k in enumerate(kinds) if k == "rot"]
        blocks = []
        i = 0
        while i < len(rot_idx):
            j = i
            while j + 1 < len(rot_idx) and rot_idx[j + 1] == rot_idx[j] + 1:
                j += 1
            start, length = rot_idx[i], j - i + 1
            blocks.append((start, length, _ROT_REPR_BY_LEN.get(length, "unknown")))
            i = j + 1
        grip = tuple(i for i, k in enumerate(kinds) if k == "grip")
        trans = tuple(i for i, k in enumerate(kinds) if k == "pos")
        return {"angle_dims": tuple(rot_idx), "rotation_blocks": blocks,
                "euler_triplet": bool(blocks) and all(b[2] == "rpy" for b in blocks),
                "gripper_dims": grip, "translation_dims": trans, "source": "names"}
    if dim in (6, 7):                                   # 经典 xyz+rpy(+夹爪):老规则原样
        return {"angle_dims": (3, 4, 5), "rotation_blocks": [(3, 3, "rpy")],
                "euler_triplet": True, "gripper_dims": (6,) if dim == 7 else (),
                "translation_dims": (0, 1, 2), "source": "dim_rule"}
    return {"angle_dims": (), "rotation_blocks": [], "euler_triplet": False,
            "gripper_dims": (), "translation_dims": (), "source": "unknown"}


def layout_for_extras(layout: dict) -> dict:
    """布局写进 semantics_extras(JSON 列,随行流到漏斗;tuple 型语义字段进不了 daft)。"""
    return {"angle_dims": list(layout.get("angle_dims") or ()),
            "rotation_blocks": [list(b) for b in (layout.get("rotation_blocks") or [])],
            "euler_triplet": bool(layout.get("euler_triplet")),
            "gripper_dims": list(layout.get("gripper_dims") or ()),
            "translation_dims": list(layout.get("translation_dims") or ()),
            "source": str(layout.get("source") or "")}


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


def _names_look_ee(names: list) -> bool:
    """字段名像不像末端位姿:经典 x/y/z/roll/pitch/yaw,或按词元认出"位置+旋转"两类
    (umi 的 robot0_pos_x / robot0_rot6d_0 这种带前缀的名字,集合交集认不出)。"""
    if set(names) & _EE_NAMES:
        return True
    xyz = ("x", "y", "z", "tx", "ty", "tz")
    has_xyz = any(any(t in xyz for t in str(n).lower().replace(".", "_").split("_"))
                  for n in names)
    return has_xyz and any(_name_kind(n) == "rot" for n in names)


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


def _profile_extras_with_layout(prof: dict) -> dict:
    """档案 extras;档案若声明了 rotation_blocks(非 rpy 的姿态表示,如 umi 的 rot6d),把布局
    一并写进 extras["layout"] 让漏斗按块处理。没声明的老档案不写 → 漏斗走老规则,数值不变。"""
    act = prof.get("action", {}) or {}
    extras = dict(prof.get("extras", {}) or {})
    blocks = act.get("rotation_blocks")
    if blocks:
        lay = {"angle_dims": tuple(act.get("angle_dims", [])),
               "rotation_blocks": [tuple(b) for b in blocks],
               "euler_triplet": bool(act.get("euler_triplet", False)),
               "gripper_dims": tuple(act.get("gripper_dims", [])),
               "translation_dims": tuple(act.get("translation_dims", [])),
               "source": "profile"}
        extras["layout"] = layout_for_extras(lay)
    return extras


def resolve_semantics(info: dict, sample_action: np.ndarray | None = None,
                      dataset_name: str = "") -> DatasetSemantics:
    """解析数据集语义:先 profile 命中,否则数值指纹推断。

    info: meta/info.json;sample_action: 一条 action(用于指纹推断,可空)。
    """
    anames = _action_names(info)
    pnames = _action_names(info, "observation.state")
    is_ee = _names_look_ee(anames)
    prop_ee = _names_look_ee(pnames)

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
            extras=_profile_extras_with_layout(prof))

    # —— 回退:数值指纹推断(未知数据集也能工作)——
    cmode = _infer_control_mode(sample_action) if sample_action is not None else "unknown"
    sem = DatasetSemantics(
        action_space="ee" if is_ee else "joint",
        proprio_space="ee" if prop_ee else "joint",
        control_mode=cmode, source="inferred")
    if is_ee and sample_action is not None and np.asarray(sample_action).ndim == 2:
        lay = detect_ee_layout(list(anames), int(np.asarray(sample_action).shape[1]))
        sem.angle_dims = tuple(lay["angle_dims"])
        sem.euler_triplet = bool(lay["euler_triplet"])
        if not sem.gripper_dims and lay["gripper_dims"]:
            sem.gripper_dims = tuple(lay["gripper_dims"])
        sem.extras = dict(sem.extras or {}, layout=layout_for_extras(lay))
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
