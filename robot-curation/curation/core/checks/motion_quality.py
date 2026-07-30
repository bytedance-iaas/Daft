"""运动质量检查:六维软分(文档中的 M4b)。零语义,CPU,纯 numpy。

算法思路移植自 scorer(scores/path.py),重写要点:
- scorer 的敏感度常数(k=1000/0.05/threshold_deg=7)是魔法数字 → 参数化+自适应基准,P5.1 校准;
- scorer 的 gripper_consistency 是恒真废逻辑 → 重写为"夹爪指令抖动率";
- stuck 是我们新增维(scorer 没有)。
真数据教训(2026-07-02,aloha_sim 双臂):
- 夹爪可能有多个(双臂)→ gripper_dims 是 tuple;夹爪列不进运动学统计;
- "从未动过的关节"(任务用不到)≠卡死;卡死 = **动过之后**在别人还在动时长时间冻结;
- 饱和阈值不能用"单步步长"(真实跟踪误差天然 >> 单步)→ 用 |cmd-achieved| 相对该关节
  运动范围(median gap / range),持续大偏差才算饱和。
六维:smoothness / path_efficiency / spike / joint_stability / gripper_jitter /
actuator_saturation + stuck。总分=可用维均值,每维明细进 detail。
"""
from __future__ import annotations

import numpy as np

from ..contract import CheckResult


