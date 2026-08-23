"""M7 第二步:LLM 对去重 caption 归纳两级技能体系(类名从数据里长出,零手写)。

2026-07-11 判据显式化(用户定):
- 分类维度/颗粒度由 **guideline**(配置 skill_profile.taxonomy_guideline)声明,
  不再用 "4-8 族" 这类数字范围倒逼粒度——类数由数据在判据下自然涌现;
- 每个族/子技能要求 LLM 给一行 criterion(为什么算一类),分类可审计;
- 仅保留防退化禁令(禁单类装全部/禁每条自成一类)。

llm_ask 注入式(纯文本调用;测试注入假函数返回固定 JSON)。
"""
from __future__ import annotations

import json
import re
from typing import Callable

# llm_ask(prompt_text: str) -> str(模型回答原文)
LlmAsk = Callable[[str], str]

# 默认判据:按操作技能分,物体身份/颜色/位置不是依据。
# 2026-07-30 粒度改进:**族 = 交互类型(机器人怎么跟物体发生作用),不再是动词**。
#   证据 droid-200:旧判据(Family = coarse skill verb)归出 24 族,前四族
#   Put(102)/Pick(15)/Take(13)/Move(11)=72% 其实是同一种抓放,11 个族只有 1 条,
#   其中还有 Fold/Unfold、Push/Pull、Open/Unscrew 这类互逆对。根因是英语动词天然细碎,
#   模型是忠实执行判据而不是跑偏 → 改判据文本(代码逻辑不动)。
#   典型族只作参考不是封闭枚举(客户数据可能是缝合/布线,硬套示例族反而荒谬)。
# 与 default.yaml 的 skill_profile.taxonomy_guideline 保持同文(改一处必须改另一处);
# 客户可整段替换。
DEFAULT_GUIDELINE = (
    "Group by MANIPULATION SKILL, at TWO levels: family = interaction type, "
    "sub-skill = motion-pattern variant.\n"
    "- FAMILY = the TYPE OF PHYSICAL INTERACTION between robot and object (how the "
    "robot acts on it), NOT whichever verb a caption happens to use. Typical "
    "families, take only what the data needs: grasp-and-transport (grasp / lift / "
    "carry / place / release — pick, take, put, move, stack ALL belong here); "
    "articulated-object actuation (open, close, unscrew, plug, press, switch); "
    "deformable-object manipulation (fold, unfold, bundle, drape); non-prehensile "
    "manipulation (push, pull, tip, flip, slide — object never grasped); tool cyclic "
    "motion (wipe, stir, sweep, vacuum); pouring / granular transfer.\n"
    "- That list is a REFERENCE, not a closed set. If the data is a different kind of "
    "interaction (suturing, cable routing, assembly, ...), name a family after ITS "
    "interaction type. Never file an episode into a listed family whose interaction "
    "type it does not actually match — a wrong-but-familiar label is worse than a new "
    "family.\n"
    "- NEAR-SYNONYM verbs NEVER get their own family: pick / take / grab / lift are "
    "ONE family, and put / place / set belong to that SAME family.\n"
    "- INVERSE action pairs stay in the SAME family ONLY IF they share the same CONTACT "
    "MODE (fold/unfold, open/close, screw/unscrew — both directions touch and hold the "
    "object the same way); for those, direction is a SUB-SKILL difference, never a "
    "family difference. push/pull are NOT such a pair: pushing normally never grasps "
    "while pulling normally requires a grasp, so file each of them by its actual "
    "CONTACT MODE, not by the fact that they are opposites.\n"
    "- SUB-SKILL = verb + motion-pattern variant WITHIN the family: place-onto-surface "
    "vs place-into-container vs stack; open vs close; fold vs unfold.\n"
    "- SUB-SKILL granularity is governed as STRICTLY as family granularity. A sub-skill "
    "is a RECURRING MOTION PATTERN, not a one-off phrasing. Two candidate sub-skills "
    "that differ ONLY by destination, by object identity, or by what the object is "
    "attached to / detached from are the SAME sub-skill and must be one.\n"
    "- INVERSE DIRECTIONS must stay SEPARATE sub-skills inside their family: open vs "
    "close, fold vs unfold, pour-in vs pour-out, screw vs unscrew are NOT the same "
    "skill and must NEVER be merged — the contact is alike but the motion runs the "
    "opposite way. Whenever two sub-skills are opposite directions of the same action, "
    "they are DIFFERENT skills. Never name a combined group after one direction.\n"
    "- Do NOT open a new sub-skill for a single caption merely because no existing one "
    "fits it exactly: if ANY sub-skill already in that family shares its motion "
    "pattern, put the caption there. A one-member sub-skill is legitimate ONLY when NO "
    "sub-skill in the family shares its motion pattern.\n"
    "- NEAR-SYNONYM verbs never get their own SUB-SKILL either: connect / join / "
    "attach / mate / insert / plug describing the SAME contact and the SAME motion "
    "path are ONE sub-skill. Before opening a second sub-skill inside a family, ask "
    "whether the two differ in the MOTION ITSELF (approach direction, contact mode, "
    "release) — if the only difference is which English verb the caption chose, they "
    "are the same sub-skill.\n"
    "- The number of sub-skills follows the number of DISTINCT MOTION PATTERNS in the "
    "family — no more, no fewer. A family whose episodes all share one motion pattern "
    "has exactly ONE sub-skill, and that is correct, not a degenerate failure. A "
    "family containing several distinct approach / contact / release patterns must "
    "expose all of them. Count is never the criterion; the motion is.\n"
    "- Object identity, color, size, brand, and table position are NOT grouping "
    "criteria: 'move the blue fork to the burner' and 'move the silver pot to the "
    "burner' are the SAME skill.\n"
    "- The DESTINATION/LOCATION (burner, sink, drawer, tray, side of table, ...) is "
    "also NOT a criterion: place-on-burner and place-on-tray are the SAME sub-skill "
    "(both place-onto-surface).\n"
    "- An object CATEGORY may distinguish sub-skills ONLY when it changes the motion "
    "pattern: place-onto-surface vs place-into-container differ (different approach "
    "and release motion); blue cube vs red cube do NOT, burner vs tray does NOT.\n"
    "- criterion texts must describe MOTION, never colors/object identities/locations.\n"
    "- The number of families and sub-skills must EMERGE from the data under this "
    "criterion: create exactly as many as naturally exist. Do not pad, do not force "
    "a count. One family holding everything is a failure; one sub-skill per caption "
    "is a failure. Emitting a family that has NO members is ALSO a failure — the "
    "typical families listed above are a VOCABULARY, not a checklist; never output a "
    "family you did not actually fill."
)

