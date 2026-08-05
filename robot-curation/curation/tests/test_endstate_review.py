"""endstate_review(逐机位复核 v7.2)分支测试(2026-08-04)。

背景:该协议原长在 funnel 闭包里,无法独立测试;抽成 core 纯函数后本文件把
**每条判决分支**钉死:漏斗/考卷/单测共用同一份协议。

v7.2 判决矩阵(初判 × 汇票 → 终判;救人一签、杀人双签、复核有否决权无处决权):
    成功候选 × yes/split/abstain → 过(split/abstain 留痕)
    成功候选 × no                → 强分数过(disputed)/ 弱分数弃权进人工
    失败候选 × yes               → 人工(两层打架:全程无进度 vs 复核说完成)
    失败候选 × no                → **杀**(全表唯一杀格,双签)
    失败候选 × split/abstain     → 弃权(缺第二签)
    gap契约违约 × yes            → 人工(打架;联合打分平滑掉violation时靠灰区版拦)
    灰区 × yes 且 final≤0.25     → 人工(末态回原点 vs 复核说完成 = 实质矛盾,ep143)
    score_blind × yes 孤证(<2票) → 人工(打分层零信息,救回也双签,8b ep143)
    其余 × yes                   → 救回;× 其它 → 弃权
    投票器不可用                 → 成功候选过(留痕);失败候选降弃权(废除单方杀)
"""
from __future__ import annotations

import numpy as np
import pytest

from curation.core.checks.task_success import cam_vote, endstate_review, tally
from curation.core.contract import CheckResult


def _res(passed, verdict="failure", strong=None, final=None):
    detail = {"verdict": verdict, "reason": "score-orig"}
    if strong is not None:
        detail["strong_score"] = strong
    if final is not None:
        detail["completion_final"] = final
    return CheckResult(name="task_success", passed=passed, detail=detail)


def _cams(n_cams=3, n_frames=16):
    return {f"cam{i}": [np.zeros((4, 4, 3), np.uint8) for _ in range(n_frames)]
            for i in range(n_cams)}


def _voter(*answers):
    """按相机顺序依次吐出指定票;记录每次调用的 (帧数, 标签)。"""
    seq = list(answers)
    calls = []

    def voter(starts, ends, cam_label, desc):
        calls.append((len(starts), len(ends), cam_label))
        return seq[len(calls) - 1] if len(calls) <= len(seq) else seq[-1]
    voter.calls = calls
    return voter


# ───────── 票的合成与汇总(纯函数) ─────────

def test_cam_vote_matrix():
    assert cam_vote("yes", "no") == "yes"
    assert cam_vote("no", "yes") == "no"
    assert cam_vote("unclear", "yes") == "unclear"     # 弃权优先于矛盾
    assert cam_vote("yes", "unclear") == "unclear"
    assert cam_vote("yes", "yes") == "contradictory"   # 既完成又失败=胡说
    assert cam_vote("no", "no") == "contradictory"
    assert cam_vote("?", "no") == "contradictory"


def test_tally_asymmetry():
    assert tally(["yes", "unclear", "contradictory"]) == "yes"    # 实票 1yes 0no
    assert tally(["no", "unclear", "unclear"]) == "no"
    assert tally(["yes", "no", "unclear"]) == "split"             # 证人真打架
    assert tally(["unclear", "contradictory", "unavail"]) == "abstain"


# ───────── 判决矩阵逐格 ─────────

def test_success_is_reviewed_not_short_circuited():
    """成功候选不免检:每个机位都被独立问到(否决权的前提)。"""
    v = _voter("yes", "yes", "yes")
    r = endstate_review(_res(True, "success", strong=True), "t", v, _cams())
    assert r.passed is True and len(v.calls) == 3
    assert r.detail["review"] == "yes"


def test_success_weak_vetoed_by_unanimous_no():
    r = endstate_review(_res(True, "success", strong=False), "t",
                        _voter("no", "no", "unclear"), _cams())
    assert r.passed is None
    assert r.detail["verdict"] == "endstate_failure_suspect"


def test_success_strong_survives_veto_flagged():
    r = endstate_review(_res(True, "success", strong=True), "t",
                        _voter("no", "no", "no"), _cams())
    assert r.passed is True and r.detail.get("review_disputed") is True


def test_success_weak_abstain_goes_to_human():
    """v7.3 钉子:弱成功 + 全体弃权/矛盾 = 纯打分层孤证 → 人工(不可复现性下
    弱曲线跨 run 漂移,孤证放行是漏径——ep131 冒烟实锤);强成功不受影响。"""
    r = endstate_review(_res(True, "success", strong=False), "t",
                        _voter("unclear", "contradictory", "unclear"), _cams())
    assert r.passed is None and r.detail["verdict"] == "endstate_unconfirmed"
    r = endstate_review(_res(True, "success", strong=True), "t",
                        _voter("unclear", "contradictory", "unclear"), _cams())
    assert r.passed is True


