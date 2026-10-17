"""动作语义预检(preflight,2026-09-02 用户定):profile 没命中时,不再靠字段名猜,而是拿样本
数据验 5 种假设——关节-绝对 / 关节-增量 / 末端-绝对 / 末端-增量(含速度) / 无法判断。

起因:libero 的 action 名只有笼统的 "actions",字段名规则把末端增量当成关节角送去对 Franka
关节极限,5/5 判废。数据本身其实分得很清:末端增量与位姿差分逐帧高度吻合(相关 0.9+),
关节角从第 0 帧起就在极限内——把这些"像不像"算出来,选最像的;都不像就老实说不知道。

只用数值(action/state/fps + 本体规格的关节数与极限),不碰视频/模型;样本=摄入时已读的前
N 条。profile 命中的数据集也跑一遍作核对(声明与数据打架要报);profile 永远优先。
"""
from __future__ import annotations

import numpy as np

HYPOTHESES = ("joint_absolute", "joint_delta", "ee_absolute", "ee_increment")
#: 一个末端位姿 = 3 平移 + 3/4/6 旋转(+夹爪):单臂 6–8 列,双臂 12–16 列;列数在此之外的 action 不可能是位姿
#: (2026-10-14:26 关节的 G1 人形被判成末端,同步代理拿左肩三关节当 xyz)
EE_ARM_COLS = frozenset(range(6, 9)) | frozenset(range(12, 17))
#: 列名分词后含这些词 = 关节空间(肩肘腕髋膝踝腰、joint/motor/servo、手指);末端名(x/y/z/roll/pitch/yaw)可以跟在关节词后面
#: (kLeftWristRoll 是关节),所以关节词优先
JOINT_WORDS = frozenset({"joint", "joints", "motor", "servo", "shoulder", "elbow", "wrist", "hip", "knee", "ankle",
                         "waist", "torso", "finger", "thumb", "pinky", "index", "middle", "ring", "hand", "q", "dof"})
EE_WORDS = frozenset({"x", "y", "z", "roll", "pitch", "yaw", "qx", "qy", "qz", "qw", "rot6d", "ee", "eef", "pose", "tcp"})


def name_tokens(name: str) -> list[str]:
    """kLeftShoulderPitch / left_joint_0.pos / target_arm_left_joint_0 → 小写词元(拆驼峰、下划线、点、数字)。"""
    import re
    t = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    t = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", t)
    return [w for w in re.split(r"[^A-Za-z]+", t.lower()) if w]


def names_hint_of(names) -> str | None:
    """列名给出的空间提示:有关节词 → "joint";只有末端词 → "ee";都没有 → None。
    关节词优先:kLeftWristRoll / wrist_roll.pos 是关节,不是姿态角。"""
    toks = {w for n in (names or []) for w in name_tokens(n)}
    if toks & JOINT_WORDS:
        return "joint"
    if toks & EE_WORDS:
        return "ee"
    return None
CONFIDENT_MIN = 0.6       # 最像的假设至少这么像
CONFIDENT_MARGIN = 0.2    # 且比第二名至少高这么多,否则"没把握"
_HEAD = 5


def _gripper_cols(a: np.ndarray, hint=()) -> tuple:
    """夹爪列:本体规格给了就用;否则看最后一列是否二值/开度型(取值 ≤3 种或落在 [0,1]/[-1,1]
    且大多数帧不变)。"""
    if hint:
        return tuple(int(d) for d in hint if d < a.shape[1])
    j = a.shape[1] - 1
    col = a[:, j]
    uniq = np.unique(np.round(col, 3))
    still = float(np.mean(np.abs(np.diff(col)) < 1e-6)) if len(col) > 1 else 0.0
    bounded = float(col.min()) >= -1.0 - 1e-6 and float(col.max()) <= 1.0 + 1e-6
    if len(uniq) <= 3 or (bounded and still > 0.6):
        return (j,)
    return ()


def _corr_cols(x: np.ndarray, y: np.ndarray) -> list[float]:
    """逐列相关(只算两边都有变化的帧;常数列记 0)。"""
    out = []
    for j in range(min(x.shape[1], y.shape[1])):
        xj, yj = x[:, j], y[:, j]
        if xj.std() < 1e-12 or yj.std() < 1e-12:
            out.append(0.0)
            continue
        out.append(float(np.corrcoef(xj, yj)[0, 1]))
    return out


