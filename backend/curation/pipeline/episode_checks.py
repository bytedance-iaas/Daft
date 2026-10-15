"""The per-episode check bodies: ``make_timestamp_check``, ``make_kinematic_check``,
``make_motion_check``, ``make_frame_checks`` and ``task_check_episode``.

``curation check`` calls them one episode at a time (``pipeline/check_stage.py``); the checks themselves are
the pure functions of ``core/``. Model clients and the frame decoder are passed in, so the stage can wrap them
per episode to record execution incidents (D33) without touching the A-class code they call.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

import numpy as np

from ..core.contract import CheckResult
from ..core.checks.kinematics import kinematic_limits
from ..core.checks.motion_quality import motion_quality
from ..core.checks.video_action_sync import (
    camera_reading,
    global_lag,
    grouped_lag,
    joint_speed,
    optical_flow_energy,
    sync_check_result,
    timestamp_check,
)
from ..core.checks.visual_quality import is_live_channel, visual_quality
from .config import enabled


def result_to_struct(res: CheckResult) -> dict:
    """A check's result as the stage records it: ``detail`` serialized to a JSON string."""
    return {"passed": res.passed, "score": res.score,
            "detail": json.dumps(res.detail, ensure_ascii=False, default=str)}


def sync_plot_worthy(mode: str, det: dict) -> bool:
    """这条 episode 的同步曲线值不值得留存(flagged 档判据)。

    与 UI 筛选(manifest._sync_flagged)同一把尺子(2026-09-01 用户抓出口径分裂:
    筛选认弃权路而出图不认,默认档下"值得留意"的条目根本无图可筛):
    非对齐 / 可信错位·无信号(flagged_cameras) / 弃权路(abstained_cameras,
    suspect/noisy 是其子集)。all=全存;off=不存。
    """
    if mode == "all":
        return True
    if mode != "flagged":
        return False
    return bool(det.get("verdict") != "aligned"
                or det.get("flagged_cameras")
                or det.get("abstained_cameras")
                or _cameras_disagree(det))


#: 存图用的「两路读数不一致」门槛:与发现层分窗判据的最小门槛同值(findings.WINDOW_SPREAD_MIN_S),
#: 只决定曲线存不存,宁可多存——MV-3 进了人工复核线,人要看图(2026-10-13)
_DISAGREE_S = 0.1


def _cameras_disagree(det: dict) -> bool:
    """两路以上可信相机的整条读数或分窗中位数彼此相差超过 _DISAGREE_S(MV-3 会报的那类条目)。"""
    per = det.get("per_camera")
    if not isinstance(per, dict):
        return False
    for key in ("lag_s", "windowed"):
        vals = []
        for c in per.values():
            if not isinstance(c, dict):
                continue
            if key == "lag_s":
                v = c.get("lag_s") if c.get("trusted") else None
            else:
                lw = c.get("lag_windows")
                v = lw.get("lag_s") if isinstance(lw, dict) else None
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(float(v))
        if len(vals) >= 2 and max(vals) - min(vals) > _DISAGREE_S:
            return True
    return False




def _default_decode(path, from_ts, to_ts, **kw):
    """The frame decoder, looked up at call time (as the v1 bodies did with a local import)."""
    from ..adapters.decode import decode_window

    return decode_window(path, from_ts, to_ts, **kw)


# ---------- 第一段:纯数值检查(最便宜)· per-episode bodies ----------

def make_timestamp_check(cfg: dict) -> Callable:
    """``ts_check(timestamps, fps) -> result struct``."""
    p_ts = cfg["checks"]["timestamp_check"].get("params", {})

    def ts_check(timestamps, fps):
        return result_to_struct(timestamp_check(np.asarray(timestamps), fps, **p_ts))

    return ts_check


