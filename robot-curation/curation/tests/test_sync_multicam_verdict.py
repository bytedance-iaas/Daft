"""逐相机同步改造的验收(2026-08-07 用户拍板的判定表 + 两条实测样本的纸面推演)。

改造要点(每条都在本文件里被钉死):
- 测量层逐相机独立,三态:可信 lag / 测不准 / 无信号;
- 判定层**只有一种判废情形**:所有可信相机一致指向同一个 Δ≠0;
- 单相机数据集永不因同步判废;矛盾/测不准/无信号 → passed=True + 标注,
  **绝不返回 None**(None=弃权会把条目推进人工裁决队列,用户明确否掉);
- 正负不对称:正滞后(相机链路延迟,有良性解释)门槛高,负滞后(数据装配错误)门槛严。
"""
from __future__ import annotations

import numpy as np
import pytest

from curation.core.checks.video_action_sync import (  # noqa: F401
    camera_diagnosis,
    camera_reading,
    global_lag,
    peak_metrics,
    sync_check_result,
    sync_health,
    sync_verdict,
    trim_static_span,
)


def _cam(lag=None, code="aligned", trusted=True, corr=0.8, ratio=4.0, width=0.3,
         zero=None):
    """构造一条 per_camera 读数(测量层的产物形状,判定层只认这个)。

    zero(零滞后处的相关)默认等于峰值;要构造"峰真的赢了 0"的情形必须显式传低值——
    这个差额正是「假峰」与「疑似错位」的分水岭(见 camera_diagnosis)。
    """
    r = {"lag_s": lag, "corr_peak": corr,
         "corr_at_zero": corr if zero is None else zero,
         "peak_ratio": ratio, "peak_width_s": width,
         "trusted": trusted, "code": code, "note": ""}
    r["diagnosis"] = camera_diagnosis(r)
    return r


def _mis(lag, **kw):
    return _cam(lag=lag, code="misaligned", trusted=True, **kw)


def _ali(lag=0.0, **kw):
    return _cam(lag=lag, code="aligned", trusted=True, **kw)


# ---------- 判定表矩阵 ----------

def test_all_trusted_cameras_agree_on_delta_kills():
    """唯一的判废情形:全部可信相机一致指向同一个 Δ≠0(且幅度够)。"""
    det = sync_verdict({"ext1": _mis(0.62), "ext2": _mis(0.58), "wrist": _mis(0.60)}, 3)
    assert det["verdict"] == "misaligned_all"
    assert det["consensus_lag_s"] == pytest.approx(0.60, abs=0.02)
    assert det["n_trusted"] == 3 and set(det["flagged_cameras"]) == {"ext1", "ext2", "wrist"}
    assert sync_check_result({"ext1": _mis(0.62), "ext2": _mis(0.58),
                              "wrist": _mis(0.60)}, 3).passed is False


def test_consistent_but_small_positive_lag_is_annotated_not_killed():
    """正滞后有良性解释(相机链路延迟普遍存在)→ 判废门槛抬到 0.5s,0.3s 只标注。"""
    det = sync_verdict({"a": _mis(0.30), "b": _mis(0.32)}, 2)
    assert det["verdict"] == "annotated"
    assert det["consensus_lag_s"] == pytest.approx(0.31, abs=0.01)   # 一致性仍如实记录
    assert "标定" in det["reason"]


def test_negative_lag_threshold_is_stricter():
    """负滞后无良性解释(只可能是数据装配错误)→ 容差量级(0.25s)即判废。

    同样是 |Δ|=0.30s:正的只标注(上一条),负的判废。这就是"正负不对称"。
    """
    det = sync_verdict({"a": _mis(-0.30), "b": _mis(-0.31)}, 2)
    assert det["verdict"] == "misaligned_all"
    assert "装配" in det["reason"]


def test_single_bad_camera_only_annotates():
    """某路异常 → 只标注该路,**绝不废弃相机、绝不删 video 指针**,整条照收。"""
    r = sync_check_result({"ext1": _mis(0.9), "ext2": _ali(0.02), "wrist": _ali(-0.01)}, 3)
    assert r.passed is True
    assert r.detail["verdict"] == "annotated"
    assert r.detail["flagged_cameras"] == ["ext1"]
    assert r.detail["consensus_lag_s"] is None            # 有矛盾就没有共识
    assert "矛盾" in r.detail["reason"]


