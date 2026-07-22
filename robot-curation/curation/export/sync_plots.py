"""同步曲线证据图(2026-07-15 用户定,"证据附件"体系第一块)。

漏斗算同步时顺风车暂存的两条曲线(光流能量/手臂速度)→ 每 episode 渲染一张
双面板 PNG:左=两条 z 归一曲线(肉眼判读同起同落),右=互相关滞后扫描(星=最佳
偏移)。标签全英文(图要跨环境看,不赌 CJK 字体——2026-07-15 乱码教训)。
matplotlib 缺失时静默跳过(可选依赖,报告注明)。

⚠️ 2026-07-22 复发:静态标签确实全英文,但标题里拼了检查产出的中文 `reason`,
   在无 CJK 字体的容器里整段渲染成豆腐块(□),而且只 warn 不报错、图照常交付。
   教训:"全英文"这条纪律只管得住字面量,**管不住拼进去的数据**。
   两道修补:①检查同时吐语言无关的 `code`,这里映射成英文短语(中文 reason 留给
   报告,不动);②`_ascii_only()` 兜底——任何非 ASCII 一律剥掉,宁可少字不出方框。
   由 tests/test_sync_plot_text.py 钉死。
"""
from __future__ import annotations

import json
import os

# 检查产出的语言无关 code → 图上英文短语。新增分支时这里补一条;
# 漏了也不会出豆腐块(会退化成显示 code 本身,仍是 ASCII)。
_CODE_EN = {
    "aligned": "aligned",
    "short_signal": "signal too short",
    "no_motion": "no motion (flat signal)",
    "short_sequence": "sequence too short (unreliable)",
    "weak_corr": "correlation too weak",
    "ambiguous_peak": "lag over tol but peak not prominent",
    "weak_corr_no_kill": "suspected lag, corr too weak to kill",
    "lag_exceeds_tol": "lag exceeds tolerance",
}


def _ascii_only(s: str) -> str:
    """最后一道闸:剥掉所有非 ASCII,保证绝不出现豆腐块。

    宁可信息少一点,也不能把 □□□ 交给客户——那既没信息又显得系统坏了。
    """
    return "".join(ch for ch in s if ch.isascii())


def plot_title(eid: str, verdict: str, det: dict) -> str:
    """拼左面板标题。**保证返回纯 ASCII**(独立成函数是为了能被测试直接盯住)。

    只用语言无关的 `code` 映射,不翻译中文 reason,更不在这里重算判定分支
    ——那会把阈值复制一份出去然后各自漂移。
    """
    code = str(det.get("code", ""))
    why = _CODE_EN.get(code, code)              # 未知 code 直接显示,仍是 ASCII
    nums = []
    if "lag_s" in det:
        nums.append(f"lag={float(det['lag_s']):.2f}s")
    if "corr_peak" in det:
        nums.append(f"peak={float(det['corr_peak']):.2f}")
    if "corr_at_zero" in det:
        # 判 ambiguous_peak 的依据就是 corr@0 贴着 peak,不显示它等于只给结论不给证据
        nums.append(f"corr@0={float(det['corr_at_zero']):.2f}")
    if "n_samples" in det:
        nums.append(f"n={int(det['n_samples'])}")
    title = f"{eid}  [{verdict}]  {why}" + ("   " + "  ".join(nums) if nums else "")
    return _ascii_only(title)


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
            ax.set_title(plot_title(str(eid), str(verdict), det), fontsize=10, loc="left")
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
