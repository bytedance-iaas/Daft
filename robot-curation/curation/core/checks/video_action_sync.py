"""video-action 时序同步检查(文档中的 M5a)。硬门,自研工程,最难模块。

三层:
- L0 时间戳检查:单调/等间隔/三通道对齐(几乎免费,纯数值)→ 硬门(乱序/大空洞)
- L1 全局 lag:光流能量曲线 × 关节速度曲线 → 重采样公共时间轴 → scipy.signal.correlate
  扫 lag;corr_peak < 阈值标"不可判"(诚实指标,不硬判)
- L2 分段漂移:V1(PLAN.md §9 backlog)

⚠️ daft 内置 pearson_correlation 是零延迟相关,替不了滞后扫描 —— 求 time offset 必须 scipy。

── 2026-08-07 逐相机改造(用户拍板,真数据实测驱动)──────────────────────────
改造前:**只算一路相机**(与视觉质量共用解码的那路,sorted 取第一个),一路读数
就代表整条 episode 的判决。droid ep4 实测把这个设计打穿了:三路各自给出
+0.60s(corr 0.44 又矮又平)/ −0.07s(0.50)/ wrist 0.00s(**0.80,峰尖**)——
真相几乎肯定是对齐的,而生产只看了报 +0.60s 的那一路,全靠"峰不突出"这道
护栏侥幸没杀。so101 ep0 则是另一种病:互相关在 0s 与 −0.5s 两个峰几乎并列
(0.65 vs 0.64),旧判据完全看不出这是"掷硬币"。两条 episode 开头都有数秒静止段
(两条曲线都平),纯噪声进了互相关分母,把相关性系统性拉低。

改造后的三条纪律:
1. **测量层逐相机独立,永不合并成一个数**:每路各算一遍 lag/corr,读数三态
   (可信 lag / 测不准 / 无信号);
2. **判定层只在 episode 级杀,且只有一种情形**:所有可信相机一致指向同一个
   Δ≠0(见 `sync_verdict`)。单相机数据集永不因同步判废;矛盾/测不准/无信号
   一律 **passed=True + 标注**(不再返回 None——弃权会误进人工裁决队列);
3. **绝不废弃相机、绝不删 video 指针**:某路异常只标注该路。
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


def _quiet_mask(x: np.ndarray, frac: float) -> np.ndarray:
    """曲线上"接近静止"的样本掩码。

    判据:低于自身活跃幅度的 frac 比例——阈值 = p10 + frac×(p90 − p10)。
    为什么用分位而不是绝对值:光流能量与关节速度量纲完全不同(像素/秒 vs 弧度/秒),
    且各数据集基线噪声不一样,只有"相对自己有多静"才可跨相机跨数据集比较。
    为什么用 p10/p90 而不是 min/max:单帧压缩噪声或一次抖动就能把 max 顶上天,
    静止判据必须对离群点免疫(与 kinematics 的 p95 单位守卫同一条经验)。
    """
    lo, hi = np.percentile(x, [10, 90])
    if hi - lo <= 0:                     # 全常数(含全零)= 整条静止
        return np.ones(len(x), dtype=bool)
    return x <= lo + frac * (hi - lo)


def trim_static_span(flow: np.ndarray, speed: np.ndarray, *,
                     quiet_frac: float = 0.15) -> tuple[int, int]:
    """两条曲线**同时**接近静止的首尾整段 → 返回保留区间 [lo, hi)。

    为什么剔:实测两条 episode(droid ep4 / so101 ep0)开头都有数秒静止段,两条曲线
    都平。这段纯噪声进了互相关的分母(z 归一用的是全程 std),把相关系数系统性拉低
    ——corr 0.44 里有多少是"证据弱"、多少是"被静止段稀释",旧实现分不清。

    为什么只剔首尾、不剔中间:互相关的滞后↔下标映射依赖**均匀采样**,抠掉中间样本会
    让 lag 读数错位(比删得更糟:悄悄错)。而中段的静止是有信息的("这时候两边都该
    不动"),首尾静止才是纯稀释。首尾同时静止的整段删掉后,剩下部分仍是均匀网格,
    lag 语义不变。

    "两条都静"才算:只有一条平的区间恰恰是最该留下的证据(一边动一边不动 = 真错位
    的指纹),删了等于自毁证据。

    剔完样本太少怎么办:本函数如实返回窄区间,由调用方降级为"无信号"(见 global_lag),
    绝不在这里硬撑出一个统计上站不住的读数。
    """
    f = np.asarray(flow, dtype=np.float64)
    s = np.asarray(speed, dtype=np.float64)
    n = int(min(len(f), len(s)))
    if n == 0:
        return 0, 0
    quiet = _quiet_mask(f[:n], quiet_frac) & _quiet_mask(s[:n], quiet_frac)
    lo = 0
    while lo < n and quiet[lo]:
        lo += 1
    hi = n
    while hi > lo and quiet[hi - 1]:
        hi -= 1
    return lo, hi


def peak_metrics(xc: np.ndarray, lags_s: np.ndarray, k: int, *,
                 peak_sep_s: float = 0.3) -> tuple[float, float]:
    """互相关曲线的峰可信度两指标 → (主峰/次高峰之比, 峰宽秒)。

    ① **主峰/次高峰之比**:次高峰 = 与主峰间隔 ≥ peak_sep_s 的**另一个局部极大**。
       so101 ep0 实测:0s 处 0.65 与 −0.5s 处 0.64 两峰并列(比值 1.02)——曲线在
       "掷硬币",任何一个 lag 断言都是抽签抽出来的。只看 corr_peak 完全看不出这种病。
       只数局部极大(不是"远处最大值")是为了与峰宽解耦:一个又宽又干净的单峰,
       它远端的值高只是因为峰胖,不该被算成"另一个候选答案"。
    ② **峰宽**(半高全宽,基线取窗内中位数):峰过胖 = 时间分辨率不足,曲线在很宽的
       一段滞后上几乎一样高,读出来的极值点位置是噪声决定的,不足以支撑 lag 断言。
       droid ep4 exterior_1 那种又矮又平的峰就是靠这条被判"测不准"。
    """
    peak = float(xc[k])
    far = np.abs(lags_s - lags_s[k]) >= peak_sep_s
    second = 0.0
    if far.any():
        loc = np.zeros(len(xc), dtype=bool)
        if len(xc) >= 3:
            loc[1:-1] = (xc[1:-1] >= xc[:-2]) & (xc[1:-1] >= xc[2:])
        cand = far & loc
        # 远端没有局部极大(从主峰单调下降)= 没有第二个候选答案 → 比值不设障碍,
        # 该情形的病(峰太胖)交给峰宽判据抓,两个指标各管一件事不重复定罪
        second = float(np.max(xc[cand])) if cand.any() else 0.0
    if second <= 1e-6:
        ratio = 99.0 if peak > 0 else 0.0     # 次高峰≤0 = 主峰独一份
    else:
        ratio = peak / second

    base = float(np.median(xc))
    level = base + 0.5 * (peak - base)
    i = k
    while i > 0 and xc[i - 1] >= level:
        i -= 1
    j = k
    while j < len(xc) - 1 and xc[j + 1] >= level:
        j += 1
    width = float(abs(lags_s[j] - lags_s[i]))
    return round(ratio, 4), round(width, 4)


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
    trim_static: bool = True,     # 剔除首尾"两条曲线同时静止"的段(见 trim_static_span)
    quiet_frac: float = 0.15,
    min_active_samples: int = 16,  # 剔完剩这么少 = 无信号(不硬撑读数)
    min_peak_ratio: float = 1.25,  # 主峰/次高峰:低于此 = 测不准(见 peak_metrics)
    max_peak_width_s: float = 1.0,  # 峰宽上限:超过此 = 时间分辨率不足,不支撑 lag 断言
    peak_sep_s: float = 0.3,
) -> CheckResult:
    """L1:光流能量 × 关节速度 → 重采样公共轴 → scipy 互相关扫全局 lag。**单路相机**。

    语义:返回 lag_s > 0 = 视觉事件**晚于**动作(视频滞后);符号约定由单测锚定。
    速度代理务必用 achieved proprio 差分(spike2 learning:用 action 指令 r 掉到 0.24)。

    ⚠️ 本函数是**测量**,不是 episode 判决:它的 passed 只描述"这一路读数说什么"。
    整条 episode 的判废由 `sync_verdict` 跨相机汇总(判废只有一种情形:所有可信相机
    一致指向同一个 Δ≠0)。detail["trusted"] 才是给判定层用的"这路读数可不可信"。
    """
    from scipy import signal

    f = np.asarray(flow_energy, dtype=np.float64)
    s = np.asarray(speed, dtype=np.float64)
    ft = np.asarray(flow_t, dtype=np.float64)
    st = np.asarray(speed_t, dtype=np.float64)
    if len(f) < 8 or len(s) < 8:
        return CheckResult(name="video_action_sync", passed=None,
                           detail={"code": "short_signal", "trusted": False,
                                   "reason": f"信号过短(flow={len(f)}, speed={len(s)})"})

    # 重采样到光流时间轴(通常更稀)
    s_on_f = np.interp(ft, st, s)

    # 静止段剔除:首尾"两条曲线同时静止"的整段不进互相关(理由见 trim_static_span)
    n_raw, trimmed = len(f), 0
    if trim_static:
        lo, hi = trim_static_span(f, s_on_f, quiet_frac=quiet_frac)
        if hi - lo < min_active_samples:
            return CheckResult(name="video_action_sync", passed=None,
                               detail={"code": "no_motion", "trusted": False,
                                       "n_samples": int(max(hi - lo, 0)),
                                       "n_samples_raw": n_raw,
                                       "reason": f"剔除首尾静止段后有效样本仅 {max(hi - lo, 0)} "
                                                 f"(原 {n_raw}),无足够运动信号可判"})
        f, s_on_f, ft = f[lo:hi], s_on_f[lo:hi], ft[lo:hi]
        trimmed = n_raw - (hi - lo)

    if f.std() < min_active_std or s_on_f.std() < min_active_std:
        return CheckResult(name="video_action_sync", passed=None,
                           detail={"code": "no_motion", "trusted": False,
                                   "n_samples": len(f), "n_samples_raw": n_raw,
                                   "reason": "静止段无信号(std≈0)"})
    zf = (f - f.mean()) / f.std()
    zs = (s_on_f - s_on_f.mean()) / s_on_f.std()

    dt = float(np.median(np.diff(ft)))
    xc = signal.correlate(zf, zs, mode="full") / len(zf)
    lags = signal.correlation_lags(len(zf), len(zs), mode="full")
    win = np.abs(lags * dt) <= max_lag_s
    xc_win, lags_win = xc[win], lags[win] * dt
    k = int(np.argmax(xc_win))
    lag_s = float(lags_win[k])                # >0: flow 比 speed 晚(视频滞后)
    corr_peak = float(xc_win[k])
    corr_zero = float(xc[lags == 0][0])
    peak_ratio, peak_width_s = peak_metrics(xc_win, lags_win, k, peak_sep_s=peak_sep_s)

    detail = {"lag_s": round(lag_s, 4), "corr_peak": round(corr_peak, 4),
              "corr_at_zero": round(corr_zero, 4), "dt": round(dt, 4),
              "n_samples": len(zf), "n_samples_raw": n_raw, "n_trimmed_static": trimmed,
              "peak_ratio": peak_ratio, "peak_width_s": peak_width_s,
              "trusted": False}
    # 样本量下限(统计正当性):N 个样本的互相关标准差≈1/√N,在 ±K 个滞后点里取最大
    # 值的噪声假峰可达 (1/√N)·√(2lnK)——bridge 25样本实测假峰 lag 0.8-1.8s/corr 0.44。
    # 短序列的滞后估计统计上不可靠 → 硬门诚实弃权(5fps 短片=方法边界,非数据有罪)
    if len(zf) < 60:
        detail["code"] = "short_sequence"
        detail["reason"] = f"序列过短(n={len(zf)}<60),滞后估计统计上不可靠,不可判"
        return CheckResult(name="video_action_sync", passed=None, detail=detail)
    if corr_peak < corr_min:
        detail["code"] = "weak_corr"
        detail["reason"] = f"corr_peak {corr_peak:.2f} < {corr_min} 不可判"
        return CheckResult(name="video_action_sync", passed=None, detail=detail)
    # 峰值突出度门控(2026-07-07 评测教训:bridge 26帧@5fps 短序列互相关噪声假峰
    # lag 高达 0.8-1.8s,而 LeRobot 转换数据按构造对齐,真错位不可能存在):
    # 真错位=0滞后处相关低、真滞后处显著高;噪声假峰=曲线平坦。
    # 0滞后处相关贴着峰值(差<prominence)→ 实质对齐,判 pass;
    # 峰显著突出且滞后超容差(容差下限=2个帧周期,滞后分辨率所限)→ 才够格硬杀。
    # 硬杀四条件(2026-07-07 双侧实测定):样本量够 + 相关够 + 峰显著突出 + 滞后超容差;
    # 任一不满足→不杀。容差下限=2帧周期(滞后分辨率所限)。
    prominence = ZERO_PROMINENCE
    tol_eff = max(lag_tol_s, 2.0 * dt)
    detail["lag_tol_s"] = round(tol_eff, 4)
    # 峰可信度(2026-08-07 新增,两条实测校准):比值不足 = 曲线在掷硬币(so101 ep0
    # 双峰 0.65/0.64,比值 1.02);峰过胖 = 时间分辨率不足(droid ep4 exterior_1 那种
    # 又矮又平的峰)。默认 1.25 / 1.0s 的依据:①并列双峰的比值必然落在 1.0~1.1,
    # 留 0.25 的余量才挡得住"略高一点点"的伪冠军;②真错位的峰宽实测在 0.3~0.6s 量级
    # (峰宽≈运动事件自相关宽度),1.0s 以上意味着半秒内怎么挪都一样高。
    # 这两个指标只决定 **trusted**(读数可不可信),不改本函数的 passed 语义——
    # 判废是 sync_verdict 的事,单路测量不该越权。
    peak_ok = peak_ratio >= min_peak_ratio and peak_width_s <= max_peak_width_s
    if not peak_ok:
        detail["peak_issue"] = ("ambiguous_peak" if peak_ratio < min_peak_ratio
                                else "flat_peak")
    if abs(lag_s) <= tol_eff:
        detail["code"] = "aligned"
        detail["trusted"] = peak_ok
        if not peak_ok:
            detail["reason"] = (f"读数对齐(lag {lag_s:.2f}s)但峰不够可信"
                                f"(主峰/次高峰 {peak_ratio:.2f},峰宽 {peak_width_s:.2f}s)"
                                f",本路读数不参与判定")
        return CheckResult(name="video_action_sync", passed=True, detail=detail)
    if corr_zero >= corr_peak - prominence:
        detail["code"] = "ambiguous_peak"
        detail["reason"] = (f"滞后 {lag_s:.2f}s 超容差但峰不突出"
                            f"(corr0 {corr_zero:.2f}≈peak {corr_peak:.2f}),证据含糊 → 测不准")
        return CheckResult(name="video_action_sync", passed=None, detail=detail)
    if corr_peak < kill_corr_min:
        detail["code"] = "weak_corr_no_kill"
        detail["reason"] = (f"疑似滞后 {lag_s:.2f}s 但相关偏弱"
                            f"(corr {corr_peak:.2f} < {kill_corr_min}),证据不足 → 测不准")
        return CheckResult(name="video_action_sync", passed=None, detail=detail)
    if not peak_ok:
        detail["code"] = detail["peak_issue"]
        detail["reason"] = (
            f"疑似滞后 {lag_s:.2f}s,但峰形不支撑该断言"
            + (f"(主峰/次高峰仅 {peak_ratio:.2f},存在并列的另一个候选滞后)"
               if peak_ratio < min_peak_ratio
               else f"(峰宽 {peak_width_s:.2f}s > {max_peak_width_s}s,时间分辨率不足)")
            + " → 测不准")
        return CheckResult(name="video_action_sync", passed=None, detail=detail)
    detail["code"] = "lag_exceeds_tol"
    detail["trusted"] = True
    return CheckResult(name="video_action_sync", passed=False, detail=detail)


# ── 单路读数 → 接口契约里的 per_camera 条目 ──────────────────────────────────
# global_lag 的 code 是实现层的分支名(历史留下的,被 sync_plots 的英文映射表和
# 单测钉住);契约里的 code 是给 UI/客户看的六态。这里做一次映射,不改任何一头。
_CONTRACT_CODE = {
    "aligned": "aligned",
    "lag_exceeds_tol": "misaligned",
    "ambiguous_peak": "ambiguous_peak",
    "flat_peak": "flat_peak",
    "weak_corr": "low_corr",
    "weak_corr_no_kill": "low_corr",
    "no_motion": "no_motion",
    "short_signal": "no_motion",
    "short_sequence": "low_corr",
}

_CODE_NOTE = {
    "aligned": "画面与动作对齐,峰形可信",
    "misaligned": "峰形可信且滞后超容差:本路读数指向真实错位",
    "ambiguous_peak": "互相关有并列的第二个候选滞后,读数在掷硬币 → 测不准",
    "flat_peak": "互相关峰过胖(时间分辨率不足),撑不起 lag 断言 → 测不准",
    "low_corr": "相关性太弱(画面里机械臂占比小/机位太脏/样本太短)→ 测不准",
    "no_motion": "本路几乎没有运动信号(静止段占满/画面冻结)→ 无信号",
}


#: 零滞后处的相关比峰值低多少才算"峰真的赢了"。低于这个差 = 0 仍是合理解释,
#: 那个偏离 0 的峰就没有资格被叫做"疑似错位"(2026-08-07 用户在 droid ep4 上纠正:
#: "又矮又胖"是有明确病因的形态,不该笼统扣一顶疑似错位的帽子)。
ZERO_PROMINENCE = 0.15


def camera_diagnosis(r: dict, *, lag_tol_s: float = 0.25,
                     min_peak_ratio: float = 1.25,
                     max_peak_width_s: float = 1.0,
                     min_corr: float = 0.3) -> dict:
    """单路读数 → {cause, label, text, advice}:**按病因说话,不含糊其辞**。

    判别顺序刻意如此:
      ① 整体相关太弱 → 这一路信号本身不适合做互相关。**必须排最前**:相关只有
         0.2 时"零滞后也说得通"是句废话(什么都说得通),拿它当结论会把
         "信号弱"误诊成"假峰";
      ② 零滞后处相关与峰值相当 → 假峰。数据与"对齐"相容,病在画面(背景运动/
         相机晃动/机械臂占比小),**不是**错位嫌疑;
      ③ 峰过胖 → 画面运动不锐利,时间分辨率撑不起 lag 断言;
      ④ 双峰并列 → 两个候选滞后在掷硬币。
    只有"读数偏离 0 **且** 0 已经站不住"才配叫疑似错位(见 sync_verdict 的 suspect)。
    """
    code = str(r.get("code") or "")
    lag = float(r.get("lag_s") or 0.0)
    peak = float(r.get("corr_peak") or 0.0)
    zero = float(r.get("corr_at_zero") or 0.0)
    ratio = float(r.get("peak_ratio") or 0.0)
    width = float(r.get("peak_width_s") or 0.0)

    if code == "no_motion":
        return {"cause": "no_motion", "label": "无信号",
                "text": "这一路几乎没有运动信号(静止段占满或画面冻结)。",
                "advice": "确认该路是否录制正常,或本来就是占位相机。"}
    if r.get("trusted"):
        if code == "misaligned":
            return {"cause": "misaligned", "label": "错位",
                    "text": (f"峰形可信({peak:.2f},主峰/次峰 {ratio:.2f}),"
                             f"滞后 {lag:+.2f}s 超出容差 —— 这一路指向真实错位。"),
                    "advice": ("画面晚于动作(正滞后)多为相机链路延迟;画面早于动作"
                               "(负滞后)无良性解释,应回查数据装配环节。")}
        return {"cause": "aligned", "label": "对齐",
                "text": (f"峰落在 0 附近({lag:+.2f}s)且峰形可信"
                         f"(相关 {peak:.2f},主峰/次峰 {ratio:.2f})。"),
                "advice": ""}

    # 以下都是"这一路读数不可信"的情形,病因各不相同
    if peak < min_corr:
        return {"cause": "weak_signal", "label": "测不准 · 信号弱",
                "text": (f"整体相关只有 {peak:.2f},画面运动与机械臂动作的关联太弱,"
                         f"任何滞后读数都没有支撑。"),
                "advice": "机位太远、机械臂在画面里占比太小,或本段运动幅度不足。"}
    if abs(lag) > lag_tol_s and zero >= peak - ZERO_PROMINENCE:
        return {"cause": "false_peak", "label": "测不准 · 画面干扰",
                "text": (f"峰在 {lag:+.2f}s 但又矮又平(相关只有 {peak:.2f}),"
                         f"而零滞后处的相关 {zero:.2f} 与它相当 —— "
                         f"**这个峰赢不过 0**,多半是背景有人走动、相机晃动或画面里"
                         f"机械臂占比小造成的假峰。"),
                "advice": ("证据其实偏向对齐,不构成错位嫌疑;这一路本次测不准,"
                           "结论以其它相机为准。想让它也可判:固定相机、"
                           "让机械臂在画面里占更大比例。")}
    if width > max_peak_width_s:
        return {"cause": "blurry_motion", "label": "测不准 · 画面不锐利",
                "text": (f"互相关峰宽 {width:.2f}s(超过 {max_peak_width_s}s)—— "
                         f"半秒内怎么挪都差不多高,时间分辨率撑不起 {lag:+.2f}s 这个断言。"),
                "advice": "多为机位太远/运动缓慢;换更近的机位或选运动更明显的片段才测得准。"}
    if ratio and ratio < min_peak_ratio:
        return {"cause": "rival_lags", "label": "测不准 · 候选并列",
                "text": (f"互相关有两个几乎并列的候选滞后(主峰/次峰仅 {ratio:.2f}),"
                         f"读数在掷硬币。"),
                "advice": "画面里有与机械臂节奏相近的其它运动(人/传送带/晃动)时常见。"}
    return {"cause": "weak_signal", "label": "测不准 · 信号弱",
            "text": (f"整体相关只有 {peak:.2f},画面运动与机械臂动作的关联太弱,"
                     f"任何滞后读数都没有支撑。"),
            "advice": "机位太远、机械臂在画面里占比太小,或本段运动幅度不足。"}


def camera_reading(res: CheckResult) -> dict:
    """global_lag 的 CheckResult → 接口契约的 per_camera 条目(纯字典,可直接进 JSON)。

    三态由 code 决定:aligned/misaligned = 可信 lag;ambiguous_peak/flat_peak/low_corr
    = 测不准;no_motion = 无信号。trusted 只对前两者为真(且峰可信度过关)。
    """
    d = res.detail or {}
    raw = str(d.get("code", ""))
    code = _CONTRACT_CODE.get(raw, "low_corr")
    trusted = bool(d.get("trusted", False)) and code in ("aligned", "misaligned")
    if not trusted and code in ("aligned", "misaligned"):
        # 读数落在容差内/外,但峰不可信 → 按峰的病因归类,绝不冒充可信读数
        code = str(d.get("peak_issue") or "ambiguous_peak")
    note = str(d.get("reason") or _CODE_NOTE.get(code, ""))
    out = {
        "lag_s": d.get("lag_s"),
        "corr_peak": d.get("corr_peak"),
        "corr_at_zero": d.get("corr_at_zero"),
        "peak_ratio": d.get("peak_ratio"),
        "peak_width_s": d.get("peak_width_s"),
        "trusted": trusted,
        "code": code,
        "note": note[:200],
    }
    # 病因诊断随读数一起落盘:UI 的逐图诊断框直接读它,不在界面层重新推理
    out["diagnosis"] = camera_diagnosis(out)
    return out


# ── 判定层:跨相机汇总(纯函数,判废只有一种情形)────────────────────────────
# 2026-08-07 用户拍板的判定表,逐条对应:
#   ① 所有可信相机一致指向同一个 Δ≠0(|Δ| 超容差 + 各路彼此接近)→ 判废整条;
#   ② 单相机数据集**永不**因同步判废(孤证不定罪)——只标注;
#   ③ 其余一切不杀:某路异常只标注该路(绝不废弃相机、绝不删 video 指针);
#      测不准/矛盾/无信号 → 标注,passed=True(**不再返回 None**:弃权会把条目推进
#      人工裁决队列,用户明确否掉),也不参与综合软分(本检查是硬门,本来就不打分)。
#   ④ 正负不对称:正 lag 有良性解释(相机链路延迟,普遍存在)→ 判废门槛抬高;
#      负 lag 无良性解释(只可能来自数据装配错误:转换错行/边界切错/开头掉帧)
#      → 门槛更严(容差量级即可定罪),并在数据集级产出告警。

def sync_verdict(
    per_camera: dict,
    n_cams: int,
    *,
    lag_tol_s: float = 0.25,
    spread_tol_s: float = 0.3,      # 各路 lag 彼此"接近"的上限(极差)
    kill_lag_min_s: float = 0.5,    # 正 lag 判废门槛:超出常见相机链路延迟才够格
    neg_kill_lag_min_s: float = 0.25,  # 负 lag 判废门槛:无良性解释,容差量级即定罪
    min_kill_cameras: int = 2,      # 判废至少要几路可信相机同证(孤证不定罪)
) -> dict:
    """逐相机读数 → episode 级结论。返回值即 CheckResult.detail(接口契约顶层结构)。

    per_camera: {相机短名: camera_reading() 的输出}
    n_cams:     本条 episode 实际被质检的相机路数(含读不出数的路)

    verdict 四态:
      aligned        全部可信相机都说对齐(或无可信读数时不用这个态)
      misaligned_all 所有可信相机一致指向同一个 Δ≠0 → **唯一判废情形**
      annotated      有异常但不判废(矛盾/单相机/幅度不够/部分路异常)
      undecidable    没有一路给出可信读数(测不准/无信号)
    """
    per_camera = dict(per_camera or {})
    trusted = {c: r for c, r in per_camera.items() if r.get("trusted")}
    mis = {c: r for c, r in trusted.items() if r.get("code") == "misaligned"}
    ali = {c: r for c, r in trusted.items() if r.get("code") == "aligned"}
    # 标注(不废弃!)的相机:读数异常的 + 完全无信号的(后者常是冻结/占位路,
    # 值得客户看一眼,但同样不构成判废理由)
    flagged = sorted([c for c in mis]
                     + [c for c, r in per_camera.items()
                        if r.get("code") == "no_motion"])
    # 「疑似错位但证据不足」:读数不可信(峰不突出/过胖/相关弱),但测出来的滞后
    # **明显超容差**。这类必须显式说出来 —— 2026-08-07 用户在 droid ep4 上实见:
    # exterior_1 的峰明明偏到 +0.60s,系统却因为"测不准"把它整个咽掉,徽章还报
    # 「同步正常」。用户当初拍板的是"弃权 = 只标注",不是"弃权 = 隐身"。
    # 它仍然**不判废、不进人工队列、不参与软分**,只是不再沉默。
    # 「疑似错位」的门槛(2026-08-07 用户纠正后收窄):读数偏离 0 **且零滞后已经
    # 站不住**(corr0 明显低于峰值)。若 0 处的相关与峰值相当,那个偏离的峰赢不过 0,
    # 病因是画面干扰而非错位 —— 那种情况由 diagnosis 的 false_peak 如实说明,
    # 绝不冒充错位嫌疑(否则每个脏机位都会被扣一顶错位的帽子)。
    suspect = sorted(
        c for c, r in per_camera.items()
        if not r.get("trusted")
        and (r.get("diagnosis") or {}).get("cause") in ("blurry_motion", "rival_lags",
                                                        "weak_signal")
        and abs(float(r.get("lag_s") or 0.0)) > lag_tol_s)
    # 读数看着偏了、但证据其实偏向对齐的路:要能被数出来(用户会盯着曲线问
    # "这个峰明明偏了为什么不说话"),但**不进 suspect**
    noisy = sorted(c for c, r in per_camera.items()
                   if (r.get("diagnosis") or {}).get("cause") == "false_peak")
    # 读数不可信的全部(含滞后本身就在容差内的),供数据集级统计"某路长期测不准"
    abstained = sorted(c for c, r in per_camera.items()
                       if not r.get("trusted") and r.get("code") != "no_motion")
    out = {
        "verdict": "annotated",
        "per_camera": per_camera,
        "flagged_cameras": flagged,
        "suspect_cameras": suspect,
        "noisy_cameras": noisy,
        "abstained_cameras": abstained,
        "consensus_lag_s": None,
        "n_cameras": int(n_cams),
        "n_trusted": len(trusted),
        "reason": "",
    }
    sus_txt = ", ".join(f"{c} {float(per_camera[c].get('lag_s') or 0.0):+.2f}s"
                        for c in suspect)

    if not trusted:
        out["verdict"] = "undecidable"
        out["reason"] = (f"{n_cams} 路相机均未给出可信读数"
                         f"(测不准/无信号),同步不下结论,不影响判决"
                         + (f";其中 {len(suspect)} 路读数疑似错位({sus_txt}),"
                            f"证据不足以定论,已标注" if suspect else ""))
        return out

    if not mis:
        if suspect:
            # 不能报「同步正常」:有相机的峰明显偏了,只是证据不够硬
            out["verdict"] = "annotated"
            out["reason"] = (
                f"{len(ali)}/{n_cams} 路可信相机对齐,但另有 {len(suspect)} 路"
                f"疑似错位({sus_txt})——峰形不够可信,不足以定论,"
                f"已标注该路;不判废、不进人工队列")
            return out
        out["verdict"] = "aligned"
        out["reason"] = (f"{len(ali)}/{n_cams} 路可信相机全部对齐"
                         f"(|lag| ≤ {lag_tol_s}s)")
        if noisy:
            out["reason"] += (
                f";另 {len(noisy)} 路({', '.join(noisy)})峰偏离 0 但赢不过 0 —— "
                f"画面干扰造成的假峰,证据仍偏向对齐,不参与判定")
        elif abstained:
            out["reason"] += f";另 {len(abstained)} 路测不准,不参与判定"
        return out

    # 相机名与读数必须同序配对(按名字排序后再取值,别拿 dict 顺序去 zip 排序后的名字)
    mis_pairs = [(c, float(mis[c].get("lag_s") or 0.0)) for c in sorted(mis)]
    lags = [v for _, v in mis_pairs]
    if ali:
        out["reason"] = (
            f"相机间矛盾:{len(ali)} 路读对齐({', '.join(sorted(ali))}),"
            f"{len(mis)} 路读滞后({', '.join(f'{c} {v:+.2f}s' for c, v in mis_pairs)})"
            f" → 只标注异常路,不判废(证据不一致时不定罪)"
            + (f";另有 {len(suspect)} 路疑似错位但证据不足({sus_txt})" if suspect else ""))
        return out

    # 到这里:所有可信相机都说错位。是否"一致指向同一个 Δ"?
    same_sign = all(x > 0 for x in lags) or all(x < 0 for x in lags)
    spread = float(max(lags) - min(lags))
    consistent = same_sign and spread <= spread_tol_s
    consensus = float(np.median(lags))
    if consistent:
        out["consensus_lag_s"] = round(consensus, 4)

    if not consistent:
        out["reason"] = (f"{len(mis)} 路可信相机都报错位但读数不一致"
                         f"(极差 {spread:.2f}s > {spread_tol_s}s"
                         f"{'' if same_sign else ',且正负号不同'}),"
                         f"不构成一致证据 → 只标注不判废")
        return out
    if n_cams < 2 or len(trusted) < min_kill_cameras:
        out["reason"] = (f"仅 {len(trusted)} 路可信相机报一致滞后 {consensus:+.2f}s;"
                         f"单相机(或孤证)永不因同步判废 → 只标注,建议人工确认")
        return out
    thr = neg_kill_lag_min_s if consensus < 0 else kill_lag_min_s
    if abs(consensus) < thr:
        out["reason"] = (
            f"全部 {len(trusted)} 路可信相机一致滞后 {consensus:+.2f}s,"
            f"但幅度未达判废门槛 {thr}s"
            + ("(负滞后=数据装配错误嫌疑,门槛已从严)" if consensus < 0
               else "(正滞后有良性解释:相机链路延迟普遍存在)")
            + " → 标注,建议整库标定")
        return out
    out["verdict"] = "misaligned_all"
    out["reason"] = (
        f"全部 {len(trusted)}/{n_cams} 路可信相机一致指向 Δ={consensus:+.2f}s"
        f"(极差 {spread:.2f}s ≤ {spread_tol_s}s),超判废门槛 {thr}s → 判废整条"
        + ("。**负滞后无良性解释**,只可能来自数据装配错误(转换错行/边界切错/开头掉帧)"
           if consensus < 0 else ""))
    return out


def sync_check_result(per_camera: dict, n_cams: int, **kw) -> CheckResult:
    """判定表 → CheckResult。**passed 只有两种值**:判废 False,其余一律 True。

    绝不返回 None:None = 弃权,会把条目推进人工裁决队列(review.json),
    而同步的"测不准"是方法边界不是数据有罪,不该占人工的注意力(用户 2026-08-07 定)。
    """
    det = sync_verdict(per_camera, n_cams, **kw)
    return CheckResult(name="video_action_sync",
                       passed=det["verdict"] != "misaligned_all", detail=det)


# ── 数据集级诊断:全库逐相机 lag 分布 → 结论倾向 ──────────────────────────────

def sync_health(per_episode: dict, *, lag_tol_s: float = 0.25,
                stable_iqr_s: float = 0.2) -> dict:
    """{episode_id: sync_verdict 输出} → report["dataset"]["sync_health"]。

    产品逻辑(本次改造的主要产品):全库同号同量 → "建议整库标定 Δ"(一次系统性
    偏移,改一个数就全好了);逐条乱跳 → "录制不稳定",只能逐条看标注。
    只统计**可信**读数——把测不准的数混进分布,中位数就是噪声的中位数。
    """
    per_cam: dict = {}
    neg_eps: list = []
    flagged_eps: list = []
    suspect_eps: list = []      # 疑似错位但证据不足:必须能被数出来,不许静默
    noisy_eps: list = []        # 假峰(峰偏离 0 但赢不过 0):同上,不许静默
    for eid in sorted(per_episode):
        det = per_episode[eid] or {}
        if det.get("flagged_cameras"):
            flagged_eps.append({
                "episode_id": eid, "verdict": det.get("verdict"),
                "cameras": {c: (det.get("per_camera") or {}).get(c, {})
                            for c in det["flagged_cameras"]}})
        if det.get("suspect_cameras"):
            suspect_eps.append({
                "episode_id": eid, "verdict": det.get("verdict"),
                "cameras": {c: (det.get("per_camera") or {}).get(c, {})
                            for c in det["suspect_cameras"]}})
        # 假峰条目同样要立账:它既不在 flagged 也不在 negative,不单列的话
        # **整条 episode 在报告里查无此人**(2026-08-07:UI 已修,报告还漏着)。
        if det.get("noisy_cameras"):
            noisy_eps.append({
                "episode_id": eid, "verdict": det.get("verdict"),
                "cameras": {c: (det.get("per_camera") or {}).get(c, {})
                            for c in det["noisy_cameras"]}})
        for cam, r in (det.get("per_camera") or {}).items():
            slot = per_cam.setdefault(cam, {"lags": [], "n_flagged": 0,
                                            "n_suspect": 0, "n_noisy": 0,
                                            "n_abstained": 0})
            if r.get("trusted") and r.get("lag_s") is not None:
                slot["lags"].append(float(r["lag_s"]))
            if cam in (det.get("flagged_cameras") or []):
                slot["n_flagged"] += 1
            if cam in (det.get("suspect_cameras") or []):
                slot["n_suspect"] += 1
            if cam in (det.get("noisy_cameras") or []):
                slot["n_noisy"] += 1
            if cam in (det.get("abstained_cameras") or []):
                slot["n_abstained"] += 1
        neg = [(c, r["lag_s"]) for c, r in (det.get("per_camera") or {}).items()
               if r.get("trusted") and r.get("code") == "misaligned"
               and (r.get("lag_s") or 0) < -lag_tol_s]
        if neg:
            neg_eps.append({"episode_id": eid,
                            "cameras": {c: round(float(v), 4) for c, v in sorted(neg)},
                            "verdict": det.get("verdict")})

    out_cams: dict = {}
    for cam, slot in sorted(per_cam.items()):
        lags = np.asarray(slot["lags"], dtype=np.float64)
        extra = {"n_flagged": int(slot["n_flagged"]),
                 "n_suspect": int(slot.get("n_suspect", 0)),
                 "n_noisy": int(slot.get("n_noisy", 0)),
                 "n_abstained": int(slot.get("n_abstained", 0))}
        if len(lags):
            q1, q3 = np.percentile(lags, [25, 75])
            out_cams[cam] = {"n": len(lags),
                             "median_lag_s": round(float(np.median(lags)), 4),
                             "iqr_s": round(float(q3 - q1), 4), **extra}
        else:
            out_cams[cam] = {"n": 0, "median_lag_s": None, "iqr_s": None, **extra}

    measured = {c: v for c, v in out_cams.items() if v["n"]}
    if not measured:
        advice = "全库无可信同步读数(相机机位/运动量不适合互相关),同步一项未下结论"
    else:
        off = {c: v for c, v in measured.items() if abs(v["median_lag_s"]) > lag_tol_s}
        stable = all(v["iqr_s"] <= stable_iqr_s for v in measured.values())
        if not off:
            advice = ("全库逐相机中位滞后均在容差内(|median| ≤ "
                      f"{lag_tol_s}s),未见系统性错位")
            if not stable:
                advice += ";但逐条离散度偏大(IQR > "
                advice += f"{stable_iqr_s}s),录制稳定性见逐条标注"
        elif stable:
            advice = ("全库同号同量:" + "、".join(
                f"{c} 中位 {v['median_lag_s']:+.2f}s(IQR {v['iqr_s']:.2f}s)"
                for c, v in sorted(off.items()))
                + " → **建议整库标定 Δ**(系统性偏移,逐条判废是浪费数据)")
        else:
            advice = ("逐条乱跳(IQR > " + f"{stable_iqr_s}s"
                      + "):" + "、".join(
                          f"{c} 中位 {v['median_lag_s']:+.2f}s / IQR {v['iqr_s']:.2f}s"
                          for c, v in sorted(off.items()))
                      + " → 录制不稳定,见逐条标注,不宜整库标定")
    if neg_eps:
        advice += (f";⚠️ {len(neg_eps)} 条出现**负滞后**(画面早于动作)——"
                   "无良性解释,只可能来自数据装配错误(转换错行/边界切错/开头掉帧),"
                   "建议核对转换脚本")
    # 「疑似错位但证据不足」也要出现在数据集级建议里,否则又变成只有翻曲线才看得见
    if suspect_eps:
        worst = sorted(out_cams.items(), key=lambda kv: -kv[1]["n_suspect"])[0]
        advice += (f";另有 {len(suspect_eps)} 条存在**疑似错位但证据不足**的相机"
                   f"(最多的是 {worst[0]},{worst[1]['n_suspect']} 条)——"
                   "不判废也不进人工队列,但做逐帧对齐敏感的训练时建议对该路降权")
    if noisy_eps:
        worst_n = sorted(out_cams.items(), key=lambda kv: -kv[1]["n_noisy"])[0]
        advice += (f";{len(noisy_eps)} 条有相机出现**假峰**(峰偏离 0 但赢不过 0,"
                   f"最多的是 {worst_n[0]},{worst_n[1]['n_noisy']} 条)——"
                   "成因是背景运动/相机晃动/机械臂占比小,证据仍偏向对齐,"
                   "不判废;想让这些路也可判需改善机位")
    # 某一路长期给不出可信读数 = 机位本身不适合这套方法,值得单独点名
    n_ep = len(per_episode)
    chronic = sorted(c for c, v in out_cams.items()
                     if n_ep and v["n_abstained"] >= max(2, 0.6 * n_ep))
    if chronic:
        advice += (f";{'、'.join(chronic)} 在多数 episode 上都测不准"
                   "(机位太远/机械臂在画面里占比小/背景干扰大)——"
                   "该路的同步结论请以人工抽检为准")
    return {"per_camera": out_cams, "advice": advice,
            "suspect_episodes": suspect_eps,
            "noisy_episodes": noisy_eps,
            "negative_lag_episodes": neg_eps,
            # 契约三键之外的附加项(纯增量,UI 不读也不受影响):逐条被标注的相机
            # 及其读数——报告的「相机流健康度」节要逐条列出来给人看
            "flagged_episodes": flagged_eps,
            "n_episodes": len(per_episode)}
