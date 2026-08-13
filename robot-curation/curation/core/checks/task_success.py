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


def _rule(detail: dict, name: str) -> None:
    """记一条"走到了哪个判据"(纯旁路:只往 detail 里追加,判定分支一个都不碰)。

    为什么要有它:reason 是给人读的中文句子,离线扫规则得靠正则去猜"这条是被哪条
    判据处理的";verdict 又只留最终态,复核推翻初判后连"打分层原本怎么想"都没了。
    校准要按判据分桶统计(改一条阈值会动多少条),必须有机器可读的标识。
    """
    detail.setdefault("rules", []).append(name)


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
                           detail={"reason": "帧数不足", "rules": ["frames_insufficient"]})

    try:
        voc, preds, idx = voc_score(frames, instruction, vlm_completion, n_probe=n_probe)
    except Exception as e:  # noqa: BLE001  真模型会抽风(格式不符/超时)→ 不可判,不崩管线
        return CheckResult(name="task_success", passed=None,
                           detail={"reason": f"VLM 调用/解析失败: {type(e).__name__}: {e}",
                                   "rules": ["vlm_call_failed"]})
    final = float(np.median(preds[-2:]))
    peak = float(preds.max())
    gap = peak - final
    last = float(preds[-1])
    detail = {"voc": round(voc, 4), "completion_final": round(final, 4),
              "completion_peak": round(peak, 4), "completion_gap": round(gap, 4),
              "completions": [round(float(p), 3) for p in preds],
              "probe_frames": idx.tolist()}
    # 展示用的读数一律四舍五入(上面那几个 round 是历史约定,交付/UI 都在读它们),
    # 但校准要按阈值重扫,0.4166… 被截成 0.417 再跟 0.45 比就可能翻边 —— 原始精度
    # 单独留一份,谁也不动谁。多视角联合打分是各路取均值,小数尾巴是真实存在的。
    detail["raw"] = {"voc": voc, "completion_final": final, "completion_peak": peak,
                     "completion_gap": gap, "completion_last": last,
                     "completions": [float(p) for p in preds]}

    # 全平低位 = 打分层对这条完全无信息(每帧都答 0):弃权交复核,绝不是失败证据。
    # 注意只有**低位**才算瞎——全平在高位(如 ep99 全 1.0)是正常满分,别一刀切。
    if float(np.std(preds)) < 1e-9 and float(preds.mean()) <= fail_max:
        detail["verdict"] = "score_blind"
        detail["reason"] = "逐帧分数全平于低位:打分层无信息(非失败证据),交复核裁决"
        _rule(detail, "score_blind_flat_low")
        return CheckResult(name="task_success", passed=None, detail=detail)

    if final >= success_min:
        if voc < 0.0:
            # VOC 绊线:结论好但分数与时序负相关 = 过程语无伦次,final 不敢采信
            detail["verdict"] = "voc_tripwire"
            _rule(detail, "voc_tripwire_negative")
            detail["reason"] = f"末态 {final:.2f} 达标但 VOC {voc:.2f} 为负:过程混乱,不敢信"
            return CheckResult(name="task_success", passed=None, detail=detail)
        # 强分数 = 复核否决也压得过的两种形态(见 endstate_review):
        #   ①末段稳定高位;②单调爬升到末帧完成(ep30/ep54 型:最后一刻做完,
        #     中位数被拉低但末帧 1.0 + 过程单调,证据链完整)
        strong = (final >= 0.8 and gap <= 0.2) or (voc >= 0.6 and last >= 0.8
                                                   and peak - last <= 0.2)
        detail["strong_score"] = bool(strong)
        _rule(detail, "success_candidate_strong" if strong else "success_candidate_weak")
        running_max = np.maximum.accumulate(preds)
        dip = float((running_max - preds).max())
        detail["raw"]["dip"] = dip
        if dip >= recovery_dip:
            detail["verdict"] = "recovery"          # 中途回落后仍完成:保留但标注
            detail["dip"] = round(dip, 4)
            _rule(detail, "recovery_dip")
        else:
            detail["verdict"] = "success"
        return CheckResult(name="task_success", passed=True, detail=detail)

    if peak <= fail_max:
        # 全程从未有过进度,才有资格谈失败(复核 no 双签后才真杀)
        detail["verdict"] = "failure"
        _rule(detail, "fail_candidate_no_progress")
        detail["reason"] = f"全程物证进度峰值 {peak:.2f} ≤ {fail_max}:失败候选(待复核双签)"
        return CheckResult(name="task_success", passed=False, detail=detail)

    if gap >= gap_max:
        # 契约违约:冲高又崩回。物证打分理论上单调,回落只有两种解释——模型对末态
        # 看不清(ep17/19/34 型)或真回退。两种都不该硬判,进灰区由复核+人工兜。
        detail["verdict"] = "gap_violation"
        detail["reason"] = (f"峰值 {peak:.2f} 崩至末态 {final:.2f}(gap≥{gap_max}):"
                            "单调契约违约,模型抽风或真回退,不硬判")
        _rule(detail, "gap_violation_monotonicity")
        return CheckResult(name="task_success", passed=None, detail=detail)

    detail["verdict"] = "uncertain"
    _rule(detail, "gray_zone_final")
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
    # 打分层的结论必须在这里存档:复核推翻初判时 verdict 会被就地改写,
    # 不留一份的话"打分层原本判了什么"就永远查不回来了(校准的第一手输入)。
    res.detail["init_verdict"] = res.detail.get("verdict", "")

    if cam_voter is None:
        res.detail["endstate"] = "复核投票器不可用,仅打分层单判据"
        _rule(res.detail, "voter_unavailable")
        if res.passed is False:
            res.passed = None                         # 杀人必须双签
            res.detail["reason"] = "失败候选但复核不可用:不凭单判据硬杀,进人工"
            _rule(res.detail, "kill_downgraded_no_second_signature")
        return res

    # 各机位投票相互独立 → 并发问询(2026-08-06:droid-30 基准显示复核段是
    # 判定后的第二大墙钟,单条复核链 3 机位串行 ≈ 3×两问×20s;并发后 ≈ 1×)。
    # 标签按机位序预先定死(camera A/B/C),并发不改变任何判定输入输出。
    tasks = []
    for i, (name, fr) in enumerate(cam_frames.items()):
        fr = list(fr)
        if not fr:
            continue
        pick = [fr[j] for j in _sample_indices(len(fr), endstate_frames)]
        mid = max(1, len(pick) // 2)
        label = f"camera {chr(ord('A') + i)} ({name})"
        tasks.append((name, pick[:mid], pick[mid:] or pick[-1:], label))
    votes = {}
    if tasks:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
            results = list(ex.map(
                lambda t: cam_voter(t[1], t[2], t[3], str(task_desc)), tasks))
        votes = {t[0]: v for t, v in zip(tasks, results)}
    if not votes:
        _rule(res.detail, "no_frames_to_review")
        return res                                    # 无帧可复核,原样返回

    review = tally(votes.values())
    yes_votes = sum(1 for v in votes.values() if v == "yes")
    res.detail["cam_votes"] = dict(votes)
    res.detail["review"] = review

    if init == "success_cand":
        if review == "yes":
            # 这一格什么也不改(初判即终判),但**必须留痕**:否则通过的条目
            # 只看得出"复核没反对",看不出"复核真的看清了并且点了头"。
            _rule(res.detail, "review_confirms_success")
        if review == "no":
            if strong:
                res.detail["review_disputed"] = True
                res.detail["reason"] = "复核一致判未完成,但打分层强证据压过否决"
                _rule(res.detail, "review_overruled_by_strong_score")
            else:
                res.passed = None
                res.detail["verdict"] = "endstate_failure_suspect"
                res.detail["reason"] = "打分层弱成功证据被逐机位复核一致否决:进人工,不硬杀"
                _rule(res.detail, "weak_success_vetoed_by_review")
        elif review == "split":
            res.detail["review_split"] = True         # 有实票反对,留痕可过滤
            _rule(res.detail, "review_split_recorded")
        elif review == "abstain":
            if strong:
                res.detail["endstate"] = "全体机位弃权/矛盾,仅打分层单判据(强证据)"
                _rule(res.detail, "single_evidence_strong_score")
            else:
                # v7.3(端到端冒烟实锤):弱分数 + 没有任何够格证人 = 纯打分层孤证。
                # 不可复现性下弱曲线会跨 run 漂移(ep131 一轮 final 0.25 一轮 0.65),
                # 孤证放行就是漏径 → 转人工。注意与"投票器不可用"(系统故障态,
                # cam_voter is None 分支)刻意区分:那是基础设施缺席,不是证据缺席。
                res.passed = None
                res.detail["verdict"] = "endstate_unconfirmed"
                res.detail["reason"] = "打分层弱成功证据且全体机位弃权/矛盾:孤证不放行,进人工"
                _rule(res.detail, "weak_success_uncorroborated")
        return res

    if init == "fail_cand":
        if review == "no":
            res.passed = False                        # ◆全表唯一杀格:双签
            res.detail["verdict"] = "failure"
            res.detail["reason"] = "联合打分全程无进度且逐机位复核一致判未完成:双签硬杀"
            _rule(res.detail, "double_signed_kill")
        elif review == "yes":
            res.passed = None
            res.detail["verdict"] = "review_conflict"
            res.detail["reason"] = "联合打分全程无进度 vs 复核判完成:两层打架,进人工"
            _rule(res.detail, "arms_conflict_fail_vs_done")
        else:
            res.passed = None
            res.detail["reason"] = "失败候选但复核无一致结论:缺第二签,不硬杀,进人工"
            _rule(res.detail, "kill_missing_second_signature")
        return res

    if init == "gap_violation":
        if review == "yes":
            res.detail["verdict"] = "review_conflict"
            res.detail["reason"] = "打分层契约违约 vs 复核判完成:两层证据激烈矛盾,进人工"
            _rule(res.detail, "arms_conflict_gap_vs_done")
        else:
            _rule(res.detail, "abstain_kept_review_not_done")
        return res                                    # 其余列维持弃权

    # 灰区 / score_blind / voc_tripwire / 调用失败:复核 yes 才可能救回
    if review != "yes":
        _rule(res.detail, "rescue_declined_review_not_done")
    if review == "yes":
        if init == "gray" and final is not None and final <= 0.25:
            res.detail["verdict"] = "review_conflict"
            res.detail["reason"] = "打分层末态回到原点 vs 复核判完成:实质矛盾,进人工"
            _rule(res.detail, "gray_final_zero_vs_review_done")
            return res
        if init == "score_blind" and yes_votes < blind_rescue_votes:
            res.detail["reason"] = (f"打分层无信息且复核仅 {yes_votes} 张实票 yes"
                                    f"(<{blind_rescue_votes}):孤证不救,进人工")
            _rule(res.detail, "blind_rescue_needs_two_votes")
            return res
        res.passed = True
        res.detail["verdict"] = "endstate_success"
        res.detail["reason"] = f"打分层{init};逐机位复核判完成,救回"
        _rule(res.detail, "review_rescue")
    return res


# ══ 取证仲裁链(2026-08-13 用户拍板:C严 + 杀需≥2路)═══════════════════════════
# 只接管打分层+复核层都弃权的条目
# (res.passed is None),给出 过/杀/继续弃权 —— 没有推翻老判决的权力。
# droid-200 真值实测:人工 50→32(-36%),代价 = 冤杀 1→4(被拒复议轨道兜底)。
#
# 三条不许丢的护栏(每条都有实验数字撑着,删一条冤杀就涨):
#   ① 意图打架:标注与 caption 语义不同 → 双意图各跑一遍,两遍结论相同才自动判
#     (去掉它冤杀 17→26);
#   ② 判官讲理标准(prompt 在 adapters,三条:达成即YES/看不见答UNCLEAR绝不猜NO/
#     空间短语按说话人本意)把冤杀 52→17;
#   ③ 杀需≥2条有效路:实测只有一路的杀 5 条全是冤杀(与 v7.3 杀人双签同源)。

# 腕部线取证帧的时间偏移(秒,相对抓取/投放锚点)。取自 arb_bench_b_v2 实验版:
# 瞬态任务看抓取前后(松爪≠失败,ep181 教训),持久任务看投放前后与其后的静置。
_ARB_WRIST_OFFSETS_TRANSIENT = (-0.5, 0.0, 0.5, 1.5)
_ARB_WRIST_OFFSETS_PERSISTENT = (-0.5, 0.0, 1.0, 2.5)


def gripper_event_time(gripper, ts, *, closing: bool,
                       min_step: float = 0.15) -> float | None:
    """夹爪信号里幅度最大的闭爪(closing=True)/开爪时刻(事件后一帧的时间)。

    - 夹爪列由调用方按 registry 的 gripper_dims 提供(⚠️ 不硬编码数据集布局);
      可为 [T] 或 [T,G](双臂多爪:取事件幅度最大的那一列)。
    - 每列先按本条 min-max 归一再差分:0-1 制与 0-100 制同一把尺,min_step 才有
      统一含义。代价是"全程只抖不动"的夹爪可能被放大出假事件——但这只影响选哪
      一帧取证,判官看到的仍是真实画面,不构成误判来源。
    - 极性沿用 droid 约定(信号增大=闭合);极性反的数据集事件方向会认反,
      取证帧偏离但同样只是"看错时刻",不是"看假证据"。
    - 找不到达标事件返回 None(调用方自选兜底帧,这里不猜)。
    """
    g = np.asarray(gripper, dtype=np.float64)
    if g.ndim == 1:
        g = g[:, None]
    t = np.asarray(ts, dtype=np.float64)
    n = min(len(g), len(t))
    if n < 2:
        return None
    g, t = g[:n], t[:n]
    best: tuple[float, float] | None = None          # (事件幅度, 时刻)
    for j in range(g.shape[1]):
        col = g[:, j]
        lo, hi = float(col.min()), float(col.max())
        if hi - lo < 1e-9:
            continue                                  # 全程不动的爪,无事件
        d = np.diff((col - lo) / (hi - lo))
        if not closing:
            d = -d
        k = int(np.argmax(d))
        if d[k] >= min_step and (best is None or float(d[k]) > best[0]):
            best = (float(d[k]), float(t[k + 1]))
    return None if best is None else best[1]


def _arb_union_crop(img: np.ndarray, boxes: list, pad: float, upscale: int):
    """两框并集 + 外扩 pad → 裁剪 → 放大 upscale 倍(小目标看得清)。裁不出返回 None。"""
    import cv2

    arr = np.asarray(img)
    h, w = arr.shape[:2]
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes); y1 = max(b[3] for b in boxes)
    dx, dy = (x1 - x0) * pad, (y1 - y0) * pad
    x0, x1 = max(0, int(x0 - dx)), min(w, int(x1 + dx))
    y0, y1 = max(0, int(y0 - dy)), min(h, int(y1 + dy))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    tile = arr[y0:y1, x0:x1]
    return cv2.resize(tile, (tile.shape[1] * upscale, tile.shape[0] * upscale),
                      interpolation=cv2.INTER_LANCZOS4)


