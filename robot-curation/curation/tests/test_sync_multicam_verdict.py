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
         zero=None, at_edge=False, vis=None):
    """构造一条 per_camera 读数(测量层的产物形状,判定层只认这个)。

    zero(零滞后处的相关)默认等于峰值;要构造"峰真的赢了 0"的情形必须显式传低值——
    这个差额正是「假峰」与「疑似错位」的分水岭(见 camera_diagnosis)。
    at_edge:峰顶是否贴在 ±max_lag_s 扫描窗的边界格点上(噪声曲线的典型归宿)。
    vis:可见性度量(不传 = 老交付那种没有探针数据的读数,连键都不该有)。
    """
    r = {"lag_s": lag, "corr_peak": corr,
         "corr_at_zero": corr if zero is None else zero,
         "peak_ratio": ratio, "peak_width_s": width, "at_scan_edge": at_edge,
         "trusted": trusted, "code": code, "note": ""}
    if vis is not None:
        r["visibility"] = vis
    r["diagnosis"] = camera_diagnosis(r)
    return r


def _vis(blind=0.3, head=0.0, vis_lag=0.0, vis_corr=0.6, vis_n=100, rev=0.0):
    return {"blind_frac": blind, "rev_blind_frac": rev, "head_blind_s": head,
            "vis_lag_s": vis_lag, "vis_corr": vis_corr, "vis_corr0": vis_corr,
            "vis_n": vis_n}


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
    # 锚点挂在**语义**上不挂修辞:两个数都要摆出来、都要说"错开和不错开一样像"
    # (2026-08-11 用户点名文案"AI 味太重"后重写,原文"峰赢不过 0"已废)
    assert "0.44" in diag["text"] and "0.33" in diag["text"]
    assert "相似度" in diag["text"] and "不错开" in diag["text"]
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


def test_droid_ep20_weak_signal_camera_is_not_called_suspect():
    """droid-200-full ep000020 的 exterior_image_2_left:corr 0.203(低于 0.3 判读门)、
    lag −2.0s 恰好落在 ±2.0s 扫描窗的边界、corr0 0.177。

    2026-08-11 用户在这条上指出:系统把一个**入画晚/覆盖不足**的相机判成了错位。
    同一个条目两种口径 —— 逐相机诊断写"测不准 · 信号弱",小节标题却喊"疑似错位"。
    病根:相关只有 0.20 时"零滞后站不站得住"是句废话,这一路的 lag 本身就是噪声,
    它作不出「疑似错位」小节前言承诺的那句断言(峰偏离 0 **且**零滞后已站不住)。
    修法:suspect 准入只留 blurry_motion / rival_lags(它们已过"零点可弃"检验),
    信号弱照旧只进 abstained。另两路可信对齐 → 整条 aligned,不是 annotated。
    """
    per_cam = {
        "exterior_image_1_left": _ali(0.02, corr=0.55, ratio=3.0, width=0.4),
        "exterior_image_2_left": _cam(lag=-2.0, code="low_corr", trusted=False,
                                      corr=0.203, zero=0.177, ratio=1.6, width=0.6,
                                      at_edge=True),
        "wrist_image_left": _ali(0.00, corr=0.78, ratio=8.0, width=0.2),
    }
    det = sync_check_result(per_cam, 3).detail
    assert per_cam["exterior_image_2_left"]["diagnosis"]["cause"] == "weak_signal"
    assert det["suspect_cameras"] == []                       # ← 不许扣错位的帽子
    assert det["abstained_cameras"] == ["exterior_image_2_left"]   # 但测不准仍立账
    assert det["verdict"] == "aligned"
    assert "疑似错位" not in det["reason"]


