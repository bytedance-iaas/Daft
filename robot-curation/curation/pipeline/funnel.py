"""漏斗装配:DataFrame 链即编排(文档中的 M3,Daft 下大幅简化)。

顺序按成本:纯数值(时间戳/运动学/运动质量)→ filter(硬门) → 抽帧(视觉/同步)→
filter(硬门) → VLM 任务成败(垫底只跑幸存者,省一个数量级)→ verdict。
本模块是编排壳,允许 import daft(core/ 才是无 daft 区);检查本体全在 core/ 纯函数。
"""
from __future__ import annotations

import json
import time
from typing import Callable

import numpy as np

from ..adapters.daft_adapter import result_to_struct
from ..core.checks.kinematics import kinematic_limits
from ..core.checks.motion_quality import motion_quality
from ..core.checks.task_success import endstate_review, task_success
from ..core.checks.video_action_sync import (
    global_lag,
    joint_speed,
    optical_flow_energy,
    timestamp_check,
)
from ..core.checks.visual_quality import visual_quality
from .config import enabled
from .progress import _progress_init, _progress_tick
from .verdict import episode_verdict


def _result_dtype():
    from daft import DataType

    return DataType.struct({"passed": DataType.bool(), "score": DataType.float64(),
                            "detail": DataType.string()})


# 进度显示已抽到 pipeline/progress.py(M7 在 run.py 里也要用,不该 import funnel 私有名)

# 并发探针(仅 CURATION_DEBUG_CONCURRENCY=1 时启用):量 VLM 段真实在飞条数。
# 单进程单事件循环内 += 无竞态(协程间无抢占点),故不需要锁——加锁反而不可 pickle。
_INFLIGHT: dict = {"n": 0, "max": 0, "t0": 0.0}

# episode 级并发闸门。⚠️ 2026-07-22 实测:daft 0.7.16 的 `max_concurrency=` 对 async
# **行级** UDF 不限在飞协程数——设 1/2/4/12 实测峰值一律 = morsel 全部行数(12/12)。
# 即在飞数 = morsel 行数,乘帧级并发后可达数百,会砸穿服务端配额。故闸门必须自己装。
# 放在模块级:UDF 闭包只按引用捞它(与 _PROGRESS 同理),不进 cloudpickle。
_EPISODE_SEM: dict = {}


