"""执行器饱和的速度域换算(2026-09-02 用户定):速度/增量型末端指令 × 末端位姿读数。

droid 类数据集 action 是归一化的末端速度指令(无单位)、state 是米制绝对位姿——同一个
物理空间,只差一个增益和一点延迟(ep000000 实测:Δstate×fps≈g·action,逐轴 g≈0.18/0.23/
0.28 每秒,r≈0.84/0.85/0.75)。此前语义门要求"指令必须是绝对位置目标",把它整列拒成
不适用;这里把两边都放到速度域再比。

数据集级标定(一次,样本=语义层已急切读的前 N 条,只读 parquet 不碰视频):
  ① 实际速度 = Δstate × fps;② 增益 g 逐轴最小二乘、各条目取中位数(逐条拟合会把
  整条饱和吸进增益);③ 延迟 k 帧取各条相关中位数最高者;④ 用拟合后的欠速比在样本上
  建基线(中位数/MAD)——控制器动力学与滤波本身就有一层残差,饱和要相对同批判。
只用平移三轴:profile 注记实测旋转 euler 与指令不线性(R²~0),拟不出增益。
限制(用户拍板 2026-09-02):按 embodiment 分别标定(LeRobot 一个数据集一个本体,由
构造满足);系统性整批饱和的"绝对参照"(对照规格档速度极限)本轮不做,只抓相对离群。
"""
from __future__ import annotations

import numpy as np

TRANSLATION_AXES = (0, 1, 2)


def achieved_velocity(state: np.ndarray, fps: float, axes=TRANSLATION_AXES) -> np.ndarray:
    """实际速度 = 相邻位姿差分 × fps,形状 (T-1, len(axes))。"""
    s = np.asarray(state, dtype=np.float64)[:, list(axes)]
    return np.diff(s, axis=0) * float(fps)


def _moving_mask(cmd: np.ndarray, frac: float = 0.1) -> np.ndarray:
    """逐轴"指令在推"的帧:|指令| > 该轴 95 分位的 10%(静止段与死区不参与拟合)。"""
    q = np.percentile(np.abs(cmd), 95, axis=0)
    return np.abs(cmd) > (frac * q + 1e-12)


def _fit_one(action: np.ndarray, state: np.ndarray, fps: float, lag: int,
             axes=TRANSLATION_AXES, min_frames: int = 20):
    """单条:给定延迟 k,逐轴最小二乘增益与相关。返回 (gain[axes], r[axes], n_moving[axes]) 或 None。"""
    a = np.asarray(action, dtype=np.float64)
    if a.ndim != 2 or a.shape[1] <= max(axes) or state is None:
        return None
    s = np.asarray(state, dtype=np.float64)
    if s.ndim != 2 or s.shape[1] <= max(axes) or min(len(a), len(s)) < min_frames + lag + 2:
        return None
    n = min(len(a), len(s))
    v = achieved_velocity(s[:n], fps, axes)                # (n-1, m):v[t] = s[t+1]-s[t]
    cmd = a[:n - 1 - lag, list(axes)]                     # 指令 t → 实际 t+k
    v = v[lag:]
    mask = _moving_mask(cmd)
    gain = np.zeros(len(axes)); r = np.zeros(len(axes)); nm = np.zeros(len(axes), dtype=int)
    for j in range(len(axes)):
        mj = mask[:, j]
        nm[j] = int(mj.sum())
        if nm[j] < min_frames:
            gain[j] = np.nan; r[j] = np.nan
            continue
        x, y = cmd[mj, j], v[mj, j]
        gain[j] = float((x * y).sum() / ((x * x).sum() + 1e-12))
        r[j] = float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 and y.std() > 0 else 0.0
    return gain, r, nm


def _expected_from_curve(cmd_abs: np.ndarray, curve: dict | None, g: float) -> np.ndarray:
    """期望速度:有档位曲线用曲线(分箱中心线性插值),没有退回线性增益。"""
    if curve and curve.get("centers") and curve.get("median_v"):
        c = np.asarray(curve["centers"], dtype=np.float64)
        m = np.asarray(curve["median_v"], dtype=np.float64)
        return np.interp(cmd_abs, c, m)
    return np.abs(g * cmd_abs)


