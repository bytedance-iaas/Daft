"""任务成败判定:v6.5 协议(文档中的 M4c)。语义点,VLM,GPU,漏斗垫底只跑幸存者。

打分层借 OpenGVL 的思想但问法已改(2026-08-04,105 条人工真值消融定稿):
- 抽 K 帧**打乱顺序**逐帧问 VLM"物证进度 0-1"(单帧提问,VLM 看不到时序;
  只按物体/场景状态打分,不看机械臂动作 → 做了就留痕的任务真值曲线单调不减)。
- VOC(Spearman(完成度, 真实时序))**不再是逐条闸门**——旧协议拿它当"疑幻觉"
  硬门,实测在真值集上拦下的 11 条全是好数据(撤手型),而 3 条真失败全拿高分
  放行(自洽的错话测谎仪抓不住)。降为:①数据集级模型体检指标(按 run 取中位,
  抓 v2 时代"按图片位置编数列"型协议性崩坏仍一抓一个准);②绊线(final 高但
  voc<0 = 结论好过程语无伦次 → 不敢信,进灰区)。
- 复核层(endstate_review)全员运行并获得否决权,见该函数 docstring。

决定表 v6.5(初判 × 复核 → 终判;救人一签就够,杀人必须双签):
                复核=完成   复核=未完成        复核=两问矛盾  复核不可用
  成功候选       过         强分数过(disputed) 过(留痕)      过(留痕单判据)
                            弱分数→人工
  失败候选       过(救回)   **杀**(唯一杀格)   人工          人工(废除单方杀)
  gap契约违约    人工(打架) 人工               人工          人工
  灰区/全平/异常 过(救回)   人工               人工          人工

vlm_call 依赖注入 —— 本模块不关心模型是谁(模型名只在 pipeline YAML 一处,
换模型零代码改动;测试=确定性假函数)。
"""
from __future__ import annotations

from typing import Callable, Sequence

import numpy as np

from ..contract import CheckResult

# 批式 VLM 接口:vlm_completion(reference_frame, shuffled_frames, instruction) -> K 个完成度(0-1)
VlmCompletion = Callable[[np.ndarray, list, str], Sequence[float]]


def _sample_indices(n: int, k: int) -> np.ndarray:
    """均匀抽 k 帧,**linspace 含端点**(首帧和末帧必在)。

    ⚠️ 别改回步进切片 frames[::step]:那会漏掉末尾最多 ~22% 帧(2026-08-04
    ep30 消融实锤——复核层曾因此从未见过"任务完成之后"的画面,"做完即停"型
    任务被系统性否决)。
    """
    return np.unique(np.linspace(0, n - 1, num=min(k, n), dtype=int))