def _episode_gate(limit: int):
    """取当前事件循环上的 episode 并发信号量(懒建,按 loop 缓存)。

    懒建的原因:Semaphore 必须绑在**运行中的** loop 上,而 UDF 定义期还没有 loop。
    协程从进入到首个 await 之间不会被抢占,故这里的检查-创建无竞态,不需要锁。
    """
    import asyncio

    loop = asyncio.get_running_loop()
    if _EPISODE_SEM.get("loop") is not loop or _EPISODE_SEM.get("limit") != limit:
        _EPISODE_SEM.update(loop=loop, limit=limit, sem=asyncio.Semaphore(limit))
    return _EPISODE_SEM["sem"]


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
    # 终态复核纳入的相机路数上限(主相机优先)。默认 4 覆盖现有数据集(DROID 3 / Bridge 4);
    # 图片数 = 路数 × endstate_frames,过多会干扰模型且涨 token,故封顶而非无限。
    max_endstate_cams = pcfg.get("max_endstate_cams", 4)
    # 二值复核每路取几帧(全程均匀,含中段)。8 帧经 ep34 消融验证足够;首尾2帧会漏掉
    # 阶跃型任务(倒/放/开关)的动作瞬间证据。图片总数 = 路数 × 本值。
    endstate_frames = pcfg.get("endstate_frames", 8)
    # VLM 段的 episode 级并发。VLM 段占端到端 ~97%,且几乎全是等服务端响应 → 唯一有效提速手段。
    # 2026-07-22 实测 10 条 DROID(VLM 段墙钟):1→447s、2→221s(2.0×)、4→117s(3.8×)、8→84s(5.3×),
    # 均延迟恒定 ~21s(服务端零排队)。默认 8;闸门见下方 _episode_gate。
    episode_concurrency = pcfg.get("vlm_episode_concurrency", 8)
    hard_cols: list[str] = []

    # ---------- 第一段:纯数值检查(最便宜) ----------
    # 进度:三个检查是三个独立 UDF,每条 episode 会过 3 次 → 只在**最后一个启用的**
    # 检查里 tick,否则计数变 3 倍(10 条显示 30/30)。标签同样按实际启用拼,
    # --only/--skip 时不会还写着没跑的检查名。
    _NUMERIC_CN = {"timestamp_check": "时间戳", "kinematic_limits": "运动学极限",
                   "motion_quality": "运动质量"}
    _numeric_on = [n for n in _NUMERIC_CN if enabled(cfg, n)]
    _pk_num = None
    if _numeric_on:
        _pk_num = _progress_init(
            "numeric", stats["input"],
            "数值检查(" + " + ".join(_NUMERIC_CN[n] for n in _numeric_on) + ")",
            quiet_before_s=3.0)      # 快就只留一行完成汇总;慢(十万条)才逐步报
    _num_last = _numeric_on[-1] if _numeric_on else None

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

    # 数值段计数:单独一个 UDF 挂在段末,而不是往三个检查里插 tick。
    # 理由:kin/motion 有多处 early return(弃权分支),逐个插必漏;且启用组合随
    # --only/--skip 变,"哪个是最后一个"会漂。独立 UDF 与检查逻辑解耦,组合怎么变都对。
    # daft 会把相邻投影融进同一条流水线,故每行是"过完三个检查再 tick",计数语义
    # 仍是"已完成"。开销=每行一次函数调用,相对解码/VLM 可忽略。
    if _pk_num is not None:
        @daft.func(return_dtype=daft.DataType.bool())
        def _numeric_progress(_eid):
            _progress_tick(_pk_num)
            return True

        df = df.with_column("_numeric_done", _numeric_progress(col("episode_id")))

    # ⚡ 物化一次:daft 惰性,每个 count_rows/to_pydict 都会从头重算整条检查链。
    # 数值段建完后 collect(),后续统计/过滤都基于物化结果 → 检查只跑一遍(2026-07-10 修
    # "懒重算8次"效率bug:此前 timestamp 等被重复调用 8×)。
    df = df.collect()
    if _pk_num is not None:
        df = df.exclude("_numeric_done")     # 计数用的临时列,不进后续 schema/交付

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

        # 标签用**语义化检查名**,不是实现机制(2026-07-22 用户反馈:"抽帧检查(解码+光流)"
        # 让人以为视觉质量/运动学没跑——机制名把纪律"用户界面只用语义名"开了个后门)。
        # 括号里点明两者共用一次解码,解释了它们为何合成一个阶段、无法分开报进度。
        _frame_names = (["视觉质量"] if do_visual else []) + (["视频动作同步"] if do_sync else [])
        _frame_label = (" + ".join(_frame_names)
                        + ("(共用一次解码)" if len(_frame_names) > 1 else "(需解码视频)"))
        _pk_frame = _progress_init("frame", stats["after_numeric_gates"], _frame_label)

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
            _progress_tick(_pk_frame)
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
        _pk_vlm = _progress_init("vlm", stats["survivors_for_vlm"], "VLM 任务成败判定")
        try:
            from ..adapters.vlm_client import make_endstate_judge
            vcfg_t = cfg["checks"]["task_success"]["vlm"]
            endstate_judge = make_endstate_judge(vcfg_t["endpoint"], vcfg_t["model"],
                                                 api_key_env=vcfg_t.get("api_key_env"))
        except Exception as _e:  # noqa: BLE001
            endstate_judge = None
            # 不静默:构造失败=配置问题(它不做网络IO,只拼URL/闭包)。若无此提示,
            # 现场会看到"VOC 判失败→硬杀"一片却不知复核压根没启动。
            print(f"[curation] ⚠️ 二值复核不可用({type(_e).__name__}:{_e}),"
                  "task_success 将仅凭 VOC 单判据判定", flush=True)

        def _task_check_sync(video, task_desc, task_src, fps):
            from ..adapters.decode import decode_window

            cam = sorted(video.keys())[0]
            v = video[cam]
            frames, _ = decode_window(v["path"], v["from_ts"], v["to_ts"],
                                      sample_interval_s=interval, max_side=max_side)
            res = task_success(frames, task_desc, vlm_completion, **p_task)
            res.detail["task_desc"] = str(task_desc)[:80]
            res.detail["task_desc_source"] = str(task_src)
            # ---- 二值复核:协议本体在 core/checks/task_success.endstate_review(纯函数)----
            # 2026-07-23 从本闭包抽出:考卷/单测/漏斗共用同一份协议,永不分叉。
            # ep34 三处修正(触发条件/全程帧/相机放开)的消融史见该函数 docstring。
            # 这里只负责框架侧的活:惰性解其余相机的帧(解码失败=少一路视角,不中断)。
            def _extra_cam_frames():
                # ⚠️ 必须与主相机同款**全程解码**(0.5s 间隔),抽帧交给 endstate_review
                #   的 linspace(含端点)。旧写法 span/endstate_frames 间隔采样止步于
                #   ~87% 处,复核看不到片尾(2026-08-04 ep30 消融实锤的截尾 bug 之一)。
                out = []
                for cam2 in [c for c in sorted(video.keys()) if c != cam][:max_endstate_cams - 1]:
                    v2 = video[cam2]
                    try:
                        fr, _ = decode_window(v2["path"], v2["from_ts"], v2["to_ts"],
                                              sample_interval_s=interval,
                                              max_side=max_side)
                        if fr:
                            out.append(fr)
                    except Exception:  # noqa: BLE001
                        continue
                return out

            res = endstate_review(res, str(task_desc), endstate_judge, frames,
                                  extra_frames_fn=_extra_cam_frames,
                                  endstate_frames=endstate_frames)
            _progress_tick(_pk_vlm)
            return result_to_struct(res)

        # ---- async 壳:让 daft 并发跑多条 episode(2026-07-22)----
        # 为什么这么套:VLM 段单条约 30s,其中绝大部分是**等方舟响应**(网络阻塞),CPU 闲着。
        # daft 的并发只对 async UDF 生效(同步 UDF 会明确报 "has no effect"),而把 async
        # 一路传染进 core/ 会破坏"检查=框架无关纯函数"的红线。故只在最外层套 async,
        # 内部同步工作交给 asyncio.to_thread → **core/ 与 vlm_client 零改动**。
        #
        # ⚠️⚠️ 闸门是自己的信号量,不是 daft 的 `max_concurrency=`(2026-07-22 血泪):
        # 该参数对 async 行级 UDF **完全不限在飞数**——设 1/2/4/12 实测峰值一律等于
        # morsel 全部行数。误信它会导致:①以为在跑串行基线,其实是全并发(白测一轮);
        # ②上规模时在飞数随 morsel 涨,乘帧级并发后砸穿服务端配额。故不再传该参数。
        # 总并发 = vlm_episode_concurrency × vlm.max_concurrency(帧级),两层相乘。
        _INFLIGHT.update(n=0, max=0, t0=time.time())

        @daft.func(return_dtype=_result_dtype())
        async def task_check(video, task_desc, task_src, fps):
            import asyncio
            import os
            import sys
            import time

            async with _episode_gate(episode_concurrency):
                if not os.environ.get("CURATION_DEBUG_CONCURRENCY"):
                    return await asyncio.to_thread(
                        _task_check_sync, video, task_desc, task_src, fps)

                from ..adapters.vlm_client import http_stats

                _INFLIGHT["n"] += 1
                _INFLIGHT["max"] = max(_INFLIGHT["max"], _INFLIGHT["n"])
                t_in = time.time()
                try:
                    return await asyncio.to_thread(
                        _task_check_sync, video, task_desc, task_src, fps)
                finally:
                    _INFLIGHT["n"] -= 1
                    n_req, s_req = http_stats()
                    wall = time.time() - _INFLIGHT["t0"]
                    print(f"[并发探针] 出 t={wall:6.1f}s 本条={time.time() - t_in:5.1f}s "
                          f"在飞={_INFLIGHT['n']} 峰值={_INFLIGHT['max']} | "
                          f"累计请求={n_req} Σ请求耗时={s_req:.0f}s "
                          f"有效并发={s_req / max(wall, 1e-9):.1f} "
                          f"均延迟={s_req / max(n_req, 1):.1f}s",
                          file=sys.stderr, flush=True)

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
