"""同步检查的互相关曲线(daemon/results/sync_curves.py 给前端画):与正式判定同一套预处理。"""
from __future__ import annotations



def _static_prefix_signals(fs=30.0, static_s=20.0, active_s=24.0, sigma=1.8, shift=8):
    """带前导静止段的合成同步曲线:前 static_s 秒两条都不动,之后三串运动;
    flow 比 speed 晚 shift 帧。峰做得胖(sigma 大)是刻意的——ep000013 那种平台峰
    才是 argmax 会滑动的场合,尖峰下"重算"和"正式"碰巧一致,测不出这个 bug。
    """
    import numpy as np
    rng = np.random.default_rng(7)
    n_s, n_a = int(static_s * fs), int(active_s * fs)
    t = np.arange(n_s + n_a) / fs
    base = np.zeros(n_s + n_a)
    ta = np.arange(n_a) / fs
    for c in (4.0, 12.0, 18.0):
        base[n_s:] += np.exp(-0.5 * ((ta - c) / sigma) ** 2)
    speed = base + 0.01 * rng.standard_normal(len(base))
    flow = np.roll(base, shift) + 0.01 * rng.standard_normal(len(base))
    flow[:shift] = 0.0
    return t, np.clip(flow, 0, None), np.clip(speed, 0, None)


def _untrimmed_curve(t, flow, speed):
    """出事那版画法:**不剔静止段**直接重算互相关(留在测试里当反例基准)。"""
    import numpy as np
    from scipy import signal
    zf = (flow - flow.mean()) / (flow.std() + 1e-9)
    zs = (speed - speed.mean()) / (speed.std() + 1e-9)
    xc = signal.correlate(zf, zs, mode="full") / max(len(zf), 1)
    dt = float(np.median(np.diff(t)))
    lags = signal.correlation_lags(len(zf), len(zs), mode="full") * dt
    win = np.abs(lags) <= 2.0
    return lags[win], xc[win]


def test_xcorr_curve_peak_follows_official_reading():
    """曲线与正式读数同源(2026-08-11 droid-200-full ep000013「图数两账」):正式判定
    (global_lag)在互相关前先用 trim_static_span 剔首尾静止段,曲线若不剔,静止段把峰
    压成平台、argmax 滑走。①用旧画法证明这条夹具真会滑;②新曲线的顶点与正式读数对齐
    到 1 个采样步内。"""
    import numpy as np

    from curation.core.checks.video_action_sync import global_lag
    from curation.export.sync_plots import _xcorr_curve

    t, flow, speed = _static_prefix_signals()
    lag_s = global_lag(flow, t, speed, t).detail["lag_s"]

    old_lags, old_xc = _untrimmed_curve(t, flow, speed)
    old_argmax = float(old_lags[int(np.argmax(old_xc))])
    assert abs(old_argmax - lag_s) > 0.1, f"夹具失效:旧画法 {old_argmax} ≈ 正式 {lag_s}"

    lags, xc = _xcorr_curve(t, flow, speed)
    win = np.abs(lags) <= 2.0
    lags, xc = lags[win], xc[win]
    step = float(np.median(np.diff(lags)))
    new_argmax = float(lags[int(np.argmax(xc))])
    assert abs(new_argmax - lag_s) <= step + 1e-9, \
        f"曲线顶点 {new_argmax} 与正式读数 {lag_s} 差了 {abs(new_argmax - lag_s)}s"


def test_xcorr_curve_falls_back_when_trim_leaves_nothing():
    """剔完静止段没剩几个点时退回未剔的整条(画得糙好过画不出来)。"""
    import numpy as np

    from curation.export.sync_plots import _xcorr_curve

    t = np.arange(60) / 30.0
    flow = np.zeros(60)
    flow[30:34] = 1.0            # 通篇静止只中间抖一下 → trim 认定整条都"静"
    speed = flow.copy()
    lags, xc = _xcorr_curve(t, flow, speed)
    assert len(lags) == len(xc) == 2 * len(t) - 1
    assert np.isfinite(xc).all()