def voc_score(
    frames: Sequence[np.ndarray],
    instruction: str,
    vlm_completion: VlmCompletion,
    *,
    n_probe: int = 8,
    shuffle_seed: int = 0,
) -> tuple[float, np.ndarray, np.ndarray]:
    """打乱抽帧 → 参考帧+打乱帧逐帧提问 → Spearman(完成度, 真实时序)。

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
        return 0.0, preds, idx                        # 全同预测:秩相关无定义,记 0
    rho, _ = stats.spearmanr(np.arange(len(idx)), preds)
    return float(rho), preds, idx


def task_success(
    frames: Sequence[np.ndarray],
    instruction: str,
    vlm_completion: VlmCompletion,
    *,
    n_probe: int = 8,
    voc_min: float | None = None,  # v6.5 已废弃(仅兼容旧 YAML,不参与判定)
    success_min: float = 0.45,     # 末态物证≥此=成功候选(2026-07-08 锚定评测分界;
                                   # v6.5 真值集复核后维持)
    fail_max: float = 0.25,        # 末态≤此才可能谈失败(真失败聚集 0.1 附近)
    gap_max: float = 0.5,          # peak-final≥此=单调契约违约(物证打分不该回落:
                                   # 回落=模型抽风或真回退,都不配硬判)→ 灰区
    recovery_dip: float = 0.25,    # 中途回落幅度超过此且最终成功 = 恢复(仅展示,
                                   # 真值集精确率仅 13%,不作为筛选依据)
) -> CheckResult:
    """打分层初判(v6.5)。终判需经 endstate_review 全员复核合成,本函数产出:

    passed=True  verdict=success/recovery   成功候选(detail.strong_score 标强弱)
    passed=False verdict=failure            失败候选(全程无进度;复核 no 才真杀)
    passed=None  verdict=uncertain          灰区(证据不足)
                 verdict=gap_violation      契约违约(冲高又崩回,谁也别信)
                 verdict=voc_tripwire       分数高但过程语无伦次(voc<0)
                 verdict=score_blind        全平低位=打分层完全无信息(≠失败证据!
                                            cosmos 系在 droid 宽景上 85% 如此)
    """
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
    gap = peak - final
    last = float(preds[-1])
    detail = {"voc": round(voc, 4), "completion_final": round(final, 4),
              "completion_peak": round(peak, 4), "completion_gap": round(gap, 4),
              "completions": [round(float(p), 3) for p in preds],
              "probe_frames": idx.tolist()}

    # 全平低位 = 打分层对这条完全无信息(每帧都答 0):弃权交复核,绝不是失败证据。
    # 注意只有**低位**才算瞎——全平在高位(如 ep99 全 1.0)是正常满分,别一刀切。
    if float(np.std(preds)) < 1e-9 and float(preds.mean()) <= fail_max:
        detail["verdict"] = "score_blind"
        detail["reason"] = "逐帧分数全平于低位:打分层无信息(非失败证据),交复核裁决"
        return CheckResult(name="task_success", passed=None, detail=detail)

    if final >= success_min:
        if voc < 0.0:
            # VOC 绊线:结论好但分数与时序负相关 = 过程语无伦次,final 不敢采信
            detail["verdict"] = "voc_tripwire"
            detail["reason"] = f"末态 {final:.2f} 达标但 VOC {voc:.2f} 为负:过程混乱,不敢信"
            return CheckResult(name="task_success", passed=None, detail=detail)
        # 强分数 = 复核否决也压得过的两种形态(见 endstate_review):
        #   ①末段稳定高位;②单调爬升到末帧完成(ep30/ep54 型:最后一刻做完,
        #     中位数被拉低但末帧 1.0 + 过程单调,证据链完整)
        strong = (final >= 0.8 and gap <= 0.2) or (voc >= 0.6 and last >= 0.8
                                                   and peak - last <= 0.2)
        detail["strong_score"] = bool(strong)
        running_max = np.maximum.accumulate(preds)
        dip = float((running_max - preds).max())
        if dip >= recovery_dip:
            detail["verdict"] = "recovery"          # 中途回落后仍完成:保留但标注
            detail["dip"] = round(dip, 4)
        else:
            detail["verdict"] = "success"
        return CheckResult(name="task_success", passed=True, detail=detail)

    if peak <= fail_max:
        # 全程从未有过进度,才有资格谈失败(复核 no 双签后才真杀)
        detail["verdict"] = "failure"
        detail["reason"] = f"全程物证进度峰值 {peak:.2f} ≤ {fail_max}:失败候选(待复核双签)"
        return CheckResult(name="task_success", passed=False, detail=detail)

    if gap >= gap_max:
        # 契约违约:冲高又崩回。物证打分理论上单调,回落只有两种解释——模型对末态
        # 看不清(ep17/19/34 型)或真回退。两种都不该硬判,进灰区由复核+人工兜。
        detail["verdict"] = "gap_violation"
        detail["reason"] = (f"峰值 {peak:.2f} 崩至末态 {final:.2f}(gap≥{gap_max}):"
                            "单调契约违约,模型抽风或真回退,不硬判")
        return CheckResult(name="task_success", passed=None, detail=detail)

    detail["verdict"] = "uncertain"
    detail["reason"] = (f"末态物证 {final:.2f} 在灰区({fail_max}~{success_min}),"
                        "证据不足以硬判")
    return CheckResult(name="task_success", passed=None, detail=detail)


def endstate_review(
    res: CheckResult,
    task_desc: str,
    endstate_judge: Callable | None,
    primary_frames: Sequence,
    extra_frames_fn: Callable[[], list] | None = None,
    endstate_frames: int = 8,
) -> CheckResult:
    """二值复核(v6.5:**全员复核 + 否决权**;多视角全程帧,双问法互检)。

    ★ 2026-07-23 从 funnel 闭包抽出为纯函数:漏斗/考卷/单测共用同一份协议。
    ★ 2026-08-04 v6.5 三处升级(105 条人工真值 × 三模型消融定案):
     ① **成功候选不再免检**:旧协议 passed=True 直接返回,复核没有否决权——
        ep131(空夹叶子)/ep143(没挂上)这类"动作像样物体没动"的自洽错话从
        免检通道直接溜走(豆包真失败拦截 0/3)。现在全员复核:复核 no 时,
        强分数(detail.strong_score)压过否决带 disputed 标签放行,弱分数打回
        人工。复核只有否决权没有处决权(3 条失败样本撑不起给它发枪)。
     ② **抽帧修复**:旧 primary_frames[::step] 步进切片漏掉末尾最多 ~22% 帧,
        复核从未见过"任务完成之后"的画面,"做完即停"型被系统性否决(ep30
        消融:37 帧只看到第 28 帧,而玩具入槽在最后一瞬)。改 linspace 含端点。
     ③ **废除"复核不可用时凭失败候选单方杀"**:杀人永远双签,复核缺席只能弃权。
     另:gap_violation(冲高崩回)+ 复核 yes = 两层激烈打架(ep143 就靠此拦住:
     打分层看到回落,复核对错标注答 yes),谁也不赢,进人工。

    注入接口(全部框架无关):
      endstate_judge(starts, ends, desc) -> True完成 / False未完成 / None两问矛盾;
        传 None = 判官不可用(构造失败),detail 留痕后按"复核不可用"列合成。
      primary_frames: 主相机全程帧(VOC 已解码的直接复用)。
      extra_frames_fn: () -> [各补充相机的**全程**帧列表]。惰性:仅在调用时解码;
        解码失败的相机由调用方自行跳过。
    历史(2026-07-21 ep34 消融,三处修正仍有效):触发面放宽/全程帧/相机放开。
    """
    init = res.detail.get("verdict", "undecidable")
    strong = bool(res.detail.get("strong_score"))

    if endstate_judge is None:
        # 复核该跑却没跑(judge 构造失败)→ 按"复核不可用"列合成 + 留痕
        res.detail["endstate"] = "二值复核不可用,仅打分层单判据"
        if res.passed is False:
            res.passed = None                         # ③杀人必须双签:单判据不许杀
            res.detail["reason"] = "失败候选但复核不可用:不凭单判据硬杀,进人工"
        return res

    # ---- 组复核素材:每路相机 linspace 含端点抽 endstate_frames 帧 ----
    starts, ends = [], []

    def _feed(fr):
        """前半段进 start 组、后半段进 end 组:保留双问法的前后对照语义。"""
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
            _feed(fr)
    if not starts:
        return res                                    # 无帧可复核(极端情形),原样返回

    es = endstate_judge(starts, ends, str(task_desc))
    res.detail["endstate_answer"] = {True: "yes", False: "no", None: "contradictory"}[es]

    if es is True:
        if init == "gap_violation":
            # 两层打架:打分层看到崩盘,复核却说完成。要么复核被错标注带偏
            # (ep143),要么打分层对末态失明(ep19 烂机位)。谁也不赢 → 人工。
            res.passed = None
            res.detail["verdict"] = "review_conflict"
            res.detail["reason"] = "打分层契约违约 vs 复核判完成:两层证据激烈矛盾,进人工"
        elif res.passed is not True:
            res.passed = True                         # 救人一签就够
            res.detail["verdict"] = "endstate_success"
            res.detail["reason"] = f"打分层{init};多视角全程帧二值复核判完成,救回"
        return res

    if es is False:
        if res.passed is True:
            if strong:
                # 强分数压过否决:证据链完整的分数 > 一次 no(复核对"做完即停"
                # 型的误 no 率不可忽略);disputed 标签保留给下游过滤
                res.detail["verdict"] = res.detail.get("verdict", "success")
                res.detail["review_disputed"] = True
                res.detail["reason"] = "复核判未完成,但打分层强证据(末段高位/单调爬升)压过否决"
            else:
                res.passed = None                     # 否决:弱分数 + 复核 no → 人工
                res.detail["verdict"] = "endstate_failure_suspect"
                res.detail["reason"] = "打分层弱成功证据被多视角二值复核否决:存疑进人工,不硬杀"
        elif init == "failure":
            res.passed = False                        # ◆全表唯一杀格:双签
            res.detail["verdict"] = "failure"
            res.detail["reason"] = "打分层全程无进度且复核判未完成:双判据一致,硬杀"
        else:
            res.passed = None
            res.detail["verdict"] = "endstate_failure_suspect"
            res.detail["reason"] = f"打分层{init}且复核判未完成:证据不足以放行,进人工"
        return res

    # es is None:两问矛盾 → 复核弃权,不采信
    res.detail["endstate"] = "两问法矛盾,不采信"
    if res.passed is True:
        res.detail["review_contradictory"] = True     # 成功候选维持,留痕
    elif res.passed is False:
        res.passed = None                             # 失败候选失去第二签 → 弃权
        res.detail["reason"] = "失败候选但复核两问矛盾:缺第二签,不硬杀,进人工"
    return res
