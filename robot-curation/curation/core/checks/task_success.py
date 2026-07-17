"""任务成败判定:VOC 逻辑(文档中的 M4c)。语义点,VLM,GPU,漏斗垫底只跑幸存者。

核心借 OpenGVL 的 VOC(Value-Order Correlation)思想:
- 抽 K 帧**打乱顺序**逐帧问 VLM"任务完成度 0-1"(单帧提问,VLM 看不到时序)→
  还原真实顺序 → 与帧序做 Spearman 相关。真演示的完成度应随时间上升 →
  相关高=VLM 判断可信;相关低=VLM 在幻觉 → **不可判**(抗幻觉的关键设计)。
增强(我们的):三分类 成功/失败/恢复(recovery=中途明显回落后再完成)。

vlm_call 依赖注入 —— 本模块不关心模型是谁(生产=daft prompt() 指 vLLM 端点,
模型名只在 pipeline YAML 一处,换模型零代码改动;测试=确定性假函数)。
"""
from __future__ import annotations

from typing import Callable, Sequence

import numpy as np

from ..contract import CheckResult

# 批式 VLM 接口(OpenGVL 真实协议;2026-07-02 实测单帧绝对估计模型一律回 50):
# vlm_completion(reference_frame, shuffled_frames, instruction) -> K 个完成度(0-1)
VlmCompletion = Callable[[np.ndarray, list, str], Sequence[float]]


def _sample_indices(n: int, k: int) -> np.ndarray:
    return np.unique(np.linspace(0, n - 1, num=min(k, n), dtype=int))


def voc_score(
    frames: Sequence[np.ndarray],
    instruction: str,
    vlm_completion: VlmCompletion,
    *,
    n_probe: int = 8,
    shuffle_seed: int = 0,
) -> tuple[float, np.ndarray, np.ndarray]:
    """VOC:打乱抽帧 → 参考帧+打乱帧一次提问 → Spearman(完成度, 真实时序)。

    返回 (voc, 按时序排列的完成度数组, 抽帧下标)。
    """
    from scipy import stats

    idx = _sample_indices(len(frames), n_probe)
    order = np.random.default_rng(shuffle_seed).permutation(len(idx))  # 打乱提问顺序
    shuffled = [frames[idx[j]] for j in order]
    preds_shuffled = [float(p) for p in vlm_completion(frames[0], shuffled, instruction)]
    if len(preds_shuffled) != len(idx):
        raise ValueError(f"VLM 返回 {len(preds_shuffled)} 个数,期望 {len(idx)}")
    preds = np.empty(len(idx), dtype=np.float64)
    preds[order] = preds_shuffled                     # 还原真实时序
    if np.std(preds) < 1e-9:
        return 0.0, preds, idx                        # 全同预测:无信息
    rho, _ = stats.spearmanr(np.arange(len(idx)), preds)
    return float(rho), preds, idx


def task_success(
    frames: Sequence[np.ndarray],
    instruction: str,
    vlm_completion: VlmCompletion,
    *,
    n_probe: int = 8,
    voc_min: float = 0.3,          # VOC 低于此 = VLM 不可信,不可判
    success_min: float = 0.45,     # 末态完成度≥此=成功(2026-07-08:锚定7模型评测的32B自然
                                   # 分界0.45——原0.7会错杀0.45-0.7区间的真成功;P5.1标注后再校)
    fail_max: float = 0.25,        # 末态完成度≤此=失败硬杀(真失败聚集0.1附近,留余量)
    recovery_dip: float = 0.25,    # 中途回落幅度超过此且最终成功 = 恢复
) -> CheckResult:
    """三分类:成功(keep)/失败(硬门 drop)/恢复(keep,标注)。VOC 低→不可判。"""
    if len(frames) < 2:
        return CheckResult(name="task_success", passed=None,
                           detail={"reason": "帧数不足"})

    try:
        voc, preds, idx = voc_score(frames, instruction, vlm_completion, n_probe=n_probe)
    except Exception as e:  # noqa: BLE001  真模型会抽风(格式不符/超时)→ 不可判,不崩管线
        return CheckResult(name="task_success", passed=None,
                           detail={"reason": f"VLM 调用/解析失败: {type(e).__name__}: {e}"})
    final = float(np.median(preds[-2:]))
    peak = float(preds.max())
    detail = {"voc": round(voc, 4), "completion_final": round(final, 4),
              "completion_peak": round(peak, 4),
              "completions": [round(float(p), 3) for p in preds],
              "probe_frames": idx.tolist()}

    if voc < voc_min:
        detail["reason"] = f"VOC {voc:.2f} < {voc_min}:VLM 判断与时序不符(疑幻觉),不可判"
        return CheckResult(name="task_success", passed=None, detail=detail)

    if final >= success_min:
        # 中途明显回落后仍完成 → 恢复演示(有训练价值,保留但标注)。
        # 回落 = 相对"累计最高进度"的最大跌幅(峰值在末尾时"峰后最小值"恒等于终值,是错的)
        running_max = np.maximum.accumulate(preds)
        dip = float((running_max - preds).max())
        if dip >= recovery_dip:
            detail["verdict"] = "recovery"
            detail["dip"] = round(dip, 4)
        else:
            detail["verdict"] = "success"
        return CheckResult(name="task_success", passed=True, detail=detail)

    if final <= fail_max:
        detail["verdict"] = "failure"
        detail["reason"] = f"末态完成度 {final:.2f} ≤ {fail_max}:有把握的失败"
        return CheckResult(name="task_success", passed=False, detail=detail)
    # 三段带的灰区(2026-07-08):只杀有把握的失败;介于两线之间证据不足 → 弃权进
    # 人工裁决队列——直接回应"错杀太多"(0.45-0.7 的真成功曾被 0.7 单阈值错杀)
    detail["verdict"] = "uncertain"
    detail["reason"] = (f"末态完成度 {final:.2f} 在灰区({fail_max}~{success_min}),"
                        "证据不足以硬判 → 不可判")
    return CheckResult(name="task_success", passed=None, detail=detail)
