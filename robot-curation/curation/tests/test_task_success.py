"""任务成败 v6.5 协议单测(逻辑层,确定性假 VLM)。

假 VLM 约定:合成帧的**亮度编码物证进度**(frame.mean()/255 = 进度),
这样"VLM 读图"有确定性真值,决定表逻辑可被严格单测。

v6.5 语义要点(2026-08-04,105 条人工真值×三模型消融定稿):
- VOC 不再是逐条闸门(旧 test_hallucination 那套"VOC 拦幻觉"已废——实测拦下的
  全是撤手型好数据);抗幻觉职责移交复核否决权(见 test_review_veto_*)。
- 全平低位=失败候选(2026-09-02 起,豆包真值全零×复核 no 9/9 真失败;曾按 cosmos-8b
  失明签名单列 score_blind 弃权,已撤销);全平高位=正常满分(ep99 教训)。
- 参考帧(probe0)完成度强制归 0,原值留痕(ep000014 假峰教训)。
- 杀人必须双签:失败候选 + 复核 no;复核缺席/矛盾只能弃权。
- 复核抽帧 linspace 含端点(ep30 截尾 bug 回归测试)。
"""
from __future__ import annotations

import numpy as np
import pytest

from curation.core.checks.task_success import endstate_review, task_success, voc_score
from curation.tests import corrupt


