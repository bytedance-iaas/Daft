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
    # 2026-08-07 逐相机改造新增(契约里的 per_camera.code + 新的测量分支)
    "misaligned": "trusted lag beyond tolerance",
    "flat_peak": "peak too wide (no time resolution)",
    "low_corr": "correlation too weak to read",
}

# episode 级 verdict → 图上英文短语(中文 reason 只进报告,绝不进图)
_VERDICT_EN = {
    "aligned": "all trusted cameras aligned",
    "misaligned_all": "ALL trusted cameras agree on a nonzero lag -> dropped",
    "annotated": "annotated only (no drop)",
    "undecidable": "no trusted reading",
    # 兼容旧曲线文件里的三态字面量
    "pass": "aligned", "fail": "lag exceeds tolerance", "abstain": "undecidable",
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


def _normalize_curves(c: dict) -> dict:
    """曲线 JSON → 统一的 {相机短名: {t/flow/speed/...}}。

    兼容两种格式:2026-08-07 起漏斗写的多相机格式(顶层 "cameras"),以及此前的
    单相机扁平格式(顶层直接 t/flow/speed)——旧输出目录里的曲线文件还得画得出来。
    """
    if isinstance(c.get("cameras"), dict):
        return c["cameras"]
    if "t" in c:
        det = c.get("detail") or {}
        return {"camera": {"t": c["t"], "flow": c["flow"], "speed": c["speed"],
                           "lag_s": det.get("lag_s"), "corr_peak": det.get("corr_peak"),
                           "code": det.get("code")}}
    return {}


def camera_panel_title(cam: str, meta: dict, reading: dict) -> str:
    """单路时域面板的标题(**保证纯 ASCII**:pod 上没有 CJK 字体,中文会渲染成豆腐块)。"""
    code = str((reading or {}).get("code") or (meta or {}).get("code") or "")
    trusted = (reading or {}).get("trusted")
    # 两行:第一行"相机名 · 可信度",第二行读数。改窄版式后单行会横向溢出、
    # 压到右侧相关图的标题上(2026-08-07 实见),所以拆行且省掉冗长的 code 英文。
    mark = "" if trusted is None else ("  (trusted)" if trusted else "  (not trusted)")
    line1 = (_ascii_only(str(cam)) or "camera") + mark
    bits = []
    for key, fmt in (("lag_s", "lag {:+.2f}s"), ("corr_peak", "corr {:.2f}"),
                     ("peak_ratio", "p1/p2 {:.2f}"), ("peak_width_s", "w {:.2f}s")):
        v = (reading or {}).get(key, (meta or {}).get(key))
        if v is not None:
            bits.append(fmt.format(float(v)))
    line2 = "  ".join(bits)
    if code and code != "aligned":
        line2 += ("   " if line2 else "") + _CODE_EN.get(code, code)
    return _ascii_only(line1 + ("\n" + line2 if line2 else ""))


def episode_title(eid: str, c: dict) -> str:
    """整图标题(**保证纯 ASCII**)。带 verdict 与全票一致的 Δ。"""
    verdict = str(c.get("verdict", "?"))
    bits = [str(eid), f"[{verdict}]", _VERDICT_EN.get(verdict, verdict)]
    if c.get("n_cameras") is not None:
        bits.append(f"cams={int(c['n_cameras'])} trusted={int(c.get('n_trusted') or 0)}")
    if c.get("consensus_lag_s") is not None:
        bits.append(f"consensus={float(c['consensus_lag_s']):+.2f}s")
    if c.get("flagged_cameras"):
        bits.append("flagged: " + ",".join(str(x) for x in c["flagged_cameras"]))
    return _ascii_only("  ".join(bits))


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


def _peak_marker(cam: str, lags, xc, lag_s, corr_peak):
    """(画出的互相关曲线, 正式读数) → (圆点 x, 圆点 y, 图例文本);**保证纯 ASCII**。

    ⚠️ 2026-08-11 事故:圆点和图例数字曾取"画图时重算的互相关曲线"的 argmax,而正式
    判定(global_lag)在互相关前先剔了首尾静止段。峰尖时两者巧合一致,峰是平台时
    argmax 就滑走——droid-200-full ep000013 的 exterior_image_1_left 表头报
    lag=+0.27s/corr=0.55,右图圆点却画在 +0.13s/0.59,恰好从容差带外滑进带内,
    用户看图和看数对不上,等于系统自己出两份账。
    修法:横坐标一律用落盘的正式读数 lag_s,纵坐标用 np.interp 在画出的曲线上取值
    (点必须贴着曲线,不能悬空);图例数字也一律用正式读数。重算的曲线本身照画
    ——它作视觉参考没错,错的只是点位和数字。

    读数缺失(不可判)时不画圆点:宁可图例只写相机名,也不拿重算值冒充读数。
    """
    import numpy as np

    if lag_s is None:
        return None, None, _ascii_only(f"{cam} (no reading)")
    x = float(lag_s)
    corr_txt = "n/a" if corr_peak is None else f"{float(corr_peak):.2f}"
    label = _ascii_only(f"{cam} @{x:+.2f}s ({corr_txt})")
    lg = np.asarray(lags, dtype=float)
    if len(lg) == 0 or x < lg.min() or x > lg.max():
        # 读数落在画出的窗口之外:画点就得靠 interp 端点值把它按在边界上=假位置,
        # 不画;数字仍照实写在图例里(缺图不缺账)。
        return None, None, label
    y = float(np.interp(x, lg, np.asarray(xc, dtype=float)))
    return x, y, label


#: 配色与坐标轴风格(2026-08-07 用户:"图像 matplotlib 原味,不精美")。
#: 参照现代图表面板的观感:去掉上/右边框、淡横向网格、克制的三色、无边框图例。
_C_FLOW = "#2563eb"     # 画面运动(蓝)
_C_SPEED = "#ef4444"    # 机械臂速度(红)
_C_OK = "#10b981"       # 容差带(绿)


def _style_ax(ax) -> None:
    """统一坐标轴观感:细边框、淡网格、小字号刻度。"""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#cbd5e1")
        ax.spines[side].set_linewidth(0.8)
    ax.grid(True, axis="y", color="#e2e8f0", lw=0.7, alpha=0.9)
    ax.set_axisbelow(True)
    ax.tick_params(labelsize=6.8, colors="#64748b", length=3, width=0.8)


def _savefig_fsx_safe(fig, dst: str, **kw) -> None:
    """savefig 到本地临时文件,再顺序整份拷到目的地。

    交付目录在 TOS 的 FSX 挂载上,**savefig 直写会静默产出零填充的坏文件**
    (2026-08-07 实锤:5 张图坏 3 张,字节数看着正常、PNG 签名却是全零,浏览器
    一片空白而服务端毫无报错)。matplotlib 写 PNG 期间有多次写+回填,挂载扛不住。
    同一挂载已经用同一招治过 CSV 追加 / pyarrow parquet / PyAV 转码,这是第五处。
    坏在"不报错"上——所以这里写完还要**验签名再落地**,坏了直接抛,不留哑弹。
    """
    import shutil
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        fig.savefig(tmp, **kw)
        with open(tmp, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                raise OSError(f"matplotlib 产出的不是合法 PNG: {dst}")
        shutil.copyfile(tmp, dst)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def render_sync_plots(curve_rows: list[tuple], out_dir: str) -> list[str]:
    """curve_rows: [(episode_id, curves_json_str), ...] → 生成 PNG,返回文件名列表。

    一条 episode **一张图**(文件名 details/plots/<episode_id>_sync.png 不变,UI 靠它定位):
    每路相机一格「光流 vs 关节速度」时域图,最后一格是所有相机的「互相关 vs lag」叠图
    (标出容差带、各路峰位、全票一致的 Δ)。逐相机独立测量的结论必须逐相机看得见——
    合成一张曲线正是改造前那个"一路代表全条"的病。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        from scipy import signal  # noqa: F401  只作可用性探针:相关计算在 _xcorr_curve 里
    except Exception:  # noqa: BLE001  可选依赖缺失:不画,调用方在报告注明
        return []

    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    for eid, cj in curve_rows:
        try:
            c = json.loads(cj)
            cams = _normalize_curves(c)
            readings = c.get("per_camera") or {}
            usable = {k: v for k, v in cams.items() if len(v.get("t") or []) >= 8}
            if not usable:
                continue
            names = sorted(usable)
            n = len(names)
            tol = float(c.get("lag_tol_s") or 0.25)

            # 版式(2026-08-07 用户定):原来 13:6 的横幅在任何网格里都两头留白,
            # 右侧相关图还被压成细条。改成接近 3:2 的卡片——平铺两列刚好,
            # 相关图拿到近乎一半的宽度。
            # 扁宽横幅比例(2026-08-07 用户定稿:"比例调到扁扁形状"):每路时域格
            # 压到 ~1.45in 高,整图约 2.4:1;相关图仍占 46% 宽不许再瘦回去。
            fig_h = max(1.45 * n + 1.35, 3.0)
            fig = plt.figure(figsize=(13.8, fig_h))
            # 底边留 ~1in 绝对高度给相关图挪下来的图例(占比随图高换算,设上下限)
            bot = min(0.26, max(0.13, 1.0 / fig_h))
            gs = fig.add_gridspec(n, 2, width_ratios=[1.18, 1.0],
                                  hspace=0.55, wspace=0.16,
                                  left=0.05, right=0.985, top=0.88, bottom=bot)
            ax_xc = fig.add_subplot(gs[:, 1])
            colors = [_C_FLOW, "#f59e0b", _C_OK, "#8b5cf6"]

            for i, cam in enumerate(names):
                cv = usable[cam]
                t = np.asarray(cv["t"], dtype=float)
                flow = np.asarray(cv["flow"], dtype=float)
                speed = np.asarray(cv["speed"], dtype=float)
                zf = (flow - flow.mean()) / (flow.std() + 1e-9)
                zs = (speed - speed.mean()) / (speed.std() + 1e-9)
                ax = fig.add_subplot(gs[i, 0])
                ax.plot(t, zf, lw=1.1, label="video motion", color=_C_FLOW)
                ax.plot(t, zs, lw=1.1, label="arm speed", color=_C_SPEED, alpha=.85)
                ax.set_title(camera_panel_title(cam, cv, readings.get(cam, {})),
                             fontsize=7, loc="left", color="#475569", pad=5,
                             linespacing=1.5)
                _style_ax(ax)
                if i == 0:
                    ax.legend(fontsize=6.8, loc="upper right", frameon=True,
                              facecolor="white", framealpha=.88, edgecolor="none",
                              handlelength=1.4, borderaxespad=.2, ncol=2)
                if i == n - 1:
                    ax.set_xlabel("time (s)", fontsize=8)

                # 叠图:每路一条互相关曲线 + 正式读数处的圆点。曲线与读数必须同源
                # (同一套剔静止段预处理),否则点会落在曲线顶点之外——见 _xcorr_curve
                lags, xc = _xcorr_curve(t, flow, speed)
                win = np.abs(lags) <= 2.0
                if not win.any():
                    continue
                col = colors[i % len(colors)]
                # 点位/数字只认落盘的正式读数(reading 优先,旧扁平格式退回曲线自带),
                # 绝不用这里重算的 argmax —— 理由见 _peak_marker
                _rd = readings.get(cam) or {}
                px, py, plabel = _peak_marker(
                    cam, lags[win], xc[win],
                    _rd.get("lag_s", cv.get("lag_s")),
                    _rd.get("corr_peak", cv.get("corr_peak")))
                ax_xc.plot(lags[win], xc[win], lw=1.5, color=col, label=plabel)
                if px is not None:
                    ax_xc.plot(px, py, "o", ms=5.5, color=col, mec="white", mew=1.1)

            ax_xc.axvspan(-tol, tol, color=_C_OK, alpha=0.11, lw=0)   # 容差带
            ax_xc.axvline(0, color="#94a3b8", ls="--", lw=0.9)
            if c.get("consensus_lag_s") is not None:
                ax_xc.axvline(float(c["consensus_lag_s"]), color=_C_SPEED, ls=":", lw=1.3)
            ax_xc.set_xlabel("lag (s)   [>0 = video later]", fontsize=7.5)
            ax_xc.set_title(f"cross-correlation (tol +/-{tol:.2f}s)",
                            fontsize=8, loc="left", color="#475569", pad=4)
            _style_ax(ax_xc)
            ax_xc.legend(fontsize=6.8, loc="upper center", bbox_to_anchor=(0.5, -0.16),
                         ncol=2, frameon=False, handlelength=1.6,
                         borderaxespad=0., labelspacing=.35, columnspacing=1.2)

            fig.suptitle(episode_title(str(eid), c), fontsize=9.5, x=0.012, y=0.985,
                         ha="left", color="#0f172a", fontweight="bold")
            fname = f"{eid}_sync.png"
            _savefig_fsx_safe(fig, os.path.join(out_dir, fname), dpi=130,
                              facecolor="white", bbox_inches="tight",
                              pad_inches=0.18)
            plt.close(fig)
            written.append(fname)
        except Exception:  # noqa: BLE001  单张失败不拖垮整批
            continue
    return written
