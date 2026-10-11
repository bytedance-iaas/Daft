"""The cross-correlation curve of the sync check's evidence (``daemon/results/sync_curves.py``): the same
preprocessing as the judgement, so the curve's peak sits where the reading says."""
from __future__ import annotations

#: 剔完静止段还剩这么少就别剔了(留着不剔的整条画,总比画一段没信息的残渣强)
_MIN_TRIMMED_SAMPLES = 8


def _xcorr_curve(t, flow, speed):
    """画右侧叠图那条互相关曲线 → (lags 秒, xc)。**与正式判定同一套预处理**。

    ⚠️ 2026-08-11 事故的后半截:圆点改用正式读数之后,droid-200-full ep000013 的
    蓝点落在 +0.27s,而**画出的蓝曲线顶点还在 +0.13s 附近**——点不在曲线顶上,
    视觉上依旧自相矛盾。病根和前半截同源:正式判定(global_lag)在互相关前先用
    trim_static_span 剔掉首尾静止段,这里重算却是"未剔"的近似,静止段把峰压成
    平台并整体拉偏。故这里直接复用同一个纯函数,曲线与读数从此同源。

    (长 episode 的曲线落盘时降采样到 ≤600 点,这点近似仍在,但平台峰滑动的主因是
    静止段,不是降采样。)剔完样本太少就退回不剔的整条:画得糙好过画不出来。
    """
    import numpy as np
    from scipy import signal

    from ..core.checks.video_action_sync import trim_static_span

    t = np.asarray(t, dtype=float)
    flow = np.asarray(flow, dtype=float)
    speed = np.asarray(speed, dtype=float)
    lo, hi = trim_static_span(flow, speed)
    if hi - lo >= _MIN_TRIMMED_SAMPLES:
        t, flow, speed = t[lo:hi], flow[lo:hi], speed[lo:hi]
    zf = (flow - flow.mean()) / (flow.std() + 1e-9)
    zs = (speed - speed.mean()) / (speed.std() + 1e-9)
    xc = signal.correlate(zf, zs, mode="full") / max(len(zf), 1)
    dt = float(np.median(np.diff(t))) if len(t) > 1 else 1.0
    lags = signal.correlation_lags(len(zf), len(zs), mode="full") * dt
    return lags, xc
