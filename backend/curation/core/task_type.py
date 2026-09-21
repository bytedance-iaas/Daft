"""任务类型:瞬时(transient)还是持久(persistent)。(2026-09-18 方案 2,umi 抽屉条目教训)

"拿起 / 举起 / 取出 / 拔出 / 抓住"这类任务,做成的那一刻是**物体在手里、离开原位**,之后放哪、
镜头转哪都与任务无关,最后一帧常常什么都看不出来;"放进 / 放到 / 挂上"这类任务则相反,做成
与否要看撤手之后的末态。以前这个区分只在取证仲裁的出题器里做(ARB_QUESTION_PROMPT),打分层
与复核层一概按"末态"判,瞬时任务的"冲高又回落"被当成契约违约,好数据进人工。

这里把类型判定提前到意图确定之时、只判一次、三层共用:
  1. 先按关键词(纯规则,零成本、可复现):有放置线索 → 持久;以瞬时动词开头 → 瞬时;
  2. 规则判不出 → 交给注入的出题器(同一次调用产出的 spec 交给仲裁复用,不再重问);
  3. 出题器不可用/异常 → 持久(老口径,保守)。
将来深改的"动作模式"槽落地后,直接替换第 1、2 步。
"""
from __future__ import annotations

import re
from typing import Callable

TRANSIENT = "transient"
PERSISTENT = "persistent"

# 放置线索:只要出现,就是持久任务(哪怕开头是 pick up:"pick up X and put it in Y")
_PLACEMENT = re.compile(
    r"\b(put|place|places|placing|drop|insert|hang|stack|set|move|transfer|pour|scoop|"
    r"return|load|store|slide|push|press|open|close|fold|wipe|flip|stir|arrange|tidy|"
    r"turn|unzip|zip|cover|uncover|attach|plug|screw|unscrew|clean|rotate|twist|"
    r"swipe|drag|sweep)\b"
    r"|\b(into|onto|in to|on to|toward|towards)\b|\bin the\b|\bon the\b"
    r"|\bto the (left|right|center|centre|middle|side|front|back|top|bottom|edge|corner)\b",
    re.IGNORECASE)
# 瞬时动词:以此开头且无放置线索 → 瞬时
_TRANSIENT_HEAD = re.compile(
    r"^\s*(pick\s+up|pick|lift|raise|grab|grasp|hold|take\s+out|take|remove|pull\s+out|"
    r"pull|get|retrieve|fetch|catch)\b",
    re.IGNORECASE)
# 瞬时动词后常见的"从 A 拿出"结构:take X out of A / remove X from A / take X off A
_OUT_OF = re.compile(r"\b(out\s+of|from|off)\b", re.IGNORECASE)


def classify_by_rule(text: str) -> str | None:
    """关键词判定;判不出返回 None(交给出题器)。"""
    t = str(text or "").strip()
    if not t:
        return None
    if _TRANSIENT_HEAD.match(t):
        rest = _TRANSIENT_HEAD.sub("", t, count=1)
        # "take the box out of the drawer":out of / from 不算放置;其它放置词才算
        if _PLACEMENT.search(re.sub(r"\b(out\s+of|from|off)\b\s+\S+(\s+\S+)?", "", rest, flags=re.I)):
            return PERSISTENT
        return TRANSIENT
    if _PLACEMENT.search(t):
        return PERSISTENT
    return None


def resolve(text: str, question_writer: Callable | None = None) -> tuple[str, dict | None, str]:
    """→ (task_type, spec|None, source)。source ∈ rule / writer / default。
    spec 是出题器整份输出(task_type/target_location/verify_question…),给仲裁复用。"""
    by_rule = classify_by_rule(text)
    if by_rule:
        return by_rule, None, "rule"
    if question_writer is not None:
        try:
            spec = dict(question_writer(str(text)))
            tt = str(spec.get("task_type") or "").strip().lower()
            if tt in (TRANSIENT, PERSISTENT):
                return tt, spec, "writer"
        except Exception:  # noqa: BLE001  出题失败=没有类型证据,按持久
            pass
    return PERSISTENT, None, "default"