def test_contradicting_cameras_do_not_kill():
    """两路都说错位但一正一负 = 证据打架,不构成一致证据 → 不杀。"""
    r = sync_check_result({"a": _mis(0.8), "b": _mis(-0.9)}, 2)
    assert r.passed is True and r.detail["verdict"] == "annotated"
    assert r.detail["consensus_lag_s"] is None


def test_same_sign_but_spread_too_wide_does_not_kill():
    r = sync_check_result({"a": _mis(0.6), "b": _mis(1.4)}, 2)
    assert r.passed is True and r.detail["verdict"] == "annotated"
    assert "极差" in r.detail["reason"]


def test_single_camera_dataset_never_killed():
    """孤证不定罪:单相机数据集永不因同步判废(哪怕读数又大又可信)。"""
    r = sync_check_result({"only": _mis(1.5)}, 1)
    assert r.passed is True and r.detail["verdict"] == "annotated"
    assert r.detail["n_cameras"] == 1 and r.detail["flagged_cameras"] == ["only"]


def test_two_cameras_but_only_one_trusted_never_killed():
    """可信证人只有一个 = 仍是孤证,不杀。"""
    r = sync_check_result({"a": _mis(1.2),
                           "b": _cam(lag=0.1, code="low_corr", trusted=False)}, 2)
    assert r.passed is True and r.detail["verdict"] == "annotated"


def test_no_trusted_reading_is_undecidable_but_passes():
    """测不准/无信号 → passed=True(不是 None!),不进人工裁决队列,不参与软分。"""
    r = sync_check_result({"a": _cam(code="ambiguous_peak", trusted=False),
                           "b": _cam(code="no_motion", trusted=False, lag=None)}, 2)
    assert r.passed is True                       # ← 红线:弃权不得再返回 None
    assert r.detail["verdict"] == "undecidable" and r.detail["n_trusted"] == 0


def test_all_aligned_is_aligned():
    r = sync_check_result({"a": _ali(0.01), "b": _ali(-0.03), "c": _ali(0.0)}, 3)
    assert r.passed is True and r.detail["verdict"] == "aligned"
    assert r.detail["flagged_cameras"] == []


def test_undecidable_never_enters_manual_review_queue():
    """裁决层回归:同步是硬门,passed=None 会被 episode_verdict 记进 undecidable
    → review.json → 人工队列。改造后无论测不准还是矛盾都必须 passed=True,队列里
    不该出现同步条目(用户明确否掉的那条路径)。"""
    from curation.pipeline.verdict import episode_verdict

    cfg = {"checks": {"video_action_sync": {"enable": True, "gate": "hard"}},
           "verdict": {"soft_threshold": 0.5}}
    for cams, n in [({"a": _cam(code="low_corr", trusted=False)}, 1),
                    ({"a": _mis(0.9), "b": _ali(0.0)}, 2),
                    ({"a": _mis(1.2)}, 1)]:
        res = sync_check_result(cams, n)
        v = episode_verdict({"video_action_sync": {"passed": res.passed, "score": None}}, cfg)
        assert v["verdict"] == "keep" and v["undecidable"] == []


# ---------- 两条实测样本的纸面推演(读数来自 2026-08-06 /mnt/tos 实测) ----------

