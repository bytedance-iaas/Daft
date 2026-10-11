"""VLM 调用类型的名字、顺序与解释：报告共用这一处（2026-08-15 起），v1 的五类随 v1 删掉（D89）。"""
from __future__ import annotations

import re

from curation.vlm_call_kinds import CALL_KIND_LABELS, CALL_KIND_NOTES, CALL_KIND_ORDER


def test_display_names_and_flow_order():
    assert CALL_KIND_ORDER == ["probe", "eef_review"]
    assert CALL_KIND_LABELS == {"probe": "任务成败判定", "eef_review": "EEF 复核"}
    # 埋点标签是数据契约：显示名怎么改，键都不动
    assert set(CALL_KIND_NOTES) == set(CALL_KIND_LABELS) == set(CALL_KIND_ORDER)


def test_the_judgement_is_one_joint_request_per_episode():
    """D71：任务成败每条一次请求，多机位连续视频一起看；不再写「逐帧」，也不再有复核和仲裁。"""
    probe = CALL_KIND_NOTES["probe"]
    assert "连续视频" in probe and "联合" in probe and "每条" in probe
    assert "逐帧" not in probe and "复核" not in probe and "仲裁" not in probe


def test_bold_markers_actually_render_in_chinese_text():
    """中文 markdown 的坑：收尾 `**` 前面不能是标点。

    CommonMark 规定「前有标点、后接文字」的 `**` 不算合法收尾定界符，整段星号会原样印在页面上
    （2026-08-15 实见）。这条对全部对外文案生效。
    """
    bad = []
    for tag, note in CALL_KIND_NOTES.items():
        for m in re.finditer(r"\*\*([^*]+?)\*\*", note):
            if m.group(1)[-1] in ")）】」』,,。;;::!!??":
                bad.append(f"{tag}: **{m.group(1)}**")
    assert not bad, "收尾 ** 前是标点，页面上会印出星号：" + "; ".join(bad)
