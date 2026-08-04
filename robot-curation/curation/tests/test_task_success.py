"""任务成败 v6.5 协议单测(逻辑层,确定性假 VLM)。

假 VLM 约定:合成帧的**亮度编码物证进度**(frame.mean()/255 = 进度),
这样"VLM 读图"有确定性真值,决定表逻辑可被严格单测。

v6.5 语义要点(2026-08-04,105 条人工真值×三模型消融定稿):
- VOC 不再是逐条闸门(旧 test_hallucination 那套"VOC 拦幻觉"已废——实测拦下的
  全是撤手型好数据);抗幻觉职责移交复核否决权(见 test_review_veto_*)。
- 全平低位=打分层无信息=弃权(非失败证据);全平高位=正常满分(ep99 教训)。
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


def test_flat_low_is_blind_not_failure():
    """全平低位=打分层无信息 → 弃权(cosmos 系 droid 宽景 85% 如此,当失败证据
    会造成 36 条冤杀——v6.1 事故回归测试)。"""
    frames = [np.zeros((32, 32, 3), dtype=np.uint8)] * 12
    r = task_success(frames, "t", lambda ref, fs, i: [0.0] * len(fs))
    assert r.passed is None and r.detail["verdict"] == "score_blind"


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


# ---------- 复核合成(v6.5 决定表) ----------

def _run(vlm, judge, frames=None):
    frames = frames or _progress_frames()
    res = task_success(frames, "t", vlm)
    return endstate_review(res, "t", judge, frames)


def test_review_veto_weak_success(monkeypatch):
    """抗幻觉新机制:弱成功候选(分数中庸)被复核 no 否决 → 人工,不放行。"""
    prog = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.5, 0.5]   # final=0.5:过线但弱
    frames = [np.full((32, 32, 3), int(p * 255), dtype=np.uint8) for p in prog]
    r = _run(_fake_vlm, _judge(False), frames)
    assert r.passed is None and r.detail["verdict"] == "endstate_failure_suspect"


def test_strong_success_survives_veto_with_flag():
    """强分数(末段高位)压过复核否决,但带 disputed 标签供下游过滤。"""
    r = _run(_fake_vlm, _judge(False))                # final=1.0 强
    assert r.passed is True and r.detail.get("review_disputed") is True


def test_kill_needs_double_sign():
    """杀格唯一:失败候选+复核no=杀;复核缺席/矛盾 → 只能弃权(废除单方杀)。"""
    short = _progress_frames(final=1.0)[:8]           # 峰值≈0.23 → 失败候选
    res = task_success(short, "t", _fake_vlm)
    assert res.passed is False
    r = endstate_review(task_success(short, "t", _fake_vlm), "t", _judge(False), short)
    assert r.passed is False                          # 双签 → 真杀
    r = endstate_review(task_success(short, "t", _fake_vlm), "t", _judge(None), short)
    assert r.passed is None                           # 两问矛盾 → 弃权
    r = endstate_review(task_success(short, "t", _fake_vlm), "t", None, short)
    assert r.passed is None                           # 判官不可用 → 弃权


def test_review_rescues_blind():
    """打分层全瞎 + 复核 yes → 救回(cosmos 系的主要放行通道)。"""
    frames = [np.zeros((32, 32, 3), dtype=np.uint8)] * 12
    res = task_success(frames, "t", lambda ref, fs, i: [0.0] * len(fs))
    r = endstate_review(res, "t", _judge(True), frames)
    assert r.passed is True and r.detail["verdict"] == "endstate_success"


def test_gap_violation_vs_review_yes_is_conflict():
    """两层打架(ep143 拦截机制):契约违约曲线 + 复核 yes → 人工,谁也不赢。"""
    prog = [0.05, 0.2, 0.9, 1.0, 1.0, 0.1, 0.05, 0.05]
    frames = [np.full((32, 32, 3), int(p * 255), dtype=np.uint8) for p in prog]
    r = _run(_fake_vlm, _judge(True), frames)
    assert r.passed is None and r.detail["verdict"] == "review_conflict"


def test_review_material_includes_last_frame():
    """ep30 截尾 bug 回归:复核素材必须含**真末帧**(linspace 含端点)。"""
    frames = _progress_frames(n=37)                   # 37 帧,故意选不整除 8 的长度
    got = []

    def spy_judge(starts, ends, desc):
        got.extend(float(f.mean()) for f in list(starts) + list(ends))
        return True

    res = task_success(frames, "t", _fake_vlm)
    endstate_review(res, "t", spy_judge, frames)
    assert max(got) == pytest.approx(float(frames[-1].mean()), abs=0.5), \
        "复核素材缺真末帧——步进切片截尾 bug 回归!"