def test_droid_ep4_paper_replay_is_not_killed():
    """droid ep4 三路:+0.60s(corr 0.44,峰又矮又平)/ −0.07s(0.50)/ wrist 0.00s(0.80 峰尖)。

    改造前:生产只看 sorted 第一路(报 +0.60s 的那路最脏的外部机位),全靠"峰不突出"
    这道护栏侥幸没杀。改造后:第一路因峰形不可信被判"测不准"退出投票,另两路可信
    且都对齐 → **不判废**。真相(几乎肯定是对齐的)被正确读出。

    第一路的诊断必须**说出病因**,不能笼统扣一顶"疑似错位"的帽子(2026-08-07
    用户纠正):真实读数 corr_peak 0.44 而**零滞后处 0.33**,差值 0.11 < 0.15 的
    显著性门槛 —— 那个 +0.60s 的峰赢不过 0,证据其实偏向对齐,病因是画面干扰
    (背景运动/相机晃动)造成的假峰。所以它进 noisy_cameras 而非 suspect_cameras,
    整条仍报 aligned,但 reason 与逐相机 diagnosis 会把这件事说清楚。
    """
    per_cam = {
        "exterior_image_1": _cam(lag=0.60, code="ambiguous_peak", trusted=False,
                                 corr=0.44, zero=0.3348, ratio=1.74, width=1.13),
        "exterior_image_2": _ali(-0.07, corr=0.50, ratio=2.6, width=0.5),
        "wrist_image_left": _ali(0.00, corr=0.80, ratio=9.0, width=0.2),
    }
    r = sync_check_result(per_cam, 3)
    assert r.passed is True                       # 绝不因此判废
    assert r.detail["verdict"] == "aligned"       # 证据偏向对齐,不诬告错位
    assert r.detail["flagged_cameras"] == []
    assert r.detail["suspect_cameras"] == []      # ← 关键:不是错位嫌疑
    assert r.detail["noisy_cameras"] == ["exterior_image_1"]
    assert "假峰" in r.detail["reason"] and "0.60" not in r.detail["reason"]
    diag = per_cam["exterior_image_1"]["diagnosis"]
    assert diag["cause"] == "false_peak"
    assert "赢不过 0" in diag["text"] and "0.33" in diag["text"]
    assert diag["advice"]                          # 必须给出怎么办


def test_real_misalignment_still_called_suspect_when_zero_loses():
    """反向守卫:零滞后处相关**明显低于**峰值时,0 已经站不住 → 才配叫疑似错位。

    否则这次的收窄就会矫枉过正,把真正可疑的读数也一并放过。
    """
    per_cam = {
        "ext": _cam(lag=0.70, code="flat_peak", trusted=False,
                    corr=0.75, zero=0.20, ratio=1.1, width=1.4),
        "wrist": _ali(0.00, corr=0.80, ratio=9.0, width=0.2),
    }
    det = sync_check_result(per_cam, 2).detail
    assert det["suspect_cameras"] == ["ext"] and det["noisy_cameras"] == []
    assert det["verdict"] == "annotated"
    assert per_cam["ext"]["diagnosis"]["cause"] == "blurry_motion"


def test_diagnosis_names_the_cause_not_a_generic_label():
    """四种测不准必须给出**各不相同**的病因与建议(用户:"你得给出正确的诊断啊")。"""
    causes = {
        "false_peak": _cam(lag=0.6, trusted=False, code="ambiguous_peak",
                           corr=0.44, zero=0.35, ratio=1.7, width=1.1),
        "blurry_motion": _cam(lag=0.7, trusted=False, code="flat_peak",
                              corr=0.75, zero=0.2, ratio=1.5, width=1.4),
        "rival_lags": _cam(lag=0.6, trusted=False, code="ambiguous_peak",
                           corr=0.7, zero=0.2, ratio=1.02, width=0.5),
        "weak_signal": _cam(lag=0.6, trusted=False, code="low_corr",
                            corr=0.25, zero=0.1, ratio=3.0, width=0.5),
    }
    seen = {}
    for want, cam in causes.items():
        d = cam["diagnosis"]
        assert d["cause"] == want, (want, d["cause"])
        assert d["advice"] and d["text"]
        seen[d["label"]] = d["text"]
    assert len(seen) == 4, "四种病因的标签必须互不相同"
    # 对齐/错位/无信号也各有说法
    assert _ali(0.0)["diagnosis"]["cause"] == "aligned"
    assert _mis(0.9)["diagnosis"]["cause"] == "misaligned"
    assert _cam(code="no_motion", trusted=False)["diagnosis"]["cause"] == "no_motion"


def test_abstain_within_tolerance_stays_aligned():
    """弃权但读数**在容差内** → 仍报 aligned:不能把"峰不够漂亮"渲染成疑似错位,
    否则整页都是黄的,真正偏了的那路反而淹没。只有 |lag| 超容差才算 suspect。"""
    per_cam = {
        "ext": _cam(lag=0.05, code="ambiguous_peak", trusted=False,
                    corr=0.5, ratio=1.1, width=1.1),
        "wrist": _ali(0.00, corr=0.80, ratio=9.0, width=0.2),
    }
    r = sync_check_result(per_cam, 2)
    assert r.detail["verdict"] == "aligned"
    assert r.detail["suspect_cameras"] == []
    assert r.detail["abstained_cameras"] == ["ext"]   # 但"测不准"本身仍被记账


