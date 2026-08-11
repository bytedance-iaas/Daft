"""同步曲线证据图验收(2026-07-15 用户定):flagged 默认只画非'过';all 全画;off 不画。"""
from __future__ import annotations

import json
import os

import pytest

PUSHT = "/data03/hao/data/pusht"
需要 = pytest.mark.skipif(not os.path.exists(os.path.join(PUSHT, "meta")),
                          reason="无 pusht 数据")


def test_renderer_pure():
    """渲染器纯函数:合法曲线出 PNG;坏 JSON 跳过不炸;短曲线跳过。"""
    import numpy as np

    from curation.export.sync_plots import render_sync_plots
    t = np.linspace(0, 10, 100)
    good = json.dumps({"t": t.tolist(), "flow": np.sin(t).tolist(),
                       "speed": np.sin(t - 0.2).tolist(), "verdict": "abstain",
                       "detail": {"reason": "corr_peak 0.2 < 0.3"}})
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = render_sync_plots([("ep000001", good), ("ep000002", "不是json"),
                                 ("ep000003", json.dumps({"t": [1], "flow": [1],
                                                          "speed": [1]}))], d)
        assert out == ["ep000001_sync.png"]
        assert os.path.exists(os.path.join(d, "ep000001_sync.png"))


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


def test_peak_marker_follows_official_reading_not_replot_argmax():
    """右侧叠图的曲线和圆点都必须与正式读数同源:点在读数上,且落在曲线顶点上。

    防的事故:2026-08-11 用户在 droid-200-full ep000013 上发现"图数两账"——
    exterior_image_1_left 表头和诊断都报 lag=+0.27s/corr=0.55,右侧叠图的圆点却画在
    +0.13s/0.59,而且恰好从容差带外滑到带内:图说"没问题",数说"超容差"。
    病根:正式判定(global_lag)在互相关前先用 trim_static_span 剔首尾静止段,画图却是
    "未剔"的近似——静止段把峰压成平台,argmax 就滑走。
    第一轮只把圆点挪到正式读数上,用户随即指出后半截:蓝点在 +0.27,**蓝曲线顶点仍在
    +0.13**,点不在曲线顶上,看着依然自相矛盾。故曲线本身也改用同一套预处理。
    本测试两头都钉:①用旧画法证明这条夹具真会滑(否则等于没测);②新画法的曲线顶点
    必须与正式读数对齐到 1 个采样步内;③圆点 x = 正式读数、y 落在曲线上。
    """
    import numpy as np
    import pytest as _pytest

    from curation.core.checks.video_action_sync import global_lag
    from curation.export.sync_plots import _peak_marker, _xcorr_curve

    t, flow, speed = _static_prefix_signals()
    det = global_lag(flow, t, speed, t).detail
    lag_s, corr_peak = det["lag_s"], det["corr_peak"]

    old_lags, old_xc = _untrimmed_curve(t, flow, speed)
    old_argmax = float(old_lags[int(np.argmax(old_xc))])
    # 夹具自检:旧画法必须真的和正式读数分家,否则这条测试等于没测(ep13 的病就在此)
    assert abs(old_argmax - lag_s) > 0.1, f"夹具失效:旧画法 {old_argmax} ≈ 正式 {lag_s}"

    lags, xc = _xcorr_curve(t, flow, speed)
    win = np.abs(lags) <= 2.0
    lags, xc = lags[win], xc[win]
    step = float(np.median(np.diff(lags)))
    # 曲线与读数同源之后,顶点该自己对上(容 1 个采样步:降采样/z 归一分母的零头)
    new_argmax = float(lags[int(np.argmax(xc))])
    assert abs(new_argmax - lag_s) <= step + 1e-9, \
        f"曲线顶点 {new_argmax} 与正式读数 {lag_s} 差了 {abs(new_argmax - lag_s)}s"

    x, y, label = _peak_marker("exterior_image_1_left", lags, xc, lag_s, corr_peak)
    assert x == _pytest.approx(lag_s), f"圆点画在 {x},正式读数却是 {lag_s}"
    assert x != _pytest.approx(old_argmax)
    # 点必须落在画出的那条曲线上(纵坐标取插值),不能悬空
    assert y == _pytest.approx(float(np.interp(lag_s, lags, xc)))
    assert xc.min() <= y <= xc.max()
    # 图例数字同样是正式读数,且仍是纯 ASCII(无 CJK 字体,见 test_sync_plot_text)
    assert label.isascii()
    assert f"@{lag_s:+.2f}s" in label and f"({corr_peak:.2f})" in label

    # ep13 的杀伤力在于滑动跨过了容差带:旧画法的点和图例数字会落在带的两边
    tol = 0.1
    assert abs(lag_s) <= tol < abs(old_argmax)


