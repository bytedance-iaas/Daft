"""报告的同步小节:假峰/疑似错位条目必须出现,且带病因诊断。

2026-08-07 的教训:同一个"弃权=静默丢弃"的洞先在 UI 上被用户发现并修掉,
报告侧却还漏着 —— ep4 那种条目既不在 flagged 也不在负滞后,于是**整条 episode
在交付给客户的报告里查无此人**。这个文件就是防它第三次出现。
"""
from __future__ import annotations

from curation.export.report import to_markdown


def _report(sync_health: dict) -> str:
    return to_markdown({
        "数据集": "d", "机器人": {}, "生成时间": "t", "代码版本": "v",
        "dataset": {"input_episodes": 1, "hard_gate_filtered": 0,
                    "verdict_keep": 1, "verdict_drop": 0, "dedup_removed": 0,
                    "delivered": 1, "hard_fail_breakdown": {},
                    "sync_health": sync_health},
        "skills": {}, "config": {},
        "episodes": {"results": [], "dropped": []},
    })


_NOISY_CAM = {
    "lag_s": 0.60, "corr_peak": 0.44, "corr_at_zero": 0.3348,
    "peak_ratio": 1.74, "peak_width_s": 1.13, "trusted": False,
    "code": "ambiguous_peak",
    "diagnosis": {"cause": "false_peak", "label": "测不准 · 画面干扰",
                  "text": "峰在 +0.60s 但赢不过 0", "advice": "固定相机"},
}


def test_noisy_episode_appears_with_its_diagnosis():
    """假峰条目必须自成一节,写明不判废、并给出病因和处方。"""
    md = _report({
        "per_camera": {"ext": {"n": 4, "median_lag_s": 0.0, "iqr_s": 0.02,
                               "n_flagged": 0, "n_suspect": 0, "n_noisy": 1,
                               "n_abstained": 1}},
        "advice": "未见系统性错位",
        "negative_lag_episodes": [], "flagged_episodes": [],
        "suspect_episodes": [],
        "noisy_episodes": [{"episode_id": "ep000004", "verdict": "aligned",
                            "cameras": {"ext": _NOISY_CAM}}],
    })
    assert "### 假峰(" in md
    assert "ep000004" in md                       # ← 报告里查得到
    assert "测不准 · 画面干扰" in md               # 病因
    assert "固定相机" in md                        # 处方
    assert "不判废" in md and "证据偏向对齐" in md


def test_suspect_section_is_separate_from_noisy():
    """「疑似错位」与「假峰」是两回事,不能混成一节。"""
    cam = dict(_NOISY_CAM)
    cam["diagnosis"] = {"cause": "blurry_motion", "label": "测不准 · 画面不锐利",
                        "text": "峰宽 1.40s", "advice": "换更近的机位"}
    md = _report({
        "per_camera": {}, "advice": "x",
        "negative_lag_episodes": [], "flagged_episodes": [], "noisy_episodes": [],
        "suspect_episodes": [{"episode_id": "ep000009", "verdict": "annotated",
                              "cameras": {"ext": cam}}],
    })
    assert "疑似错位,证据不足" in md and "ep000009" in md
    assert "### 假峰(" not in md                 # 没有假峰就不出这一节
    assert "换更近的机位" in md


def test_health_table_has_no_statistical_jargon():
    """客户读的是报告不是 UI —— 黑话留在这里等于没改。"""
    md = _report({
        "per_camera": {"ext": {"n": 3, "median_lag_s": 0.22, "iqr_s": 0.05,
                               "n_flagged": 1, "n_suspect": 0, "n_noisy": 0,
                               "n_abstained": 0}},
        "advice": "x", "negative_lag_episodes": [], "flagged_episodes": [],
    })
    assert "IQR" not in md and "四分位距" not in md
    for col in ("有效读数", "典型滞后", "逐条波动", "疑似错位", "假峰",
                "测不准", "已标注"):
        assert col in md, col


def test_legacy_health_without_new_fields_does_not_crash():
    """老交付没有 suspect/noisy 字段 → 只出原有内容,不报错、不出空节。"""
    md = _report({"per_camera": {"ext": {"n": 2, "median_lag_s": 0.0,
                                         "iqr_s": 0.0, "n_flagged": 0}},
                  "advice": "老版本", "negative_lag_episodes": [],
                  "flagged_episodes": []})
    assert "相机流健康度" in md
    assert "假峰 / 测不准" not in md and "疑似错位,证据不足" not in md


def _report_with_plots(mode, n_plot=2):
    return to_markdown({
        "数据集": "d", "机器人": {}, "生成时间": "t", "代码版本": "v",
        "config_effective": {"pipeline": {"sync_plots": mode}},
        "dataset": {"input_episodes": 7, "hard_gate_filtered": 0,
                    "verdict_keep": 7, "verdict_drop": 0, "dedup_removed": 0,
                    "delivered": 7, "hard_fail_breakdown": {},
                    "sync_plots": {"生成": n_plot, "目录": "details/plots/"},
                    "sync_health": {"per_camera": {}, "advice": "x",
                                    "negative_lag_episodes": [],
                                    "flagged_episodes": []}},
        "skills": {}, "config": {},
        "episodes": {"results": [], "dropped": []},
    })


def test_plot_coverage_is_stated():
    """只给问题条目画图时必须说破:没有图 ≠ 没检查。"""
    md = _report_with_plots("flagged")
    assert "只为需要留意的条目画图" in md and "没有图不等于没检查" in md
    assert "sync_plots=all" in md

    md_all = _report_with_plots("all", n_plot=7)
    assert "每条被检查的 episode 都出了图" in md_all
    assert "没有图不等于没检查" not in md_all


def test_kill_policy_is_spelled_out():
    """"标注了"不等于"坏了" —— 判废口径必须白纸黑字。"""
    md = _report_with_plots("all")
    assert "判废口径" in md
    assert "所有可信相机一致指向同一个偏移" in md
    assert "不进人工裁决队列" in md and "数据照常交付" in md


def test_abstained_episodes_get_their_own_section():
    """「其余测不准」(弃权路)也要逐条立账(2026-09-01 用户抓出:UI 筛选与曲线
    亮出它们之后,报告只剩逐相机计数列,读者对着图答不上"这条哪路怎么了")。
    老交付没有 abstained_episodes 键 → 不出这一节,不崩。"""
    cam = dict(_NOISY_CAM)
    cam["diagnosis"] = {"cause": "blurry_motion", "label": "测不准 · 画面不锐利",
                        "text": "定位精度不够", "advice": "换更近的机位"}
    md = _report({
        "per_camera": {}, "advice": "x",
        "negative_lag_episodes": [], "flagged_episodes": [],
        "noisy_episodes": [], "suspect_episodes": [],
        "abstained_episodes": [{"episode_id": "ep000020", "verdict": "aligned",
                                "cameras": {"ext2": cam}}],
    })
    assert "### 测不准(" in md and "ep000020" in md
    assert "测不准 · 画面不锐利" in md and "结论以其余相机为准" in md
    # 老交付(无该键):整节静默跳过
    md_old = _report({"per_camera": {}, "advice": "x",
                      "negative_lag_episodes": [], "flagged_episodes": []})
    assert "### 测不准(" not in md_old