TAXONOMY_PROMPT_TMPL = (
    "Below are task captions of robot manipulation episodes. Build a TWO-LEVEL skill "
    "taxonomy following the GUIDELINE strictly.\n\n"
    "GUIDELINE:\n{guideline}\n\n"
    "Return STRICT JSON:\n"
    '{{"families": [{{"name": str, "criterion": str, "subskills": '
    '[{{"name": str, "criterion": str, "members": [<exact caption strings>]}}]}}]}}\n'
    "criterion = ONE short sentence (max 15 words) describing the shared MOTION; "
    "never mention specific objects, colors, or locations in it.\n"
    "EVERY caption must appear in EXACTLY ONE subskill's members, copied VERBATIM "
    "(character-for-character) — omitting or altering any caption is an error.\n\n"
    "CAPTIONS:\n")


def _parse_json(text: str) -> dict:
    t = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(t)


def induce_taxonomy(captions: list[str], llm_ask: LlmAsk,
                    guideline: str | None = None) -> dict:
    """去重 caption → LLM(带判据 guideline)→ {"families": [...]}。

    guideline 为空时用 DEFAULT_GUIDELINE(按技能动词分,物体/颜色不是依据)。
    返回体系中每个族/子技能带 criterion(LLM 自述的归类理由,进报告可审计)。
    """
    uniq = sorted(set(c.strip() for c in captions if c.strip()))
    if not uniq:
        return {"families": []}
    prompt = TAXONOMY_PROMPT_TMPL.format(guideline=(guideline or DEFAULT_GUIDELINE).strip())
    return _parse_json(llm_ask(prompt + "\n".join(f"- {c}" for c in uniq)))