def _progress_frames(n: int = 40, final: float = 1.0, dip_at: float | None = None):
    """亮度 = 进度 的合成帧序列;dip_at ∈ (0,1) 处插入回落(模拟失手再恢复)。"""
    prog = np.linspace(0.05, final, n)
    if dip_at is not None:
        k = int(n * dip_at)
        prog[k:k + n // 6] = prog[max(0, k - n // 5)] * 0.3   # 掉回去一大截
    return [np.full((32, 32, 3), int(p * 255), dtype=np.uint8) for p in prog]


def _fake_vlm(reference, shuffled, instruction):
    return [float(f.mean() / 255.0) for f in shuffled]


def _noisy_vlm(seed: int = 0):
    rng = np.random.default_rng(seed)
    return lambda ref, fs, i: [float(np.clip(f.mean() / 255.0 + rng.normal(0, 0.05), 0, 1))
                               for f in fs]


def _judge(answer):
    """确定性假判官:True完成/False未完成/None两问矛盾。"""
    return lambda starts, ends, desc: answer


# ---------- 打分层初判 ----------

def test_success_detected():
    r = task_success(_progress_frames(final=1.0), "push T", _fake_vlm)
    assert r.passed is True and r.detail["verdict"] == "success"
    assert r.detail["voc"] > 0.9
    assert r.detail["strong_score"] is True           # 末段高位 → 强分数


def test_truncate_injection_judged_failure_candidate():
    """截断的演示(必失败)→ 失败候选;真杀还需复核双签(见 test_kill_needs_double_sign)。"""
    frames = _progress_frames(final=1.0)
    row = {"action": np.zeros((len(frames), 2), dtype=np.float32),
           "proprio_state": None, "video": {},
           "timestamps": np.arange(len(frames)) / 10.0, "fps": 10.0}
    bad, _ = corrupt.truncate(row, keep_fraction=0.2)
    kept_frames = frames[:len(bad["action"])]
    r = task_success(kept_frames, "push T", _fake_vlm)
    assert r.passed is False and r.detail["verdict"] == "failure"


def test_truncate_three_bands():
    """三段带:峰值≤0.25 失败候选 / 灰区弃权 / ≥0.45 成功候选。"""
    frames = _progress_frames(final=1.0, n=60)
    r = task_success(frames[:int(60 * 0.2)], "t", _fake_vlm)
    assert r.passed is False and r.detail["verdict"] == "failure"
    r = task_success(frames[:int(60 * 0.35)], "t", _fake_vlm)
    assert r.passed is None and r.detail["verdict"] == "uncertain"
    r = task_success(frames[:int(60 * 0.6)], "t", _fake_vlm)
    assert r.passed is True


def test_recovery_detected():
    r = task_success(_progress_frames(n=60, final=1.0, dip_at=0.5), "t", _fake_vlm,
                     n_probe=12)
    assert r.passed is True
    assert r.detail["verdict"] == "recovery"


def test_voc_robust_to_rank_preserving_noise():
    r = task_success(_progress_frames(), "t", _noisy_vlm())
    assert r.passed is True and r.detail["voc"] > 0.7


def test_retreat_curve_is_gap_violation():
    """冲高又崩回(峰值 1.0 → 末态 0)= 单调契约违约 → 灰区,绝不硬判。
    (物证打分下这形态只有两种解释:模型对末态失明,或真回退——ep17/19 型)"""
    prog = [0.05, 0.2, 0.9, 1.0, 1.0, 0.1, 0.05, 0.05]
    frames = [np.full((32, 32, 3), int(p * 255), dtype=np.uint8) for p in prog]
    r = task_success(frames, "t", _fake_vlm)
    assert r.passed is None and r.detail["verdict"] == "gap_violation"


def test_flat_low_is_failure_candidate():
    """全平低位 → 失败候选(2026-09-02 用户定):豆包看得见,全零就是没进展;
    droid-200 真值全零×复核 no 9/9 真失败、零冤杀。杀仍需复核双签(见
    test_endstate_review 失败候选各格),这里只钉打分层不再单列 score_blind 弃权。"""
    frames = [np.zeros((32, 32, 3), dtype=np.uint8)] * 12
    r = task_success(frames, "t", lambda ref, fs, i: [0.0] * len(fs))
    assert r.passed is False and r.detail["verdict"] == "failure"
    assert "flat_low_scores" in r.detail["rules"]
    assert "fail_candidate_no_progress" in r.detail["rules"]
    assert "score_blind" not in r.detail["rules"] and r.detail["verdict"] != "score_blind"


def test_probe0_forced_zero_with_trace():
    """参考帧自检(ep000014 教训):probe0 就是参考图,模型回 100 只能是错位/幻觉。
    归 0 后 peak 由假的 1.0 变成真实的 0.7,标签从契约违约回到灰区;原值留在 raw。"""
    seq = [1.0, 0.0, 0.3, 0.3, 0.7, 0.5, 0.3, 0.2]

    def vlm(ref, fs, i):                     # 按打乱后的帧序号回对应分数
        return [seq[int(round(f.mean()))] for f in fs]
    frames = [np.full((8, 8, 3), k, dtype=np.uint8) for k in range(8)]
    r = task_success(frames, "t", vlm)
    assert r.detail["completions"][0] == 0.0
    assert r.detail["raw"]["probe0_model"] == 1.0
    assert "probe0_forced_zero" in r.detail["rules"]
    assert r.detail["completion_peak"] == 0.7
    assert r.detail["verdict"] == "uncertain"      # 不再是 gap_violation
    # 模型本来就答 0 的参考帧:不留痕、不加规则
    r2 = task_success(frames, "t", lambda ref, fs, i: [0.0 if int(round(f.mean())) == 0
                                                       else 0.5 for f in fs])
    assert "probe0_model" not in r2.detail["raw"]
    assert "probe0_forced_zero" not in r2.detail["rules"]


def test_flat_high_is_success_not_blind():
    """全平高位=正常满分(ep99:碗从头到尾在目标位),别一刀切当无信息。"""
    r = task_success(_progress_frames(), "t", lambda ref, fs, i: [0.9] * len(fs))
    assert r.passed is True


def test_voc_tripwire_blocks_incoherent_high_final():
    """末态达标但分数与时序负相关(过程语无伦次)→ 绊线拦下,进灰区。"""
    prog = [1.0, 1.0, 1.0, 0.9, 0.2, 0.1, 0.9, 0.9]   # 末两帧中位 0.9,但整体下坡
    frames = [np.full((32, 32, 3), int(p * 255), dtype=np.uint8) for p in prog]
    r = task_success(frames, "t", _fake_vlm)
    assert r.passed is None and r.detail["verdict"] == "voc_tripwire"


def test_vlm_exception_is_undecidable():
    def broken(ref, fs, i):
        raise TimeoutError("模型超时")
    r = task_success(_progress_frames(), "t", broken)
    assert r.passed is None and "失败" in r.detail["reason"]


def test_shuffle_actually_shuffles():
    """打乱确实发生:批式假 VLM 收到的帧序列不是时序单调的。"""
    seen = []

    def spy_vlm(reference, shuffled, instruction):
        seen.extend(float(f.mean()) for f in shuffled)
        return [float(f.mean() / 255.0) for f in shuffled]

    voc_score(_progress_frames(), "t", spy_vlm)
    assert seen != sorted(seen), "提问顺序仍是时序(未打乱)"


# ---------- 多视角帧(core 零感知) ----------

def test_core_agnostic_to_multiview_frames():
    """帧元素 = [(相机名, 图), ...] 时 core 原样工作(内容只被注入的 vlm 消费)。"""
    plain = _progress_frames()
    mv = [[("camA", f), ("camB", f)] for f in plain]

    def mv_fake_vlm(reference, shuffled, instruction):
        return [float(fr[0][1].mean() / 255.0) for fr in shuffled]   # 读 camA

    r = task_success(mv, "t", mv_fake_vlm)
    assert r.passed is True and r.detail["voc"] > 0.9


# ---------- 复核合成(v7.2 决定表;逐格矩阵见 test_endstate_review) ----------

def _voter_const(answer):
    return lambda starts, ends, label, desc: answer


def _run(vlm, vote, frames=None):
    frames = frames or _progress_frames()
    res = task_success(frames, "t", vlm)
    return endstate_review(res, "t", _voter_const(vote), {"cam": frames})


def test_review_veto_weak_success():
    """抗幻觉主机制:弱成功候选被复核一致 no 否决 → 人工,不放行。"""
    prog = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.5, 0.5]
    frames = [np.full((32, 32, 3), int(p * 255), dtype=np.uint8) for p in prog]
    r = _run(_fake_vlm, "no", frames)
    assert r.passed is None and r.detail["verdict"] == "endstate_failure_suspect"