def saturation_ratio(action: np.ndarray, state: np.ndarray, fps: float, *,
                     gain, lag: int, axes=TRANSLATION_AXES, curves: list | None = None,
                     hi_cmd: list | None = None, hi_quantile: float = 0.75,
                     min_hi_frames: int = 8, blocked_frac: float = 0.15,
                     best_quantile: float = 0.9, contact_frac: float = 0.3) -> tuple[float | None, dict]:
    """欠速比:高指令段里**确实在动**的帧,取"最好的帧"(实际/期望 90 分位)离期望还差多少,
    逐轴算再取最差轴。0 = 能跟上,0.5 = 连最快时也只跟到一半(天花板)。

    "指令大但几乎不动"(实际 < blocked_frac×期望)的帧**不算欠速**,单独记成 blocked 比例:
    droid 真机(2026-09-02 基准)这类帧大量来自接触(顶着桌面/物体推)和卡顿——那是被挡住,
    不是执行器跟不上;饱和的签名是"在动、但比期望慢、顶到速度天花板"。

    期望速度按数据集标定的**指令档位→典型速度曲线**(curves,逐轴)取,不用线性增益:droid 的
    指令是分位数归一化速度,映射单调但非线性,线性增益在高指令段系统性高估期望,干净数据
    也会"欠速"(基准 39/98 误报)。高指令门槛 hi_cmd 也用数据集级(指令 75 分位),让"本条根本
    没推过大指令"诚实地返回 None,而不是拿本条最大的几帧硬比。"""
    a = np.asarray(action, dtype=np.float64)
    s = np.asarray(state, dtype=np.float64)
    if a.ndim != 2 or s.ndim != 2 or a.shape[1] <= max(axes) or s.shape[1] <= max(axes):
        return None, {"reason": "列数不足"}
    n = min(len(a), len(s))
    if n < min_hi_frames * 2 + lag + 2:
        return None, {"reason": "帧数不足"}
    v = np.abs(achieved_velocity(s[:n], fps, axes)[lag:])
    cmd = a[:n - 1 - lag, list(axes)]
    per_axis = {}
    blocked = {}
    worst = 0.0
    used = 0
    for j, ax in enumerate(axes):
        g = float(gain[j]) if gain is not None and j < len(gain) else np.nan
        if not np.isfinite(g) or abs(g) < 1e-12:
            continue
        cmd_abs = np.abs(cmd[:, j])
        curve = curves[j] if curves and j < len(curves) else None
        expect = _expected_from_curve(cmd_abs, curve, g)
        if hi_cmd is not None and j < len(hi_cmd) and hi_cmd[j] is not None:
            hi = cmd_abs >= float(hi_cmd[j])              # 数据集级高指令门槛
        else:
            hi = expect >= max(np.quantile(expect, hi_quantile), 1e-9)
        hi &= expect > 1e-9
        if hi.sum() < min_hi_frames:
            continue
        e_hi, v_hi = expect[hi], v[hi, j]
        moving = v_hi >= blocked_frac * e_hi
        blocked[int(ax)] = round(float(1.0 - moving.mean()), 4)
        if moving.sum() < min_hi_frames or blocked[int(ax)] >= contact_frac:
            # 高指令段几乎全被挡住 / 接触主导(droid 基准:开抽屉、往下按的 z 轴被挡 60-95%,
            # 剩下"在动"的帧也是负载下的慢动作)→ 该轴不谈饱和,留给卡顿/接触解释
            continue
        # 天花板签名:看高指令段里**最好**的帧(实际/期望的 90 分位)——接触摩擦只拖慢
        # 一部分帧,自由运动的帧仍能到位;真饱和是连最好的帧都到不了(注入削顶 50% 实测
        # 全员 ≈0.5)。中位数口径在 droid 干净数据上 p90 达 0.39,是推/擦类任务的摩擦。
        ratio_best = float(np.quantile(v_hi[moving] / e_hi[moving], best_quantile))
        dj = float(np.clip(1.0 - ratio_best, 0.0, 1.0))
        per_axis[int(ax)] = round(dj, 4)
        worst = max(worst, dj)
        used += 1
    if not used:
        return None, {"reason": "无高指令且在动的帧可比(高指令段几乎全被挡住)", "blocked": blocked}
    return worst, {"per_axis": per_axis, "blocked": blocked}


