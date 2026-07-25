"""endstate_review(二值复核协议)分支测试(2026-07-23)。

背景:该协议原长在 funnel 闭包里,无法独立测试——直接后果是小考只能考裸 VOC 层,
得出"8B 冤杀 2/10"的错误结论(走全协议实测 1 救回 1 进人工,硬杀 0)。
抽成 core 纯函数后,本文件把**每条判决分支**钉死:漏斗/考卷/单测共用同一份协议。

判决矩阵(VOC 初判 × 复核结论 → 终判):
    VOC=True             → 复核不启动,原样返回
    VOC=False × 复核True  → 救回 pass(OR 原则)
    VOC=None  × 复核True  → 救回 pass
    VOC=False × 复核False → 维持硬杀(双判据一致)
    VOC=None  × 复核False → 弃权进人工(不硬杀)
    VOC=False × 复核矛盾  → 降级弃权(不凭 VOC 单方硬杀)
    VOC=None  × 复核矛盾  → 维持弃权
    判官不可用            → detail 留痕"仅 VOC 单判据",判定不变
"""
from __future__ import annotations

import numpy as np
import pytest

from curation.core.checks.task_success import endstate_review
from curation.core.contract import CheckResult


def _res(passed, verdict="failure"):
    return CheckResult(name="task_success", passed=passed,
                       detail={"verdict": verdict, "reason": "voc-orig"})


def _frames(n=16):
    return [np.zeros((4, 4, 3), np.uint8) for _ in range(n)]


def _judge(answer):
    """可断言调用情况的假判官。"""
    calls = []

    def judge(starts, ends, desc):
        calls.append((len(starts), len(ends), desc))
        return answer
    judge.calls = calls
    return judge


# ───────── 判决矩阵逐格 ─────────

def test_voc_pass_short_circuits():
    """VOC 已判成功 → 复核不启动:判官与惰性解码都不许被调用(省钱+省时)。"""
    j = _judge(True)
    extras_called = []
    r = endstate_review(_res(True, "success"), "t", j, _frames(),
                        extra_frames_fn=lambda: extras_called.append(1) or [])
    assert r.passed is True and not j.calls and not extras_called


def test_voc_false_judge_true_rescues():
    """ep34 形态:VOC 有把握判失败,复核看到完成 → OR 救回。"""
    r = endstate_review(_res(False), "t", _judge(True), _frames())
    assert r.passed is True
    assert r.detail["verdict"] == "endstate_success"
    assert "判失败" in r.detail["reason"]


def test_voc_none_judge_true_rescues():
    r = endstate_review(_res(None, "uncertain"), "t", _judge(True), _frames())
    assert r.passed is True and "不可判" in r.detail["reason"]


def test_voc_false_judge_false_keeps_kill():
    """双判据一致判失败 → 唯一允许硬杀的路径。"""
    r = endstate_review(_res(False), "t", _judge(False), _frames())
    assert r.passed is False
    assert r.detail["verdict"] == "endstate_failure_suspect"
    assert "一致判未完成" in r.detail["reason"]


def test_voc_none_judge_false_abstains():
    """VOC 本就拿不准,复核判未完成 → 存疑进人工,不硬杀。"""
    r = endstate_review(_res(None, "uncertain"), "t", _judge(False), _frames())
    assert r.passed is None and "不硬杀" in r.detail["reason"]


def test_voc_false_judge_contradiction_downgrades():
    """复核两问矛盾不采信 → 不凭 VOC 单方硬杀,降级弃权(冤杀保险丝)。"""
    r = endstate_review(_res(False), "t", _judge(None), _frames())
    assert r.passed is None
    assert r.detail["endstate"] == "两问法矛盾,不采信"
    assert "存疑进人工" in r.detail["reason"]


def test_voc_none_judge_contradiction_stays_none():
    r = endstate_review(_res(None, "uncertain"), "t", _judge(None), _frames())
    assert r.passed is None
    assert r.detail["endstate"] == "两问法矛盾,不采信"
    assert r.detail["reason"] == "voc-orig"          # 不改写原 reason


def test_no_judge_leaves_note():
    """判官构造失败 → 留痕"仅 VOC 单判据",判定原样(报告可见,不静默)。"""
    r = endstate_review(_res(False), "t", None, _frames())
    assert r.passed is False
    assert r.detail["endstate"] == "二值复核不可用,仅 VOC 单判据"


# ───────── 帧供给语义 ─────────

def test_no_frames_no_judge_call():
    """极端情形:一帧都没有 → 判官不被调用,原样返回(不许拿空图去问)。"""
    j = _judge(True)
    r = endstate_review(_res(False), "t", j, [], extra_frames_fn=lambda: [])
    assert r.passed is False and not j.calls


def test_extras_lazy_and_fed():
    """其余相机帧惰性供给:触发复核才调用,且帧真的进了 starts/ends。"""
    j = _judge(True)
    called = []

    def extras():
        called.append(1)
        return [_frames(8), _frames(8)]

    endstate_review(_res(False), "t", j, _frames(16), extra_frames_fn=extras,
                    endstate_frames=8)
    assert called == [1]
    n_starts, n_ends, _ = j.calls[0]
    # 主相机 16 帧步进采样→8 帧(4+4),两路补充相机各 8 帧(各 4+4)⇒ 12+12
    assert (n_starts, n_ends) == (12, 12)


def test_frame_split_semantics():
    """双问法前后对照:帧按中点分 starts/ends;单帧时两组都拿到它。"""
    j = _judge(True)
    endstate_review(_res(False), "t", j, _frames(1))
    assert j.calls[0][:2] == (1, 1)                  # fr[:1] 与 fr[-1:] 都是那一帧


def test_endstate_frames_cap():
    """每路帧数封顶 endstate_frames(图太多干扰模型且涨 token)。"""
    j = _judge(True)
    endstate_review(_res(False), "t", j, _frames(100), endstate_frames=8)
    n_starts, n_ends, _ = j.calls[0]
    assert n_starts + n_ends <= 9    # 100//8=12 步进取 9 帧,截到 8;分组 4+4(容 1 帧余量)