def test_strong_success_survives_veto_with_flag():
    r = _run(_fake_vlm, "no")                          # final=1.0 强
    assert r.passed is True and r.detail.get("review_disputed") is True


def test_kill_needs_double_sign():
    """杀格唯一:失败候选+一致no=杀;票不齐 → 弃权(无单方杀)。"""
    short = _progress_frames(final=1.0)[:8]            # 峰值≈0.23 → 失败候选
    assert task_success(short, "t", _fake_vlm).passed is False
    r = endstate_review(task_success(short, "t", _fake_vlm), "t",
                        _voter_const("no"), {"cam": short})
    assert r.passed is False
    r = endstate_review(task_success(short, "t", _fake_vlm), "t",
                        _voter_const("contradictory"), {"cam": short})
    assert r.passed is None
    r = endstate_review(task_success(short, "t", _fake_vlm), "t", None, {"cam": short})
    assert r.passed is None


def test_gap_violation_vs_review_yes_is_conflict():
    """两层打架(ep19 烂机位/真回退同型):契约违约曲线 + 复核 yes → 人工。"""
    prog = [0.05, 0.2, 0.9, 1.0, 1.0, 0.1, 0.05, 0.05]
    frames = [np.full((32, 32, 3), int(p * 255), dtype=np.uint8) for p in prog]
    r = _run(_fake_vlm, "yes", frames)
    assert r.passed is None and r.detail["verdict"] == "review_conflict"