def make_kinematic_check(cfg: dict, registry) -> Callable:
    """``kin_check(action, embodiment_id, fps, action_space, control_mode, proprio_state,
    proprio_space) -> result struct``."""
    p_kin = cfg["checks"]["kinematic_limits"].get("params", {})

    def kin_check(action, embodiment_id, fps, action_space, control_mode,
                  proprio_state, proprio_space):
        prof = registry.get(embodiment_id)
        from ..core.contract import CheckResult as _CR

        # B0(2026-09-02 预检):动作含义没把握 → 不硬猜,弃权并说清
        if str(action_space) == "unknown":
            return result_to_struct(_CR(
                name="kinematic_limits", passed=None,
                detail={"reason": "动作数据的含义无法判断(既不像关节角也不像末端指令),"
                                  "未做极限对照;请提供动作定义或登记数据集格式"}))
        # B2:关节增量指令无绝对角可对照极限 → 诚实弃权(而非静默空转)
        if str(control_mode) == "delta" and str(action_space) != "ee":
            return result_to_struct(_CR(
                name="kinematic_limits", passed=None,
                detail={"reason": "action 是关节增量指令(数值指纹判定),无绝对角可对照极限"}))
        # B1:单位错配守卫——数据与极限量级差太远=单位不符,硬比会帧帧超限全灭。
        # 统计量必须鲁棒(p95 非 max,2026-07-14 注入实测):单位错配是整条轨迹
        # 的属性(所有值同倍数缩放),单帧毛刺(9999)用 max 会被误判成"错配弃权",
        # 反而放走了本该被极限检查硬杀的坏数据
        if str(action_space) != "ee" and prof.joint_limits:
            a_scale = float(np.percentile(np.abs(np.asarray(action)), 95))
            lmax = max(abs(v) for pair in prof.joint_limits for v in pair)
            if a_scale > 1e-9 and lmax > 1e-9 and (a_scale / lmax > 3.0
                                                   or a_scale / lmax < 0.02):
                return result_to_struct(_CR(
                    name="kinematic_limits", passed=None,
                    detail={"reason": f"单位疑似错配:数据典型幅值(p95) {a_scale:.3g} vs "
                                      f"极限幅值 {lmax:.3g}(profile 单位 {prof.unit}),拒绝硬比",
                            "unit_mismatch": True}))
        # 分派(P2 既定 + 2026-07-14 EE 规格):EE 空间数据绝不拿关节极限硬卡
        # (DROID 7 维 EE 恰与 Franka dof 7 同维,不挡会静默错判)。
        # 有 EE 规格(ee_reach_m 等)→ 用 proprio 的 EE 绝对位姿查可达性+笛卡尔
        # 速度(不做 IK,弱于关节检查,增量非平替);无 EE 规格才弃权。
        if str(action_space) == "ee" and not prof.action_space.startswith("ee"):
            if (prof.has_ee_limits and proprio_state is not None
                    and str(proprio_space) == "ee"):
                from ..core.checks.kinematics import ee_limits
                return result_to_struct(ee_limits(
                    np.asarray(proprio_state)[:, :6], prof, fps))
            from ..core.contract import CheckResult

            return result_to_struct(CheckResult(
                name="kinematic_limits", passed=None,
                detail={"reason": f"action 是 EE 空间指令,{prof.embodiment_id} 极限是关节空间,"
                                  "且 profile 无 EE 规格/无 EE 位姿读数——不可判"}))
        # 2026-10-14(11-3 so101):极限是机器人的属性。有同空间的关节读数就查读数;leader 臂的指令
        # 想多快多快(人手一帧捏到底 406/s,follower 读数峰值 181/s),指令超速只作提示。没有读数时
        # 位置极限仍按指令硬判,速度仍只提示(没有读数就说不准机器人超没超)
        a = np.asarray(action)
        gd = tuple(int(x) for x in (prof.gripper_dims or ()) if a.ndim == 2 and int(x) < a.shape[1])
        st = None
        if proprio_state is not None and str(proprio_space) == str(action_space) and str(action_space) != "ee":
            st = np.asarray(proprio_state)
            if st.ndim != 2 or st.shape[1] != prof.dof or len(st) != len(a):
                st = None
        kw = dict(p_kin, gripper_dims=gd)
        if st is not None:
            res = kinematic_limits(st, prof, fps, **kw)
            basis = "state"
        else:
            res = kinematic_limits(a, prof, fps, **dict(kw, check_velocity=False))
            basis = "command"
        cmd = kinematic_limits(a, prof, fps, **dict(kw, check_velocity=True))
        cmd_vel = ([v for v in (cmd.detail.get("violations") or []) if v.get("type") == "velocity_limit"]
                   if cmd.passed is not None else [])
        if res.passed is None:
            return result_to_struct(res)
        # an approximate velocity limit (an observed envelope, so101 / aloha) informs, it does not fail the gate
        blocking = [v for v in (res.detail.get("violations") or [])
                    if not (v.get("type") == "velocity_limit" and prof.quality == "approximate")]
        detail = dict(res.detail, basis=basis, n_command_velocity=len(cmd_vel),
                      command_velocity_violations=cmd_vel[:20])
        return result_to_struct(_CR(name="kinematic_limits", passed=not blocking, score=res.score, detail=detail))

    return kin_check