def test_scan_edge_peak_never_carries_a_misalignment_claim():
    """扫描窗边缘防御:同样是"峰宽超标 + 大 lag"的一路,峰落在窗内才够格叫疑似错位;
    峰顶贴在 ±max_lag_s 的边界格点上 = 曲线一路爬到边界被截断,是噪声的典型归宿。

    这是 2026-08-11 那次修正的第二道保险(病因收窄之外再加一道),对 blurry_motion /
    rival_lags 同样生效 —— 否则 droid ep20 那种边界读数换个病因又会溜回 suspect。
    """
    inside = {"ext": _cam(lag=0.70, code="flat_peak", trusted=False,
                          corr=0.75, zero=0.20, ratio=1.1, width=1.4),
              "wrist": _ali(0.00)}
    assert sync_verdict(inside, 2)["suspect_cameras"] == ["ext"]
    assert inside["ext"]["diagnosis"]["cause"] == "blurry_motion"

    at_edge = {"ext": _cam(lag=2.0, code="flat_peak", trusted=False,
                           corr=0.75, zero=0.20, ratio=1.1, width=1.4, at_edge=True),
               "wrist": _ali(0.00)}
    det = sync_verdict(at_edge, 2)
    assert at_edge["ext"]["diagnosis"]["cause"] == "blurry_motion"   # 病因不变
    assert det["suspect_cameras"] == [] and det["abstained_cameras"] == ["ext"]
    assert det["verdict"] == "aligned"


def test_false_peak_only_prints_causes_it_actually_measured():
    """病因不许罗列着印 —— 每一句成因都得有本条实测数据撑着。

    2026-08-11 用户在 droid ep000013 上点名:那条被印了一句"背景有人走动",
    可它实测的反向盲段(臂不动而画面在动)只有 0.055 —— 纯属栽赃,客户照着
    改环境是白改。三种形态各钉一条:只有几何证据 / 只有背景证据 / 一条都没有。
    """
    # ① ep13 形状:正向盲段 0.134 过线、反向盲段 0.055 不过线 → 只说沿光轴,不提背景
    geo = _cam(lag=0.6, code="ambiguous_peak", trusted=False, corr=0.44, zero=0.35,
               ratio=1.7, width=1.1,
               vis=_vis(blind=0.134, rev=0.055, vis_lag=-0.27, vis_corr=0.5))["diagnosis"]
    assert geo["cause"] == "false_peak"
    assert "沿光轴" in geo["text"] and "13%" in geo["text"]
    assert "背景" not in geo["text"]
    assert "改善机位" in geo["advice"] and "改善环境" not in geo["advice"]

    # ② 反向盲段 0.4:臂不动画面还在动 —— 这才配说背景有人走动
    bg = _cam(lag=0.6, code="ambiguous_peak", trusted=False, corr=0.44, zero=0.35,
              ratio=1.7, width=1.1,
              vis=_vis(blind=0.02, rev=0.4, vis_lag=-0.27, vis_corr=0.5))["diagnosis"]
    assert "背景有人走动" in bg["text"] and "40%" in bg["text"]
    assert "沿光轴" not in bg["text"]
    assert "改善环境" in bg["advice"] and "改善机位" not in bg["advice"]

    # ③ 两条证据都没有(以及老读数根本没有 visibility)→ 老实说没定位到,不瞎猜
    for cam in (_cam(lag=0.6, code="ambiguous_peak", trusted=False, corr=0.44,
                     zero=0.35, ratio=1.7, width=1.1,
                     vis=_vis(blind=0.02, rev=0.05, vis_lag=-0.27, vis_corr=0.5)),
                _cam(lag=0.6, code="ambiguous_peak", trusted=False, corr=0.44,
                     zero=0.35, ratio=1.7, width=1.1)):
        d = cam["diagnosis"]
        assert d["cause"] == "false_peak"
        assert "未能定位具体来源" in d["text"]
        assert "沿光轴" not in d["text"] and "背景" not in d["text"]


