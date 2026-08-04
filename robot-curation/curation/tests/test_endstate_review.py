"""endstate_review(二值复核协议)分支测试(v6.5,2026-08-04)。

背景:该协议原长在 funnel 闭包里,无法独立测试;抽成 core 纯函数后本文件把
**每条判决分支**钉死:漏斗/考卷/单测共用同一份协议。

v6.5 判决矩阵(初判 × 复核 → 终判;救人一签,杀人双签,复核有否决权无处决权):
    成功候选 × 复核True   → 过(全员复核:成功不再免检,判官必须被调用)
    成功候选 × 复核False  → 强分数过(标 review_disputed)/ 弱分数弃权进人工
    成功候选 × 复核矛盾   → 过(标 review_contradictory)
    失败候选 × 复核True   → 救回 pass
    失败候选 × 复核False  → **杀**(全表唯一杀格,双签)
    失败候选 × 复核矛盾   → 弃权(缺第二签)
    gap契约违约 × 复核True → 弃权(两层打架,谁也不赢——ep143 拦截机制)
    灰区 × 复核True       → 救回 pass
    灰区 × 复核False/矛盾 → 弃权
    判官不可用            → 成功候选过(留痕单判据);失败候选降弃权(废除单方杀)
"""
from __future__ import annotations

import numpy as np
import pytest

from curation.core.checks.task_success import endstate_review
from curation.core.contract import CheckResult


def _res(passed, verdict="failure", strong=None):
    detail = {"verdict": verdict, "reason": "voc-orig"}
    if strong is not None:
        detail["strong_score"] = strong
    return CheckResult(name="task_success", passed=passed, detail=detail)


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

def test_success_is_reviewed_not_short_circuited():
    """v6.5 核心改动:成功候选**不再免检**——判官必须被调用(否决权的前提)。
    (旧协议在此短路,ep131"空夹叶子"型自洽错话从免检通道直接溜走)"""
    j = _judge(True)
    r = endstate_review(_res(True, "success", strong=True), "t", j, _frames())
    assert r.passed is True and len(j.calls) == 1
    assert r.detail["endstate_answer"] == "yes"


def test_success_weak_vetoed_to_human():
    """弱成功候选被复核否决 → 弃权进人工(否决权,非处决权)。"""
    r = endstate_review(_res(True, "success", strong=False), "t", _judge(False), _frames())
    assert r.passed is None
    assert r.detail["verdict"] == "endstate_failure_suspect"


def test_success_strong_survives_veto_flagged():
    """强分数压过否决,带 review_disputed 标签放行(供下游过滤)。"""
    r = endstate_review(_res(True, "success", strong=True), "t", _judge(False), _frames())
    assert r.passed is True and r.detail.get("review_disputed") is True


def test_success_review_contradiction_passes_flagged():
    r = endstate_review(_res(True, "success", strong=True), "t", _judge(None), _frames())
    assert r.passed is True and r.detail.get("review_contradictory") is True


def test_fail_judge_true_rescues():
    """ep34 形态:打分层判失败,复核看到完成 → 救回。"""
    r = endstate_review(_res(False), "t", _judge(True), _frames())
    assert r.passed is True
    assert r.detail["verdict"] == "endstate_success"
    assert "救回" in r.detail["reason"]


def test_gray_judge_true_rescues():
    r = endstate_review(_res(None, "uncertain"), "t", _judge(True), _frames())
    assert r.passed is True and "救回" in r.detail["reason"]


def test_fail_judge_false_kills():
    """全表唯一杀格:失败候选 + 复核未完成 = 双签硬杀。"""
    r = endstate_review(_res(False), "t", _judge(False), _frames())
    assert r.passed is False
    assert r.detail["verdict"] == "failure"
    assert "双判据一致" in r.detail["reason"]


def test_gray_judge_false_abstains():
    r = endstate_review(_res(None, "uncertain"), "t", _judge(False), _frames())
    assert r.passed is None and "进人工" in r.detail["reason"]