def make_motion_check(cfg: dict, registry) -> Callable:
    """``motion_check(action, proprio_state, fps, action_space, control_mode, proprio_space,
    embodiment_id, stuck_strategy, semantics_extras) -> result struct``."""
    p_motion = cfg["checks"]["motion_quality"].get("params", {})

    def motion_check(action, proprio_state, fps, action_space, control_mode,
                     proprio_space, embodiment_id, stuck_strategy, semantics_extras):
        pr = np.asarray(proprio_state) if proprio_state is not None else None
        a = np.asarray(action)
        _layout = None
        try:
            _layout = (json.loads(str(semantics_extras) or "{}") or {}).get("layout")
        except Exception:  # noqa: BLE001
            _layout = None
        # F8 (2026-10-14):预检判不出整列空间,但列名把位姿块写清了(HIW-500)→ 按布局算,不弃权
        _names_layout = isinstance(_layout, dict) and _layout.get("names_only") and _layout.get("informative")
        _unknown = str(action_space) in ("", "unknown", "None") or str(control_mode) in ("", "unknown", "None")
        if _unknown and not _names_layout:
            # 预检没把握这些列是什么(2026-10-14 HIW-500:unknown 照算,平滑度 0 分):不猜,弃权,
            # 每个子项带原因进发现(ACT-6 数据集级的「判断不了动作的语义」另有一条)
            why = "动作列含义不明（预检分不清是关节角还是末端位姿），运动质量不算"
            return result_to_struct(CheckResult(
                name="motion_quality", passed=None,
                detail={"reason": why, "smoothness_reason": why, "spike_reason": why,
                        "gripper_reason": why, "saturation_reason": why, "stuck_reason": why,
                        "fluency_reason": why, "semantics_undetermined": True}))
        kw = dict(p_motion)
        # 语义层识别出的 EE 布局(2026-09-16,只在无档案的数据集上出现):按块给运动质量,
        # 不再一律假定"第 3-5 列是 rpy";有档案的数据集走下面的老规则,数值一字不变
        if "angle_dims" not in kw and a.ndim == 2 and isinstance(_layout, dict) \
                and (str(action_space) == "ee" or _names_layout):
            cmode = str(control_mode)
            mode = "delta" if cmode in ("delta", "velocity") else "absolute"
            kw.update(angle_dims=tuple(int(x) for x in _layout.get("angle_dims") or ()),
                      angle_mode=mode,
                      euler_triplet=bool(_layout.get("euler_triplet")),
                      rotation_blocks=[tuple(b) for b in _layout.get("rotation_blocks") or []],
                      translation_dims=tuple(int(x) for x in _layout.get("translation_dims") or ()),
                      velocity_dims=tuple(int(x) for x in _layout.get("velocity_dims") or ()))
            if _layout.get("gripper_dims"):
                kw.setdefault("gripper_dims", tuple(
                    int(x) for x in _layout["gripper_dims"] if int(x) < a.shape[1]) or None)
        elif "angle_dims" not in kw and a.ndim == 2:
            aspace, cmode = str(action_space), str(control_mode)
            mode = "delta" if cmode in ("delta", "velocity") else "absolute"
            if aspace == "ee" and a.shape[1] >= 6:
                kw.update(angle_dims=(3, 4, 5), angle_mode=mode,
                          euler_triplet=True)   # rpy 三维,弧度
            elif aspace != "ee":
                period = 6.283185307179586 if float(np.abs(a).max()) <= 7.0 else 360.0
                kw.update(angle_dims=tuple(range(a.shape[1])),
                          angle_mode=mode, angle_period=period)
        try:
            _prof_m = registry.get(str(embodiment_id))
            gdims = tuple(d for d in _prof_m.gripper_dims if d < a.shape[1])
            kw.setdefault("gripper_dims", gdims or None)
            if _prof_m.joint_limits and str(action_space) != "ee":
                kw.setdefault("joint_spans", tuple(
                    float(hi - lo) for lo, hi in _prof_m.joint_limits))
        except Exception:  # noqa: BLE001  未知 embodiment → 无夹爪约定,不猜
            pass
        if isinstance(_layout, dict) and _layout.get("nonjoint_dims"):
            # 名为关节、实为开合量的列(2026-10-15 G1 右臂六列):不进平滑度与尖刺,关节空间也要
            kw.setdefault("nonjoint_dims", tuple(int(x) for x in _layout["nonjoint_dims"] if int(x) < a.shape[1]))
        kw.setdefault("control_mode", str(control_mode))
        kw.setdefault("stuck_strategy", str(stuck_strategy))   # 数据集语义层提供
        try:   # 数据集 profile extras(如 droid 的经验速度系数)→ 期望位移判据
            _ex = json.loads(str(semantics_extras) or "{}")
            vs = _ex.get("velocity_scale_translation_empirical")
            if vs:
                kw.setdefault("velocity_scale", tuple(float(x) for x in vs))
            vc = _ex.get("velocity_calibration")     # 速度域标定 → 执行器饱和可算
            if isinstance(vc, dict) and vc.get("gain"):
                kw.setdefault("velocity_calib", vc)
        except Exception:  # noqa: BLE001  extras 缺失/损坏 → 走无系数回退
            pass
        kw.setdefault("same_space",                     # 两边空间都认得且相同才算同空间(unknown == unknown 不算)
                      pr is None or (str(action_space) == str(proprio_space) and str(action_space) in ("ee", "joint")))
        return result_to_struct(motion_quality(a, pr, fps, **kw))

    return motion_check


