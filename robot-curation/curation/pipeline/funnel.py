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
    camera_reading,
    global_lag,
    joint_speed,
    optical_flow_energy,
    sync_check_result,
    timestamp_check,
)
from ..core.checks.visual_quality import is_live_channel, visual_quality
from .config import enabled
from .progress import _progress_init, _progress_tick
from .verdict import episode_verdict


def _result_dtype():
    from daft import DataType

    return DataType.struct({"passed": DataType.bool(), "score": DataType.float64(),
                            "detail": DataType.string()})


def build_arbitration_deps(cfg: dict) -> dict | None:
    """checks.task_success.arbitration 段 → 取证仲裁链的注入依赖包;关掉返回 None。

    enable: false(或整段留空后显式关)= 完全退回没有仲裁链的行为 —— 返回 None 后
    管线一行仲裁代码都不会执行,这是规格要求的"逐字节等价"性质的实现方式。
    构造只拼 URL/闭包不发网络 IO;构造失败=配置问题,由调用方打印警告后按"仲裁
    不可用"继续(仲裁链的故障永远不许拖垮判定主链)。
    """
    acfg = dict(cfg["checks"]["task_success"].get("arbitration") or {})
    if not acfg.get("enable", True):
        return None
    consensus = str(acfg.get("consensus", "strict"))
    if consensus != "strict":
        # 实验版还有 majority/confident 口径,生产只落了拍板的 strict;
        # 配错要炸在这里(被调用方转成警告),不能静默换口径
        raise ValueError(f"arbitration.consensus 仅支持 strict,got {consensus!r}")
    from ..adapters.vlm_client import (SharedGate, make_evidence_judge,
                                      make_grounder, make_intent_comparer,
                                      make_question_writer, timeout_for)
    from ..dataset_level.caption import make_vlm_captioner

    vcfg = cfg["checks"]["task_success"]["vlm"]
    ep, model, key = vcfg["endpoint"], vcfg["model"], vcfg.get("api_key_env")
    # 仲裁链的对冲闸门:一条 episode 内链是串行的 ⇒ 结构并发 = episode 并发;
    # 四个工厂共享同一个闸门(各建一闸会让补发在四类间叠加,在飞总数超结构并发)。
    # ⚠️ 必须是 SharedGate 不能是裸 threading.Semaphore:这四个闭包最终进 task_check
    # 这个 daft UDF,裸锁不可 cloudpickle —— 2026-08-18 生产完整质检因此直接崩在
    # check_serializable(--lite 探测不到,e2e 单测又被 ignore,故漏到线上)。
    _epc = int(cfg.get("pipeline", {}).get("vlm_episode_concurrency", 8))
    arb_gate = SharedGate(_epc)
    t_arb = timeout_for("arbitration", vcfg)
    return {
        "question_writer": make_question_writer(ep, model, timeout_s=t_arb,
                                                api_key_env=key, gate=arb_gate),
        "grounder": make_grounder(ep, model, timeout_s=t_arb,
                                  api_key_env=key, gate=arb_gate),
        "judge": make_evidence_judge(ep, model, timeout_s=t_arb,
                                     api_key_env=key, gate=arb_gate),
        "same_task": make_intent_comparer(ep, model, timeout_s=t_arb,
                                          api_key_env=key, gate=arb_gate),
        "captioner": make_vlm_captioner(ep, model,
                                        timeout_s=timeout_for("caption", vcfg),
                                        api_key_env=key, max_in_flight=_epc),
        "caption_n_frames": int(cfg.get("skill_profile", {}).get("n_frames", 8)),
        "params": {
            "kill_min_lines": int(acfg.get("kill_min_lines", 2)),
            "n_votes": int(acfg.get("n_votes", 3)),
            "crop_pad": float(acfg.get("crop_pad", 0.15)),
            "upscale": int(acfg.get("upscale", 3)),
            "transient_offset_s": float(acfg.get("transient_offset_s", 1.0)),
            # 取证路数上限:不写默认复用复核层的相机上限(同一批解码帧,同一顶)
            "max_cams": int(acfg.get("max_cams",
                                     cfg.get("pipeline", {}).get("max_endstate_cams", 4))),
        },
    }


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
        _ps_all = dict(cfg["checks"].get("video_action_sync", {}).get("params", {}))
        # 同步检查的参数分两层:测量层(逐相机 global_lag)与判定层(跨相机 sync_verdict)。
        # 配置里是平铺的一段 params,这里按名字分派——客户配 kill_lag_min_s 不该炸在
        # global_lag 的签名上,反之亦然。lag_tol_s 两层都要(容差是同一个概念)。
        _VERDICT_KEYS = {"spread_tol_s", "kill_lag_min_s", "neg_kill_lag_min_s",
                         "min_kill_cameras"}
        ps = {k: v for k, v in _ps_all.items() if k not in _VERDICT_KEYS}
        pver = {k: v for k, v in _ps_all.items() if k in _VERDICT_KEYS}
        if "lag_tol_s" in _ps_all:
            pver["lag_tol_s"] = _ps_all["lag_tol_s"]
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
                    _res = global_lag(flow, fts[1:], speed,
                                      np.asarray(timestamps)[1:], **ps)
                    sync_cams[_short_of[c]] = camera_reading(_res)
                    # 曲线搭质检顺风车暂存(光流是全管线最贵计算,算完就扔=白扔);
                    # 降采样到 ≤600 点(画图够用,控内存),导出层渲染
                    _ft2 = np.asarray(fts[1:], dtype=float)
                    _sp2 = np.interp(_ft2, np.asarray(timestamps)[1:], speed)
                    _st = max(1, len(flow) // 600)
                    sync_curves[_short_of[c]] = {
                        "t": np.round(_ft2[::_st], 3).tolist(),
                        "flow": np.round(np.asarray(flow)[::_st], 5).tolist(),
                        "speed": np.round(_sp2[::_st], 6).tolist(),
                        **{k: _res.detail.get(k) for k in
                           ("lag_s", "corr_peak", "code", "n_trimmed_static")}}
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
                if (sync_plots_mode == "all"
                        or (sync_plots_mode == "flagged"
                            and (_det["verdict"] != "aligned"
                                 or _det["flagged_cameras"]))):
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
            from ..adapters.vlm_client import make_endstate_voter, timeout_for
            vcfg_t = cfg["checks"]["task_success"]["vlm"]
            # 对冲闸门容量 = 结构并发(episode 并发 × 每机位双问 2),不许更低
            _epc_es = int(cfg.get("pipeline", {}).get("vlm_episode_concurrency", 8))
            cam_voter = make_endstate_voter(vcfg_t["endpoint"], vcfg_t["model"],
                                            timeout_s=timeout_for("endstate", vcfg_t),
                                            api_key_env=vcfg_t.get("api_key_env"),
                                            max_in_flight=max(2, _epc_es * 2))
        except Exception as _e:  # noqa: BLE001
            cam_voter = None
            # 不静默:构造失败=配置问题(它不做网络IO,只拼URL/闭包)。若无此提示,
            # 现场会看到判定一片弃权却不知复核压根没启动。
            print(f"[curation] ⚠️ 复核投票器不可用({type(_e).__name__}:{_e}),"
                  "task_success 将仅凭打分层单判据判定(失败候选降弃权)", flush=True)
        try:
            arb_deps = build_arbitration_deps(cfg)
        except Exception as _e:  # noqa: BLE001
            arb_deps = None
            # 同复核投票器:构造失败要出声,否则弃权条目静默维持人工,看不出仲裁没启动
            print(f"[curation] ⚠️ 取证仲裁链不可用({type(_e).__name__}:{_e}),"
                  "弃权条目维持进人工", flush=True)

        def _arbitrate(res, cam_frames, cam_ts, task_desc, task_src,
                       action, timestamps, embodiment_id):
            """弃权条目 → 取证仲裁链(判定本体在 core.arbitration_review 纯函数)。

            意图 = 自产 caption:漏斗前为无标注条目补过的直接复用(task_desc 此时
            本来就是 caption);标注条目在这里现打一条 —— **复用已解码的 cam_frames**,
            不再碰视频(M7 的 caption 阶段在漏斗之后,此刻还不存在)。
            仲裁链自身的任何异常只写进留痕,绝不拖垮判定主链。
            """
            from ..core.checks.task_success import arbitration_review
            try:
                src = str(task_src)
                if src == "自产caption":
                    caption, cap_src = str(task_desc), "自产caption(漏斗前)"
                else:
                    caption, cap_src = "", "自产caption(仲裁时)"
                    groups = []
                    for name, fr in cam_frames.items():
                        idx = np.unique(np.linspace(0, len(fr) - 1,
                                                    min(arb_deps["caption_n_frames"],
                                                        len(fr)), dtype=int))
                        groups.append((name, [fr[i] for i in idx]))
                    try:
                        cap = str(arb_deps["captioner"](groups)).strip().strip('."')
                        # unclear = captioner 诚实弃权(caption.py 同款归一),留空触发
                        # arbitration_review 的 no_caption 弃权
                        if cap and not cap.lower().startswith("unclear"):
                            caption = cap
                    except Exception:  # noqa: BLE001
                        caption = ""
                annotation = str(task_desc) if src == "原始标注" else ""
                # 夹爪信号:列下标走 registry 的 gripper_dims(不硬编码数据集布局);
                # 未知 embodiment / 无夹爪列 → None,core 侧自选兜底帧
                gr = gts = None
                try:
                    gd = registry.get(str(embodiment_id)).gripper_dims
                    a = np.asarray(action)
                    dims = tuple(d for d in gd if d < a.shape[1])
                    if dims:
                        gr = a[:, dims]
                        gts = np.asarray(timestamps, dtype=float)
                except Exception:  # noqa: BLE001
                    pass
                arbitration_review(
                    res, caption=caption, caption_source=cap_src,
                    annotation=annotation, cam_frames=cam_frames, cam_ts=cam_ts,
                    gripper=gr, gripper_ts=gts,
                    question_writer=arb_deps["question_writer"],
                    grounder=arb_deps["grounder"], judge=arb_deps["judge"],
                    same_task=arb_deps["same_task"], **arb_deps["params"])
            except Exception as e:  # noqa: BLE001
                res.detail["arbitration"] = {
                    "applied": False, "error": f"{type(e).__name__}: {e}"}

        def _task_check_sync(video, task_desc, task_src, fps,
                             action, timestamps, embodiment_id):
            from ..adapters.decode import decode_window
            from ..core.contract import CheckResult

            # v7.2 多视角:全部相机(封顶 max_endstate_cams)一次解码,打分与复核共用。
            # 打分层帧 = [(相机名, 图), ...](同一时刻各路,标签随数据走,由
            # make_multiview_completion 消费);复核层逐机位独立投票。
            cam_frames = {}
            cam_ts = {}         # 帧相对时间(仲裁链选取证时刻用;同一次解码顺手留下)
            for cam in sorted(video.keys())[:max_endstate_cams]:
                v = video[cam]
                try:
                    fr, fts = decode_window(v["path"], v["from_ts"], v["to_ts"],
                                            sample_interval_s=interval, max_side=max_side)
                    if fr:
                        short = cam.split(".")[-1]    # 标签用短名(去 observation.images. 前缀)
                        cam_frames[short] = fr
                        cam_ts[short] = fts
                except Exception:  # noqa: BLE001
                    continue                          # 解码失败=少一路视角,不中断
            if not cam_frames:
                res = CheckResult(name="task_success", passed=None,
                                  detail={"reason": "所有相机解码失败,无帧可判"})
                res.detail["task_desc"] = str(task_desc)[:80]
                res.detail["task_desc_source"] = str(task_src)
                _progress_tick(_pk_vlm)
                return result_to_struct(res)
            nmin = min(len(f) for f in cam_frames.values())    # 各路对齐到最短(同步误差≤1帧)
            names = list(cam_frames)
            mv = [[(n, cam_frames[n][i]) for n in names] for i in range(nmin)]
            res = task_success(mv, task_desc, vlm_completion, **p_task)
            res.detail["task_desc"] = str(task_desc)[:80]
            res.detail["task_desc_source"] = str(task_src)
            res.detail["cams"] = names
            # ---- 复核:逐机位独立投票(协议本体在 core.endstate_review 纯函数)----
            res = endstate_review(res, str(task_desc), cam_voter, cam_frames,
                                  endstate_frames=endstate_frames)
            # ---- 取证仲裁链:**仅当打分+复核后仍弃权**才触发(老判决不许翻案),
            #      复用本函数已解码的 cam_frames,不再解一遍视频 ----
            if arb_deps is not None and res.passed is None:
                _arbitrate(res, cam_frames, cam_ts, task_desc, task_src,
                           action, timestamps, embodiment_id)
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
        async def task_check(video, task_desc, task_src, fps,
                             action, timestamps, embodiment_id):
            import asyncio
            import os
            import sys
            import time

            async with _episode_gate(episode_concurrency):
                if not os.environ.get("CURATION_DEBUG_CONCURRENCY"):
                    return await asyncio.to_thread(
                        _task_check_sync, video, task_desc, task_src, fps,
                        action, timestamps, embodiment_id)

                from ..adapters.vlm_client import http_stats

                _INFLIGHT["n"] += 1
                _INFLIGHT["max"] = max(_INFLIGHT["max"], _INFLIGHT["n"])
                t_in = time.time()
                try:
                    return await asyncio.to_thread(
                        _task_check_sync, video, task_desc, task_src, fps,
                        action, timestamps, embodiment_id)
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
            col("fps"), col("action"), col("timestamps"), col("embodiment_id")))
        df = df.collect()                     # ⚡ 物化:task_success(VLM,最贵)只跑一遍

        if arb_deps is not None:
            # 仲裁触发计数(验收:成本可见,报告里要能看出触发了多少条)。
            # 纯读已物化的 detail,零重算;enable:false 时不产生这个统计键(逐字节等价)。
            _arb_cnt = {"triggered": 0, "adopted_success": 0, "adopted_failure": 0,
                        "abstained": 0, "skipped": 0}
            for _r in df.select("check_task_success").to_pydict() \
                        .get("check_task_success", []):
                try:
                    _a = json.loads((_r or {}).get("detail") or "{}").get("arbitration")
                except Exception:  # noqa: BLE001
                    _a = None
                if not isinstance(_a, dict):
                    continue
                _arb_cnt["triggered"] += 1
                if _a.get("skipped") or _a.get("error"):
                    _arb_cnt["skipped"] += 1
                elif _a.get("final") == "yes":
                    _arb_cnt["adopted_success"] += 1
                elif _a.get("final") == "no":
                    _arb_cnt["adopted_failure"] += 1
                else:
                    _arb_cnt["abstained"] += 1
            stats["arbitration"] = _arb_cnt

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
