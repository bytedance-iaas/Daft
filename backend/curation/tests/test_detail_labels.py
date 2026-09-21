"""明细 CSV 中文列名 + 整列不适用折叠(2026-09-02 用户定)。

CSV 对 CLI 用户就是报告:列名直接写中文;整列全空的子项(如 droid 的执行器饱和)不出
这一列改成一句说明;部分为空的子项列保留、界面显示「—」;数值一个不动。
"""
from __future__ import annotations

from curation.export import detail_labels as DL
from curation.export.report import to_markdown


def _rows():
    return [
        {"episode": "ep0", "score": 0.9, "smoothness": 0.5, "spike": 1.0, "stuck": 1.0,
         "gripper_jitter": 1.0, "actuator_saturation": None, "joint_stability": 0.2,
         "path_efficiency": 0.0, "fluency": 1.0, "active_ratio": 1.0,
         "saturation_gap_ratio": None, "spike_isolation": 2.6},
        {"episode": "ep1", "score": 0.8, "smoothness": 0.6, "spike": None, "stuck": 1.0,
         "gripper_jitter": 1.0, "actuator_saturation": None, "joint_stability": 0.3,
         "path_efficiency": 0.1, "fluency": 0.9, "active_ratio": 0.8,
         "saturation_gap_ratio": None, "spike_isolation": None},
    ]


def test_all_none_column_is_dropped_with_reason_and_companions():
    """执行器饱和整列空 → 列不出、饱和偏差比一并折叠、原因取留痕文本。"""
    h, rows, na, partial = DL.collapse_na_columns(
        _rows(), DL.MOTION_COLS,
        reasons={"actuator_saturation": "指令与读数不同空间"},
        companions=DL.MOTION_COMPANION)
    assert "执行器饱和" not in h and "饱和偏差比" not in h
    assert na == {"执行器饱和": "指令与读数不同空间"}
    assert h[:3] == ["条目", "运动总分", "平滑度"]          # 列名中文、顺序不变
    assert rows[0]["平滑度"] == 0.5 and rows[1]["条目"] == "ep1"   # 数值不动


def test_partial_none_column_is_kept_and_noted():
    """尖刺只有 ep1 空 → 列保留、记为部分不适用(原因没留痕就用通用说法)。"""
    h, rows, na, partial = DL.collapse_na_columns(
        _rows(), DL.MOTION_COLS, reasons={}, companions=DL.MOTION_COMPANION)
    assert "尖刺" in h and "尖刺孤立度" in h
    assert partial == {"尖刺": DL.GENERIC_NA_REASON}
    assert rows[1]["尖刺"] is None                               # CSV 写空,界面显「—」


def test_no_rows_means_nothing_is_judged_not_applicable():
    h, rows, na, partial = DL.collapse_na_columns([], DL.MOTION_COLS)
    assert rows == [] and na == {} and partial == {}
    assert h == [lb for _, lb in DL.MOTION_COLS]


def test_translate_rows_maps_headers_and_enums():
    h, rows = DL.translate_rows(
        [{"episode": "e", "camera": "wrist", "status": "PAD"},
         {"episode": "e", "camera": "ext", "status": "OK", "sharpness": 0.7}],
        DL.VISUAL_COLS, enums={"status": DL.VISUAL_STATUS})
    assert h[:4] == ["条目", "相机", "状态", "视觉总分"]
    assert rows[0]["状态"] == "占位(无画面)" and rows[1]["状态"] == "正常"
    assert rows[1]["清晰度"] == 0.7 and rows[0]["清晰度"] is None
    hk, rk = DL.translate_rows([{"episode": "e", "type": "joint_limit", "joint": 3}],
                               DL.KINEMATIC_COLS, enums={"type": DL.KINEMATIC_TYPES})
    assert rk[0]["违规类型"] == "关节超限" and rk[0]["关节/轴"] == 3


def test_report_prints_subdim_applicability_section():
    """报告(CLI 交付的 report.md 同源)有整列不适用时多一节,说清哪列没列、为什么。"""
    base = {"数据集": "d", "机器人": {}, "生成时间": "t", "代码版本": "v",
            "dataset": {"input_episodes": 1, "hard_gate_filtered": 0, "verdict_keep": 1,
                        "verdict_drop": 0, "dedup_removed": 0, "delivered": 1,
                        "hard_fail_breakdown": {}},
            "skills": {}, "config": {}, "episodes": {"results": [], "dropped": []}}
    md = to_markdown(base)
    assert "运动质量子项适用性" not in md
    base["dataset"]["motion_subdims"] = {
        "不适用": {"执行器饱和": "指令与读数不同空间"},
        "部分不适用": {"尖刺": "运动占空比<0.15"}}
    md = to_markdown(base)
    assert "## 运动质量子项适用性" in md
    assert "执行器饱和:本数据集不适用——指令与读数不同空间(明细表不列此列)" in md
    assert "尖刺:部分条目不适用,明细表中以「—」标出——运动占空比<0.15" in md