# ---------- 第二段:抽帧检查 · per-episode body ----------

def frame_modes(cfg: dict) -> tuple[bool, bool, str]:
    """(do_visual, do_sync, sync_plots_mode) of the frame stage."""
    return (enabled(cfg, "visual_quality"), enabled(cfg, "video_action_sync"),
            str(cfg.get("pipeline", {}).get("sync_plots", "flagged")))


def sync_speed_columns(n_cols: int, proprio_space, layout: dict | None, profile_gripper_dims=()) -> list[int]:
    """The state columns the sync check turns into its speed proxy (one place, shared with the benchmark).

    ee: the layout's translation columns (both arms' xyz when there are two; RH20T's numeric fingerprint and
    FastUMI's names both give them); without a layout the first three columns, but only when the column count
    looks like a pose (3..8) - a 26-joint humanoid misread as ee would otherwise have its left shoulder taken for
    xyz (2026-10-14 G1 ep26). joint: every column but the gripper (profile or layout) and the fingers of a
    dexterous hand (layout hand_dims) - their flutter is not arm motion.

    This is the union of ``sync_speed_groups``; the measurement reads the groups one by one (see there)."""
    cols: list[int] = []
    for g in sync_speed_groups(n_cols, proprio_space, layout, profile_gripper_dims).values():
        cols.extend(c for c in g if c not in cols)
    return sorted(cols)


