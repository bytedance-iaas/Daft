"""video-action 时序同步检查(文档中的 M5a)。硬门,自研工程,最难模块。

三层:
- L0 时间戳检查:单调/等间隔/三通道对齐(几乎免费,纯数值)→ 硬门(乱序/大空洞)
- L1 全局 lag:光流能量曲线 × 关节速度曲线 → 重采样公共时间轴 → scipy.signal.correlate
  扫 lag;corr_peak < 阈值标"不可判"(诚实指标,不硬判)
- L2 分段漂移:V1(PLAN.md §9 backlog)

坑(DESIGN.md §M5a):只用外部固定相机(wrist 会动干扰);静止段无信号跳过;
⚠️ daft 内置 pearson_correlation 是零延迟相关,替不了滞后扫描 —— 求 time offset 必须 scipy。
"""
from __future__ import annotations

import numpy as np

from ..contract import CheckResult


def timestamp_check(
    timestamps: np.ndarray,
    fps: float | None,
    *,
    gap_mult: float = 1.8,        # 间隔超过名义 dt×此倍数 = 空洞(丢帧)
    jitter_tol: float = 0.25,     # 间隔偏离名义 dt 超过 ±25% 记为抖动帧
    jitter_ratio_bad: float = 0.05,
) -> CheckResult:
    """L0:时间戳单调/等间隔(几乎免费,纯数值)。硬门:乱序/大空洞判废。"""
    ts = np.asarray(timestamps, dtype=np.float64)
    if len(ts) < 2:
        return CheckResult(name="timestamp_check", passed=False,
                           detail={"reason": f"时间戳过短({len(ts)})"})

    dt = np.diff(ts)
    detail: dict = {"n": len(ts)}

    # ① 单调:倒退/重复直接判废
    if dt.min() <= 0:
        k = int(np.argmin(dt))
        return CheckResult(name="timestamp_check", passed=False,
                           detail={**detail, "reason": "时间戳非严格递增",
                                   "frame": k, "ts": [float(ts[k]), float(ts[k + 1])]})

    # ② 等间隔:名义 dt 取中位数(比标称 fps 更可信);空洞=丢帧
    dt_nominal = 1.0 / fps if fps else float(np.median(dt))
    gaps = np.where(dt > gap_mult * dt_nominal)[0]
    detail["dt_nominal"] = round(dt_nominal, 6)
    detail["max_dt"] = round(float(dt.max()), 6)
    if len(gaps):
        detail["reason"] = "时间戳有空洞(丢帧)"
        detail["gap_frames"] = [
            {"frame": int(k), "dt": round(float(dt[k]), 4)} for k in gaps[:10]]
        return CheckResult(name="timestamp_check", passed=False, detail=detail)

    # ③ 抖动率(软信息,不单独判废,记录进 detail)
    jitter_ratio = float((np.abs(dt - dt_nominal) > jitter_tol * dt_nominal).mean())
    detail["jitter_ratio"] = round(jitter_ratio, 4)
    return CheckResult(name="timestamp_check",
                       passed=jitter_ratio <= jitter_ratio_bad, detail=detail)