def test_success_split_passes_flagged():
    """split(实票打架)≠否决:有看得清的证人说做成了 → 过,留痕可过滤。"""
    r = endstate_review(_res(True, "success", strong=False), "t",
                        _voter("yes", "no", "unclear"), _cams())
    assert r.passed is True and r.detail.get("review_split") is True


def test_fail_unanimous_no_kills():
    """全表唯一杀格:联合打分全程无进度 + 看得清的证人一致 no。"""
    r = endstate_review(_res(False), "t", _voter("no", "unclear", "no"), _cams())
    assert r.passed is False and r.detail["verdict"] == "failure"


def test_fail_yes_is_conflict_not_rescue():
    """v7.1 教训:打分层看不到任何进度、复核却说完成 → 两层打架进人工,不救。"""
    r = endstate_review(_res(False), "t", _voter("yes", "yes", "yes"), _cams())
    assert r.passed is None and r.detail["verdict"] == "review_conflict"


def test_fail_split_or_abstain_abstains():
    r = endstate_review(_res(False), "t", _voter("yes", "no", "unclear"), _cams())
    assert r.passed is None
    r = endstate_review(_res(False), "t",
                        _voter("contradictory", "unclear", "unavail"), _cams())
    assert r.passed is None


def test_gap_violation_yes_conflicts():
    r = endstate_review(_res(None, "gap_violation"), "t",
                        _voter("yes", "yes", "yes"), _cams())
    assert r.passed is None and r.detail["verdict"] == "review_conflict"


def test_gray_low_final_yes_conflicts():
    """ep143 拦截钉子:灰区曲线末态回到原点(final≤0.25)+ 复核 yes = 实质矛盾。"""
    r = endstate_review(_res(None, "uncertain", final=0.25), "t",
                        _voter("yes", "yes", "yes"), _cams())
    assert r.passed is None and r.detail["verdict"] == "review_conflict"


def test_gray_mid_final_yes_rescues():
    r = endstate_review(_res(None, "uncertain", final=0.35), "t",
                        _voter("yes", "unclear", "unclear"), _cams())
    assert r.passed is True and r.detail["verdict"] == "endstate_success"


def test_blind_lone_witness_not_rescued():
    """8b ep143 钉子:打分层零信息 + 仅 1 张实票 yes → 孤证不救,进人工。"""
    r = endstate_review(_res(None, "score_blind"), "t",
                        _voter("yes", "contradictory", "contradictory"), _cams())
    assert r.passed is None


def test_blind_two_witnesses_rescued():
    r = endstate_review(_res(None, "score_blind"), "t",
                        _voter("yes", "yes", "unclear"), _cams())
    assert r.passed is True and r.detail["verdict"] == "endstate_success"


def test_no_voter_fail_candidate_downgrades():
    """投票器不可用:杀人永远双签 → 失败候选降弃权;成功候选过并留痕。"""
    r = endstate_review(_res(False), "t", None, _cams())
    assert r.passed is None and "单判据" in r.detail["endstate"]
    r = endstate_review(_res(True, "success", strong=True), "t", None, _cams())
    assert r.passed is True and "单判据" in r.detail["endstate"]


# ───────── 帧供给语义 ─────────

def test_no_frames_no_voter_call():
    v = _voter("yes")
    r = endstate_review(_res(False), "t", v, {})
    assert r.passed is False and not v.calls          # 无帧原样返回(上游另有兜底)


def test_each_cam_gets_own_frames_and_label():
    v = _voter("yes", "yes", "yes")
    endstate_review(_res(False), "t", v, _cams(3, 16), endstate_frames=8)
    assert len(v.calls) == 3
    for n_starts, n_ends, label in v.calls:
        assert (n_starts, n_ends) == (4, 4)           # 每路 linspace 8 帧对半分
    assert [c[2] for c in v.calls] == [
        "camera A (cam0)", "camera B (cam1)", "camera C (cam2)"]


def test_material_includes_endpoint_frames():
    """ep30 截尾 bug 回归:每路素材必须含首帧与**真末帧**(linspace 含端点)。"""
    frames = {"c": [np.full((4, 4, 3), i, np.uint8) for i in range(37)]}
    seen = []

    def spy(starts, ends, label, desc):
        seen.extend(int(f.mean()) for f in list(starts) + list(ends))
        return "yes"

    endstate_review(_res(None, "uncertain", final=0.4), "t", spy, frames,
                    endstate_frames=8)
    assert 0 in seen and 36 in seen, f"素材帧下标 {sorted(seen)} 缺端点(截尾回归!)"


def test_votes_recorded_in_detail():
    r = endstate_review(_res(True, "success", strong=True), "t",
                        _voter("yes", "no", "unclear"), _cams())
    assert r.detail["cam_votes"] == {"cam0": "yes", "cam1": "no", "cam2": "unclear"}
    assert r.detail["review"] == "split"