def test_so101_ep0_paper_replay_is_not_killed():
    """so101 ep0:front corr 0.35 贴着可判门(0.3)地板;wrist 互相关在 0s / −0.5s
    两个峰几乎并列(0.65 vs 0.64)→ 峰比 1.02 → 掷硬币 → 测不准。

    两路都不可信 → undecidable,passed=True 且不进人工队列。就算 front 侥幸可信,
    也只有一个证人 → 仍不杀(孤证不定罪)。
    """
    per_cam = {
        "front": _cam(lag=0.0, code="low_corr", trusted=False, corr=0.35,
                      ratio=1.4, width=0.6),
        "wrist": _cam(lag=-0.5, code="ambiguous_peak", trusted=False, corr=0.65,
                      ratio=1.02, width=0.35),
    }
    r = sync_check_result(per_cam, 2)
    assert r.passed is True and r.detail["verdict"] == "undecidable"

    # 反事实:即使 front 读数被判可信且报错位,单一证人依然不构成判废
    per_cam2 = dict(per_cam, front=_mis(-0.6, corr=0.35))
    assert sync_check_result(per_cam2, 2).passed is True


# ---------- 静止段剔除 ----------

def _active(n, seed=0):
    rng = np.random.default_rng(seed)
    return np.clip(np.convolve(rng.normal(0, 1, n + 60), np.ones(12) / 12, "same"),
                   0, None)[30:30 + n]


def test_trim_drops_only_leading_and_trailing_quiet_span():
    act = _active(200)
    quiet = np.full(80, 0.001)
    flow = np.concatenate([quiet, act, quiet])
    lo, hi = trim_static_span(flow, flow.copy())
    assert lo >= 78 and hi <= 282                 # 首尾静止段被剔掉
    assert hi - lo >= 150                          # 活跃段整段留下


def test_trim_keeps_interior_quiet_span():
    """中段静止是有信息的(两边都该不动),而且抠掉会破坏均匀采样 → 只剔首尾。"""
    act = _active(80)
    flow = np.concatenate([act, np.full(60, 0.001), act])
    lo, hi = trim_static_span(flow, flow.copy())
    # 中段那 60 帧静止整段留在保留区间内(首尾各几帧恰好落在低位被剔属正常)
    assert lo <= 80 and hi >= 140
    assert hi - lo >= len(flow) - 20


def test_trim_raises_correlation_on_quiet_head():
    """静止段进互相关分母 = 把相关性系统性稀释;剔除后同一条数据相关更高。"""
    act = _active(200, seed=3)
    quiet = np.full(120, 0.001)
    t = np.arange(320) * 0.1
    flow = np.concatenate([quiet, act]) + np.random.default_rng(1).normal(0, 0.01, 320)
    speed = np.concatenate([quiet, act])
    with_trim = global_lag(flow, t, speed, t)
    without = global_lag(flow, t, speed, t, trim_static=False)
    assert with_trim.detail["corr_peak"] > without.detail["corr_peak"]
    assert with_trim.detail["n_trimmed_static"] >= 100
    assert abs(with_trim.detail["lag_s"]) <= 0.1   # 剔除不得移动 lag 读数


def test_trim_to_nothing_degrades_to_no_motion():
    """剔完样本太少 → 降级"无信号",绝不硬撑一个统计上站不住的读数。"""
    t = np.arange(200) * 0.1
    flow = np.full(200, 0.001)
    flow[100:104] = 1.0                            # 只有 4 帧动了一下
    r = global_lag(flow, t, flow.copy(), t)
    assert r.passed is None and r.detail["code"] == "no_motion"
    assert camera_reading(r)["trusted"] is False
    assert camera_reading(r)["code"] == "no_motion"


# ---------- 峰可信度(主峰/次高峰 + 峰宽) ----------

