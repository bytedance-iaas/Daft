"""判成败任务文本策略的绊线(2026-09-02 用户要求:切换到自产标注优先时自动触发)。

策略唯一落点 = pipeline.run.judge_text_and_source;本测试钉住"标注优先"。哪天把它翻成
caption_first,本测试必红,失败信息列出的两条就是切换 checklist——不是让你改回来,
是让你**先做完那两条再把本测试改成钉新策略**。
"""
from __future__ import annotations

from curation.pipeline.run import JUDGE_TEXT_POLICY, judge_text_and_source

CHECKLIST = (
    "判成败文本策略已不是标注优先——切换前必须同步:\n"
    "  ① funnel._label_guard 触发条件改为「标注与 caption 两段都在且不同」"
    "(现只在 task_src=='原始标注' 时触发,切换后判废护栏静默休眠);\n"
    "  ② funnel._arbitrate 的 annotation 不再只在原始标注时传入"
    "(仲裁双意图链同款盲区);\n"
    "  做完再把本测试改成钉新策略。"
)


def test_judge_text_policy_tripwire():
    assert JUDGE_TEXT_POLICY == "annotation_first", CHECKLIST
    # 行为钉:两段都在 → 用标注;只有 caption → 用 caption;都没有 → 无
    assert judge_text_and_source("put x", "place x") == ("put x", "原始标注"), CHECKLIST
    assert judge_text_and_source("  ", "place x") == ("place x", "自产caption")
    assert judge_text_and_source(None, "") == ("", "无")


def test_label_guard_gate_matches_policy():
    """护栏与仲裁的门控字面钉在源码上:策略翻了而这两处没动,这里也红。"""
    import inspect
    from curation.pipeline import funnel
    src = inspect.getsource(funnel)
    assert 'res.passed is False and str(task_src) == "原始标注"' in src, CHECKLIST
    assert 'annotation = str(task_desc) if src == "原始标注" else ""' in src, CHECKLIST