def test_diagnosis_text_speaks_plain_language():
    """诊断文字直接进报告和 UI 给客户看 —— 互相关行话一个都不许漏出去
    (2026-08-11 用户:"AI 味太重")。这条把六种病因的文案一起钉住。"""
    cams = [
        _ali(0.02), _mis(0.9),
        _cam(lag=0.6, trusted=False, code="ambiguous_peak", corr=0.44, zero=0.35,
             ratio=1.7, width=1.1),
        _cam(lag=0.7, trusted=False, code="flat_peak", corr=0.75, zero=0.2,
             ratio=1.5, width=1.4),
        _cam(lag=0.6, trusted=False, code="ambiguous_peak", corr=0.7, zero=0.2,
             ratio=1.02, width=0.5),
        _cam(lag=0.6, trusted=False, code="low_corr", corr=0.25, zero=0.1),
        _cam(lag=-2.0, trusted=False, code="low_corr", corr=0.2, zero=0.18,
             vis=_vis(blind=0.27, head=2.2, vis_corr=0.62, vis_n=75)),
        _cam(code="no_motion", trusted=False),
    ]
    # "正滞后/负滞后"是保留的说法(错位 advice 里解释相机链路延迟用),不在禁词里
    banned = ("corr", "峰", "互相关", "主峰", "次峰", "赢不过")
    for cam in cams:
        d = cam["diagnosis"]
        for word in banned:
            assert word not in d["text"], (d["cause"], word, d["text"])
            assert word not in d["advice"], (d["cause"], word, d["advice"])


def test_droid_ep20_gets_a_positive_diagnosis_not_just_an_abstention():
    """同一条 ep000020/exterior_2,这次要求的不是"别喊错位",而是**说对是什么病**。

    真实读数(2026-08-11 pod 实测):全程 corr 0.203 / lag −2.0s / corr0 0.177,
    而可见性探针说:盲段占 27%、开头 2.2s 光流静默但臂在动、可见窗 75 样本内
    corr 0.62 @ 0.00s。全程统计被开头那段"臂还没进画面"整个淹没了——
    只报"信号弱"等于把原因藏起来,能解释就别喊弱。
    """
    cam = _cam(lag=-2.0, code="low_corr", trusted=False, corr=0.203, zero=0.177,
               ratio=1.6, width=0.6, at_edge=True,
               vis=_vis(blind=0.2674, head=2.2, vis_lag=0.0, vis_corr=0.6225,
                        vis_n=75))
    d = cam["diagnosis"]
    assert d["cause"] == "partial_visibility" and d["label"] == "测不准 · 覆盖不足"
    assert "尚未进入画面" in d["text"] and "2.2s" in d["text"]
    assert "不是错位" in d["text"] and "0.62" in d["text"]
    assert "完整覆盖任务全程" in d["advice"]


def test_partial_visibility_wording_without_a_head_blind_span():
    """盲段不在开头(中途出画/遮挡)→ 不许硬说"尚未进入画面",改说运动时段占比。"""
    d = _cam(lag=0.9, code="low_corr", trusted=False, corr=0.22, zero=0.18,
             vis=_vis(blind=0.35, head=0.0, vis_corr=0.55))["diagnosis"]
    assert d["cause"] == "partial_visibility"
    assert "35%" in d["text"] and "运动时段" in d["text"]
    assert "尚未进入画面" not in d["text"]


def test_droid_ep13_geometry_case_is_not_mistaken_for_coverage():
    """锚点反例:ep000013 的 exterior_2 盲段占比 0.263 已过门槛,但它的**可见段自己
    也偏**(vis_lag −0.27s)—— 那是投影几何把曲线拧歪,不是相机没拍到。

    "可见段必须对齐"这一条就是为拦住这类病例设的:少了它,覆盖不足会变成一顶
    比"疑似错位"更能唬人的新帽子(它还自带一句"是机位覆盖问题")。
    """
    d = _cam(lag=0.6, code="ambiguous_peak", trusted=False, corr=0.44, zero=0.35,
             ratio=1.7, width=1.1,
             vis=_vis(blind=0.263, head=0.0, vis_lag=-0.27, vis_corr=0.5))["diagnosis"]
    assert d["cause"] == "false_peak"


def test_visible_window_must_speak_clearly_enough():
    """可见段相关 0.30 < 0.40:漏拍是真的,但拍到的部分自己都没说清楚 →
    只能报信号弱,不许升格成"覆盖不足"(那句"与动作对齐"会变成没有支撑的断言)。"""
    d = _cam(lag=0.6, code="low_corr", trusted=False, corr=0.25, zero=0.1,
             vis=_vis(blind=0.5, vis_corr=0.3))["diagnosis"]
    assert d["cause"] == "weak_signal"