def _arb_upscale(img: np.ndarray, factor: int = 2):
    import cv2

    arr = np.asarray(img)
    return cv2.resize(arr, (arr.shape[1] * factor, arr.shape[0] * factor),
                      interpolation=cv2.INTER_LANCZOS4)


def _arb_line_verdict(votes: list) -> str:
    """一路的三票 → 该路结论:严格多数(plurality)才算,平票=看不清。

    unclear(与调用失败的 error)参与计票——两票 unclear 压一票 yes 时,这一路
    就是"大体看不清",不能算有效证词(实验版 line_majority 的语义);三方平票
    时实验版取 Counter 插入序纯属未定义行为,生产改为诚实判 unclear。
    """
    y = votes.count("yes")
    n = votes.count("no")
    u = len(votes) - y - n
    top = max(y, n, u)
    if y == top and n < top and u < top:
        return "yes"
    if n == top and y < top and u < top:
        return "no"
    return "unclear"


def _arb_single_chain(intent: str, cam_frames: dict, cam_ts: dict,
                      gripper, gripper_ts, *,
                      question_writer, grounder, judge,
                      n_votes: int, crop_pad: float, upscale: int,
                      transient_offset_s: float, max_cams: int) -> dict:
    """单意图跑完整取证链,返回该 run 的痕迹与共识(不做杀门槛,门槛在上层)。

    路的划分:相机名含 wrist(不分大小写)= 腕部线,其余 = 外部取证线(封顶
    max_cams)。registry 的 cameras 角色表当前全库为空,名字启发式是唯一可用的
    通用判据;没有腕部相机就没有腕部线,少一路而已。
    """
    from concurrent.futures import ThreadPoolExecutor

    run: dict = {"intent": intent, "lines": {}, "line_verdicts": {}}
    try:
        spec = question_writer(intent)
    except Exception as e:  # noqa: BLE001  问题生成失败=整条链无题可验,如实弃权
        run.update(error=f"问题生成失败: {type(e).__name__}: {e}",
                   consensus="abstain", n_effective=0)
        return run
    run["spec"] = spec
    target = str(spec.get("target_location") or "the target area")
    visual = str(spec.get("target_visual") or "")
    obj = str(spec.get("object") or "the object")
    question = str(spec.get("verify_question") or "")
    transient = str(spec.get("task_type")) == "transient"

    def _vote(imgs: list, scene: str) -> list:
        def one(_):
            try:
                return str(judge(imgs, target=target, question=question, scene=scene))
            except Exception:  # noqa: BLE001  单票失败=少一票,按 error 记不计入多数
                return "error"
        # 三票互不依赖 → 并发(与 endstate_review 的机位并发同款;判定输入输出不变)
        with ThreadPoolExecutor(max_workers=max(1, n_votes)) as ex:
            return list(ex.map(one, range(n_votes)))

    names = sorted(cam_frames)
    ext_cams = [c for c in names if "wrist" not in c.lower()][:max_cams]
    wrist_cams = [c for c in names if "wrist" in c.lower()]

    t_grasp = None
    if transient and gripper is not None and gripper_ts is not None:
        t_grasp = gripper_event_time(gripper, gripper_ts, closing=True)

    # ── 外部取证线:定位 → 裁剪放大 → 判官三票 ──────────────────────────────
    for cam in ext_cams:
        frames = list(cam_frames.get(cam) or [])
        fts = np.asarray(cam_ts.get(cam, ()), dtype=np.float64)
        if not frames:
            continue
        if transient:
            if t_grasp is not None and len(fts) == len(frames) and len(fts):
                fi = int(np.argmin(np.abs(fts - (t_grasp + transient_offset_s))))
            else:
                fi = len(frames) // 2     # 无夹爪事件可依:取中段,不猜末帧(松爪≠失败)
        else:
            fi = len(frames) - 1          # 持久任务:验末帧(撤手后仍须成立)
        pic = np.asarray(frames[fi])
        line: dict = {"frame": int(fi)}
        try:
            boxes = grounder(pic, target, visual, obj)
        except Exception as e:  # noqa: BLE001  定位失败=该路无证据,跳过不产票
            line["skipped"] = f"grounding 失败: {type(e).__name__}"
            run["lines"][cam] = line
            continue
        line["n_boxes"] = len(boxes)
        if not boxes:
            line["skipped"] = "target/object 均不可见,无框可裁"
            run["lines"][cam] = line
            continue
        tile = _arb_union_crop(pic, boxes, crop_pad, upscale)
        imgs = [pic] + ([tile] if tile is not None else [])
        votes = _vote(imgs, "exterior_post_grasp" if transient else "exterior_final")
        line["votes"] = votes
        run["lines"][cam] = line
        v = _arb_line_verdict(votes)
        if v in ("yes", "no"):
            run["line_verdicts"][cam] = v

    # ── 腕部线:抓取(瞬态)/投放(持久)前后 4 帧 → 判官三票 ─────────────────
    for cam in wrist_cams:
        frames = list(cam_frames.get(cam) or [])
        fts = np.asarray(cam_ts.get(cam, ()), dtype=np.float64)
        if not frames:
            continue
        if transient:
            t0, offs, scene = t_grasp, _ARB_WRIST_OFFSETS_TRANSIENT, "wrist_grasp"
        else:
            t0 = (gripper_event_time(gripper, gripper_ts, closing=False)
                  if gripper is not None and gripper_ts is not None else None)
            offs, scene = _ARB_WRIST_OFFSETS_PERSISTENT, "wrist_release"
        if len(fts) != len(frames) or not len(fts):
            sel = sorted({0, len(frames) // 2, len(frames) - 1})
            t0 = None
        else:
            if t0 is None:                 # 无夹爪事件:瞬态取中段,持久取末段
                t0 = float(fts[len(fts) // 2]) if transient else float(fts[-1])
            sel = sorted({int(np.argmin(np.abs(fts - (t0 + o)))) for o in offs})
        imgs = [_arb_upscale(frames[i]) for i in sel]
        votes = _vote(imgs, scene)
        line = {"frames": [int(i) for i in sel], "votes": votes}
        if t0 is not None:
            line["anchor_t"] = round(float(t0), 3)
        run["lines"][cam] = line
        v = _arb_line_verdict(votes)
        if v in ("yes", "no"):
            run["line_verdicts"][cam] = v

    eff = [v for v in run["line_verdicts"].values()]
    run["n_effective"] = len(eff)
    if not eff:
        run["consensus"] = "abstain"
    elif all(v == eff[0] for v in eff):
        run["consensus"] = eff[0]         # strict:全部有效路一致才有结论
    else:
        run["consensus"] = "split"
    return run


def _arb_final(run: dict, kill_min_lines: int) -> tuple[str, bool]:
    """共识 → 终结论 (final, kill_downgraded)。🔴 杀需≥2条有效路,孤证降级弃权。"""
    c = run.get("consensus")
    if c == "yes":
        return "yes", False
    if c == "no":
        if run.get("n_effective", 0) < kill_min_lines:
            return "abstain", True        # 实测:仅一路的杀 5 条全是冤杀
        return "no", False
    return "abstain", False


def arbitration_review(
    res: CheckResult,
    *,
    caption: str,
    caption_source: str = "自产caption",
    annotation: str = "",
    cam_frames: dict,
    cam_ts: dict,
    gripper=None,
    gripper_ts=None,
    question_writer: Callable | None = None,
    grounder: Callable | None = None,
    judge: Callable | None = None,
    same_task: Callable | None = None,
    kill_min_lines: int = 2,
    n_votes: int = 3,
    crop_pad: float = 0.15,
    upscale: int = 3,
    transient_offset_s: float = 1.0,
    max_cams: int = 4,
) -> CheckResult:
    """取证仲裁链(与 endstate_review 同风格:注入式依赖,core 内不发 HTTP)。

    只在 res.passed is None(打分+复核后仍弃权)时工作;其余条目**原样返回**
    (逐字段不动)—— 老算法判过的条目不许被翻案。

    注入接口(全部由 adapters.vlm_client 工厂构造,测试注入假函数):
      question_writer(intent) -> {task_type, target_location, target_visual,
        object, verify_question}(异常=该意图整链弃权)
      grounder(img, target, visual, obj) -> [(x0,y0,x1,y1)像素框, ...](空=该路跳过)
      judge(imgs, *, target, question, scene) -> 'yes'/'no'/'unclear'
      same_task(a, b) -> bool(True=同一任务;不可用/异常=按打架从严处理)

    结论写回:yes → passed=True verdict=arbitration_success;
    no(≥kill_min_lines 条有效路)→ passed=False verdict=arbitration_failure;
    其余维持原弃权,**不覆盖**打分层/复核层已写的 verdict 与 reason。
    无论结论如何都在 detail["arbitration"] 留痕(task_trace 纯装配搬走)。
    """
    if res.passed is not None:
        return res

    arb: dict = {"applied": False}
    res.detail["arbitration"] = arb
    caption = str(caption or "").strip()
    if not caption:
        # 主意图=自产 caption;拿不到就不跑(拿标注顶会退回"标注优先",违背规格)
        arb["skipped"] = "无自产 caption(拿不到独立意图,维持弃权)"
        _rule(res.detail, "arbitration_skipped_no_caption")
        return res
    if question_writer is None or grounder is None or judge is None:
        arb["skipped"] = "仲裁依赖不可用,维持弃权"
        _rule(res.detail, "arbitration_skipped_deps_unavailable")
        return res

    arb.update(intent=caption, intent_source=caption_source, intent_conflict=False)
    annotation = str(annotation or "").strip()
    if annotation and annotation.lower() != caption.lower():
        # 意图打架护栏:语义比对判 DIFFERENT 才算打架(措辞不同不算)。
        # 比对器不可用/失败 → 按打架从严(宁可多跑一遍,不省这道护栏)。
        if same_task is None:
            arb["intent_conflict"] = True
            arb["intent_conflict_basis"] = "语义比对器不可用,按打架从严处理"
        else:
            try:
                arb["intent_conflict"] = not bool(same_task(caption, annotation))
            except Exception as e:  # noqa: BLE001
                arb["intent_conflict"] = True
                arb["intent_conflict_basis"] = (
                    f"语义比对失败({type(e).__name__}),按打架从严处理")

    chain_kw = dict(question_writer=question_writer, grounder=grounder, judge=judge,
                    n_votes=n_votes, crop_pad=crop_pad, upscale=upscale,
                    transient_offset_s=transient_offset_s, max_cams=max_cams)
    run_a = _arb_single_chain(caption, cam_frames, cam_ts, gripper, gripper_ts,
                              **chain_kw)
    arb["spec"] = run_a.get("spec")
    arb["lines"] = run_a["lines"]
    arb["n_effective"] = run_a["n_effective"]
    arb["consensus"] = run_a["consensus"]
    if run_a.get("error"):
        arb["error"] = run_a["error"]
    final, downgraded = _arb_final(run_a, kill_min_lines)
    if downgraded:
        arb["kill_downgraded"] = True
        _rule(res.detail, "arbitration_kill_needs_two_lines")

    if arb["intent_conflict"]:
        # 双意图各跑一遍,两遍结论(各自过完杀门槛)相同才自动判,否则维持弃权
        run_b = _arb_single_chain(annotation, cam_frames, cam_ts, gripper,
                                  gripper_ts, **chain_kw)
        final_b, downgraded_b = _arb_final(run_b, kill_min_lines)
        arb["annotation_run"] = {
            "intent": annotation, "spec": run_b.get("spec"),
            "lines": run_b["lines"], "n_effective": run_b["n_effective"],
            "consensus": run_b["consensus"], "final": final_b}
        if downgraded_b:
            arb["annotation_run"]["kill_downgraded"] = True
        if final != final_b or final not in ("yes", "no"):
            if final in ("yes", "no") or final_b in ("yes", "no"):
                _rule(res.detail, "arbitration_intent_conflict_disagree")
            final = "abstain"

    arb["final"] = final
    n_eff = arb["n_effective"]
    if final == "yes":
        res.passed = True
        res.detail["verdict"] = "arbitration_success"
        res.detail["reason"] = (f"取证仲裁:{n_eff} 条有效取证路一致判完成,救回"
                                +("(双意图结论一致)" if arb["intent_conflict"] else ""))
        _rule(res.detail, "arbitration_success")
        arb["applied"] = True
    elif final == "no":
        res.passed = False
        res.detail["verdict"] = "arbitration_failure"
        res.detail["reason"] = (f"取证仲裁:{n_eff} 条有效取证路一致判未完成"
                                f"(≥{kill_min_lines} 路双签)"
                                + ("(双意图结论一致)" if arb["intent_conflict"] else ""))
        _rule(res.detail, "arbitration_kill_double_signed")
        arb["applied"] = True
    else:
        _rule(res.detail, "arbitration_abstain")
    return res