def _gauss(lags, mu, sigma, amp):
    return amp * np.exp(-((lags - mu) ** 2) / (2 * sigma ** 2))


def test_peak_ratio_catches_tied_double_peak():
    """so101 ep0 那种"0s 0.65 / −0.5s 0.64"的并列双峰 = 掷硬币,比值必须≈1。"""
    lags = np.linspace(-2, 2, 161)
    xc = _gauss(lags, 0.0, 0.15, 0.65) + _gauss(lags, -0.5, 0.15, 0.64)
    ratio, width = peak_metrics(xc, lags, int(np.argmax(xc)))
    assert ratio < 1.25 and width < 1.0            # 比值判据抓,峰宽判据不该误伤


def test_peak_width_catches_flat_hill():
    """droid ep4 exterior_1 那种又矮又平的峰:没有第二个候选,但胖到读不出位置。"""
    lags = np.linspace(-2, 2, 161)
    xc = _gauss(lags, 0.6, 0.8, 0.44)
    ratio, width = peak_metrics(xc, lags, int(np.argmax(xc)))
    assert width > 1.0 and ratio >= 1.25           # 峰宽判据抓,比值判据不重复定罪


def test_sharp_peak_is_trusted():
    lags = np.linspace(-2, 2, 161)
    xc = _gauss(lags, 0.0, 0.12, 0.80)
    ratio, width = peak_metrics(xc, lags, int(np.argmax(xc)))
    assert ratio >= 1.25 and width <= 1.0


# ---------- 数据集级诊断 ----------

def test_sync_health_advises_calibration_when_uniform():
    """全库同号同量 → 建议整库标定 Δ(系统性偏移,逐条判废是浪费数据)。"""
    eps = {f"ep{i:06d}": sync_verdict({"ext": _mis(0.60 + 0.01 * (i % 3)),
                                       "wrist": _mis(0.61)}, 2) for i in range(10)}
    h = sync_health(eps)
    assert h["per_camera"]["ext"]["n"] == 10
    assert h["per_camera"]["ext"]["median_lag_s"] == pytest.approx(0.61, abs=0.02)
    assert "整库标定" in h["advice"]
    assert h["negative_lag_episodes"] == []


def test_sync_health_flags_unstable_recording():
    lags = [-0.9, 0.8, 0.1, 1.5, -1.2, 0.6, 1.9, -0.4]
    eps = {f"ep{i:06d}": sync_verdict({"ext": _mis(v), "wrist": _mis(v + 0.05)}, 2)
           for i, v in enumerate(lags)}
    h = sync_health(eps)
    assert "录制不稳定" in h["advice"] or "乱跳" in h["advice"]
    assert h["negative_lag_episodes"], "负滞后条目必须单列告警"
    assert "负滞后" in h["advice"]


def test_sync_health_lists_flagged_cameras_per_episode():
    eps = {"ep000001": sync_verdict({"ext": _mis(0.9), "wrist": _ali(0.0)}, 2),
           "ep000002": sync_verdict({"ext": _ali(0.0), "wrist": _ali(0.0)}, 2)}
    h = sync_health(eps)
    assert [x["episode_id"] for x in h["flagged_episodes"]] == ["ep000001"]
    assert list(h["flagged_episodes"][0]["cameras"]) == ["ext"]
    assert h["per_camera"]["ext"]["n_flagged"] == 1


def test_sync_health_without_any_trusted_reading():
    eps = {"ep000001": sync_verdict({"a": _cam(code="low_corr", trusted=False)}, 1)}
    h = sync_health(eps)
    assert h["per_camera"]["a"]["n"] == 0 and h["per_camera"]["a"]["median_lag_s"] is None
    assert "未下结论" in h["advice"]


# ---------- 交付层:旁挂 meta/curation_camera_health.json ----------