def test_peak_marker_abstains_when_no_reading():
    """读数缺失(不可判)时不画圆点、图例不编数字——诚实弃权,别拿重算值冒充读数。"""
    import numpy as np

    from curation.export.sync_plots import _peak_marker

    lags = np.linspace(-2, 2, 41)
    xc = np.cos(lags)
    x, y, label = _peak_marker("wrist_image_left", lags, xc, None, None)
    assert x is None and y is None
    assert label.isascii() and "wrist_image_left" in label
    assert "@" not in label and "0." not in label

    # corr_peak 缺失但 lag 有读数:点照画(位置有据),相关值老实写 n/a
    x2, y2, label2 = _peak_marker("cam", lags, xc, 0.5, None)
    assert x2 == 0.5 and y2 is not None
    assert "@+0.50s" in label2 and "n/a" in label2


def test_xcorr_curve_falls_back_when_trim_leaves_nothing():
    """剔完静止段没剩几个点时退回画未剔的整条,渲染不许崩(画得糙好过画不出来)。"""
    import json as _json
    import tempfile

    import numpy as np

    from curation.export.sync_plots import _xcorr_curve, render_sync_plots

    t = np.arange(60) / 30.0
    flow = np.zeros(60)
    flow[30:34] = 1.0            # 通篇静止只中间抖一下 → trim 认定整条都"静"
    speed = flow.copy()
    lags, xc = _xcorr_curve(t, flow, speed)
    assert len(lags) == len(xc) == 2 * len(t) - 1   # 退回整条,没被剔成残渣
    assert np.isfinite(xc).all()

    curves = _json.dumps({
        "cameras": {"cam": {"t": t.tolist(), "flow": flow.tolist(),
                            "speed": speed.tolist(), "lag_s": 0.0, "corr_peak": 0.9}},
        "verdict": "annotated", "consensus_lag_s": None, "n_cameras": 1,
        "n_trusted": 0, "flagged_cameras": [], "per_camera": {}, "lag_tol_s": 0.25})
    with tempfile.TemporaryDirectory() as d:
        assert render_sync_plots([("ep000001", curves)], d) == ["ep000001_sync.png"]


def test_renderer_survives_missing_reading():
    """整图渲染:一路有读数、一路不可判(lag_s=None)时不许炸,且 PNG 是真 PNG。"""
    import json as _json

    from curation.export.sync_plots import render_sync_plots

    t, flow, speed = _static_prefix_signals()
    cam = {"t": t.tolist(), "flow": flow.tolist(), "speed": speed.tolist()}
    curves = _json.dumps({
        "cameras": {"exterior_image_1_left": {**cam, "lag_s": 0.27, "corr_peak": 0.55},
                    "wrist_image_left": {**cam, "lag_s": None, "corr_peak": None}},
        "verdict": "annotated", "consensus_lag_s": None,
        "n_cameras": 2, "n_trusted": 1, "flagged_cameras": [],
        "per_camera": {}, "lag_tol_s": 0.25})
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = render_sync_plots([("ep000013", curves)], d)
        assert out == ["ep000013_sync.png"]
        with open(os.path.join(d, "ep000013_sync.png"), "rb") as f:
            assert f.read(8) == b"\x89PNG\r\n\x1a\n"


@需要
def test_flagged_mode_no_plots_when_all_pass(tmp_path):
    """pusht 同步全过 → flagged 默认不出图(不给没人看的图占磁盘)。"""
    from curation.cli import main
    out = tmp_path / "out"
    rc = main(["run", "--input", PUSHT, "--output", str(out),
               "--embodiment-id", "pusht", "--max-episodes", "3",
               "--only", "video_action_sync", "--report-only"])
    assert rc == 0
    plots = out / "details" / "plots"
    assert not plots.exists() or not list(plots.glob("*.png"))


@需要
def test_all_mode_plots_every_episode(tmp_path):
    """sync_plots: all → 每条一张,报告注明目录。"""
    import yaml

    from curation.cli import main
    from curation.pipeline.config import DEFAULT_CONFIG_PATH
    cfg = yaml.safe_load(open(DEFAULT_CONFIG_PATH))
    cfg["pipeline"]["sync_plots"] = "all"
    cfgp = tmp_path / "all.yaml"
    yaml.safe_dump(cfg, open(cfgp, "w"), allow_unicode=True)
    out = tmp_path / "out"
    rc = main(["run", "--config", str(cfgp), "--input", PUSHT, "--output", str(out),
               "--embodiment-id", "pusht", "--max-episodes", "3",
               "--only", "video_action_sync", "--report-only"])
    assert rc == 0
    pngs = list((out / "details" / "plots").glob("ep*_sync.png"))
    assert len(pngs) == 3
    rep = json.load(open(out / "passed.json"))
    assert rep["dataset"]["sync_plots"]["生成"] == 3