def test_partial_visibility_camera_abstains_and_is_never_suspect():
    """判定层零改动的回归:覆盖不足的病因不在 suspect 准入名单里 → 大 lag 也只进
    abstained;其余相机可信对齐时整条仍是 aligned。"""
    per_cam = {
        "ext1": _ali(0.02, corr=0.55, ratio=3.0, width=0.4),
        "ext2": _cam(lag=-2.0, code="low_corr", trusted=False, corr=0.203,
                     zero=0.177, vis=_vis(blind=0.27, head=2.2, vis_corr=0.62,
                                          vis_n=75)),
        "wrist": _ali(0.00, corr=0.78, ratio=8.0, width=0.2),
    }
    det = sync_check_result(per_cam, 3).detail
    assert per_cam["ext2"]["diagnosis"]["cause"] == "partial_visibility"
    assert det["suspect_cameras"] == [] and det["noisy_cameras"] == []
    assert det["abstained_cameras"] == ["ext2"] and det["verdict"] == "aligned"


def test_old_reading_without_visibility_key_still_diagnosed():
    """老交付读数没有 visibility 键(2026-08-11 才加):不许炸,也不许因为"没数据"
    就顺手扣一个覆盖不足——证据不足就走原来的病因链。"""
    cam = _cam(lag=0.6, code="low_corr", trusted=False, corr=0.25, zero=0.1)
    assert "visibility" not in cam
    assert cam["diagnosis"]["cause"] == "weak_signal"
    # 探针跑了但没算出数(可见窗太窄)也一样:键在、值是 None → 不下诊断
    blank = _cam(lag=0.6, code="low_corr", trusted=False, corr=0.25, zero=0.1,
                 vis={"blind_frac": 0.9, "head_blind_s": None, "vis_lag_s": None,
                      "vis_corr": None, "vis_corr0": None, "vis_n": None})
    assert blank["diagnosis"]["cause"] == "weak_signal"


def test_old_reading_without_scan_edge_key_still_works():
    """老交付的 per_camera 读数没有 at_scan_edge 键(2026-08-11 才加),回放不许炸,
    且行为与"不在边界"一致 —— 否则历史交付重新渲染时结论会莫名其妙翻掉。"""
    cam = _cam(lag=0.70, code="flat_peak", trusted=False,
               corr=0.75, zero=0.20, ratio=1.1, width=1.4)
    cam.pop("at_scan_edge")
    det = sync_verdict({"ext": cam, "wrist": _ali(0.00)}, 2)
    assert det["suspect_cameras"] == ["ext"]


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
    """droid ep4 exterior_1 那种又矮又平的峰:没有第二个候选,但胖到读不出位置。

    2026-09-01 全库校准后上限 1.0→1.2:
    真平峰(本例 w≈1.25)仍被抓;ep31 那类 w≈1.05 的贴线尖峰不再误伤。"""
    lags = np.linspace(-2, 2, 161)
    xc = _gauss(lags, 0.6, 0.8, 0.44)
    ratio, width = peak_metrics(xc, lags, int(np.argmax(xc)))
    assert width > 1.2 and ratio >= 1.25           # 峰宽判据抓,比值判据不重复定罪
    # ep31 形态(尖峰):半高宽落在 1.0~1.2 贴线带 → 新上限下不误伤
    xc2 = _gauss(lags, 0.0, 0.5, 0.9)
    _, width2 = peak_metrics(xc2, lags, int(np.argmax(xc2)))
    assert 1.0 < width2 <= 1.2, f"贴线带样例应落 1.0~1.2,实测 {width2}"


def test_peak_width_cap_is_calibrated_to_1p2():
    """峰宽上限 = 1.2s(2026-09-01 全库校准定,两处默认必须一致):
    真错位读数全库 w≤0.8,1.0-1.2 贴线带 79% 峰心在容差内,1.2 下判废零新增。
    改这个数必须重跑全库校准的采集与模拟再定。"""
    import inspect
    from curation.core.checks.video_action_sync import global_lag, camera_diagnosis
    assert inspect.signature(global_lag).parameters["max_peak_width_s"].default == 1.2
    assert inspect.signature(camera_diagnosis).parameters["max_peak_width_s"].default == 1.2