def test_camera_health_sidecar_written_with_renumbering(tmp_path):
    """v2/v3 导出旁挂相机健康度:重编号后仍能对回源 episode,且**不碰 info.json**。"""
    import json as _json

    from curation.export.lerobot_writer import _write_camera_health

    eps = {"ep000004": sync_verdict({"ext": _mis(0.9), "wrist": _ali(0.0)}, 2),
           "ep000009": sync_verdict({"ext": _ali(0.0), "wrist": _ali(0.0)}, 2)}
    health = {"dataset": sync_health(eps), "episodes": eps}
    out = tmp_path / "curated"
    (out / "meta").mkdir(parents=True)
    (out / "meta" / "info.json").write_text('{"codebase_version": "v2.1"}')
    _write_camera_health(str(out), health, [4, 9])

    got = _json.loads((out / "meta" / "curation_camera_health.json").read_text())
    assert [r["episode_index"] for r in got["episodes"]] == [0, 1]
    assert [r["source_episode_id"] for r in got["episodes"]] == ["ep000004", "ep000009"]
    assert got["episodes"][0]["flagged_cameras"] == ["ext"]
    assert got["episodes"][0]["per_camera"]["ext"]["lag_s"] == 0.9
    assert "advice" in got["dataset"]
    # 标准 schema 一个字节都不许改
    assert (out / "meta" / "info.json").read_text() == '{"codebase_version": "v2.1"}'


def test_camera_health_sidecar_absent_when_no_data(tmp_path):
    from curation.export.lerobot_writer import _write_camera_health

    _write_camera_health(str(tmp_path), None, [0, 1])
    assert not (tmp_path / "meta" / "curation_camera_health.json").exists()


# ---------- 报告层:相机流健康度一节 ----------

def test_report_renders_camera_health_section():
    from curation.export.report import build_report, to_markdown

    eps = {"ep000001": sync_verdict({"ext": _mis(-0.6), "wrist": _mis(-0.62)}, 2),
           "ep000002": sync_verdict({"ext": _mis(-0.58), "wrist": _mis(-0.6)}, 2)}
    rep = build_report({}, {"input": 2, "output": 2}, [], {}, "cfg")
    rep["dataset"]["sync_health"] = sync_health(eps)
    md = to_markdown(rep)
    assert "## 相机流健康度" in md
    assert "整库标定" in md                 # 同号同量 → 建议标定
    assert "负滞后" in md                   # 负滞后必须单列告警
    assert "只标注不废弃" in md or "只标注" in md


# ---------- 接口契约(UI 并行开发,字段名/枚举一个字都不许漂)----------

_CONTRACT_TOP = {"verdict", "per_camera", "flagged_cameras", "suspect_cameras",
                 "noisy_cameras", "abstained_cameras", "consensus_lag_s",
                 "n_cameras", "n_trusted", "reason"}
_CONTRACT_CAM = {"lag_s", "corr_peak", "corr_at_zero", "peak_ratio", "peak_width_s",
                 "trusted", "code", "note", "diagnosis"}
_CONTRACT_VERDICTS = {"aligned", "misaligned_all", "annotated", "undecidable"}
_CONTRACT_CODES = {"aligned", "misaligned", "ambiguous_peak", "flat_peak",
                   "low_corr", "no_motion"}


def test_detail_matches_ui_contract():
    det = sync_verdict({"ext": _mis(0.9), "wrist": _ali(0.0),
                        "top": _cam(code="no_motion", trusted=False, lag=None)}, 3)
    assert set(det) == _CONTRACT_TOP
    assert det["verdict"] in _CONTRACT_VERDICTS
    for r in det["per_camera"].values():
        assert set(r) == _CONTRACT_CAM
        assert r["code"] in _CONTRACT_CODES
    assert isinstance(det["flagged_cameras"], list)
    assert isinstance(det["n_cameras"], int) and isinstance(det["n_trusted"], int)


def test_camera_reading_only_emits_contract_codes():
    """测量层的实现分支名(lag_exceeds_tol / weak_corr / short_sequence …)不得漏进
    契约:UI 只认六态,多一个它就渲染不出来。"""
    t = np.arange(200) * 0.1
    rng = np.random.default_rng(0)
    cases = [
        global_lag([1, 2, 3], [0, 1, 2], [1, 2, 3], [0, 1, 2]),         # 信号过短
        global_lag(np.ones(200), t, np.ones(200), t),                    # 无信号
        global_lag(rng.normal(size=30), t[:30], rng.normal(size=30), t[:30]),  # 序列过短
        global_lag(rng.normal(size=200), t, rng.normal(size=200), t),    # 相关太弱
    ]
    for shift in (-8, 0, 8):
        rng2 = np.random.default_rng(1)
        base = np.clip(np.convolve(rng2.normal(0, 1, 400), np.ones(12) / 12, "same"), 0, None)
        cases.append(global_lag(base[50 - shift:350 - shift] + rng2.normal(0, .02, 300),
                                np.arange(300) * .1, base[50:350], np.arange(300) * .1))
    for r in cases:
        rd = camera_reading(r)
        assert set(rd) == _CONTRACT_CAM
        assert rd["code"] in _CONTRACT_CODES, rd
        assert isinstance(rd["trusted"], bool) and isinstance(rd["note"], str)