def test_fail_judge_contradiction_abstains():
    """失败候选缺第二签(两问矛盾)→ 弃权,不许单方杀(冤杀保险丝)。"""
    r = endstate_review(_res(False), "t", _judge(None), _frames())
    assert r.passed is None
    assert r.detail["endstate"] == "两问法矛盾,不采信"
    assert "不硬杀" in r.detail["reason"]


def test_gray_judge_contradiction_stays_none():
    r = endstate_review(_res(None, "uncertain"), "t", _judge(None), _frames())
    assert r.passed is None
    assert r.detail["endstate"] == "两问法矛盾,不采信"
    assert r.detail["reason"] == "voc-orig"          # 不改写原 reason


def test_gap_violation_vs_judge_true_conflicts():
    """两层打架:契约违约曲线 + 复核 yes → 弃权(ep143 拦截;ep19 烂机位同型)。"""
    r = endstate_review(_res(None, "gap_violation"), "t", _judge(True), _frames())
    assert r.passed is None and r.detail["verdict"] == "review_conflict"


def test_no_judge_fail_candidate_downgrades():
    """v6.5:判官不可用时失败候选降弃权——杀人永远双签(废除旧'单判据维持硬杀')。"""
    r = endstate_review(_res(False), "t", None, _frames())
    assert r.passed is None
    assert r.detail["endstate"] == "二值复核不可用,仅打分层单判据"


def test_no_judge_success_passes_with_note():
    r = endstate_review(_res(True, "success", strong=True), "t", None, _frames())
    assert r.passed is True
    assert "单判据" in r.detail["endstate"]


# ───────── 帧供给语义 ─────────

def test_no_frames_no_judge_call():
    """极端情形:一帧都没有 → 判官不被调用,原样返回(不许拿空图去问)。"""
    j = _judge(True)
    r = endstate_review(_res(False), "t", j, [], extra_frames_fn=lambda: [])
    assert r.passed is False and not j.calls


def test_extras_fed():
    """其余相机帧真的进了 starts/ends:主 16 帧→8(4+4),两路各 8→各 4+4 ⇒ 12+12。"""
    j = _judge(True)
    called = []

    def extras():
        called.append(1)
        return [_frames(8), _frames(8)]

    endstate_review(_res(False), "t", j, _frames(16), extra_frames_fn=extras,
                    endstate_frames=8)
    assert called == [1]
    assert j.calls[0][:2] == (12, 12)


def test_frame_split_semantics():
    """双问法前后对照:帧按中点分 starts/ends;单帧时两组都拿到它。"""
    j = _judge(True)
    endstate_review(_res(False), "t", j, _frames(1))
    assert j.calls[0][:2] == (1, 1)                  # pick[:1] 与 pick[-1:] 都是那一帧


def test_endstate_frames_cap():
    """每路帧数封顶 endstate_frames(图太多干扰模型且涨 token)。"""
    j = _judge(True)
    endstate_review(_res(False), "t", j, _frames(100), endstate_frames=8)
    n_starts, n_ends, _ = j.calls[0]
    assert n_starts + n_ends <= 8                    # linspace 精确取 8


def test_material_includes_endpoint_frames():
    """ep30 截尾 bug 回归:linspace 含端点——首帧与**真末帧**必须都在素材里。"""
    frames = [np.full((4, 4, 3), i, np.uint8) for i in range(37)]   # 亮度=下标
    j = _judge(True)
    endstate_review(_res(False), "t", j, frames, endstate_frames=8)
    # 通过亮度还原判官实际收到的帧下标
    seen = []

    def spy(starts, ends, desc):
        seen.extend(int(f.mean()) for f in list(starts) + list(ends))
        return True

    endstate_review(_res(False), "t", spy, frames, endstate_frames=8)
    assert 0 in seen and 36 in seen, f"素材帧下标 {sorted(seen)} 缺端点(截尾回归!)"
