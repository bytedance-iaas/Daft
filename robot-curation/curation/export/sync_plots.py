"""同步曲线证据图(2026-07-15 用户定,"证据附件"体系第一块)。

漏斗算同步时顺风车暂存的两条曲线(光流能量/手臂速度)→ 每 episode 渲染一张
双面板 PNG:左=两条 z 归一曲线(肉眼判读同起同落),右=互相关滞后扫描(星=最佳
偏移)。标签全英文(图要跨环境看,不赌 CJK 字体——2026-07-15 乱码教训)。
matplotlib 缺失时静默跳过(可选依赖,报告注明)。
"""
from __future__ import annotations

import json
import os


def render_sync_plots(curve_rows: list[tuple], out_dir: str) -> list[str]:
    """curve_rows: [(episode_id, curves_json_str), ...] → 生成 PNG,返回文件名列表。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        from scipy import signal
    except Exception:  # noqa: BLE001  可选依赖缺失:不画,调用方在报告注明
        return []

    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    for eid, cj in curve_rows:
        try:
            c = json.loads(cj)
            t = np.asarray(c["t"], dtype=float)
            flow = np.asarray(c["flow"], dtype=float)
            speed = np.asarray(c["speed"], dtype=float)
            if len(t) < 8:
                continue
            zf = (flow - flow.mean()) / (flow.std() + 1e-9)
            zs = (speed - speed.mean()) / (speed.std() + 1e-9)
            det = c.get("detail") or {}
            verdict = c.get("verdict", "?")

            fig, (ax, ax2) = plt.subplots(1, 2, figsize=(12, 3.2), width_ratios=[2.2, 1])
            ax.plot(t, zf, lw=1.1, label="optical flow (video motion)", color="tab:blue")
            ax.plot(t, zs, lw=1.1, label="arm speed (proprio)", color="tab:red", alpha=0.75)
            reason = str(det.get("reason", ""))[:60]
            ax.set_title(f"{eid}  [{verdict}]  {reason}", fontsize=10, loc="left")
            ax.set_xlabel("time (s)")
            ax.legend(fontsize=7, loc="upper right")

            xc = signal.correlate(zf, zs, mode="full") / max(len(zf), 1)
            dt = float(np.median(np.diff(t))) if len(t) > 1 else 1.0
            lags = signal.correlation_lags(len(zf), len(zs), mode="full") * dt
            win = np.abs(lags) <= 2.0
            if win.any():
                ax2.plot(lags[win], xc[win], lw=1.1, color="tab:green")
                ax2.axvline(0, color="gray", ls="--", lw=0.8)
                k = int(np.argmax(xc[win]))
                ax2.plot(lags[win][k], xc[win][k], "r*", ms=9)
                ax2.set_title(f"lag scan: corr={xc[win][k]:.2f} @ {lags[win][k]:.2f}s",
                              fontsize=9)
            ax2.set_xlabel("lag (s)")
            fig.tight_layout()
            fname = f"{eid}_sync.png"
            fig.savefig(os.path.join(out_dir, fname), dpi=100)
            plt.close(fig)
            written.append(fname)
        except Exception:  # noqa: BLE001  单张失败不拖垮整批
            continue
    return written