def motion_quality(
    action: np.ndarray,
    proprio: np.ndarray | None,
    fps: float,
    *,
    gripper_dims: tuple[int, ...] | None = None,  # 夹爪列下标(双臂可多个;None=无夹爪列)
    smooth_ref: float | None = None,      # 平滑参考(None=按轨迹步长自适应)
    spike_iso_knee: float = 30.0,         # 峰值孤立度拐点(默认锚定 aloha 真机:干净≤28,注入≥69)
    spike_iso_scale: float = 10.0,        # 超过拐点后的衰减尺度
    stable_window_s: float = 2.0,         # 末态稳定窗口
    stuck_frames: int = 30,               # 冻结帧数上限阈(与 1 秒×fps 取小:5fps 短片也能判)
    saturation_ref: float = 0.15,         # median|cmd-achieved|/关节运动范围 ≥此值算显著饱和
    angle_dims: tuple[int, ...] | None = None,  # 角度维下标(EE 的 rpy / 关节角)
    angle_mode: str | None = None,        # "absolute"=解绕 / "delta"=回卷到±半周期
    angle_period: float = 6.283185307179586,    # 弧度=2π,度=360
    euler_triplet: bool = False,          # angle_dims 是姿态rpy三元组(EE)而非独立关节角
    control_mode: str | None = None,      # absolute/delta/velocity(数据集语义层解析)
    same_space: bool = True,              # action 与 proprio 是否同空间(M1 推断)
    stuck_strategy: str = "auto",         # cmd_delta_vs_pos/increment_vs_pos/
                                          #   velocity_dual_scale/abstain/auto(按control_mode)
    velocity_scale: tuple | None = None,  # velocity 控制的经验位移系数(米/单位·帧,
                                          # 数据集 profile extras 提供;有它才能做
                                          # "期望位移 vs 实际位移"的响应效率判据)
    joint_spans: tuple | None = None,     # 各关节全工作量程(M2 规格表 hi-lo;绝对
                                          # 控制的定罪幅度地板:指令净挪≥2%全量程,
                                          # 死区级微动无资格立案——so101 舵机死区教训)
) -> CheckResult:
    """action: [T,dim] 指令;proprio: [T,dim'] 实际(可空);fps 来自 M1。"""
    a = np.asarray(action, dtype=np.float64)
    T, dim = a.shape
    _rot_seq = None                               # EE 姿态四元数序列(euler_triplet 时填)
    # C1:角度回绕伪影防护——yaw 跨 ±180° 产生 ≈∓2π 假跳变,裸 diff 会误报成物理尖刺
    if angle_dims and angle_mode in ("absolute", "delta"):
        ad = [d for d in angle_dims if d < dim]
        if ad:
            if angle_mode == "absolute":
                a[:, ad] = np.unwrap(a[:, ad], axis=0, period=angle_period)
            else:
                half = angle_period / 2.0
                a[:, ad] = (a[:, ad] + half) % angle_period - half
    if angle_dims and proprio is not None:
        # proprio 是绝对姿态读数,其欧拉三元组的差分有两种表示法陷阱:
        # ①±π回绕(droid roll 悬停边界,假跳变 iso 288-889) ②万向节锁死
        # (droid ep91:pitch→-90°时 roll/yaw 数值等量对冲狂摆而真实转动温和)。
        # 根治=不在欧拉角上做差分:换"姿态里程表"(四元数测地距离的累计转角),
        # 差分它=每帧真实转角,对两种陷阱双重免疫。关节角(非三元组)仍走解绕。
        p_ = np.asarray(proprio, dtype=np.float64).copy()
        pd = [d for d in angle_dims if d < p_.shape[1]]
        if euler_triplet and len(pd) == 3:        # EE 的 rpy 三元组(显式声明,防3关节臂误路由)
            from scipy.spatial.transform import Rotation

            rot = Rotation.from_euler("xyz", p_[:, pd])
            _rot_seq = rot                        # stuck 健康门要算窗口净旋转(见下)
            steps = (rot[:-1].inv() * rot[1:]).magnitude()
            p_[:, pd[0]] = np.concatenate([[0.0], np.cumsum(steps)])
            p_[:, pd[1]] = 0.0
            p_[:, pd[2]] = 0.0
        elif pd:                                  # 关节角:解绕足够
            p_[:, pd] = np.unwrap(p_[:, pd], axis=0, period=angle_period)
        proprio = p_
    dt = 1.0 / fps

    grip_cols = tuple(gripper_dims or ())
    arm_cols = [j for j in range(dim) if j not in grip_cols]
    arm = a[:, arm_cols]

    step = np.abs(np.diff(arm, axis=0))                 # [T-1, n_arm]
    step_scale = float(step.mean() + 1e-9)

    sub: dict[str, float | None] = {}
    detail: dict = {}

    # ① smoothness:加速度 RMS,基准自适应(步长尺度/dt²)
    if T >= 3:
        accel = np.diff(arm, n=2, axis=0) / dt**2
        rms = float(np.sqrt((accel**2).mean()))
        ref = smooth_ref if smooth_ref is not None else 4.0 * step_scale / dt**2
        sub["smoothness"] = float(np.exp(-rms / (ref + 1e-12)))
        detail["accel_rms"] = round(rms, 4)
    else:
        sub["smoothness"] = None

    # ② path_efficiency:直线/路径(关节空间;scorer 同款;软分,来回动作天然低)
    path = float(np.linalg.norm(np.diff(arm, axis=0), axis=1).sum())
    straight = float(np.linalg.norm(arm[-1] - arm[0]))
    sub["path_efficiency"] = None if path < 1e-9 else float(np.clip(straight / path, 0, 1))
    detail["path_len"] = round(path, 4)

    # ③ spike:加速度离群帧。真数据加速度**重尾**(aloha 实测 MAD-z>15 的帧占 10%+)→
    # MAD/中位数阈值全军覆没;真尖刺的特征是比自身 p99 还高一个量级 → 分位数相对阈值
    if proprio is not None and np.asarray(proprio).shape[1] > max(arm_cols, default=-1):
        src = np.asarray(proprio, dtype=np.float64)[:, arm_cols]
    else:
        src = arm
    # 判别量 = **峰值孤立度**:数据尖刺是孤立 1-3 帧(前后干净),真实接触有物理衰减
    # (邻域整体抬高)。2026-07-02 实测:aloha 干净(含猛烈接触)iso 3~28,注入 69~101;
    # 合成平滑轨迹 干净 iso≈1,注入 6.6~9 → **无普适绝对阈值**,拐点是配置项
    # (默认锚定真机 aloha;平滑 embodiment 用更低拐点,P5.1 按校准集定)。
    src_step = np.abs(np.diff(src, axis=0)).max(axis=1) if len(src) >= 2 else np.array([])
    pos_steps = src_step[src_step > 0]
    duty = float((src_step > 0.1 * np.percentile(pos_steps, 95)).mean()) if len(pos_steps) else 0.0
    if len(src) >= 3 and duty < 0.15:
        # 静止主导(运动占空比<15%,如 droid 遥操作长停顿):任何动作起步都长得像
        # "孤立高加速度",孤立度判据结构性失效 → 诚实弃权(2026-07-07 实测 iso 中位 408)
        sub["spike"] = None
        detail["spike_reason"] = f"运动占空比 {duty:.2f}<0.15,孤立度判据不适用"
    elif len(src) >= 3:
        acc = np.abs(np.diff(src, n=2, axis=0))
        # p95 基线必须剔除峰值邻域:短 episode(如 bridge 26帧)里尖刺自己就是 p95,
        # 除以自己 → 孤立度恒≈1 → 永远漏检(2026-07-07 评测抓出的真 bug)
        k0 = int(np.argmax(acc.max(axis=1)))
        keep = np.ones(len(acc), dtype=bool)
        keep[max(0, k0 - 2):k0 + 3] = False
        base = acc[keep] if keep.sum() >= 5 else acc
        p95 = np.quantile(base, 0.95, axis=0, keepdims=True) + 1e-12
        norm = (acc / p95).max(axis=1)                    # 每帧最大标准化加速度
        k = int(np.argmax(norm))
        lo, hi = max(0, k - 8), min(len(norm), k + 9)
        surround = [norm[t] for t in range(lo, hi) if abs(t - k) > 2]
        iso = float(norm[k] / (np.median(surround) + 1.0))
        sub["spike"] = float(np.exp(-max(0.0, iso - spike_iso_knee) / spike_iso_scale))
        spike_mask = norm > max(5.0, 0.5 * norm[k])
        detail["spike_frames"] = np.where(spike_mask)[0].tolist()[:20]
        detail["spike_peak"] = round(float(norm[k]), 2)
        detail["spike_isolation"] = round(iso, 2)
    else:
        sub["spike"] = None

    # ④ joint_stability:末窗 std 相对全程 std(演示末尾应收敛)
    n_tail = max(2, int(stable_window_s * fps))
    tail_std = float(arm[-n_tail:].std(axis=0).mean())
    full_std = float(arm.std(axis=0).mean() + 1e-9)
    sub["joint_stability"] = float(np.exp(-2.0 * tail_std / full_std))
    detail["tail_std"] = round(tail_std, 5)

    # ⑤ gripper_jitter:夹爪高频翻转 = 抖动。度量必须按**时间(Hz)**而非帧占比——
    # 2026-07-09 bridge 实测:正常一次抓放(2次翻转)在 26 帧短片里 flip_rate 0.08 被误罚,
    # 同样 2 次翻转在 45 帧长片却 0.045 通过(纯 fps/时长差异,与夹爪行为无关)。
    # 真抖动 = 每秒翻转多次(物理上一次抓放需 1-2 秒,故 >2Hz 才算不正常)。
    if grip_cols:
        worst = 1.0
        flips_all, hz_all = [], []
        dur_s = max(T / fps, 1e-6)
        for gcol in grip_cols:
            g = a[:, gcol]
            rng_g = float(g.max() - g.min())
            if rng_g < 1e-9:
                flips_all.append(0)
                hz_all.append(0.0)
                continue
            binary = g > (g.min() + 0.5 * rng_g)
            flips = int(np.abs(np.diff(binary.astype(int))).sum())
            flips_all.append(flips)
            flip_hz = flips / dur_s               # 翻转频率(次/秒),与帧率/时长无关
            hz_all.append(round(flip_hz, 3))
            worst = min(worst, float(np.exp(-2.0 * max(0.0, flip_hz - 2.0))))
        sub["gripper_jitter"] = worst
        detail["gripper_flips"] = flips_all
        detail["gripper_flip_hz"] = hz_all
    else:
        sub["gripper_jitter"] = None

    # ⑥ actuator_saturation:median|cmd-achieved| / 关节运动范围(持续大偏差;取最差关节)
    # 语义门:值直比要求 cmd/achieved 同空间且 cmd 是绝对目标(增量/速度指令 × 位置读数
    # = 跨语义直比,bridge/droid 实测干净全员 0.0 的教训)
    if proprio is not None and (not same_space or control_mode != "absolute"):
        sub["actuator_saturation"] = None
        detail["saturation_reason"] = "指令与读数不同空间或指令非绝对目标(delta/unknown),值直比无意义"
    elif proprio is not None and np.asarray(proprio).shape[1] > max(arm_cols, default=-1):
        p = np.asarray(proprio, dtype=np.float64)[:, arm_cols]
        gap_med = np.median(np.abs(arm[:-1] - p[1:]), axis=0)      # a_t ≈ s_{t+1}
        joint_range = arm.max(axis=0) - arm.min(axis=0) + 1e-9
        ratio = float((gap_med / joint_range).max())
        sub["actuator_saturation"] = float(np.exp(-np.log(2) * ratio / saturation_ref))
        detail["saturation_gap_ratio"] = round(ratio, 4)
    else:
        sub["actuator_saturation"] = None

    # ⑦ stuck(dead actuator):**指令在动、实际不响应**。
    # 真数据教训:action 恒值的"有意保持"(aloha 单臂持物 7s)是正常行为,不可用 action
    # 冻结判卡死;物理卡死的特征 = cmd_step 明显而 achieved_step ≈ 0 持续 stuck_frames。
    # 策略选择:显式 stuck_strategy 优先;auto 时按 control_mode 派生(向后兼容)
    strat = str(stuck_strategy)
    if strat == "auto":
        strat = {"absolute": "cmd_delta_vs_pos", "delta": "increment_vs_pos",
                 "velocity": "velocity_dual_scale"}.get(str(control_mode), "abstain")
    if proprio is None or np.asarray(proprio).shape[1] <= max(arm_cols, default=-1):
        sub["stuck"] = None                              # 无 achieved,无从判(诚实缺省)
    elif not same_space or strat == "abstain":
        sub["stuck"] = None
        detail["stuck_reason"] = ("action 与 proprio 不同空间" if not same_space
                                  else f"stuck 策略=abstain(control_mode={control_mode})")
    else:
        # EE rpy 三元组已被测地里程表改写(累计转角+两个0),无法与 action 原始 rpy 对照
        # → stuck 只用平移维(物理卡死时平移也冻,平移无回绕/万向节问题,足够可靠)
        stuck_cols = arm_cols
        if euler_triplet and angle_dims:
            adset = set(angle_dims)
            stuck_cols = [c for c in arm_cols if c not in adset] or arm_cols
        arm_s = a[:, stuck_cols]
        p_arm = np.asarray(proprio, dtype=np.float64)[:, stuck_cols]
        ach_step = np.abs(np.diff(p_arm, axis=0))
        if strat == "increment_vs_pos":
            cmd_step = np.abs(arm_s[1:])                  # delta:增量本身即运动量
        elif strat == "velocity_dual_scale":
            cmd_step = np.abs(arm_s[1:])                  # velocity:速度大小
        else:                                            # cmd_delta_vs_pos(absolute)
            cmd_step = np.abs(np.diff(arm_s, axis=0))
        cmd_scale = np.array([np.median(c[c > 0]) if (c > 0).any() else 0.0
                              for c in cmd_step.T])
        # "实际冻结"判据 = **窗口净位移**(2026-07-15 用户用例击穿逐帧判据后重写):
        # 震颤(每帧都在动但哪儿也没去)逐帧步长永远分不清"抖着不走"vs"慢慢在走",
        # 正确的量是 1 秒窗内的净位移 < 5%×整条量程。两个退化兜底:
        # ①从未真动列(量程<20×典型步长=纯噪声,ep23 全程 2cm 盒子)→ 整列可冻结;
        # ②误杀防线:精细慢动作的指令本身小,过不了"指令在推"门,不在打击面。
        _min_run = min(stuck_frames, max(5, int(1.0 * fps)))
        _W = _min_run                                     # 净位移窗=定罪门时长
        n_steps = ach_step.shape[0]
        frozen_w = np.zeros((n_steps, len(stuck_cols)), dtype=bool)
        # 指令有意义性门(2026-07-15 aloha 回归教训):"从未真动"守卫会把**有意保持**
        # 的关节(单臂持物,指令恒值)也当成可冻结列,而保持指令的噪声有一半天然在
        # 自身中位之上 → 误定罪。资格判据:指令轨迹自己必须真动过(绝对控制看指令
        # 量程;增量/速度控制看指令积分的量程)——恒值/纯噪声指令列无定罪资格。
        cmd_meaningful = np.ones(len(stuck_cols), dtype=bool)
        if strat == "cmd_delta_vs_pos":
            # 该门只适用于绝对控制(保持指令的量化噪声问题是绝对控制特有的);
            # velocity/delta 的来回试探指令积分天然抵消,按积分量程判会误杀
            # (ep23 教训:推7秒不动被判成"指令无意义"→ 0 stuck)
            for jj in range(len(stuck_cols)):
                traj = arm_s[:, jj]
                tsteps = np.abs(np.diff(traj))
                tpos = tsteps[tsteps > 0]
                cmd_meaningful[jj] = bool(len(tpos)) and \
                    float(traj.max() - traj.min()) >= 20.0 * float(np.median(tpos))
        for jj in range(len(stuck_cols)):
            if not cmd_meaningful[jj]:
                continue                                 # 无有意义指令 → 不定罪(全 False)
            col = p_arm[:, jj]
            pos = ach_step[:, jj][ach_step[:, jj] > 0]
            rng_j = float(col.max() - col.min())
            if not len(pos) or rng_j < 20.0 * float(np.median(pos)):
                frozen_w[:, jj] = True                    # 纯噪声列:视为全程无进展
                continue
            if len(col) > _W:
                net = np.abs(col[_W:] - col[:-_W])        # net[t]=帧t→t+W 的净位移
                thr = 0.05 * rng_j
                fw = net < thr
                # 步 t(帧 t→t+1)的冻结判定用窗口起点 t;尾部继承最后一个可判值
                m = min(len(fw), n_steps)
                frozen_w[:m, jj] = fw[:m]
                if m and m < n_steps:
                    frozen_w[m:, jj] = fw[m - 1]
            # 短于窗口的 episode:保持 False(判不了,不硬猜)
        # "指令在推"判据按策略分(2026-07-15 aloha 关节7 教训:绝对控制的保持指令
        # 有±量化抖动,逐帧"步长>中位"会把完美跟踪的保持判成在推)。绝对控制=目标
        # 轨迹的窗口净位移≥5%自身量程(与冻结判据对称:目标挪了、实际没跟才算推而
        # 不动);增量/速度控制=指令幅值>自身中位(松杆≈0,天然干净)。
        active_arr = np.zeros_like(frozen_w)
        # 响应比门(仅绝对控制,2026-07-15 aloha ep12/15 教训):指令挪 0.14 而实际
        # 跟着挪 0.045(响应比~35%)是**慢速跟踪**不是卡死;真卡死响应比≈0。
        # 条件:同窗口内 实际净位移 < 20% × 指令净位移 才算"推而不动"。
        responsive = np.zeros_like(frozen_w)              # True=在跟(响应比≥20%)
        for jj in range(len(stuck_cols)):
            if not cmd_meaningful[jj]:
                continue
            if strat == "cmd_delta_vs_pos":
                traj = arm_s[:, jj]
                rng_c = float(traj.max() - traj.min())
                if len(traj) > _W and rng_c > 0:
                    cnet_v = np.abs(traj[_W:] - traj[:-_W])
                    anet_v = np.abs(p_arm[_W:, jj] - p_arm[:-_W, jj])
                    _floor = 0.05 * rng_c
                    if joint_spans is not None and stuck_cols[jj] < len(joint_spans):
                        # 幅度地板:2%×全工作量程(M2)——"命令死区内的微动、舵机
                        # 没理你"是硬件规格不是故障(so101 ep18:2.1/200单位≈1%)
                        _floor = max(_floor, 0.02 * float(joint_spans[stuck_cols[jj]]))
                    cnet = cnet_v >= _floor
                    resp = anet_v >= 0.2 * (cnet_v + 1e-12)
                    m = min(len(cnet), n_steps)
                    active_arr[:m, jj] = cnet[:m]
                    responsive[:m, jj] = resp[:m]
                    if m and m < n_steps:
                        active_arr[m:, jj] = cnet[m - 1]
                        responsive[m:, jj] = resp[m - 1]
            elif strat == "velocity_dual_scale" and velocity_scale is not None \
                    and stuck_cols[jj] < len(velocity_scale):
                # 期望位移判据(2026-07-15 ep23 教训):缓慢蠕动(0.5cm/s)在净位移
                # 判据下"在动",但指令期望的是 ~10cm/s——响应效率 4% 就是卡死。
                # 期望位移 = k×Σ|指令|(窗口内);推=期望≥max(5%量程, 1cm);
                # 卡=实际净位移 < 10%×期望。k 来自数据集 profile extras(实测拟合,
                # 平移 R²~0.6;旋转不适用——stuck 本就只看平移列)。
                k = float(velocity_scale[stuck_cols[jj]])
                cum = np.concatenate([[0.0], np.cumsum(cmd_step[:, jj])])
                if n_steps > _W:
                    expected = k * (cum[_W:] - cum[:-_W])            # [n_steps-W+1]
                    col = p_arm[:, jj]
                    actual = np.abs(col[_W:] - col[:-_W])
                    rng_j = float(col.max() - col.min())
                    push = expected >= max(0.05 * rng_j, 0.01)
                    unresp = actual < 0.10 * (expected + 1e-12)
                    strong = push & (actual >= 0.25 * (expected + 1e-12))
                    m = min(len(push), n_steps)
                    active_arr[:m, jj] = push[:m]
                    frozen_w[:m, jj] = unresp[:m]                    # 覆盖净位移判据
                    responsive[:m, jj] = strong[:m]                  # 供全局健康门(见下注)
                    if m and m < n_steps:
                        active_arr[m:, jj] = push[m - 1]
                        frozen_w[m:, jj] = unresp[m - 1]
                        responsive[m:, jj] = strong[m - 1]
            else:
                active_arr[:, jj] = cmd_step[:, jj] > cmd_scale[jj]
        # 全局冻结门(2026-07-15 ep3 假阳性教训):纯运动学分不出"执行器卡死"和
        # "顶着物体施力"(抓取压紧时 z 指令猛而不动=任务本身!)。判别靠上下文:
        # 真卡死=**全面死机**(ep89/ep23:平移+旋转+夹爪全冻);接触施力=选择性
        # (被顶住的轴停,其它模态活着)。窗口内任一其它模态健康 → 不定罪。
        healthy_w = np.zeros(n_steps, dtype=bool)
        # 平移轴健康 = 被指挥且**强响应**(velocity=效率≥25%;绝对=跟随≥20%)。
        # 两条线必须分开(ep23 教训):定罪豁免线 10%(自己那轴存疑从宽),但"整臂
        # 还活着"要 25%——偶发 12~15% 的蠕动脉冲若够格当健康,会把其它轴的连续
        # 证据切碎(11s → 4.1s)。也不用"净位移≥5%自量程"(蠕动1mm/s 就超过
        # 2cm 量程的 5%,同样洗白全面死机)
        healthy_w |= (active_arr & responsive).any(axis=1)
        if strat not in ("cmd_delta_vs_pos",) and velocity_scale is None:
            # 无系数的 velocity/delta:responsive 恒 False,退回自量程口径(文档局限)
            for jj in range(len(stuck_cols)):
                col = p_arm[:, jj]
                rng_j = float(col.max() - col.min())
                if rng_j <= 0 or len(col) <= _W:
                    continue
                anet = np.abs(col[_W:] - col[:-_W]) >= 0.05 * rng_j
                m = min(len(anet), n_steps)
                healthy_w[:m] |= anet[:m]
                if m and m < n_steps:
                    healthy_w[m:] |= anet[m - 1]
        if _rot_seq is not None and len(_rot_seq) > _W:
            # 手腕在真转 = 窗口**净旋转**≥5°(首末姿态夹角;不能用里程表差分——
            # 里程表是路径,ep23 手腕震颤 1s 累计 8°+ 被当成健康转动,把全面死机
            # 洗成局部。震颤来回抵消,净旋转才是"真的转到别处去了")
            rnet = (_rot_seq[:-_W].inv() * _rot_seq[_W:]).magnitude() >= np.deg2rad(5.0)
            m = min(len(rnet), n_steps)
            healthy_w[:m] |= rnet[:m]
            if m and m < n_steps:
                healthy_w[m:] |= rnet[m - 1]
        for gcol in grip_cols:                           # 夹爪在开合(≥10%行程/窗)
            g = a[:, gcol]
            grng = float(g.max() - g.min())
            if grng <= 0 or len(g) <= _W:
                continue
            gs = np.array([g[t:t + _W].max() - g[t:t + _W].min()
                           for t in range(len(g) - _W)]) >= 0.1 * grng
            m = min(len(gs), n_steps)
            healthy_w[:m] |= gs[:m]
            if m and m < n_steps:
                healthy_w[m:] |= gs[m - 1]
        stuck_joints = []
        _lowconf_all = []
        if (cmd_scale > 0).any():
            dead = active_arr & frozen_w & ~responsive
            if strat != "cmd_delta_vs_pos":
                # 全局健康门只用于笛卡尔(EE)数据:"顶着物体施力"混淆是笛卡尔特有
                # (被顶方向停,其它模态活着)。关节机器人每关节独立电机,"其它关节
                # 在动"不能给死关节开脱——2026-07-15 注入实测:门若全局启用,经典
                # 单关节卡死注入 100% 被放跑
                dead &= ~healthy_w[:, None]
            _AXIS = {0: "x", 1: "y", 2: "z", 3: "roll", 4: "pitch", 5: "yaw"}
            for jj, j in enumerate(stuck_cols):
                if cmd_scale[jj] <= 0:
                    continue
                # 多段收集(2026-07-15 用户定,timeline 前置):此前只留每轴最长一段,
                # 第二段被吞——timeline 会因此结构性说谎。现在每段独立过定罪门都报。
                segs_j = []                              # [(首dead帧, 末dead帧, dead帧数)]
                # 指令安静容忍:短暂松劲(≤0.5s)不打断证据段;更久的安静=证据中断
                # (无指令期间不能定罪——2026-07-15 ep89 实测:旧的"安静不打断"把
                # 中间 4.7s 无指令静止圈进证据窗,stuck 3.6s 虚增成 9.3s)
                _gap_tol = max(3, int(0.5 * fps))
                run, rs, last_dead, quiet = 0, 0, -1, 0
                for t in range(dead.shape[0]):
                    if dead[t, jj]:
                        if run == 0:
                            rs = t
                        run += 1
                        last_dead = t
                        quiet = 0
                    elif active_arr[t, jj]:
                        if run >= _min_run:              # 段被"指令在动"终结:结算
                            segs_j.append((rs, last_dead, run))
                        run, quiet = 0, 0
                    elif run > 0:                        # 指令安静:容忍有限
                        quiet += 1
                        if quiet > _gap_tol:
                            if run >= _min_run:
                                segs_j.append((rs, last_dead, run))
                            run, quiet = 0, 0
                if run >= _min_run:
                    segs_j.append((rs, last_dead, run))
                for seg_i, (rs, last_dead, best) in enumerate(segs_j[:6]):
                    best_start = rs
                    # 冻结包络(2026-07-15 用户定):证据段=指令在推而实际不动(定罪
                    # 依据);但视频里"卡住"的观感包含之后操作员停手、手臂仍静止的
                    # 时段——把前后连续静止的总窗口也报出来,报告与眼睛对得上。
                    # 容 3 帧以内的传感毛刺(so101 空闲头同款教训)。
                    e0 = best_start + 1
                    gap = 0
                    while e0 > 0:
                        if frozen_w[e0 - 1, jj]:
                            e0 -= 1
                            gap = 0
                        elif gap < 3:
                            e0 -= 1
                            gap += 1
                        else:
                            break
                    e1 = min(last_dead + 1, n_steps - 1)   # 精确段终点起扩
                    gap = 0
                    while e1 < n_steps - 1:
                        if frozen_w[e1 + 1, jj]:
                            e1 += 1
                            gap = 0
                        elif gap < 3:
                            e1 += 1
                            gap += 1
                        else:
                            break
                    _entry = {"joint": int(j), "axis": _AXIS.get(j, str(j)),
                              "segment": int(seg_i),
                              "max_dead_run": int(best),
                              "freeze_start_frame": int(best_start + 1),
                              "freeze_end_frame": int(last_dead + 1),
                              "envelope_start_frame": int(e0),
                              "envelope_frames": int(e1 - e0 + 1)}
                    # 时长分级(2026-07-15 用户定):<1.5s 的事件证据薄(贴着定罪门+
                    # 常与负载暂态混淆)→ 降级为低置信记录,不定罪、不进报告计数
                    if (last_dead + 1 - best_start) < 1.5 * fps:
                        _entry["low_confidence"] = True
                        _lowconf_all.append(_entry)
                    else:
                        stuck_joints.append(_entry)
        sub["stuck"] = 0.0 if stuck_joints else 1.0
        detail["stuck_strategy"] = strat
        if stuck_joints:
            detail["stuck_joints"] = stuck_joints
        if _lowconf_all:
            detail["stuck_low_confidence"] = _lowconf_all[:12]

    # ⑧ operator_fluency(操作流畅度,2026-07-11 用户定):检测"指令+实际双静止"的
    # 空闲段(头/中/尾分开)。与 stuck 互补成完整矩阵:指令动+实际不动=stuck(故障);
    # 双不动=idle(操作员行为:没开始/停下调整主臂/录完没停)。smoothness 对此天生
    # 失明(停顿本身是"平滑"的)——so101 真机视频复核(空闲头3s+中段5次停顿)是直接
    # 动因。默认只报不罚(fluency 不进总分,P5.1 校准后再议)。
    strat_f = str(stuck_strategy)
    if strat_f in ("auto", "abstain"):
        strat_f = {"absolute": "cmd_delta_vs_pos", "delta": "increment_vs_pos",
                   "velocity": "velocity_dual_scale"}.get(str(control_mode),
                                                          "cmd_delta_vs_pos")

    def _lag1_rho(d: np.ndarray) -> float:
        """带符号增量序列的 lag-1 皮尔逊自相关(手算,不引依赖)。
        传感噪声的增量方向逐帧随机 → ρ≈0(增量本身即噪声,如速度控制),白噪声
        位置序列差分后甚至 ρ→-0.5(实测 -0.49);真实运动(含小幅往复)增量方向
        连贯 → ρ=cos(2π/周期),周期≥6 帧时 ≥0.5。两族离得很开,判别余量充足。
        任一侧方差为 0 → 记 0(常量列无方向信息,不该凭空获得投票权)。"""
        x, y = d[:-1], d[1:]
        x = x - x.mean()
        y = y - y.mean()
        sx, sy = float(np.sqrt(np.dot(x, x))), float(np.sqrt(np.dot(y, y)))
        if sx <= 0.0 or sy <= 0.0:
            return 0.0
        return float(np.dot(x, y) / (sx * sy))

    def _static_mask(sig: np.ndarray, raw: np.ndarray, frac: float = 0.10,
                     coh_rho: float = 0.5, coh_span_floor: float = 4.0,
                     coh_min_steps: int = 8) -> np.ndarray:
        """[T-1, n] 信号 → 每帧'全列静止'掩码。逐列自身尺度(10%×正值中位),
        免跨列量纲问题(夹爪步长天然大)。'从未动过'的列不否决静止——判据:
        位置量程 < k×典型步长(全静止列的"步长"只是传感噪声,量程仅其 ~2-5
        倍——自尺度阈值在纯噪声上会失效,全静止 episode 曾被判成"全程活跃")。
        ⚠️ k 必须随片长收紧(2026-07-29 bridge 教训):匀速运动的 量程/步长
        比值上限就是步数 T-1,固定 k=20 隐含"真运动量程≫步长"只对长片成立;
        bridge 每条仅 21 帧(fps=5),真在动的列比值只有 14~20 → 八列全落进
        豁免,ep000199 被判 0-4.2s 全程 idle/still 而视频里手臂一直在动
        (active_ratio=0.85)。地板 4.0 = 真静止列的噪声漫游量程;长片 k 仍
        为 20,行为不变。
        资格审查是**双路径取或**(2026-07-29 用户定,补幅度判据的盲区):
        ①幅度路径(上述):量程 ≥ k×中位正步长;
        ②连贯路径:增量的 lag-1 自相关 ρ ≥ coh_rho(0.5)**且** 量程 ≥
          coh_span_floor(4.0)×中位正步长。动因:小包络持续往复(擦拭/搅拌类)
          的量程不随来回次数增长,幅度路径必然误判为噪声弃权,长片上全列如此
          就会给出"死录制=全程 idle"的冤案(与 bridge 案同构)。
        常数 why(均属 P5.1 校准清单):ρ 阈 0.5——白噪声下 ρ 的 null 分布
        ≈N(0, 1/√n),n≈20 时波动 ±0.23,0.5≈2 倍波动,且对应周期 6 帧的往复;
        幅度地板 4.0——纯噪声列的漫游包络约是步长的 2~5 倍,地板挡住"增量确实
        相关但只是噪声带内游走"的结构性噪声(如 AR(1) 传感器漂移);
        样本地板 8 步——n<8 时 ρ 估计不可信(null 波动 >0.35),短片本就有
        收紧的 k 和"全豁免不主张"护栏兜底,不需要连贯路径。
        sig: **带符号**增量序列 [T-1, n](需要幅度的地方内部自取 abs);
        raw: 位置量纲序列(算量程用)。"""
        # 全豁免的含义按片长分岔(2026-07-29 用户裁决):短片(k<20)的豁免判据
        # 本身就失真(真运动也够不着 k),此时"没有一列有否决资格"= 零证据,拒绝
        # 主张静止;长片(k=20)的豁免判据可信,全豁免=整条从未动过的死录制,
        # 维持旧语义(每列全 True → np.all → 全程静止),这正是豁免规则的初衷。
        cols, voting = [], 0
        k = min(20.0, max(4.0, 0.5 * len(sig)))        # len(sig)=T-1=步数
        for c, rc in zip(sig.T, raw.T):
            mag = np.abs(c)                            # 幅度判据只看大小
            pos = mag[mag > 0]
            if not len(pos):
                cols.append(np.ones(len(c), bool))     # 从未动过:不否决
                continue
            med = float(np.median(pos))
            span = float(rc.max() - rc.min())
            ok = span >= k * med                       # ①幅度路径
            if not ok and len(c) >= coh_min_steps and span >= coh_span_floor * med:
                ok = _lag1_rho(c) >= coh_rho           # ②连贯路径(带符号增量)
            if not ok:
                cols.append(np.ones(len(c), bool))     # 纯噪声/结构性噪声:不否决
            else:
                voting += 1
                cols.append(mag < frac * med)
        if not cols or (not voting and k < 20.0):
            return np.zeros(len(sig), bool)       # 短片全豁免:不主张,不诬告
        return np.all(np.column_stack(cols), axis=1)

    # 传给 _static_mask 的增量一律**带符号**(2026-07-29 连贯路径需要方向信息;
    # 幅度判据在函数内部自取 abs,数值与旧版逐位相同)
    if strat_f == "cmd_delta_vs_pos":
        cmd_sig, cmd_raw = np.diff(a, axis=0), a
    else:                                              # delta/velocity:积分成位置量纲
        cmd_sig, cmd_raw = a[1:], np.cumsum(a, axis=0)
    idle = _static_mask(cmd_sig, cmd_raw)
    if proprio is not None:
        p_all = np.asarray(proprio, dtype=np.float64)
        idle &= _static_mask(np.diff(p_all, axis=0), p_all)
    def _close_and_segment(mask: np.ndarray, min_run: int) -> list:
        """毛刺闭合 + ≥min_run 成段 → [(s0,s1)]。
        (ceil 不用 round:round(4.5)=4 的银行家舍入曾让 4 帧毛刺恰好逃过闭合)
        闭合:比 min_run 短的"活跃"孤刺并入静止——so101 实测开录后第1-4帧有舵机
        安定毛刺,单帧尖峰会把 2.5s 的空闲头切碎成"非头段"(<0.15s 的抽动不算动作)"""
        m = mask.copy()
        t = 0
        while t < len(m):
            if not m[t]:
                t1 = t
                while t1 < len(m) and not m[t1]:
                    t1 += 1
                if t1 - t < min_run:
                    m[t:t1] = True
                t = t1
            else:
                t += 1
        out, t0 = [], None
        for t, v in enumerate(m):
            if v and t0 is None:
                t0 = t
            elif not v and t0 is not None:
                if t - t0 >= min_run:
                    out.append((t0, t))
                t0 = None
        if t0 is not None and len(m) - t0 >= min_run:
            out.append((t0, len(m)))
        return out

    if len(idle):
        min_run = max(3, int(np.ceil(0.15 * fps)))       # <0.15s 的间隙不算停顿
        segs = _close_and_segment(idle, min_run)
        head_n = tail_n = mid_n = 0
        mid = []
        for s0, s1 in segs:
            if s0 == 0:
                head_n = s1 - s0
            elif s1 == len(idle):
                tail_n = s1 - s0
            else:
                mid_n += s1 - s0
                mid.append({"start_frame": int(s0), "start_s": round(s0 / fps, 2),
                            "dur_s": round((s1 - s0) / fps, 2)})
        # 两个口径分开(2026-07-14 用户定):头尾空闲=录制卫生(没掐好起止,可修剪,
        # 与操作技能无关),不属于流畅度 → fluency 只看**执行窗**(首个活跃帧~末个
        # 活跃帧)内的中段犹豫;active_ratio 保留全程口径(修剪价值/存储浪费信号)。
        exec_n = len(idle) - head_n - tail_n              # 执行窗帧数
        sub["fluency"] = (float(1.0 - mid_n / exec_n) if exec_n > 0 else None)
        if exec_n <= 0:
            detail["fluency_reason"] = "整条无有效动作(全程空闲),无执行窗可评"
        idle_total = head_n + tail_n + mid_n
        detail["active_ratio"] = round(float(1.0 - idle_total / len(idle)), 4)
        detail["idle_head_s"] = round(head_n / fps, 2)
        detail["idle_tail_s"] = round(tail_n / fps, 2)
        detail["idle_mid_count"] = len(mid)
        detail["idle_mid_total_s"] = round(sum(m["dur_s"] for m in mid), 2)
        if mid:
            detail["idle_mid_segments"] = mid[:20]
        # 视觉静止段(2026-07-28,ep89 教训):**只看手臂动没动**(proprio 静止即算,
        # 不管指令)。上面的 idle 要求指令+实际双静止——"指令在动、手臂不动但幅度
        # 不够 stuck 定罪"的段两头不沾,时间线上曾被画成"正常",与肉眼直接矛盾
        # (ep89 17.5-20s 实测速度 0.0000 却是绿条)。此口径=视频观感,专供时间线。
        if proprio is not None:
            still = _static_mask(np.diff(p_all, axis=0), p_all)
            detail["still_segments"] = [
                {"start_s": round(s0 / fps, 2), "end_s": round(s1 / fps, 2)}
                for s0, s1 in _close_and_segment(still, min_run)][:100]
    else:
        sub["fluency"] = None

    detail.update({k: (round(v, 4) if v is not None else None) for k, v in sub.items()})
    # 不进总分的维:path_efficiency(轨迹非直线=常态)、joint_stability("末态必须停稳"
    # 是任务相关先验,对真实数据惩罚过度)、stuck(二值,不宜混入连续加权;单独进报告,
    # 2026-07-09 用户定)、fluency(操作流畅度,先报不罚,P5.1 定阈值)
    # ——均只作 detail 信息项(P5.1 校准再议)
    _EXCLUDE = ("path_efficiency", "joint_stability", "stuck", "fluency")
    vals = [v for k, v in sub.items() if v is not None and k not in _EXCLUDE]
    return CheckResult(name="motion_quality",
                       score=float(np.mean(vals)) if vals else 0.0, detail=detail)