def sync_speed_groups(n_cols: int, proprio_space, layout: dict | None, profile_gripper_dims=()) -> dict:
    """The same columns split into limbs: ``{"left": [...], "right": [...]}`` when the layout's names carry a side,
    else one group (``"arm"`` for joints, ``"ee"`` for a pose). A time offset between picture and record is a
    property of the stream, so every moving limb must show the same one; the measurement reads each limb on its
    own and only trusts a camera when the limbs agree (2026-10-14 G1 ep7/42/108: six columns named as the right
    arm's joints are a bounded, finger-like signal - mixed into one curve with the left arm they bent the
    whole-episode correlation to -1.8 s while every 2 s window read 0)."""
    lay = layout if isinstance(layout, dict) else {}
    if str(proprio_space) == "ee":
        tr = [int(c) for c in (lay.get("translation_dims") or ()) if 0 <= int(c) < n_cols]
        if tr:
            base, name = tr, "ee"
        elif 3 <= n_cols <= 8:
            return {"ee": [0, 1, 2]}
        else:
            return {"ee": list(range(n_cols))}
    else:
        drop = {int(c) for c in (profile_gripper_dims or ())}
        drop |= {int(c) for c in (lay.get("gripper_dims") or ())}
        drop |= {int(c) for c in (lay.get("hand_dims") or ())}
        drop |= {int(c) for c in (lay.get("nonjoint_dims") or ())}   # 名为关节、实为开合量的列(2026-10-15 G1)
        base, name = [j for j in range(n_cols) if j not in drop] or list(range(n_cols)), "arm"
    groups: dict = {}
    nonjoint = {int(c) for c in (lay.get("nonjoint_dims") or ())}
    for side, cols in (lay.get("limbs") or []):
        got = [int(c) for c in cols if int(c) in base]
        if got and len([c for c in cols if int(c) in nonjoint]) >= len(got):
            # A limb whose columns are mostly not joint readings (G1's right arm: six of seven) has no usable
            # record of its motion; the one joint left is not the arm. Reading it as a limb put a shoulder-pitch
            # curve against the picture (2026-10-15 bench: ep18 flagged inconsistent on it), so the limb is left
            # out entirely - the dataset-level columns_not_joints finding says why.
            base = [c for c in base if c not in got]
            continue
        if got:
            groups[str(side) or name] = got
    placed = {c for g in groups.values() for c in g}
    rest = [c for c in base if c not in placed]
    if rest:
        if groups and name not in groups:
            groups[name] = rest
        elif not groups:
            groups[name] = rest
        else:
            groups[name].extend(rest)
    return groups


def sync_speed_proxies(state, fps: float, proprio_space, layout: dict | None, profile_gripper_dims=()) -> dict:
    """``{limb: joint_speed}`` - the per-limb speed proxies the sync measurement reads (shared with the benchmark)."""
    from ..core.checks.video_action_sync import joint_speed as _js

    p = np.asarray(state, dtype=np.float64)
    return {name: _js(p[:, cols], fps)
            for name, cols in sync_speed_groups(p.shape[1], proprio_space, layout, profile_gripper_dims).items()}