def test_check_display_name_is_pinned():
    """报告/UI 里的显示名是既有契约(带连字符),任何改名都会让 UI 取不到 detail。"""
    from curation.export.report import CHECK_CN

    assert CHECK_CN["video_action_sync"] == "视频-动作同步"


def test_sync_health_matches_report_contract():
    eps = {"ep000001": sync_verdict({"ext": _mis(0.9), "wrist": _ali(0.0)}, 2)}
    h = sync_health(eps)
    assert {"per_camera", "advice", "negative_lag_episodes"} <= set(h)
    for s in h["per_camera"].values():
        assert set(s) == {"n", "median_lag_s", "iqr_s", "n_flagged",
                          "n_suspect", "n_noisy", "n_abstained"}
    assert isinstance(h["advice"], str) and isinstance(h["negative_lag_episodes"], list)


def test_sync_plot_written_via_local_tmp(tmp_path, monkeypatch):
    """曲线图不许 savefig 直写交付目录——FSX 直写会静默产出零填充坏 PNG。

    2026-08-07 生产实锤:5 张坏 3 张,字节数正常、PNG 签名全零,浏览器空白、
    服务端零报错。这里盯的是"写到哪儿",不是画得对不对。
    """
    import json as _json

    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg")
    pytest.importorskip("scipy")
    from matplotlib.figure import Figure

    from curation.export import sync_plots

    seen: list = []
    real = Figure.savefig

    def spy(self, fname, *a, **kw):
        seen.append(str(fname))
        return real(self, fname, *a, **kw)

    monkeypatch.setattr(Figure, "savefig", spy)

    out = tmp_path / "plots"
    t = [i * 0.1 for i in range(24)]
    wave = [float((i % 6) - 2) for i in range(24)]
    curves = {"tol_s": 0.25, "verdict": "aligned",
              "cameras": {"cam0": {"t": t, "flow": wave, "speed": wave,
                                   "lag_s": 0.0, "corr_peak": 0.9}}}
    written = sync_plots.render_sync_plots([("ep000000", _json.dumps(curves))],
                                           str(out))

    assert written == ["ep000000_sync.png"], written
    dst = out / "ep000000_sync.png"
    assert dst.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"      # 落地的是真 PNG
    assert seen, "根本没调用 savefig"
    assert all(str(out) not in p for p in seen), f"savefig 直写了交付目录:{seen}"


def test_noisy_episodes_are_accounted_at_dataset_level():
    """假峰条目必须在数据集级立账 —— 否则整条 episode 在报告里查无此人。

    2026-08-07:UI 修完了,报告侧还漏着(flagged/negative 都不沾)。这条守卫
    盯的就是"报告能不能查到 ep4 这种条目"。
    """
    det = sync_check_result({
        "ext": _cam(lag=0.60, code="ambiguous_peak", trusted=False,
                    corr=0.44, zero=0.3348, ratio=1.74, width=1.13),
        "wrist": _ali(0.00, corr=0.80, ratio=9.0, width=0.2),
    }, 2).detail
    assert det["noisy_cameras"] == ["ext"]

    h = sync_health({"ep000004": det})
    assert [x["episode_id"] for x in h["noisy_episodes"]] == ["ep000004"]
    assert h["per_camera"]["ext"]["n_noisy"] == 1
    assert h["per_camera"]["wrist"]["n_noisy"] == 0
    assert "假峰" in h["advice"] and "ext" in h["advice"]
    # 既没进判废、也没进负滞后 —— 正是它此前被漏掉的原因
    assert h["flagged_episodes"] == [] and h["negative_lag_episodes"] == []