def test_peak_issue_clauses_only_name_the_failing_gate():
    """病因分句只写实际失败的门(候选病因有证据才印):ratio=99(主峰独一份)
    绝不能被印成"另一个候选只差 99.00 倍"的罪状;措辞按半高宽实义,
    不再写"几乎不变"。"""
    from curation.core.checks.video_action_sync import _peak_issue_clauses
    only_w = _peak_issue_clauses(99.0, 1.4, 1.25, 1.2)
    assert "定位精度" in only_w and "99" not in only_w and "候选" not in only_w
    only_r = _peak_issue_clauses(1.1, 0.5, 1.25, 1.2)
    assert "候选" in only_r and "定位精度" not in only_r
    both = _peak_issue_clauses(1.1, 1.4, 1.25, 1.2)
    assert "候选" in both and "定位精度" in both
    assert "几乎不变" not in (only_w + only_r + both)


def test_sync_plot_worthy_matches_ui_filter_yardstick():
    """flagged 档出图判据 = UI 筛选判据,同一把尺子(2026-09-01 用户抓出口径
    分裂:筛选认弃权路而出图不认,默认档下这批条目根本无图可筛)。"""
    from curation.pipeline.funnel import sync_plot_worthy
    clean = {"verdict": "aligned", "flagged_cameras": [], "abstained_cameras": []}
    abst = {"verdict": "aligned", "flagged_cameras": [],
            "abstained_cameras": ["cam_a"]}
    undec = {"verdict": "undecidable", "flagged_cameras": [],
             "abstained_cameras": ["cam_a"]}
    flag = {"verdict": "annotated", "flagged_cameras": ["cam_b"],
            "abstained_cameras": []}
    assert not sync_plot_worthy("flagged", clean)
    assert sync_plot_worthy("flagged", abst)        # 弃权路也值得留意(修复点)
    assert sync_plot_worthy("flagged", undec)
    assert sync_plot_worthy("flagged", flag)
    assert all(sync_plot_worthy("all", d) for d in (clean, abst))
    assert not any(sync_plot_worthy("off", d) for d in (abst, undec, flag))
    # 与 UI 筛选口径互钉:同一份 detail 两边结论一致(state/ep 参数走新契约路径)
    from curation.ui.manifest import _sync_flagged
    for d in (clean, abst, undec, flag):
        assert sync_plot_worthy("flagged", d) == _sync_flagged({}, d, "通过")


def test_blurry_motion_diagnosis_speaks_precision_not_flatness():
    """逐相机诊断的峰宽措辞:说"定位精度不够/相似程度彼此接近",不说
    "怎么错开都差不多"(对贴线案言过其实——半高全宽的定义就是区间边缘
    已跌掉一半突出度);且不出现互相关行话(说人话红线)。

    corr_at_zero 取低值:零滞后处相关与峰值相当时假峰分支(②)会先接走,
    这里要测的是③峰过胖分支。"""
    r = {"lag_s": 0.07, "corr_peak": 0.72, "corr_at_zero": 0.30,
         "peak_ratio": 99.0, "peak_width_s": 1.4, "trusted": False,
         "code": "flat_peak"}
    d = camera_diagnosis(r)
    assert d["cause"] == "blurry_motion"
    assert "定位" in d["text"] and "都差不多" not in d["text"]
    assert "峰" not in d["text"] and "corr" not in d["text"]   # 行话不进客户文案


def _vis_pair(n=300, head=100, shift=0, seed=5):
    """合成"开头 head 个样本臂在动、画面却没动静,其后画面跟上"的一对曲线。

    活跃段抬了个底,好让静默判据的边界正好落在 head 上(否则头段长度随机数说了算,
    断言就只能写得很松,测出来的东西也就跟着松)。shift = 尾段相对动作平移的样本数。
    """
    a = _active(n + 40, seed=seed)
    a = a + 0.35 * float(a.max())
    t = np.arange(n) * 0.1
    speed = a[20:20 + n]
    flow = np.zeros(n)
    flow[head:] = a[20 + shift:20 + shift + n][head:]
    return flow, t, speed, t


