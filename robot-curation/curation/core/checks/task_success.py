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


def endstate_review(
    res: CheckResult,
    task_desc: str,
    endstate_judge: Callable | None,
    primary_frames: Sequence,
    extra_frames_fn: Callable[[], list] | None = None,
    endstate_frames: int = 8,
) -> CheckResult:
    """二值复核协议:VOC 未判成功时的第二意见(多视角×全程帧,双问法互检)。

    ★ 2026-07-23 从 funnel 闭包抽出为纯函数:此前协议只能随整条漏斗跑,考卷
    (spike_vlm_ab)只能考裸 VOC 层,得出"8B 冤杀 2/10"的错误结论(实测走全协议
    后 1 救回 1 进人工,硬杀 0)。抽出后:漏斗薄壳 / 考卷 / 单测共用同一份协议,
    永不分叉——回到"检查=框架无关纯函数"红线。

    注入接口(全部框架无关):
      endstate_judge(starts, ends, desc) -> True完成 / False未完成 / None两问矛盾;
        传 None = 判官不可用(构造失败),detail 留痕后原样返回。
      primary_frames: 主相机全程帧(VOC 已解码的直接复用——只要 VOC 跑到了,
        复核就一定有帧,不会因重解码失败而被静默跳过)。
      extra_frames_fn: () -> [各补充相机的全程帧列表]。**惰性**:仅在触发复核时
        调用(解码贵);解码失败的相机由调用方自行跳过。

    2026-07-21 三处修正,均由 DROID ep34("把橙杯里的东西倒进碗",人工核对为成功
    却被硬门误杀)的消融实验定案:
     ① 触发条件 passed is None → is not True:VOC 判 False("有把握的失败")时同样
        要复核。ep34 正死于此——VOC 在看不清碗内的主视角上给出 False,复核根本没
        机会开口。用户定的 OR 原则:任一路/任一判据看到成功即成功(依据:七模型
        评测 32B 干净成功率仅 0.75 ⇒ 假阴性才是主要误差,假阳性少见)。
     ② 帧集合首尾两帧 → 全程均匀:消融证明这才是根因。同一相机同一模型,首尾2帧
        无论怎么问都判"未完成";带中段的全程8帧无论怎么问都判"完成"。倒/放/开关
        这类**阶跃型任务**的证据在动作瞬间(约1-2秒),末态看不出来。
     ③ 相机放开(原按名字过滤):ep34 唯一能看清的 exterior_2 曾被挡掉。
    成本:仍是双问法 2 次请求,只是每次多带若干图。
    """
    if res.passed is True:
        return res                                    # VOC 已判成功,复核不启动
    if endstate_judge is None:
        # 复核该跑却没跑(judge 构造失败)→ detail 留痕,报告里能看出
        # "本条只有 VOC 单判据把关",不至于误以为都双判据复核过
        res.detail["endstate"] = "二值复核不可用,仅 VOC 单判据"
        return res

    voc_verdict = res.passed                          # 复核前的 VOC 结论,留痕用
    starts, ends = [], []

    def _feed(fr):
        """前半段进 start 组、后半段进 end 组:保留双问法的前后对照语义,
        同时把中段证据带进来(不再只有首尾两帧)。

        抽帧用 linspace **含端点**(_sample_indices 与打分层同款)。⚠️ 别改回
        步进切片 fr[::step]:那会漏掉末尾最多 ~22% 帧——复核从未见过"任务完成
        之后"的画面,"做完即停"型演示被系统性否决(2026-08-04 droid ep30 消融
        实锤:37 帧只看到第 28 帧,而玩具入槽发生在最后一瞬)。"""
        fr = list(fr)
        if not fr:
            return
        pick = [fr[i] for i in _sample_indices(len(fr), endstate_frames)]
        mid = max(1, len(pick) // 2)
        starts.extend(pick[:mid])
        ends.extend(pick[mid:] or pick[-1:])

    if len(primary_frames) > 0:
        _feed(primary_frames)
    if extra_frames_fn is not None:
        for fr in extra_frames_fn():                  # 惰性:仅走到这里才解其余相机
            if len(fr) > 0:
                _feed(fr)
    if not starts:
        return res                                    # 无帧可复核(极端情形),原样返回

    es = endstate_judge(starts, ends, str(task_desc))
    src = "渐变问询不可判" if voc_verdict is None else "渐变问询判失败"
    if es is True:
        res.passed = True                             # OR:任一判据看到成功即成功
        res.detail["verdict"] = "endstate_success"
        res.detail["reason"] = f"{src};多视角全程帧二值复核(双问法一致)判完成"
    elif es is False:
        res.detail["verdict"] = "endstate_failure_suspect"
        if voc_verdict is False:
            res.passed = False                        # 两个判据一致判失败 → 维持硬杀
            res.detail["reason"] = "渐变问询与多视角二值复核一致判未完成"
        else:
            res.passed = None
            res.detail["reason"] = f"{src};二值复核判未完成——存疑进人工裁决,不硬杀"
    else:
        res.detail["endstate"] = "两问法矛盾,不采信"
        if voc_verdict is False:
            res.passed = None                         # 复核不采信 → 不凭 VOC 单方硬杀,降级弃权
            res.detail["reason"] = "渐变问询判失败但二值复核两问矛盾;存疑进人工裁决"
    return res