def assign(captions: list[str], taxonomy: dict) -> tuple[list[str], list[str]]:
    """每条 caption → (族名, 子技能名);未映射/空 caption → ('未归类', '-')。"""
    fam_of, sub_of = {}, {}
    for fam in taxonomy.get("families", []):
        for sub in fam.get("subskills", []):
            for m in sub.get("members", []):
                key = m.strip().lower()
                fam_of[key] = fam["name"]
                sub_of[key] = sub["name"]
    fams, subs = [], []
    for c in captions:
        key = c.strip().lower()
        fams.append(fam_of.get(key, "未归类"))
        subs.append(sub_of.get(key, "-"))
    return fams, subs


def criteria_of(taxonomy: dict) -> tuple[dict, dict]:
    """taxonomy → ({族名: criterion}, {(族名,子技能名): criterion})(缺省空串)。"""
    fam_c, sub_c = {}, {}
    for fam in taxonomy.get("families", []):
        fam_c[fam["name"]] = str(fam.get("criterion", "") or "")
        for sub in fam.get("subskills", []):
            sub_c[(fam["name"], sub["name"])] = str(sub.get("criterion", "") or "")
    return fam_c, sub_c


# 一次一条(2026-08-23):漏网 caption 的指认是互相独立的判断,曾把全部漏网条目列进一条
# prompt 让 LLM 逐条回抄+指认——同一序列里的邻居会互相带偏(标注判官 40 对一批实测
# 13/29 对翻转,单对单 9/29),而且要求回抄 caption 原文对号、抄错一字就丢。现在每条
# caption 一个请求、并发,回答只要 (族, 子技能),不再回抄。
REPAIR_PROMPT_TMPL = (
    "An existing two-level skill taxonomy (family -> sub-skills):\n{tree}\n\n"
    "Assign the caption below to exactly one EXISTING sub-skill from the taxonomy "
    "above (do not invent new names). Return STRICT JSON: "
    '{{"family": str, "subskill": str}}\n\n'
    "CAPTION: {caption}")


def repair_unassigned(captions: list[str], taxonomy: dict, llm_ask: LlmAsk, *,
                      concurrency: int = 16) -> dict:
    """漏抄补齐:members 里被 LLM 漏掉的 caption,二次指认到既有子技能。

    每条 caption 一个请求(互相独立的判断不共享上下文),并发;修复回合只允许指认
    既有类,乱指(名字不存在)一律拒收,宁缺毋滥;单条失败只丢那一条。
    返回 {caption: (族名, 子技能名)},只含合法指认。
    """
    from concurrent.futures import ThreadPoolExecutor

    valid = {(f["name"], s["name"])
             for f in taxonomy.get("families", []) for s in f.get("subskills", [])}
    captions = [c for c in captions if c.strip()]
    if not captions or not valid:
        return {}
    tree = "\n".join(f"- {f['name']}: " + ", ".join(s["name"] for s in f.get("subskills", []))
                     for f in taxonomy.get("families", []))

    def _one(c: str):
        try:
            raw = _parse_json(llm_ask(REPAIR_PROMPT_TMPL.format(tree=tree, caption=c)))
            pair = (str(raw.get("family", "")), str(raw.get("subskill", "")))
            return pair if pair in valid else None
        except Exception:  # noqa: BLE001  单条失败不致命,该条保持未归类
            return None

    if concurrency <= 1 or len(captions) <= 1:
        pairs = [_one(c) for c in captions]
    else:
        with ThreadPoolExecutor(max_workers=min(int(concurrency), len(captions))) as ex:
            pairs = list(ex.map(_one, captions))
    return {c: p for c, p in zip(captions, pairs) if p is not None}


