"""M7 第三步:原始标注 ↔ 自产画面描述的**分歧检出**(不是判决,不预设谁对)。

⚠️ 立场(2026-07-31 修正,原文档曾写"标注身份=被审对象",错):**双方都可能错**。
我们的描述由 VLM 生成,实测两个硬伤:①**可能整段看错**——droid ep000143 真实剧本是
"从洗手池拿毛巾→贴墙擦→放回台面"(逐帧看过视频,原始标注逐字正确),自产描述却说成
"Hang the towel on the hook";②**不可复现**——同一条 episode、同一批帧,方舟
temperature=0 连打 5 次得到 5 种说法(自托管 32B 则 5 次一致)。拿一个不稳定的东西当
基准去审判客户标注,是产品级缺陷。

所以本模块只做一件事:把"原始标注与自产描述归进了不同技能族"的条目挑出来,产出
**按嫌疑排序的复核队列供人工判定**,不下结论说谁错。

机制(8 次迭代定稿):原始标注经 LLM 映射进与 caption 同一套族体系 → 动词等价类修正后,
"高对比度分歧"(如 开合↔操纵搬放)与"标注乱码"进高置信档;同义动词分歧进人工复核档。
分档之后可再经 `retier_by_caption_stability`(纯数据函数):对被标记条目重复打标 N 次,
**我方描述自己都不稳的条目降级**——连自己看的是什么都没定,就不该拿它质疑标注。
"""
from __future__ import annotations

import json
import re
from typing import Callable

LlmAsk = Callable[[str], str]
# caption 文本 → 族名(注入式;生产里由 taxonomy 的归族能力提供,测试注入假映射)
FamilyOf = Callable[[str], str]

LABEL_MAP_PROMPT = (
    "Skill families: {families}. For each annotation below, output which family it belongs "
    "to, or 'garbage' if the text is meaningless. Return STRICT JSON: "
    '{{"map": [{{"label": str, "family": str}}]}}\n\nANNOTATIONS:\n')

# 动词等价类:操纵搬放一族(place/move/grab/pick/sweep 互不立案)
_EQUIV = ("place", "move", "grab", "pick", "sweep", "manipulat", "rearrang", "put")

# 标注本身是乱码 —— 与自产描述无关,故重打标稳定度对它不适用(不参与降级)
GARBAGE_REASON = "garbage"
# 第三档:分歧仍在,但我方描述不稳,不足以据此质疑标注
UNSTABLE_TIER = "low_caption_unstable"
_UNCLASSIFIED = "未归类"


def _canon(family: str) -> str:
    f = (family or "").lower()
    return "manipulate" if any(k in f for k in _EQUIV) else f


def audit_labels(episode_ids: list[str], instructions: list[str], captions: list[str],
                 cap_families: list[str], taxonomy: dict, llm_ask: LlmAsk) -> dict:
    """→ {"high": [...高置信...], "mid_for_review": [...人工复核...]}。"""
    fam_names = [f["name"] for f in taxonomy.get("families", [])]
    labeled = [(i, instructions[i].strip()) for i in range(len(instructions))
               if instructions[i].strip()]
    if not labeled or not fam_names:
        return {"high": [], "mid_for_review": []}

    uniq = sorted(set(s for _, s in labeled))
    raw = llm_ask(LABEL_MAP_PROMPT.format(families=fam_names)
                  + "\n".join(f"- {s}" for s in uniq))
    t = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
    fam_of_label = {m["label"].strip().lower(): m["family"]
                    for m in json.loads(t)["map"]}

    high, mid = [], []
    for i, lab in labeled:
        lf = fam_of_label.get(lab.lower())
        cf = cap_families[i]
        entry = {"id": episode_ids[i], "label": lab, "caption": captions[i]}
        if lf == "garbage":
            high.append({**entry, "reason": GARBAGE_REASON})
        elif lf and cf not in (_UNCLASSIFIED, None) and lf != cf:
            # 措辞中性:两边都只是"归进了哪个族",谁对谁错交人工——尤其自产描述必须
            # 带出身(VLM 生成),不能写成"画面=X"那种听起来像客观事实的裸表述。
            if _canon(lf) != _canon(cf):
                high.append({**entry, "reason": f"分歧:原始标注归为 {lf},"
                                                f"自产描述(VLM 生成)归为 {cf}——需人工判定"})
            else:
                mid.append({**entry, "reason": f"分歧(近义动词):原始标注归为 {lf},"
                                               f"自产描述(VLM 生成)归为 {cf}——需人工判定"})
    return {"high": high, "mid_for_review": mid}