def _episode_evidence(a: np.ndarray, s, fps: float, *, joint_limits, dof, gripper):
    """单条的各假设证据(0..1);None = 该假设在本条上不适用。"""
    arm_cols = [j for j in range(a.shape[1]) if j not in gripper]
    arm = a[:, arm_cols]
    ev: dict = {}
    # ── 关节-绝对:维数对得上 + 从第 0 帧起就在极限内 + 幅值与极限同量级 ──
    if joint_limits and len(joint_limits) == a.shape[1]:
        lim = np.asarray(joint_limits, dtype=np.float64)
        lo, hi = lim[:, 0], lim[:, 1]
        band = 0.02 * (hi - lo)
        inside = (a >= lo - band) & (a <= hi + band)
        # 取**最差关节**的在限比例:真关节数据每个关节都几乎全程在限内;末端量冒充关节时
        # 总有某一列(如 joint4 的 [-3.07,-0.07])大半在限外——用均值会被其它 6 列稀释
        frac_in = float(inside.mean(axis=0).min())
        head_in = 1.0 if (len(a) >= _HEAD and bool(inside[:_HEAD].all())) else 0.0
        lmax = float(np.abs(lim).max())
        mag = float(np.percentile(np.abs(arm), 95)) / (lmax + 1e-12)
        mag_ok = 0.05 <= mag <= 1.2
        ev["joint_absolute"] = frac_in * head_in * (1.0 if mag_ok else 0.3)
        # ── 关节-增量:维数对 + 幅值远小于极限(累加后才是角度)──
        small = mag < 0.15
        ev["joint_delta"] = 0.5 if small else 0.05
        if s is not None and s.shape[1] >= a.shape[1]:
            ds = np.diff(s[:, :a.shape[1]], axis=0)
            r = np.median(_corr_cols(a[:-1], ds))
            ev["joint_delta"] = max(ev["joint_delta"], float(np.clip(r, 0, 1))) if small else ev["joint_delta"]
    elif dof and a.shape[1] == dof:
        ev["joint_absolute"] = 0.35        # 维数对但没极限可验:弱证据
        ev["joint_delta"] = 0.2
    elif s is not None and s.shape[1] >= a.shape[1] and len(s) == len(a) and len(a) >= 10:
        # 没有规格库(未登记本体):关节位置指令就是关节读数的目标,两者逐列高度相关;幅值在 ±2π 内
        # (弧度)或 ±360(度)。末端位姿的 xyz 也和自己的读数相关,所以这条只给到 0.8,靠列名和维数门分胜负。
        # 读数比指令宽时取前 a 列(与上面关节-增量一条同口径):拆列拼成的流把与指令配对的列放在最前
        # (split_features,Galaxea 指令 14 列、读数 24 列)
        r = float(np.median(_corr_cols(a, s[:, :a.shape[1]])))
        mag_ok = float(np.percentile(np.abs(arm), 99)) <= 2 * np.pi + 1e-6 or float(np.percentile(np.abs(arm), 99)) <= 360.0
        ev["joint_absolute"] = 0.8 * float(np.clip(r, 0, 1)) * (1.0 if mag_ok else 0.3)
        ev["joint_delta"] = 0.1
    else:
        ev["joint_absolute"] = 0.0
        ev["joint_delta"] = 0.0
    # ── 末端两种:手臂列数要像位姿(单臂 6–8,双臂 12–16);靠状态列验 ──
    if len(arm_cols) in EE_ARM_COLS:
        ev["ee_absolute"] = 0.2
        ev["ee_increment"] = 0.2
        if s is not None and s.shape[1] >= 6 and len(s) == len(a) and len(a) >= 10:
            xyz_a = arm[:, :3]
            xyz_s = s[:, :3]
            r_abs = float(np.median(_corr_cols(xyz_a, xyz_s)))
            ratio = float(np.percentile(np.abs(xyz_a), 95) / (np.percentile(np.abs(xyz_s), 95) + 1e-12))
            ev["ee_absolute"] = float(np.clip(r_abs, 0, 1)) * (1.0 if 0.3 <= ratio <= 3.0 else 0.3)
            ds = np.diff(xyz_s, axis=0) * fps
            mask = np.abs(xyz_a[:-1]).max(axis=1) > 0.1 * (np.percentile(np.abs(xyz_a), 95) + 1e-12)
            if mask.sum() >= 10:
                r_inc = float(np.median(_corr_cols(xyz_a[:-1][mask], ds[mask])))
            else:
                r_inc = float(np.median(_corr_cols(xyz_a[:-1], ds)))
            ev["ee_increment"] = float(np.clip(r_inc, 0, 1))
    else:
        ev["ee_absolute"] = 0.0
        ev["ee_increment"] = 0.0
    return ev