MERGE_Q_TMPL = (
    "GUIDELINE:\n{guideline}\n\n"
    "Sub-skill A: {a} — {ac}; examples: {am}\n"
    "Sub-skill B: {b} — {bc}; examples: {bm}\n"
    "Under the guideline, are A and B the SAME manipulation skill — differing only "
    "by destination/location/object/color, NOT by motion pattern? Answer ONLY yes or no.")

_CONNECTORS = ("to", "on", "in", "into", "onto", "from", "with", "at")


def _merged_name(names: list[str]) -> str:
    """合并名:'-'分词的公共前缀,去掉尾部连接词(move-to-side+move-to-burner→move)。"""
    toks = [n.split("-") for n in names]
    pref = []
    for parts in zip(*toks):
        if len(set(parts)) == 1:
            pref.append(parts[0])
        else:
            break
    while pref and pref[-1] in _CONNECTORS:
        pref.pop()
    return "-".join(pref) or names[0]


def refine_taxonomy(taxonomy: dict, llm_ask: LlmAsk,
                    guideline: str | None = None, concurrency: int = 16) -> dict:
    """守规合并:同族子技能两两二值问询,"仅目的地/物体/颜色不同"→ 代码里合并。

    为什么不是"让 LLM 审计整棵树重出 JSON":实测(2026-07-11 bridge)模型把
    move-to-burner 原样奉还,开放式审计不服从——与 M4c 的 endstate 教训同源:
    渐变/开放问询失效,**二值问题答得动**。合并动作在代码里做(并查集+成员并集),
    caption 物理上不可能丢;LLM 只回答 yes/no。异常/答非所问 → 视为 no(不合并)。

    并发(2026-08-06):全部候选对(跨族收集)**一波并发问询**,答案齐了再统一
    并查集合并——判定与逐对串行等价(合并的传递性由并查集给出),墙钟从
    Σ对数×延时 压到 ~ceil(对数/并发)×延时。droid-30 实测该阶段 605s 净串行,
    是整个 run 的第二瓶颈。串行版靠"已同组就跳过"省问询数,并行版多问那几对
    换 20 倍墙钟,划算。
    """
    g = (guideline or DEFAULT_GUIDELINE).strip()

    def _pair_q(subs, i, j):
        return MERGE_Q_TMPL.format(
            guideline=g,
            a=subs[i]["name"], ac=subs[i].get("criterion", ""),
            am="; ".join(subs[i].get("members", [])[:3]),
            b=subs[j]["name"], bc=subs[j].get("criterion", ""),
            bm="; ".join(subs[j].get("members", [])[:3]))

    # 跨族收集全部候选对 → 一波并发
    tasks = []          # (族序, i, j, prompt)
    fams = taxonomy.get("families", [])
    for fi, fam in enumerate(fams):
        subs = fam.get("subskills", [])
        for i in range(len(subs)):
            for j in range(i + 1, len(subs)):
                tasks.append((fi, i, j, _pair_q(subs, i, j)))

    def _ask(t):
        try:
            return "yes" in llm_ask(t[3]).strip().lower()[:8]
        except Exception:  # noqa: BLE001  问询失败=不合并,宁保守
            return False

    merges: dict = {}
    if tasks:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(max(1, concurrency),
                                                len(tasks))) as ex:
            for t, yes in zip(tasks, ex.map(_ask, tasks)):
                if yes:
                    merges.setdefault(t[0], []).append((t[1], t[2]))

    fams_out = []
    for fi, fam in enumerate(fams):
        subs = fam.get("subskills", [])
        n = len(subs)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, j in merges.get(fi, []):
            if find(i) != find(j):
                parent[find(j)] = find(i)
        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)
        new_subs = []
        for idxs in (groups[r] for r in sorted(groups)):
            if len(idxs) == 1:
                new_subs.append(subs[idxs[0]])
                continue
            new_subs.append({
                "name": _merged_name([subs[i]["name"] for i in idxs]),
                "criterion": subs[idxs[0]].get("criterion", ""),
                "members": [m for i in idxs for m in subs[i].get("members", [])]})
        fams_out.append({**fam, "subskills": new_subs})
    return {**taxonomy, "families": fams_out}
