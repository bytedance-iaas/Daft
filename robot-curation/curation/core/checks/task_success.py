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

v7.2 多视角(2026-08-04):打分层帧可为 [(相机名, 图), ...](联合感知,由
adapters.make_multiview_completion 消费,本模块零感知);复核层换逐机位独立
投票(cam_vote/tally)+ 决定表见 endstate_review docstring。

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


def cam_vote(done_ans: str, failed_ans: str) -> str:
    """单机位双问 → 一票(v7.2)。done_ans=完成问答案, failed_ans=失败问答案。

    互补才算数(yes/no 或 no/yes);任一问答 unclear = 该机位诚实弃权
    ("看不见"≠"没做成");同向回答 = 自相矛盾,证词不采信。
    """
    if done_ans == "yes" and failed_ans == "no":
        return "yes"
    if done_ans == "no" and failed_ans == "yes":
        return "no"
    if "unclear" in (done_ans, failed_ans):
        return "unclear"
    return "contradictory"


def tally(votes) -> str:
    """汇票(v7.2):只数实票(yes/no),弃权/矛盾/不可用不计。

    ≥1 yes 且 0 no → yes(有看得清的证人说做成,无人反对);
    0 yes 且 ≥1 no → no(看得清的证人一致说没做成);
    并存 → split(证人真打架,异常信号);全弃权 → abstain。
    """
    y = sum(1 for v in votes if v == "yes")
    n = sum(1 for v in votes if v == "no")
    if y and not n:
        return "yes"
    if n and not y:
        return "no"
    if y and n:
        return "split"
    return "abstain"


# task_success 初判 verdict → 决定表行的映射(recovery 是 success 的带注成功)
_INIT_ROW = {"success": "success_cand", "recovery": "success_cand",
             "failure": "fail_cand", "uncertain": "gray"}


