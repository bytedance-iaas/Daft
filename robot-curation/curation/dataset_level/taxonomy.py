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

# 默认判据:按操作技能(动词+运动模式)分,物体身份/颜色/位置不是依据。
# 与 default.yaml 的 skill_profile.taxonomy_guideline 保持同文;客户可整段替换。
DEFAULT_GUIDELINE = (
    "Group by MANIPULATION SKILL = verb + motion pattern (pick, place, push, pour, "
    "open, close, fold, sweep, wipe, ...).\n"
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
    "- Family = coarse skill verb; sub-skill = motion-pattern variant within the family.\n"
    "- The number of families and sub-skills must EMERGE from the data under this "
    "criterion: create exactly as many as naturally exist. Do not pad, do not force "
    "a count. One family holding everything is a failure; one sub-skill per caption "
    "is a failure."
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


REPAIR_PROMPT_TMPL = (
    "An existing two-level skill taxonomy (family -> sub-skills):\n{tree}\n\n"
    "The captions below were left out of it. Assign EACH to exactly one EXISTING "
    "sub-skill from the taxonomy above (do not invent new names). Return STRICT "
    'JSON: {{"map": [{{"caption": str, "family": str, "subskill": str}}]}}\n\n'
    "CAPTIONS:\n")


def repair_unassigned(captions: list[str], taxonomy: dict, llm_ask: LlmAsk) -> dict:
    """漏抄补齐:members 里被 LLM 漏掉的 caption,二次指认到既有子技能。

    (实测 verbatim 抄写不可靠——bridge 20 条连续两轮漏抄同一条 → 归"未归类"。
    修复回合只允许指认既有类,乱指(名字不存在)一律拒收,宁缺毋滥。)
    返回 {caption: (族名, 子技能名)},只含合法指认。
    """
    valid = {(f["name"], s["name"])
             for f in taxonomy.get("families", []) for s in f.get("subskills", [])}
    if not captions or not valid:
        return {}
    tree = "\n".join(f"- {f['name']}: " + ", ".join(s["name"] for s in f.get("subskills", []))
                     for f in taxonomy.get("families", []))
    try:
        raw = _parse_json(llm_ask(REPAIR_PROMPT_TMPL.format(tree=tree)
                                  + "\n".join(f"- {c}" for c in captions)))
    except Exception:  # noqa: BLE001  修复失败不致命,保持未归类
        return {}
    out = {}
    ask = {c.strip().lower(): c for c in captions}
    for m in raw.get("map", []):
        cap = str(m.get("caption", "")).strip().lower()
        pair = (str(m.get("family", "")), str(m.get("subskill", "")))
        if cap in ask and pair in valid:
            out[ask[cap]] = pair
    return out


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
                    guideline: str | None = None) -> dict:
    """守规合并:同族子技能两两二值问询,"仅目的地/物体/颜色不同"→ 代码里合并。

    为什么不是"让 LLM 审计整棵树重出 JSON":实测(2026-07-11 bridge)模型把
    move-to-burner 原样奉还,开放式审计不服从——与 M4c 的 endstate 教训同源:
    渐变/开放问询失效,**二值问题答得动**。合并动作在代码里做(并查集+成员并集),
    caption 物理上不可能丢;LLM 只回答 yes/no。异常/答非所问 → 视为 no(不合并)。
    """
    g = (guideline or DEFAULT_GUIDELINE).strip()
    fams_out = []
    for fam in taxonomy.get("families", []):
        subs = fam.get("subskills", [])
        n = len(subs)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(n):
            for j in range(i + 1, n):
                if find(i) == find(j):
                    continue
                q = MERGE_Q_TMPL.format(
                    guideline=g,
                    a=subs[i]["name"], ac=subs[i].get("criterion", ""),
                    am="; ".join(subs[i].get("members", [])[:3]),
                    b=subs[j]["name"], bc=subs[j].get("criterion", ""),
                    bm="; ".join(subs[j].get("members", [])[:3]))
                try:
                    ans = llm_ask(q).strip().lower()
                except Exception:  # noqa: BLE001  问询失败=不合并,宁保守
                    continue
                if "yes" in ans[:8]:
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