def make_frame_checks(cfg: dict, registry, *, decode: Callable | None = None) -> Callable:
    """``frame_checks(video, proprio_state, timestamps, fps, proprio_space, embodiment_id)
    -> {"visual": struct, "sync": struct, "curves": str}``: one decode per camera serves
    both frame modules (D18). ``decode`` defaults to ``adapters.decode.decode_window``."""
    pcfg = cfg.get("pipeline", {})
    interval = pcfg.get("frame_sample_interval_s", 0.5)
    max_side = pcfg.get("frame_max_side", 448)
    pv = cfg["checks"].get("visual_quality", {}).get("params", {})
    _ps_all = dict(cfg["checks"].get("video_action_sync", {}).get("params", {}))
    # 同步检查的参数分两层:测量层(逐相机 global_lag)与判定层(跨相机 sync_verdict)。
    # 配置里是平铺的一段 params,这里按名字分派——客户配 kill_lag_min_s 不该炸在
    # global_lag 的签名上,反之亦然。lag_tol_s 两层都要(容差是同一个概念)。
    _VERDICT_KEYS = {"spread_tol_s", "kill_lag_min_s", "neg_kill_lag_min_s",
                     "min_kill_cameras"}
    # 分窗估计的两个测量层参数(default.yaml 的 window_s / window_min_frames)打包给 global_lag(window=...)
    _WINDOW_KEYS = {"window_s": "win_s", "window_min_frames": "min_frames",
                    "window_min_trusted": "min_trusted", "window_max_iqr_s": "max_iqr_s"}
    ps = {k: v for k, v in _ps_all.items() if k not in _VERDICT_KEYS and k not in _WINDOW_KEYS}
    _win = {_WINDOW_KEYS[k]: v for k, v in _ps_all.items() if k in _WINDOW_KEYS}
    if _win:
        ps["window"] = _win
    pver = {k: v for k, v in _ps_all.items() if k in _VERDICT_KEYS}
    if "lag_tol_s" in _ps_all:
        pver["lag_tol_s"] = _ps_all["lag_tol_s"]
    do_visual, do_sync, sync_plots_mode = frame_modes(cfg)
    decode_window = decode or _default_decode

    def frame_checks(video, proprio_state, timestamps, fps,
                     proprio_space, embodiment_id, semantics_extras=None):
        # ── 逐相机一次解码,视觉质量与同步检查共用(2026-08-07 改造)──────────
        # 改造前同步只算 sorted 的第一路,而视觉质量本来就已经逐相机解码了——
        # 也就是说"多相机同步"缺的从来不是解码,只是没在同一批帧上多算一次光流。
        # 现在把两件事并进同一个逐相机循环:**解码成本零增长**,新增的只有
        # 每路一次 Farneback 光流(全管线最贵的 CPU 计算,故仍是主要成本项:
        # N 路相机 ≈ N 倍光流)。用户拍板:一路读数代表整条 episode 的风险
        # (droid ep4:三路 +0.60/−0.07/0.00,只看第一路差点误杀)远大于这份 CPU。
        #
        # 逐相机**串行处理并即时释放帧**:同一时刻内存里只有一路的帧,峰值内存
        # 与改造前持平(改造前 cam0 的帧全程驻留,反而更差)。
        cams = sorted(video.keys())
        cam0 = cams[0] if cams else None      # 一路视频都没有 → 循环空转,如实弃权
        stride = max(1, int(round(interval * fps)))
        out = {"visual": result_to_struct_none(), "sync": result_to_struct_none(),
               "curves": ""}

        # 速度代理只与本体有关,与相机无关 → 循环外算一次
        speed = None
        if do_sync and proprio_state is not None:
            # 速度代理的列选择(2026-07-15 M5a 复诊):全列范数会被两类假信号
            # 砸烂互相关——①EE 欧拉角 ±π 回绕/万向节假跳变(droid corr 0.12~0.14,
            # M4b 同款病);②夹爪列 0-100 大摆(so101 corr 0.19~0.28,视觉上几乎
            # 不可见)。EE → 只用平移三维;关节 → 剔除夹爪列。
            p_sync = np.asarray(proprio_state)
            _lay = None
            try:
                _lay = (json.loads(str(semantics_extras) or "{}") or {}).get("layout")
            except Exception:  # noqa: BLE001
                _lay = None
            try:
                _gd = tuple(registry.get(str(embodiment_id)).gripper_dims)
            except Exception:  # noqa: BLE001  未知 embodiment → 没有夹爪约定
                _gd = ()
            # 逐肢体各一根速度曲线(测量按组求一致);曲线图仍画它们的并集
            speeds = sync_speed_proxies(p_sync, fps, proprio_space, _lay, _gd)
            p_sync = p_sync[:, sync_speed_columns(p_sync.shape[1], proprio_space, _lay, _gd)]
            speed = joint_speed(p_sync, fps)

        # 短名(去 observation.images. 前缀):UI/报告/交付里逐相机都用它;
        # 万一两路短名相同(不同前缀撞车),退回全名保证键唯一
        _short_of = {}
        for c in cams:
            s = c.split(".")[-1]
            _short_of[c] = c if s in _short_of.values() else s

        per_cam = {}          # 视觉质量:相机全名 → CheckResult
        padded = []           # 占位黑帧路(只登记不打分)
        # 相机体检顺带做掉(2026-08-14):判"这一路是不是占位/黑帧"要的只是
        # 采样帧的亮度/方差,而这一遍本来就逐相机解了帧、也已经在算灰度方差。
        # 此前它是报告阶段**再逐条解一遍帧**的独立步骤(串行、零输出,20 条 ×3 路
        # 就明显卡顿,200 条像卡死)—— 为省几乎免费的统计付出整整一遍解码。
        live_cams, dead_cams = [], []
        sync_cams = {}        # 同步读数:相机短名 → per_camera 条目
        sync_curves = {}      # 同步曲线(画图用):相机短名 → {t/flow/speed/...}
        for c in cams:
            v = video[c]
            # ⚠️ 同步检查必须全帧率解码:lag 分辨率=帧间隔,抽稀到 0.5s 会粗于容忍度
            # (0.25s)→干净数据被量化误差误杀(2026-07-02 e2e 实测)。一次解码两用:
            # sync 用全帧,visual 从同批帧里按 interval 抽稀。
            try:
                frames, fts = decode_window(v["path"], v["from_ts"], v["to_ts"],
                                            max_side=max_side)
            except Exception:  # noqa: BLE001  解码失败=少一路,不中断整条
                dead_cams.append(c)           # 拿不到画面 = 不能声称这一路在拍
                if c != cam0:
                    padded.append(c)
                continue
            if not frames:
                dead_cams.append(c)
                if c != cam0:
                    padded.append(c)
                continue
            samp = frames[::stride]
            live = is_live_channel(samp)
            (live_cams if live else dead_cams).append(c)
            if not live and c != cam0:
                padded.append(c)              # 占位黑帧:两项检查都跳过
                del frames, samp
                continue
            # 多相机:全部活跃路都检;占位黑帧路(多机构采集集凑 schema 用)
            # 只登记不打分,绝不因占位杀数据。⚠️ 首路(cam0)即使判成占位也照常
            # 打分:全黑的主相机就该让视觉质量把这条判坏,不能靠"跳过"让它蒙混
            # 过关 —— 体检结论仍如实记在 camera_liveness 里。
            if do_visual:
                per_cam[c] = visual_quality(samp, **pv)
            if do_sync and speed is not None and len(frames) >= 8:
                flow = optical_flow_energy(frames)
                _res = grouped_lag(flow, fts[1:], speeds,
                                   np.asarray(timestamps)[1:], **ps)
                sync_cams[_short_of[c]] = camera_reading(_res)
                # 曲线搭质检顺风车暂存(光流是全管线最贵计算,算完就扔=白扔);
                # 降采样到 ≤600 点(画图够用,控内存),导出层渲染
                _ft2 = np.asarray(fts[1:], dtype=float)
                # 画的是出读数的那组肢体的速度曲线(2026-10-14):曲线、最高点与读数必须同源,
                # 画并集速度会让图上的最高点与读数对不上(G1 ep26)
                _grp = _res.detail.get("group")
                _sp_src = speeds.get(_grp, speed) if isinstance(_grp, str) else speed
                _sp2 = np.interp(_ft2, np.asarray(timestamps)[1:], _sp_src)
                _st = max(1, len(flow) // 600)
                sync_curves[_short_of[c]] = {
                    "t": np.round(_ft2[::_st], 3).tolist(),
                    "flow": np.round(np.asarray(flow)[::_st], 5).tolist(),
                    "speed": np.round(_sp2[::_st], 6).tolist(),
                    **{k: _res.detail.get(k) for k in
                       ("lag_s", "corr_peak", "code", "n_trimmed_static", "group", "basis", "whole_lag_s")}}
            del frames, samp

        if do_visual and per_cam:
            from ..core.contract import CheckResult as _VCR

            # 多相机聚合=加权平均(2026-07-08 用户定):恒定糊的副相机是本体特征而非
            # 缺陷(部署时也是同一颗镜头),不该一票拖垮;权重默认每路 1.0,可在
            # checks.visual_quality.camera_weights 按相机名(全名或末段短名)覆盖。
            cam_w = cfg["checks"].get("visual_quality", {}).get("camera_weights", {}) or {}

            def _w(name):
                short = name.split(".")[-1]
                return float(cam_w.get(name, cam_w.get(short, 1.0)))

            weights = {k: _w(k) for k in per_cam}
            wtot = sum(weights.values())
            score = (sum(weights[k] * r.score for k, r in per_cam.items()) / wtot
                     if wtot > 0 else 0.0)
            worst = min(per_cam, key=lambda k: per_cam[k].score)
            vdetail = dict(per_cam[worst].detail)
            vdetail.update({
                "per_camera": {k: round(r.score, 4) for k, r in per_cam.items()},
                "per_camera_detail": {k: {
                    "score": round(r.score, 4),
                    "sharpness": r.detail.get("sharpness"),
                    "exposure": r.detail.get("exposure"),
                    "integrity": r.detail.get("integrity"),
                    "blur_var_median": r.detail.get("blur_var_median"),
                    "clip_frac_median": r.detail.get("clip_frac_median"),
                    "gray_std_median": r.detail.get("gray_std_median"),
                    "frozen_ratio": r.detail.get("frozen_ratio"),
                } for k, r in per_cam.items()},
                "camera_weights": weights,
                "worst_camera": worst,
                "padded_channels": padded,
                # 相机体检结论(报告的 camera_audit 直接读这里,不重算)。
                # 与 padded_channels 的区别只在首路:首路判成占位也照常打分,
                # 所以它会出现在 dead_or_padded 里、却不在 padded_channels 里。
                "camera_liveness": {"live": live_cams,
                                    "dead_or_padded": dead_cams},
                "params": {"blur_ref_var": pv.get("blur_ref_var", 100.0),
                           "frame_max_side": max_side}})
            out["visual"] = result_to_struct(_VCR(
                name="visual_quality", passed=None,
                score=round(score, 4), detail=vdetail))
        if do_sync and sync_cams:
            # 判定层:逐相机读数 → episode 结论(判废只有"所有可信相机一致指向
            # 同一个 Δ≠0"这一种情形;单相机永不判废;测不准/矛盾一律 passed=True
            # + 标注,绝不返回 None——弃权会误进人工裁决队列)
            _sync_res = sync_check_result(sync_cams, len(sync_cams), **pver)
            out["sync"] = result_to_struct(_sync_res)
            _det = _sync_res.detail
            if sync_plot_worthy(sync_plots_mode, _det):
                out["curves"] = json.dumps({
                    "cameras": sync_curves,
                    "verdict": _det["verdict"],
                    "consensus_lag_s": _det["consensus_lag_s"],
                    "n_cameras": _det["n_cameras"], "n_trusted": _det["n_trusted"],
                    "flagged_cameras": _det["flagged_cameras"],
                    "per_camera": _det["per_camera"],
                    "lag_tol_s": float(pver.get("lag_tol_s", 0.25))})
        elif do_sync:
            # 一路都没测成(无 proprio / 全部解码失败 / 帧太少):如实标注,不判废
            from ..core.contract import CheckResult as _SCR

            out["sync"] = result_to_struct(_SCR(
                name="video_action_sync", passed=True,
                detail={"verdict": "undecidable", "per_camera": {},
                        "flagged_cameras": [], "consensus_lag_s": None,
                        "n_cameras": 0, "n_trusted": 0,
                        "reason": ("无可用相机/无 proprio 读数,同步未测量"
                                   "(不影响判决)")}))
        return out

    return frame_checks


# ---------- 第三段:VLM 任务成败 · per-episode body ----------

@dataclass(frozen=True)
class TaskDeps:
    """What one task_success judgement calls out to: the video assessor (design doc 13), wrapped per episode so
    that a failed call is recorded as an execution incident (D33) before it re-raises."""

    vlm_completion: Callable








def internal_error_struct(e: BaseException) -> dict:
    """单条轨迹判决的兜底:漏网异常一律转成 internal_error 不可判结构。

    纪律:一条轨迹的死活与其余轨迹完全隔离 —— daft UDF 一旦抛错,
    df.collect() 整批报废,一条轨迹就能拖死全部。这里保证任务一定跑完:
    passed=None → episode_verdict 记 undecidable(不冤杀也不放行,转人工);
    detail.rules=["internal_error"] 是离线扫错误码的机器可读标识;
    detail.internal_error=True 供展示层把「系统内部错误」与「证据不足
    的诚实弃权」措辞分开(report/manifest 都读它)。
    """
    import sys
    import traceback
    from ..core.contract import CheckResult
    print(f"[task_check] internal_error: {type(e).__name__}: {e}\n"
          f"{traceback.format_exc()}", file=sys.stderr, flush=True)
    return result_to_struct(CheckResult(
        name="task_success", passed=None,
        detail={"reason": f"internal_error: {type(e).__name__}: {e}",
                "rules": ["internal_error"],
                "internal_error": True}))   # 展示层按它区分「系统坏了」与「证据不足」


def task_check_episode(cfg: dict, deps: TaskDeps, video, task_desc, task_src, semantics_extras="",
                       claim=None) -> dict:
    """One episode's task-success judgement from its continuous multi-camera videos -> result struct (may raise).

    One request answers for the whole episode and every camera (D71); there is no label guard (D73)."""
    from .video_task import judge_video_episode

    return result_to_struct(judge_video_episode(
        cfg, video, task_desc, deps.vlm_completion,
        task_src=str(task_src), hints=str(semantics_extras or ""), claim=claim))


def result_to_struct_none() -> dict:
    return {"passed": None, "score": None, "detail": "{}"}