def _families_of(caps: list[str], family_of: FamilyOf) -> list[str]:
    """一组 caption → 可比的族名列表(空串/未归类不参与比较,宁少勿错)。"""
    out = []
    for c in caps:
        if not str(c or "").strip():
            continue
        try:
            f = _canon(str(family_of(c) or ""))
        except Exception:  # noqa: BLE001  归族失败=这条无证据,不当作"不一致"
            continue
        if f and f != _UNCLASSIFIED:
            out.append(f)
    return out


def retier_by_caption_stability(audit: dict, recaps_by_id: dict[str, list[str]],
                                family_of: FamilyOf) -> dict:
    """用"我方打标的稳定度"给分歧重新分档(**纯数据函数,不碰视频、不持有 captioner**)。

    caption 不可复现既是缺陷也是信号:对被标记的条目重打标 N 次,
    - N 次(连同触发立案的那条原 caption)**归到同一个族** → 我方描述是稳的 → 分歧
      更可能出在标注 → 保持原档,记 `caption_stable: True`;
    - **自己就不一致** → 我们自己都没看准 → 降级进 `low_caption_unstable`,reason 注明
      "我方画面描述不稳定,不足以质疑标注",N 次原文全部记进条目供人看。

    为什么把原 caption 也算进比较集:立案正是基于它:若 N 次重打标彼此一致却与它不同,
    那恰恰证明我方描述在飘(ep000143 就是这种形状),不能算稳。

    一致性判定在**族**这一层而不是字符串相等——措辞天然会变("put the cup on the table"
    vs "place cup onto table"),族一致就算稳。family_of 由调用方注入(复用 taxonomy 的
    归族能力),本模块不重新实现归族。

    保守处理(不误伤):
    - `reason == "garbage"`(标注乱码)与自产描述无关 → 永不降级;
    - 某条重打标失败(空串)/全部未归类,可比样本 < 2 → **证据不足,维持原档**并记
      `caption_stable: None`,既不据此指控也不据此降级;
    - `recaps_by_id` 为空(如 audit_recheck_n=1 关闭本机制)→ 原样返回,零改动。
    """
    if not recaps_by_id:
        return {k: list(v) for k, v in audit.items()}

    out: dict = {"high": [], "mid_for_review": []}
    unstable: list = []
    for tier in ("high", "mid_for_review"):
        for e in audit.get(tier, []):
            raw = recaps_by_id.get(e.get("id"))
            if e.get("reason") == GARBAGE_REASON or raw is None:
                out[tier].append(dict(e))       # 乱码档/根本没重打标过 → 原样不动
                continue
            recaps = [str(c) for c in raw if str(c).strip()]
            fams = _families_of([e.get("caption", ""), *recaps], family_of)
            if len(fams) < 2:
                out[tier].append({**e, "caption_stable": None, "recaptions": recaps,
                                  "recheck_note": "重打标未取得可比结果,稳定度未知,维持原档"})
            elif len(set(fams)) == 1:
                out[tier].append({**e, "caption_stable": True, "recaptions": recaps})
            else:
                seen = list(dict.fromkeys(fams))
                unstable.append({
                    **e, "caption_stable": False, "recaptions": recaps,
                    "recaption_families": fams,
                    "reason": (f"{e.get('reason', '')};但我方画面描述不稳定"
                               f"(重打标 {len(recaps)} 次,连同原描述共归入 "
                               f"{'/'.join(seen)}),不足以质疑标注")})
    out[UNSTABLE_TIER] = unstable
    return out