def test_visibility_probe_measures_blind_span_and_visible_window():
    """可见性度量的三种形态:头盲+尾段对齐 / 尾段整体平移 / 全程都拍到。

    这是「覆盖不足」正诊断的全部证据来源,度量错了后面整条链都是错的。
    """
    vis = global_lag(*_vis_pair(head=100)).detail["visibility"]
    assert vis["head_blind_s"] == pytest.approx(10.0, abs=0.15)  # 100 样本 × 0.1s
    assert vis["blind_frac"] >= 0.2                              # 头段全是"臂动画面不动"
    assert vis["vis_n"] == 200                                   # 可见窗 = 其后整段
    assert vis["vis_lag_s"] == pytest.approx(0.0, abs=0.1)       # 拍到的部分对得上
    assert vis["vis_corr"] >= 0.8

    # 尾段整体平移:漏拍照旧,但"拍到的部分"自己就是偏的 → 不该被叫覆盖不足
    off = global_lag(*_vis_pair(head=100, shift=5)).detail["visibility"]
    assert abs(off["vis_lag_s"]) == pytest.approx(0.5, abs=0.15)

    # 全程都拍到:盲段为 0,头盲为 0(开机静止段两边都静,不算"没拍到")
    full = global_lag(*_vis_pair(head=0)).detail["visibility"]
    assert full["blind_frac"] == pytest.approx(0.0, abs=0.02)
    assert full["head_blind_s"] == 0.0


def test_visibility_probe_does_not_recurse():
    """探针内部会再调一次 global_lag —— 守卫参数必须让那一次不再探针,
    否则每层切一刀就是一次无限递归(切片越切越短,报错会伪装成"信号过短")。"""
    inner = global_lag(*_vis_pair(head=100), _probe_visibility=False).detail
    assert "visibility" not in inner


def test_scan_edge_flag_is_measured_not_guessed():
    """测量层如实记录"峰是不是贴着扫描窗边界":真峰在窗内 → False;把窗收窄到真峰
    之外,argmax 只能停在边界格点 → True。判定层靠这个标志把噪声读数挡在 suspect 外。
    """
    rng = np.random.default_rng(1)
    base = np.clip(np.convolve(rng.normal(0, 1, 400), np.ones(12) / 12, "same"), 0, None)
    t = np.arange(300) * 0.1
    flow, speed = base[42:342] + rng.normal(0, .02, 300), base[50:350]
    assert global_lag(flow, t, speed, t).detail["at_scan_edge"] is False
    assert global_lag(flow, t, speed, t, max_lag_s=0.4).detail["at_scan_edge"] is True


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
#: 2026-08-11 之后新增的键:新读数一定有,老交付一定没有 → 只能是可选,
#: 契约检查两头都要放行(必需集一个不能少,多出来的只准是这些)
_CONTRACT_CAM_OPT = {"at_scan_edge", "visibility"}
_CONTRACT_VERDICTS = {"aligned", "misaligned_all", "annotated", "undecidable"}
_CONTRACT_CODES = {"aligned", "misaligned", "ambiguous_peak", "flat_peak",
                   "low_corr", "no_motion"}


def test_detail_matches_ui_contract():
    det = sync_verdict({"ext": _mis(0.9), "wrist": _ali(0.0),
                        "top": _cam(code="no_motion", trusted=False, lag=None)}, 3)
    assert set(det) == _CONTRACT_TOP
    assert det["verdict"] in _CONTRACT_VERDICTS
    for r in det["per_camera"].values():
        assert _CONTRACT_CAM <= set(r) <= _CONTRACT_CAM | _CONTRACT_CAM_OPT
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
        assert _CONTRACT_CAM <= set(rd) <= _CONTRACT_CAM | _CONTRACT_CAM_OPT
        assert rd["code"] in _CONTRACT_CODES, rd
        assert isinstance(rd["trusted"], bool) and isinstance(rd["note"], str)
        assert isinstance(rd["at_scan_edge"], bool)
        # note 和诊断一样会出现在 UI 的逐相机表里 → 同样不许带互相关行话
        for word in ("corr", "峰", "互相关", "std"):
            assert word not in rd["note"], (rd["code"], word, rd["note"])


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
