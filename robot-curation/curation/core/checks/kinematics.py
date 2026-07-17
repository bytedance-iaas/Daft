"""运动学校验:关节/速度极限 + 格式(文档中的 M5b 核心)。硬门,纯数值对照规格库。

对照 EmbodimentProfile(M2):关节极限(带裕度)、速度极限(差分×fps)、维度/数值校验。
违规 → 硬门短路,detail 定位到具体关节+帧号(客户能照着修)。
分派规则(P2 结论):**被检序列必须是关节空间**——action 是 EE 指令的数据集(如 DROID),
调用方应传 proprio 关节列或跳过本检查;本函数不猜语义,dim 不匹配直接判格式违规。
draft profile(极限未收集,如 agibot)→ passed=None(不可判,诚实缺省,不硬判好坏)。
注:scorer 的 "collision" 是假的(统计 spike)—— 本模块才是真物理校验。
"""
from __future__ import annotations

import numpy as np

from ..contract import CheckResult

_MAX_VIOLATIONS_IN_DETAIL = 20


def kinematic_limits(
    values: np.ndarray,
    profile,                    # registry.EmbodimentProfile
    fps: float,
    *,
    margin: float = 0.02,       # 极限裕度(量程的 2%;标定误差容忍)
    check_velocity: bool = True,
) -> CheckResult:
    """values: [T, dof] 关节空间序列(action 或 proprio 关节列,由调用方按语义选)。"""
    v = np.asarray(values, dtype=np.float64)

    # ① 格式硬校验
    if v.ndim != 2:
        return CheckResult(name="kinematic_limits", passed=False,
                           detail={"reason": f"应为 [T, dof] 二维,got shape={v.shape}"})
    if not np.isfinite(v).all():
        bad = np.argwhere(~np.isfinite(v))[:_MAX_VIOLATIONS_IN_DETAIL]
        return CheckResult(name="kinematic_limits", passed=False,
                           detail={"reason": "含 NaN/Inf",
                                   "frames": [{"frame": int(f), "joint": int(j)} for f, j in bad]})
    if v.shape[1] != profile.dof:
        return CheckResult(
            name="kinematic_limits", passed=False,
            detail={"reason": f"维度 {v.shape[1]} != {profile.embodiment_id} 的 dof {profile.dof}"
                              "(action 若是 EE 空间指令,应传 proprio 关节列或跳过本检查)"})

    # ② draft profile:极限未收集,或表虽有数字但被标记为草稿(未经人工验证/发现过
    # 约定错位)→ 不可判。硬判资格只给 authoritative/approximate 两档。
    if not profile.has_limits or profile.quality == "draft":
        return CheckResult(name="kinematic_limits", passed=None,
                           detail={"reason": f"{profile.embodiment_id} 极限表不可用于硬判"
                                             f"(quality={profile.quality}),仅做了格式校验"})

    violations = []

    # ③ 关节极限(带裕度)
    sustained = []                        # 持续限外的关节(约定错位嫌疑)
    for j, (lo, hi) in enumerate(profile.joint_limits):
        band = margin * (hi - lo)
        bad_frames = np.where((v[:, j] < lo - band) | (v[:, j] > hi + band))[0]
        # 物理硬限不可能被"持续"违反:机械臂无法全程停在限位外还正常作业。
        # 某关节 ≥90% 帧都在限外 = 数据/profile 的角度约定错位(如舵机原始角 vs ±中心角)
        # → 整体弃权待人工核对 profile,绝不误杀(so100 2026-07-07 全灭事故的教训)。
        if len(bad_frames) >= 0.9 * len(v):
            sustained.append({"joint": j,
                              "data_range": [round(float(v[:, j].min()), 1),
                                             round(float(v[:, j].max()), 1)],
                              "limit": [lo, hi]})
            continue
        for f in bad_frames[:_MAX_VIOLATIONS_IN_DETAIL]:
            violations.append({"type": "joint_limit", "joint": j, "frame": int(f),
                               "value": round(float(v[f, j]), 4),
                               "limit": [lo, hi]})

    # ④ 速度极限(差分×fps,带同样裕度)
    if check_velocity and profile.velocity_limits:
        speed = np.abs(np.diff(v, axis=0)) * fps
        for j, vmax in enumerate(profile.velocity_limits):
            bad_frames = np.where(speed[:, j] > vmax * (1.0 + margin))[0]
            for f in bad_frames[:_MAX_VIOLATIONS_IN_DETAIL]:
                violations.append({"type": "velocity_limit", "joint": j, "frame": int(f),
                                   "value": round(float(speed[f, j]), 4),
                                   "limit": vmax})

    if sustained:
        return CheckResult(
            name="kinematic_limits", passed=None,
            detail={"reason": "关节持续限外(≥90%帧)=数据与profile角度约定错位嫌疑,"
                              "拒绝硬判,待核对 profile",
                    "sustained_out_of_limit": sustained,
                    "transient_violations": violations[:_MAX_VIOLATIONS_IN_DETAIL]})
    return CheckResult(
        name="kinematic_limits",
        passed=not violations,
        detail={
            "n_violations": len(violations),
            "violations": violations[:_MAX_VIOLATIONS_IN_DETAIL],
            "profile": profile.embodiment_id,
            "unit": profile.unit,
            "margin": margin,
        },
    )