def endstate_review(
    res: CheckResult,
    task_desc: str,
    cam_voter: Callable | None,
    cam_frames: dict,
    endstate_frames: int = 8,
    blind_rescue_votes: int = 2,
) -> CheckResult:
    """二值复核 v7.2:**逐机位独立投票 + 汇票 + 决定表**(105 条人工真值 × 三模型
    小考定稿;取代 v6.5 的多机位混问——好机位的清晰证据曾被烂机位稀释)。

    注入接口(框架无关):
      cam_voter(starts, ends, cam_label, desc) -> 'yes'/'no'/'unclear'/
        'contradictory'/'unavail';None = 投票器不可用(构造失败)。
      cam_frames: {相机名: 全程帧列表}(有序 dict;每路 linspace 含端点抽
        endstate_frames 帧,前半进 starts 后半进 ends——截尾教训见 _sample_indices)。

    决定表(初判 × 汇票;救人一签、杀人双签、复核有否决权无处决权):
                    yes         split      no                abstain
      成功候选      过          过(留痕)   强过(disputed)/   过(留痕单判据)
                                           弱→人工(否决)
      失败候选      人工(打架)  人工       **杀**(唯一杀格)  人工
      gap契约违约   人工(打架)  人工       人工              人工
      灰区          过(救回)*   人工       人工              人工
      score_blind   过(救回)**  人工       人工              人工
      其余(绊线等)  过(救回)    人工       人工              人工
      * 灰区×yes 且 final≤fail_max → 人工(末态回原点 vs 复核说完成 = 实质矛盾;
        联合打分曾把 ep143 的 gap 平滑到违约线下,靠这条拦回)
      ** blind×yes 需 ≥blind_rescue_votes 张实票(打分层零信息时救回也要双签;
        8b 全瞎+腕部孤票 yes 曾漏 ep143)
    """
    init = _INIT_ROW.get(res.detail.get("verdict"), res.detail.get("verdict", "other"))
    strong = bool(res.detail.get("strong_score"))
    final = res.detail.get("completion_final")

    if cam_voter is None:
        res.detail["endstate"] = "复核投票器不可用,仅打分层单判据"
        if res.passed is False:
            res.passed = None                         # 杀人必须双签
            res.detail["reason"] = "失败候选但复核不可用:不凭单判据硬杀,进人工"
        return res

    votes = {}
    for i, (name, fr) in enumerate(cam_frames.items()):
        fr = list(fr)
        if not fr:
            continue
        pick = [fr[j] for j in _sample_indices(len(fr), endstate_frames)]
        mid = max(1, len(pick) // 2)
        label = f"camera {chr(ord('A') + i)} ({name})"
        votes[name] = cam_voter(pick[:mid], pick[mid:] or pick[-1:], label,
                                str(task_desc))
    if not votes:
        return res                                    # 无帧可复核,原样返回

    review = tally(votes.values())
    yes_votes = sum(1 for v in votes.values() if v == "yes")
    res.detail["cam_votes"] = dict(votes)
    res.detail["review"] = review

    if init == "success_cand":
        if review == "no":
            if strong:
                res.detail["review_disputed"] = True
                res.detail["reason"] = "复核一致判未完成,但打分层强证据压过否决"
            else:
                res.passed = None
                res.detail["verdict"] = "endstate_failure_suspect"
                res.detail["reason"] = "打分层弱成功证据被逐机位复核一致否决:进人工,不硬杀"
        elif review == "split":
            res.detail["review_split"] = True         # 有实票反对,留痕可过滤
        elif review == "abstain":
            if strong:
                res.detail["endstate"] = "全体机位弃权/矛盾,仅打分层单判据(强证据)"
            else:
                # v7.3(端到端冒烟实锤):弱分数 + 没有任何够格证人 = 纯打分层孤证。
                # 不可复现性下弱曲线会跨 run 漂移(ep131 一轮 final 0.25 一轮 0.65),
                # 孤证放行就是漏径 → 转人工。注意与"投票器不可用"(系统故障态,
                # cam_voter is None 分支)刻意区分:那是基础设施缺席,不是证据缺席。
                res.passed = None
                res.detail["verdict"] = "endstate_unconfirmed"
                res.detail["reason"] = "打分层弱成功证据且全体机位弃权/矛盾:孤证不放行,进人工"
        return res

    if init == "fail_cand":
        if review == "no":
            res.passed = False                        # ◆全表唯一杀格:双签
            res.detail["verdict"] = "failure"
            res.detail["reason"] = "联合打分全程无进度且逐机位复核一致判未完成:双签硬杀"
        elif review == "yes":
            res.passed = None
            res.detail["verdict"] = "review_conflict"
            res.detail["reason"] = "联合打分全程无进度 vs 复核判完成:两层打架,进人工"
        else:
            res.passed = None
            res.detail["reason"] = "失败候选但复核无一致结论:缺第二签,不硬杀,进人工"
        return res

    if init == "gap_violation":
        if review == "yes":
            res.detail["verdict"] = "review_conflict"
            res.detail["reason"] = "打分层契约违约 vs 复核判完成:两层证据激烈矛盾,进人工"
        return res                                    # 其余列维持弃权

    # 灰区 / score_blind / voc_tripwire / 调用失败:复核 yes 才可能救回
    if review == "yes":
        if init == "gray" and final is not None and final <= 0.25:
            res.detail["verdict"] = "review_conflict"
            res.detail["reason"] = "打分层末态回到原点 vs 复核判完成:实质矛盾,进人工"
            return res
        if init == "score_blind" and yes_votes < blind_rescue_votes:
            res.detail["reason"] = (f"打分层无信息且复核仅 {yes_votes} 张实票 yes"
                                    f"(<{blind_rescue_votes}):孤证不救,进人工")
            return res
        res.passed = True
        res.detail["verdict"] = "endstate_success"
        res.detail["reason"] = f"打分层{init};逐机位复核判完成,救回"
    return res