def optical_flow_energy(frames: list[np.ndarray], max_side: int = 128) -> np.ndarray:
    """相邻帧 Farneback 稠密光流 → 每帧对的运动能量标量曲线(P1 spike2 产物)。

    frames: 按时间序的帧列表(HxWx3 uint8 或 HxW 灰度)。返回长度 len(frames)-1 的能量数组
    (每个值 = 该帧对光流向量模长的均值)。光流贵 → 超过 max_side 先等比降分辨率。
    对应关节速度曲线做互相关即得 video-action lag(L1,P3.4 装配)。
    """
    import cv2  # 局部 import:core 只在用到视觉检查时才需要 opencv

    if len(frames) < 2:
        return np.zeros(0, dtype=np.float64)

    grays = []
    for f in frames:
        g = cv2.cvtColor(f, cv2.COLOR_RGB2GRAY) if f.ndim == 3 else f
        h, w = g.shape
        scale = max_side / max(h, w)
        if scale < 1.0:
            g = cv2.resize(g, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        grays.append(g)

    energy = np.empty(len(grays) - 1, dtype=np.float64)
    for i in range(len(grays) - 1):
        flow = cv2.calcOpticalFlowFarneback(
            grays[i], grays[i + 1], None,
            pyr_scale=0.5, levels=3, winsize=15,
            iterations=3, poly_n=5, poly_sigma=1.2, flags=0)
        energy[i] = float(np.linalg.norm(flow, axis=2).mean())
    return energy


def joint_speed(action: np.ndarray, fps: float) -> np.ndarray:
    """action [T, dim] 差分 → 每步速度标量曲线(L2 范数 × fps),长度 T-1。"""
    if len(action) < 2:
        return np.zeros(0, dtype=np.float64)
    return np.linalg.norm(np.diff(np.asarray(action, dtype=np.float64), axis=0), axis=1) * fps


def global_lag(
    flow_energy: np.ndarray,
    flow_t: np.ndarray,
    speed: np.ndarray,
    speed_t: np.ndarray,
    *,
    max_lag_s: float = 2.0,       # 只扫 ±此窗口内的滞后
    corr_min: float = 0.3,        # 峰值相关低于此 = 不可判(诚实缺省,不硬判)
    kill_corr_min: float = 0.35,  # 硬杀需要的相关:高于可判门(0.3)一档——so101 ep39
                                  # corr 0.31 判'滞后1.3s'证据配不上刑罚(→复核);
                                  # 0.45 又会放跑真注入错位(pusht 相关天花板低,
                                  # corr 0.35~0.45 的真错位占比不小)。P5.1 校准复审
    lag_tol_s: float = 0.25,      # |lag| 容忍(≈2-3 帧;正式默认 P5.1 校准)
    min_active_std: float = 1e-9,
) -> CheckResult:
    """L1:光流能量 × 关节速度 → 重采样公共轴 → scipy 互相关扫全局 lag。硬门。

    语义:返回 lag_s > 0 = 视觉事件**晚于**动作(视频滞后);符号约定由单测锚定。
    速度代理务必用 achieved proprio 差分(spike2 learning:用 action 指令 r 掉到 0.24)。
    corr_peak < corr_min → passed=None(不可判:静止段多/相机不合适),如实上报不硬判。
    """
    from scipy import signal

    f = np.asarray(flow_energy, dtype=np.float64)
    s = np.asarray(speed, dtype=np.float64)
    ft = np.asarray(flow_t, dtype=np.float64)
    st = np.asarray(speed_t, dtype=np.float64)
    if len(f) < 8 or len(s) < 8:
        return CheckResult(name="video_action_sync", passed=None,
                           detail={"reason": f"信号过短(flow={len(f)}, speed={len(s)})"})

    # 重采样到光流时间轴(通常更稀);无运动信号(std≈0)= 不可判
    s_on_f = np.interp(ft, st, s)
    if f.std() < min_active_std or s_on_f.std() < min_active_std:
        return CheckResult(name="video_action_sync", passed=None,
                           detail={"reason": "静止段无信号(std≈0)"})
    zf = (f - f.mean()) / f.std()
    zs = (s_on_f - s_on_f.mean()) / s_on_f.std()

    dt = float(np.median(np.diff(ft)))
    xc = signal.correlate(zf, zs, mode="full") / len(zf)
    lags = signal.correlation_lags(len(zf), len(zs), mode="full")
    win = np.abs(lags * dt) <= max_lag_s
    k = int(np.argmax(xc[win]))
    lag_s = float(lags[win][k] * dt)          # >0: flow 比 speed 晚(视频滞后)
    corr_peak = float(xc[win][k])
    corr_zero = float(xc[lags == 0][0])

    detail = {"lag_s": round(lag_s, 4), "corr_peak": round(corr_peak, 4),
              "corr_at_zero": round(corr_zero, 4), "dt": round(dt, 4),
              "n_samples": len(zf)}
    # 样本量下限(统计正当性):N 个样本的互相关标准差≈1/√N,在 ±K 个滞后点里取最大
    # 值的噪声假峰可达 (1/√N)·√(2lnK)——bridge 25样本实测假峰 lag 0.8-1.8s/corr 0.44。
    # 短序列的滞后估计统计上不可靠 → 硬门诚实弃权(5fps 短片=方法边界,非数据有罪)
    if len(zf) < 60:
        detail["reason"] = f"序列过短(n={len(zf)}<60),滞后估计统计上不可靠,不可判"
        return CheckResult(name="video_action_sync", passed=None, detail=detail)
    if corr_peak < corr_min:
        detail["reason"] = f"corr_peak {corr_peak:.2f} < {corr_min} 不可判"
        return CheckResult(name="video_action_sync", passed=None, detail=detail)
    # 峰值突出度门控(2026-07-07 评测教训:bridge 26帧@5fps 短序列互相关噪声假峰
    # lag 高达 0.8-1.8s,而 LeRobot 转换数据按构造对齐,真错位不可能存在):
    # 真错位=0滞后处相关低、真滞后处显著高;噪声假峰=曲线平坦。
    # 0滞后处相关贴着峰值(差<prominence)→ 实质对齐,判 pass;
    # 峰显著突出且滞后超容差(容差下限=2个帧周期,滞后分辨率所限)→ 才够格硬杀。
    # 硬杀四条件(2026-07-07 双侧实测定):样本量够 + 相关够 + 峰显著突出 + 滞后超容差;
    # 任一不满足→不杀。容差下限=2帧周期(滞后分辨率所限)。
    prominence = 0.15
    tol_eff = max(lag_tol_s, 2.0 * dt)
    if abs(lag_s) <= tol_eff:
        return CheckResult(name="video_action_sync", passed=True, detail=detail)
    if corr_zero >= corr_peak - prominence:
        detail["reason"] = (f"滞后 {lag_s:.2f}s 超容差但峰不突出"
                            f"(corr0 {corr_zero:.2f}≈peak {corr_peak:.2f}),证据含糊 → 不可判")
        return CheckResult(name="video_action_sync", passed=None, detail=detail)
    if corr_peak < kill_corr_min:
        detail["reason"] = (f"疑似滞后 {lag_s:.2f}s 但相关偏弱"
                            f"(corr {corr_peak:.2f} < {kill_corr_min}),不足以硬杀 → 人工复核")
        return CheckResult(name="video_action_sync", passed=None, detail=detail)
    return CheckResult(name="video_action_sync", passed=False, detail=detail)