def ee_limits(
    pose: "np.ndarray",
    profile,                    # registry.EmbodimentProfile(须有 ee_reach_m)
    fps: float,
    *,
    reach_margin: float = 0.25,     # 可达半径裕度:规格臂展量到法兰,数据常记夹爪
                                    # 尖端(droid 实测 0.944m vs 0.855m,Robotiq 工具
                                    # 偏置 ~15cm)→ 裕度须容工具长度;本检查定位=抓
                                    # "必然损坏"(2m 外),贴线精判是伪精度
    vel_margin: float = 0.10,
) -> CheckResult:
    """EE(笛卡尔)规格检查(2026-07-14,不做 IK):pose=[T,≥6] 的 x,y,z,roll,pitch,yaw
    (proprio 的 EE 绝对位姿,基座系米制)。三道:
    ① 可达性:|xyz| ≤ ee_reach_m×(1+裕度)——手掌出现在臂展之外=数据必然损坏;
    ② 平移速度:|Δxyz|×fps ≤ translation_m_s×(1+裕度)——瞬移=坏帧;
    ③ 旋转速度:**四元数测地步长**×fps ≤ rotation_rad_s×(1+裕度)——
       不用欧拉差分(±π回绕/万向节会假跳变,droid 实测教训)。
    守卫(与关节检查同门风):draft 表不可判;单位守卫(p95距离/reach>3 → 疑 mm 记
    米表,拒比);持续出界(≥90%帧)= 基座系/标定约定错位嫌疑,弃权待人工。
    定位:EE 检查弱于关节检查(手掌在空间内≠关节没超限),增量不平替。
    """
    v = np.asarray(pose, dtype=np.float64)
    if v.ndim != 2 or v.shape[1] < 6:
        return CheckResult(name="kinematic_limits", passed=False,
                           detail={"reason": f"EE 位姿应为 [T,≥6],got shape={v.shape}",
                                   "mode": "ee"})
    if not np.isfinite(v[:, :6]).all():
        bad = np.argwhere(~np.isfinite(v[:, :6]))[:_MAX_VIOLATIONS_IN_DETAIL]
        return CheckResult(name="kinematic_limits", passed=False,
                           detail={"reason": "含 NaN/Inf", "mode": "ee",
                                   "frames": [{"frame": int(f), "dim": int(j)} for f, j in bad]})
    if not profile.has_ee_limits or profile.quality == "draft":
        return CheckResult(name="kinematic_limits", passed=None,
                           detail={"reason": f"{profile.embodiment_id} 无可用 EE 规格"
                                             f"(quality={profile.quality}),仅做了格式校验",
                                   "mode": "ee"})

    xyz = v[:, :3]
    dist = np.linalg.norm(xyz, axis=1)
    reach = float(profile.ee_reach_m)

    # 单位守卫(p95 鲁棒,B1 同款教训):mm 记的数据 vs 米制表,全程"出界"实为单位不符
    d_scale = float(np.percentile(dist, 95))
    if d_scale > 3.0 * reach:
        return CheckResult(name="kinematic_limits", passed=None,
                           detail={"reason": f"单位疑似错配:EE 距离典型值(p95) {d_scale:.3g}m vs "
                                             f"臂展 {reach}m,拒绝硬比", "mode": "ee",
                                   "unit_mismatch": True})

    violations = []
    bad_reach = np.where(dist > reach * (1.0 + reach_margin))[0]
    # 持续出界 ≥90% = 基座系原点/标定约定错位嫌疑(真损坏不会整条都错)→ 弃权
    if len(bad_reach) >= 0.9 * len(v):
        return CheckResult(name="kinematic_limits", passed=None,
                           detail={"reason": "EE 位置持续出可达范围(≥90%帧)=基座系约定"
                                             "错位嫌疑,拒绝硬判,待核对 profile",
                                   "mode": "ee",
                                   "dist_range_m": [round(float(dist.min()), 3),
                                                    round(float(dist.max()), 3)],
                                   "reach_m": reach})
    for f in bad_reach[:_MAX_VIOLATIONS_IN_DETAIL]:
        violations.append({"type": "ee_reach", "joint": "xyz", "frame": int(f),
                           "value": round(float(dist[f]), 4),
                           "limit": round(reach * (1.0 + reach_margin), 4)})

    vel = profile.ee_velocity_limits or {}
    if len(v) >= 2 and vel.get("translation_m_s"):
        tv = np.linalg.norm(np.diff(xyz, axis=0), axis=1) * fps
        vmax = float(vel["translation_m_s"]) * (1.0 + vel_margin)
        for f in np.where(tv > vmax)[0][:_MAX_VIOLATIONS_IN_DETAIL]:
            violations.append({"type": "ee_translation_velocity", "joint": "xyz",
                               "frame": int(f), "value": round(float(tv[f]), 4),
                               "limit": round(vmax, 4)})
    if len(v) >= 2 and vel.get("rotation_rad_s"):
        from scipy.spatial.transform import Rotation
        rot = Rotation.from_euler("xyz", v[:, 3:6])
        rv = (rot[:-1].inv() * rot[1:]).magnitude() * fps    # 测地步长,免回绕/万向节假阳性
        rmax = float(vel["rotation_rad_s"]) * (1.0 + vel_margin)
        for f in np.where(rv > rmax)[0][:_MAX_VIOLATIONS_IN_DETAIL]:
            violations.append({"type": "ee_rotation_velocity", "joint": "rpy",
                               "frame": int(f), "value": round(float(rv[f]), 4),
                               "limit": round(rmax, 4)})

    return CheckResult(
        name="kinematic_limits",
        passed=not violations,
        detail={"mode": "ee", "n_violations": len(violations),
                "violations": violations[:_MAX_VIOLATIONS_IN_DETAIL],
                "profile": profile.embodiment_id,
                "reach_m": reach, "margin": reach_margin,
                "note": "EE 规格检查(可达性+笛卡尔速度);弱于关节检查,增量非平替"})