def preflight(sample_rows: list[dict], *, joint_limits=None, dof: int | None = None,
              gripper_hint=(), names_hint: str | None = None) -> dict:
    """样本行 → 最像的语义假设与证据。names_hint: "ee"/"joint"/None(字段名的弱先验,+0.1)。

    返回 {status: confident|ambiguous|none, hypothesis, action_space, control_mode,
          proprio_space, fit(吻合度=最像假设的分), scores{假设:分}, gripper_dims, n}
    """
    rows = [r for r in sample_rows if r.get("action") is not None]
    if not rows:
        return {"status": "none", "hypothesis": None, "action_space": "unknown",
                "control_mode": "unknown", "proprio_space": "unknown", "fit": 0.0,
                "scores": {}, "gripper_dims": (), "n": 0}
    a0 = np.asarray(rows[0]["action"], dtype=np.float64)
    gripper = _gripper_cols(a0, gripper_hint) if a0.ndim == 2 else ()
    per = {h: [] for h in HYPOTHESES}
    for r in rows:
        a = np.asarray(r["action"], dtype=np.float64)
        if a.ndim != 2 or len(a) < 3:
            continue
        s = r.get("proprio_state")
        s = np.asarray(s, dtype=np.float64) if s is not None else None
        if s is not None and s.ndim != 2:
            s = None
        ev = _episode_evidence(a, s, float(r.get("fps") or 0) or 1.0,
                               joint_limits=joint_limits, dof=dof, gripper=gripper)
        for h in HYPOTHESES:
            per[h].append(ev.get(h, 0.0))
    scores = {h: (float(np.median(v)) if v else 0.0) for h, v in per.items()}
    # 列名是强证据(2026-10-14):写着 shoulder / elbow / joint 的不是位姿,写着 x / y / z 的不是关节角;
    # 另一族的假设打两折,本族加 0.1
    if names_hint == "ee":
        scores["ee_absolute"] += 0.1; scores["ee_increment"] += 0.1
        scores["joint_absolute"] *= 0.2; scores["joint_delta"] *= 0.2
    elif names_hint == "joint":
        scores["joint_absolute"] += 0.1; scores["joint_delta"] += 0.1
        scores["ee_absolute"] *= 0.2; scores["ee_increment"] *= 0.2
    # 关节-绝对拿到了"维数对上 + 每个关节从第 0 帧起全程在极限内"的强证据时,末端假设
    # 让位:关节角数据也会和自己的读数高相关(那正是 ee_absolute 的证据),但一个 7 维向量
    # 恰好全程落进 Franka 那几个不对称区间,靠巧合几乎不可能(libero 的末端量就栽在这)。
    if joint_limits and scores["joint_absolute"] >= 0.95:
        scores["ee_absolute"] *= 0.5
        scores["ee_increment"] *= 0.5
    scores = {h: round(min(1.0, v), 3) for h, v in scores.items()}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, second = ranked[0], ranked[1]
    if best[1] >= CONFIDENT_MIN and best[1] - second[1] >= CONFIDENT_MARGIN:
        status = "confident"
    elif best[1] >= CONFIDENT_MIN:
        status = "ambiguous"
    else:
        status = "none"
    space, mode = best[0].split("_")
    mode = {"absolute": "absolute", "delta": "delta", "increment": "delta"}[mode]
    out = {"status": status, "hypothesis": best[0], "fit": best[1], "scores": scores,
           "gripper_dims": tuple(gripper), "n": len(rows)}
    if status == "confident":
        out.update(action_space=space, control_mode=mode, proprio_space=space)
    else:
        out.update(action_space="unknown", control_mode="unknown", proprio_space="unknown")
    return out


def agrees_with(pf: dict, sem) -> bool | None:
    """preflight 结论与 profile 声明是否一致(增量与速度视为同类);preflight 没把握 → None。"""
    if pf.get("status") != "confident":
        return None
    same_space = pf["action_space"] == sem.action_space
    mode_ok = (pf["control_mode"] == sem.control_mode
               or {pf["control_mode"], sem.control_mode} == {"delta", "velocity"})
    return bool(same_space and mode_ok)
