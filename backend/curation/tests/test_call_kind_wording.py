"""五类 VLM 调用的名字/顺序/解释:界面与报告共用单一事实源(2026-08-15)。

防的是哪次事故:同一类调用在界面叫「任务判定探针」、在客户报告里叫
「渐变问询(VOC)」——第三套中英夹杂的内部说法直接印给了客户;且解释错了
两处事实(复核被说成"只对没通过一审的跑",实际全员运行;打分被说成"逐帧问",
实际抽样后按时刻多视角联合问),取证仲裁整条漏了解释。
"""
from __future__ import annotations

from curation.vlm_call_kinds import (CALL_KIND_LABELS, CALL_KIND_NOTES,
                                     CALL_KIND_ORDER)

#: 五个桶都有数的延时快照(insertion 顺序故意打乱:若渲染端拿 dict 序而不是
#: CALL_KIND_ORDER,下面的顺序断言当场红)
_LAT_ALL = {
    "llm": {"n": 7, "errors": 0, "mean_s": 50.7, "p50_s": 15.7, "p90_s": 130.0,
            "p99_s": 130.0, "max_s": 130.0},
    "caption": {"n": 200, "errors": 0, "mean_s": 17.3, "p50_s": 15.5,
                "p90_s": 28.1, "p99_s": 41.2, "max_s": 54.6},
    "probe": {"n": 1583, "errors": 0, "mean_s": 20.0, "p50_s": 17.8,
              "p90_s": 33.5, "p99_s": 63.3, "max_s": 118.6},
    "endstate": {"n": 1194, "errors": 2, "mean_s": 13.6, "p50_s": 12.3,
                 "p90_s": 20.4, "p99_s": 36.5, "max_s": 40.5},
    "arbitration": {"n": 33, "errors": 0, "mean_s": 22.0, "p50_s": 20.0,
                    "p90_s": 40.0, "p99_s": 57.5, "max_s": 57.5},
}


def test_display_names_and_flow_order():
    """显示名定案 + 流程顺序定案(方案 B):打分→复核→仲裁→打标→归纳。"""
    assert CALL_KIND_ORDER == ["probe", "endstate", "arbitration", "caption", "llm"]
    assert CALL_KIND_LABELS == {"probe": "任务完成度打分", "endstate": "逐机位复核",
                                "arbitration": "取证仲裁", "caption": "技能打标",
                                "llm": "技能归纳"}
    # CSV 埋点标签是数据契约:显示名怎么改,键一个字不许动(老交付要读得回来)
    assert set(CALL_KIND_NOTES) == set(CALL_KIND_LABELS) == set(CALL_KIND_ORDER)


def test_notes_pin_three_facts():
    """★事实钉死(2026-08-15 用户点名的一漏两错,不许改回错误说法):

    ① 逐机位复核**全员运行**(代码注释:复核层全员运行并获得否决权;次数对账
       1194 ÷ ~200 条 ≈ 6 = 3 机位 × 2 问)——不许再写成"只对没通过一审的跑";
    ② 只有取证仲裁是**拿不准(弃权)才跑**(task_success:passed is None 才进链);
    ③ 打分是**抽帧后按时间顺序联合问**(多视角同一时刻一起给)——不许再写"逐帧问画面"。
    """
    endstate = CALL_KIND_NOTES["endstate"]
    assert "每条" in endstate, "复核是全员跑,解释里必须说清"
    assert "只对" not in endstate and "一审" not in endstate and "拿不准" not in endstate
    arb = CALL_KIND_NOTES["arbitration"]
    assert "只对" in arb and ("拿不准" in arb or "弃权" in arb)
    probe = CALL_KIND_NOTES["probe"]
    assert "连续视频" in probe and "联合" in probe
    assert "打乱" not in probe
    assert "逐帧" not in probe


def test_bold_markers_actually_render_in_chinese_text():
    """★ 中文 markdown 的坑:收尾 `**` 前面**不许是标点**。

    CommonMark 规定"前有标点、后接文字"的 `**` 不算合法收尾定界符 —— 于是整段
    星号会原样印在页面上。2026-08-15 实见:`**拿不准(弃权)**` 在界面上就是带
    星号的字面量,而同一段里的 `**每条**` 正常加粗,差别只在收尾前一个字符是
    汉字还是右括号。这条对全部对外文案生效,不只是这五条解释。
    """
    import re

    from curation.vlm_call_kinds import CALL_KIND_NOTES

    bad = []
    for tag, note in CALL_KIND_NOTES.items():
        # 成对取出 `**…**`,看收尾前一个字符
        for m in re.finditer(r"\*\*([^*]+?)\*\*", note):
            if m.group(1)[-1] in ")）】」』,,。;;::!!??":
                bad.append(f"{tag}: **{m.group(1)}**")
    assert not bad, "收尾 ** 前是标点,页面上会印出星号:" + "; ".join(bad)
