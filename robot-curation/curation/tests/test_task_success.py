"""P3.5 验收(逻辑层,确定性假 VLM):truncate 注入判失败;VOC 抗幻觉;恢复识别。

假 VLM 约定:合成帧的**亮度编码任务进度**(frame.mean()/255 = 完成度),
这样"VLM 读图"有确定性真值,VOC 逻辑可被严格单测。真模型 A/B(Qwen2.5-VL vs
Qwen3-VL)是模型评测(P3.5 后半),不在单测范围。
"""
from __future__ import annotations

import numpy as np
import pytest

from curation.core.checks.task_success import task_success, voc_score
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


def _hallucinating_vlm(seed: int = 0):
    rng = np.random.default_rng(seed)
    return lambda ref, fs, i: [float(rng.uniform()) for f in fs]  # 与画面无关的胡说


def test_success_detected():
    r = task_success(_progress_frames(final=1.0), "push T", _fake_vlm)
    assert r.passed is True and r.detail["verdict"] == "success"
    assert r.detail["voc"] > 0.9


def test_truncate_injection_judged_failure():
    """P3 验收主判据:截断的演示(必失败)被判 failure。"""
    frames = _progress_frames(final=1.0)
    row = {"action": np.zeros((len(frames), 2), dtype=np.float32),
           "proprio_state": None, "video": {},
           "timestamps": np.arange(len(frames)) / 10.0, "fps": 10.0}
    bad, _ = corrupt.truncate(row, keep_fraction=0.2)  # 三段带:≤0.25 才是有把握的失败
    kept_frames = frames[:len(bad["action"])]          # 帧与 action 同步截断
    r = task_success(kept_frames, "push T", _fake_vlm)
    assert r.passed is False and r.detail["verdict"] == "failure"


def test_truncate_three_bands():
    """三段带语义(2026-07-08):≤0.25 失败杀 / 0.25~0.45 灰区弃权 / ≥0.45 成功。
    (旧'一律判失败'基于 0.7 单阈值——曾错杀 0.45-0.7 的真成功,评测锚定分界后废弃)"""
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


def test_hallucination_is_undecidable_not_verdict():
    """VOC 抗幻觉:VLM 胡说 → 不可判(passed=None),绝不硬判成败。"""
    results = [task_success(_progress_frames(), "t", _hallucinating_vlm(s)) for s in range(10)]
    undecidable = [r for r in results if r.passed is None]
    # 随机预测的 |Spearman| 偶尔会撞高,但大多数应被 VOC 拦下
    assert len(undecidable) >= 7, f"仅 {len(undecidable)}/10 被判不可判"
    assert all("voc" in r.detail for r in results)


def test_shuffle_actually_shuffles():
    """打乱确实发生:批式假 VLM 收到的帧序列不是时序单调的。"""
    seen = []

    def spy_vlm(reference, shuffled, instruction):
        seen.extend(float(f.mean()) for f in shuffled)
        return [float(f.mean() / 255.0) for f in shuffled]

    voc_score(_progress_frames(), "t", spy_vlm)
    assert seen != sorted(seen), "提问顺序仍是时序(未打乱,VOC 抗幻觉失效)"


def test_constant_prediction_is_undecidable():
    r = task_success(_progress_frames(), "t", lambda ref, fs, i: [0.8] * len(fs))
    assert r.passed is None                            # 全同预测无信息 → voc=0 → 不可判


def test_vlm_exception_is_undecidable():
    def broken(ref, fs, i):
        raise TimeoutError("模型超时")
    r = task_success(_progress_frames(), "t", broken)
    assert r.passed is None and "失败" in r.detail["reason"]