def fit_velocity_gain(samples: list[dict], *, axes=TRANSLATION_AXES, max_lag: int = 3,
                      min_episodes: int = 3, n_bins: int = 8) -> dict | None:
    """数据集级标定。samples 行含 action / proprio_state / fps。
    返回 {gain, lag_frames, r, n_episodes, axes, baseline:{median,mad,n}} 或 None(样本不够/拟不出)。"""
    rows = [r for r in samples if r.get("action") is not None and r.get("proprio_state") is not None]
    if len(rows) < min_episodes:
        return None
    best = None
    for lag in range(0, max_lag + 1):
        fits = [f for f in (_fit_one(r["action"], r["proprio_state"], float(r.get("fps") or 0) or 1.0,
                                     lag, axes) for r in rows) if f is not None]
        if len(fits) < min_episodes:
            continue
        rs = np.array([f[1] for f in fits])
        score = float(np.nanmedian(rs))
        if best is None or score > best[0]:
            best = (score, lag, fits)
    if best is None:
        return None
    _, lag, fits = best
    gains = np.array([f[0] for f in fits])
    rs = np.array([f[1] for f in fits])
    gain = np.nanmedian(gains, axis=0)
    r_med = np.nanmedian(rs, axis=0)
    if not np.all(np.isfinite(gain)) or np.nanmedian(r_med) < 0.3:
        return None                                        # 拟不出可信增益,不硬标
    curves, hi_cmd = _response_curves(rows, gain, lag, axes, n_bins=n_bins)
    ratios = []
    for r in rows:
        ratio, _ = saturation_ratio(r["action"], r["proprio_state"], float(r.get("fps") or 0) or 1.0,
                                    gain=gain, lag=lag, axes=axes, curves=curves, hi_cmd=hi_cmd)
        if ratio is not None:
            ratios.append(ratio)
    if not ratios:
        return None
    med = float(np.median(ratios))
    mad = float(np.median(np.abs(np.array(ratios) - med)))
    return {"gain": [round(float(g), 5) for g in gain], "lag_frames": int(lag),
            "r": [round(float(x), 3) for x in r_med], "n_episodes": int(len(fits)),
            "axes": [int(x) for x in axes], "curves": curves,
            "hi_cmd": [None if h is None else round(float(h), 5) for h in hi_cmd],
            "baseline": {"median": round(med, 4), "mad": round(mad, 4), "n": len(ratios)}}


def _response_curves(rows, gain, lag, axes, *, n_bins: int = 8, blocked_frac: float = 0.15):
    """逐轴"指令档位 → 典型实际速度"曲线(样本池化,只用指令在推且没被挡住的帧):
    分箱边界=池化 |指令| 的等分位;每箱记中心与实际速度中位数。同时给出高指令门槛
    (池化 |指令| 的 75 分位)。返回 (curves[axis]={centers, median_v, n}, hi_cmd[axis])。"""
    pooled_c = [[] for _ in axes]
    pooled_v = [[] for _ in axes]
    for r in rows:
        a = np.asarray(r["action"], dtype=np.float64)
        st = np.asarray(r["proprio_state"], dtype=np.float64)
        n = min(len(a), len(st))
        if n < lag + 4 or a.shape[1] <= max(axes) or st.shape[1] <= max(axes):
            continue
        v = np.abs(achieved_velocity(st[:n], float(r.get("fps") or 0) or 1.0, axes)[lag:])
        cmd = a[:n - 1 - lag, list(axes)]
        mask = _moving_mask(cmd)
        for j in range(len(axes)):
            g = float(gain[j])
            c = np.abs(cmd[:, j]); vv = v[:, j]
            keep = mask[:, j] & (vv >= blocked_frac * np.abs(g * c))
            pooled_c[j].extend(c[keep].tolist()); pooled_v[j].extend(vv[keep].tolist())
    curves, hi_cmd = [], []
    for j in range(len(axes)):
        c = np.asarray(pooled_c[j]); vv = np.asarray(pooled_v[j])
        if len(c) < n_bins * 10:
            curves.append(None); hi_cmd.append(None)
            continue
        edges = np.quantile(c, np.linspace(0, 1, n_bins + 1))
        centers, med = [], []
        for b in range(n_bins):
            lo, hi = edges[b], edges[b + 1]
            sel = (c >= lo) & (c <= hi) if b == n_bins - 1 else (c >= lo) & (c < hi)
            if sel.sum() < 5:
                continue
            centers.append(float(np.median(c[sel]))); med.append(float(np.median(vv[sel])))
        curves.append({"centers": [round(x, 5) for x in centers],
                       "median_v": [round(x, 5) for x in med], "n": int(len(c))}
                      if len(centers) >= 3 else None)
        hi_cmd.append(float(np.quantile(c, 0.75)))
    return curves, hi_cmd
