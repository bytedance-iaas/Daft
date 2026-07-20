"""漏斗装配:DataFrame 链即编排(文档中的 M3,Daft 下大幅简化)。

顺序按成本:纯数值(时间戳/运动学/运动质量)→ filter(硬门) → 抽帧(视觉/同步)→
filter(硬门) → VLM 任务成败(垫底只跑幸存者,省一个数量级)→ verdict。
本模块是编排壳,允许 import daft(core/ 才是无 daft 区);检查本体全在 core/ 纯函数。
"""
from __future__ import annotations

import json
from typing import Callable

import numpy as np

from ..adapters.daft_adapter import result_to_struct
from ..core.checks.kinematics import kinematic_limits
from ..core.checks.motion_quality import motion_quality
from ..core.checks.task_success import task_success
from ..core.checks.video_action_sync import (
    global_lag,
    joint_speed,
    optical_flow_energy,
    timestamp_check,
)
from ..core.checks.visual_quality import visual_quality
from .config import enabled
from .verdict import episode_verdict


def _result_dtype():
    from daft import DataType

    return DataType.struct({"passed": DataType.bool(), "score": DataType.float64(),
                            "detail": DataType.string()})


def run_funnel(
    df,
    cfg: dict,
    registry,                                     # EmbodimentRegistry(M2)
    vlm_completion: Callable | None = None,       # M4c 依赖注入(生产=vLLM 端点;None=跳过)
) -> tuple["object", dict]:
    """输入 M1 DataFrame → 输出 (带 check_*/verdict 列的 DataFrame, 漏斗统计)。"""
    import daft
    from daft import col, lit

    stats = {"input": df.count_rows()}
    pcfg = cfg.get("pipeline", {})
    interval = pcfg.get("frame_sample_interval_s", 0.5)
    max_side = pcfg.get("frame_max_side", 448)
    hard_cols: list[str] = []

    # ---------- 第一段:纯数值检查(最便宜) ----------
    if enabled(cfg, "timestamp_check"):
        p_ts = cfg["checks"]["timestamp_check"].get("params", {})

        @daft.func(return_dtype=_result_dtype())
        def ts_check(timestamps, fps):
            return result_to_struct(timestamp_check(np.asarray(timestamps), fps, **p_ts))

        df = df.with_column("check_timestamp_check", ts_check(col("timestamps"), col("fps")))
        hard_cols.append("check_timestamp_check")

    if enabled(cfg, "kinematic_limits"):
        p_kin = cfg["checks"]["kinematic_limits"].get("params", {})

        @daft.func(return_dtype=_result_dtype())
        def kin_check(action, embodiment_id, fps, action_space, control_mode,
                      proprio_state, proprio_space):
            prof = registry.get(embodiment_id)
            from ..core.contract import CheckResult as _CR

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
            return result_to_struct(kinematic_limits(np.asarray(action), prof, fps, **p_kin))

        aspace = col("action_space") if "action_space" in df.column_names else lit("joint")
        df = df.with_column("check_kinematic_limits",
                            kin_check(col("action"), col("embodiment_id"), col("fps"), aspace,
                                      col("control_mode") if "control_mode" in df.column_names
                                      else lit("unknown"),
                                      col("proprio_state"),
                                      col("proprio_space") if "proprio_space" in df.column_names
                                      else lit("joint")))
        hard_cols.append("check_kinematic_limits")

    if enabled(cfg, "motion_quality"):
        p_motion = cfg["checks"]["motion_quality"].get("params", {})

        @daft.func(return_dtype=_result_dtype())
        def motion_check(action, proprio_state, fps, action_space, control_mode,
                         proprio_space, embodiment_id, stuck_strategy, semantics_extras):
            pr = np.asarray(proprio_state) if proprio_state is not None else None
            a = np.asarray(action)
            kw = dict(p_motion)
            if "angle_dims" not in kw and a.ndim == 2:
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
            kw.setdefault("control_mode", str(control_mode))
            kw.setdefault("stuck_strategy", str(stuck_strategy))   # 数据集语义层提供
            try:   # 数据集 profile extras(如 droid 的经验速度系数)→ 期望位移判据
                _ex = json.loads(str(semantics_extras) or "{}")
                vs = _ex.get("velocity_scale_translation_empirical")
                if vs:
                    kw.setdefault("velocity_scale", tuple(float(x) for x in vs))
            except Exception:  # noqa: BLE001  extras 缺失/损坏 → 走无系数回退
                pass
            kw.setdefault("same_space",
                          pr is None or str(action_space) == str(proprio_space))
            return result_to_struct(motion_quality(a, pr, fps, **kw))

        df = df.with_column("check_motion_quality",
                            motion_check(col("action"), col("proprio_state"), col("fps"),
                                         col("action_space") if "action_space" in df.column_names
                                         else lit("joint"),
                                         col("control_mode") if "control_mode" in df.column_names
                                         else lit("unknown"),
                                         col("proprio_space") if "proprio_space" in df.column_names
                                         else lit("joint"),
                                         col("embodiment_id"),
                                         col("stuck_strategy") if "stuck_strategy" in df.column_names
                                         else lit("auto"),
                                         col("semantics_extras") if "semantics_extras"
                                         in df.column_names else lit("{}")))

    # ⚡ 物化一次:daft 惰性,每个 count_rows/to_pydict 都会从头重算整条检查链。
    # 数值段建完后 collect(),后续统计/过滤都基于物化结果 → 检查只跑一遍(2026-07-10 修
    # "懒重算8次"效率bug:此前 timestamp 等被重复调用 8×)。
    df = df.collect()

    # 硬门 filter ①(纯数值段短路,后面贵的不再算)
    stats["hard_killed"] = []

    def _capture_killed(df_in, gate_cols):
        """硬门过滤前捕获被杀行:episode 级判决记录不能只剩统计数(demo/审计要逐条)。
        df_in 已物化 → filter/to_pydict 不再重跑检查。"""
        for c in gate_cols:
            d = df_in.filter(~(col(c)["passed"].is_null() | col(c)["passed"])) \
                     .select("episode_id", c).to_pydict()
            for eid, res in zip(d.get("episode_id", []), d.get(c, [])):
                stats["hard_killed"].append({
                    "episode_id": eid, "check": c.replace("check_", ""),
                    "detail": (res or {}).get("detail", "")})
            df_in = df_in.filter(col(c)["passed"].is_null() | col(c)["passed"])
        return df_in

    df = _capture_killed(df, hard_cols).collect()
    stats["after_numeric_gates"] = df.count_rows()

    # ---------- 第二段:抽帧检查 ----------
    frame_hard: list[str] = []
    if enabled(cfg, "visual_quality") or enabled(cfg, "video_action_sync"):
        pv = cfg["checks"].get("visual_quality", {}).get("params", {})
        ps = cfg["checks"].get("video_action_sync", {}).get("params", {})
        do_visual = enabled(cfg, "visual_quality")
        do_sync = enabled(cfg, "video_action_sync")
        # 同步曲线暂存策略(2026-07-15 用户定,证据附件第一块):flagged=只存
        # 非"过"条目(人工会看的那批,内存有界);all=全存(小数据集/演示);off=不存
        sync_plots_mode = str(pcfg.get("sync_plots", "flagged"))

        @daft.func(return_dtype=daft.DataType.struct({
            "visual": _result_dtype(), "sync": _result_dtype(),
            "curves": daft.DataType.string()}))
        def frame_checks(video, proprio_state, timestamps, fps,
                         proprio_space, embodiment_id):
            from ..adapters.decode import decode_window

            # 取第一个(外部)相机;多相机策略 P5 再扩(M2 cameras 标注 wrist/external)
            cam = sorted(video.keys())[0]
            v = video[cam]
            # ⚠️ 同步检查必须全帧率解码:lag 分辨率=帧间隔,抽稀到 0.5s 会粗于容忍度
            # (0.25s)→干净数据被量化误差误杀(2026-07-02 e2e 实测)。一次解码两用:
            # sync 用全帧,visual 从同批帧里按 interval 抽稀。
            frames, fts = decode_window(v["path"], v["from_ts"], v["to_ts"],
                                        max_side=max_side)
            out = {"visual": result_to_struct_none(), "sync": result_to_struct_none(),
                   "curves": ""}
            if do_visual and frames:
                stride = max(1, int(round(interval * fps)))
                # 多相机:全部活跃路都检,总分=最差路(平均会让好相机掩护坏相机);
                # 占位黑帧路(多机构采集集凑 schema 用)只登记不打分,绝不因占位杀数据
                per_cam = {cam: visual_quality(frames[::stride], **pv)}
                padded = []
                for c2 in sorted(video.keys()):
                    if c2 == cam:
                        continue
                    v2 = video[c2]
                    try:
                        f2, _ = decode_window(v2["path"], v2["from_ts"], v2["to_ts"],
                                              max_side=max_side)
                    except Exception:  # noqa: BLE001
                        padded.append(c2)
                        continue
                    if not f2:
                        padded.append(c2)
                        continue
                    head = np.asarray(f2[0], dtype=np.float32)
                    if head.mean() < 8.0 and head.std() < 2.0:
                        padded.append(c2)
                        continue
                    per_cam[c2] = visual_quality(f2[::stride], **pv)
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
                    "params": {"blur_ref_var": pv.get("blur_ref_var", 100.0),
                               "frame_max_side": max_side}})
                out["visual"] = result_to_struct(_VCR(
                    name="visual_quality", passed=None,
                    score=round(score, 4), detail=vdetail))
            if do_sync and proprio_state is not None and len(frames) >= 8:
                flow = optical_flow_energy(frames)
                # 速度代理的列选择(2026-07-15 M5a 复诊):全列范数会被两类假信号
                # 砸烂互相关——①EE 欧拉角 ±π 回绕/万向节假跳变(droid corr 0.12~0.14,
                # M4b 同款病);②夹爪列 0-100 大摆(so101 corr 0.19~0.28,视觉上几乎
                # 不可见)。EE → 只用平移三维;关节 → 剔除夹爪列。
                p_sync = np.asarray(proprio_state)
                if str(proprio_space) == "ee" and p_sync.shape[1] >= 3:
                    p_sync = p_sync[:, :3]
                else:
                    try:
                        _gd = set(registry.get(str(embodiment_id)).gripper_dims)
                        keep = [j for j in range(p_sync.shape[1]) if j not in _gd]
                        if keep:
                            p_sync = p_sync[:, keep]
                    except Exception:  # noqa: BLE001  未知 embodiment → 不剔,保持原样
                        pass
                speed = joint_speed(p_sync, fps)
                _sync_res = global_lag(flow, fts[1:], speed,
                                       np.asarray(timestamps)[1:], **ps)
                out["sync"] = result_to_struct(_sync_res)
                if (sync_plots_mode == "all"
                        or (sync_plots_mode == "flagged"
                            and _sync_res.passed is not True)):
                    # 曲线搭质检顺风车暂存(光流是全管线最贵计算,算完就扔=白扔);
                    # 降采样到 ≤600 点(画图够用,控内存),导出层渲染
                    _ft2 = np.asarray(fts[1:], dtype=float)
                    _sp2 = np.interp(_ft2, np.asarray(timestamps)[1:], speed)
                    _st = max(1, len(flow) // 600)
                    out["curves"] = json.dumps({
                        "t": np.round(_ft2[::_st], 3).tolist(),
                        "flow": np.round(np.asarray(flow)[::_st], 5).tolist(),
                        "speed": np.round(_sp2[::_st], 6).tolist(),
                        "verdict": ("pass" if _sync_res.passed else
                                    "fail" if _sync_res.passed is False else "abstain"),
                        "detail": _sync_res.detail})
            return out

        df = df.with_column("_frame_checks", frame_checks(
            col("video"), col("proprio_state"), col("timestamps"), col("fps"),
            col("proprio_space") if "proprio_space" in df.column_names else lit("joint"),
            col("embodiment_id")))
        if do_visual:
            df = df.with_column("check_visual_quality", col("_frame_checks")["visual"])
        if do_sync:
            df = df.with_column("check_video_action_sync", col("_frame_checks")["sync"])
            frame_hard.append("check_video_action_sync")
            if sync_plots_mode != "off":
                df = df.with_column("_sync_curves", col("_frame_checks")["curves"])
        df = df.exclude("_frame_checks")
        df = df.collect()                     # ⚡ 物化:抽帧检查(解码+光流,贵)只跑一遍

    df = _capture_killed(df, frame_hard).collect()
    stats["survivors_for_vlm"] = df.count_rows()

    # ---------- 第三段:VLM 任务成败(只跑幸存者) ----------
    if enabled(cfg, "task_success") and vlm_completion is not None:
        p_task = cfg["checks"]["task_success"].get("params", {})
        try:
            from ..adapters.vlm_client import make_endstate_judge
            vcfg_t = cfg["checks"]["task_success"]["vlm"]
            endstate_judge = make_endstate_judge(vcfg_t["endpoint"], vcfg_t["model"],
                                                 api_key_env=vcfg_t.get("api_key_env"))
        except Exception:  # noqa: BLE001
            endstate_judge = None

        @daft.func(return_dtype=_result_dtype())
        def task_check(video, task_desc, task_src, fps):
            from ..adapters.decode import decode_window

            cam = sorted(video.keys())[0]
            v = video[cam]
            frames, _ = decode_window(v["path"], v["from_ts"], v["to_ts"],
                                      sample_interval_s=interval, max_side=max_side)
            res = task_success(frames, task_desc, vlm_completion, **p_task)
            res.detail["task_desc"] = str(task_desc)[:80]
            res.detail["task_desc_source"] = str(task_src)
            # 终态二值复核:VOC 渐变问询不可判时的第二意见(双视角首末帧,双问法互检)
            if res.passed is None and endstate_judge is not None:
                starts, ends = [], []
                for cam2 in sorted(video.keys()):
                    if not (cam2 == cam or "wrist" in cam2):
                        continue
                    v2 = video[cam2]
                    try:
                        f0, _ = decode_window(v2["path"], v2["from_ts"],
                                              min(v2["to_ts"], v2["from_ts"] + 0.4),
                                              max_side=max_side)
                        f1, _ = decode_window(v2["path"], max(v2["from_ts"],
                                              v2["to_ts"] - 0.4), v2["to_ts"],
                                              max_side=max_side)
                        if f0 and f1:
                            starts.append(f0[0])
                            ends.append(f1[-1])
                    except Exception:  # noqa: BLE001
                        continue
                if starts:
                    es = endstate_judge(starts, ends, str(task_desc))
                    if es is True:
                        res.passed = True
                        res.detail["verdict"] = "endstate_success"
                        res.detail["reason"] = "渐变问询不可判;终态二值复核(双问法一致)判完成"
                    elif es is False:
                        res.detail["verdict"] = "endstate_failure_suspect"
                        res.detail["reason"] = ("渐变问询不可判;终态二值复核判未完成——"
                                                "存疑进人工裁决,不硬杀")
                    else:
                        res.detail["endstate"] = "两问法矛盾,不采信"
            return result_to_struct(res)

        df = df.with_column("check_task_success", task_check(
            col("video"),
            col("task_desc") if "task_desc" in df.column_names else col("instruction"),
            col("task_desc_source") if "task_desc_source" in df.column_names
            else lit("原始标注"),
            col("fps")))
        df = df.collect()                     # ⚡ 物化:task_success(VLM,最贵)只跑一遍

    # ---------- verdict(daft.func 需固定签名,六个已知检查逐一传,缺的传 None) ----------
    from .config import KNOWN_CHECKS

    @daft.func(return_dtype=daft.DataType.string())
    def verdict_col(ts_r, kin_r, motion_r, visual_r, sync_r, task_r):
        raw = dict(zip(KNOWN_CHECKS, (ts_r, kin_r, motion_r, visual_r, sync_r, task_r)))
        checks = {k: dict(v) for k, v in raw.items() if v is not None}
        return json.dumps(episode_verdict(checks, cfg), ensure_ascii=False)

    present = set(df.column_names)
    args = [col(f"check_{n}") if f"check_{n}" in present else lit(None) for n in KNOWN_CHECKS]
    df = df.with_column("verdict", verdict_col(*args)).collect()  # ⚡ 终物化:run.py 取结果不再重算
    stats["output"] = df.count_rows()
    return df, stats


def result_to_struct_none() -> dict:
    return {"passed": None, "score": None, "detail": "{}"}
