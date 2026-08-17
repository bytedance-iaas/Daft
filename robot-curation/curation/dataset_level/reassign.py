"""对着**现有技能体系**做纯文本再分配(2026-08-16 标注优先方针的两条闭环共用)。

背景:droid-200-new 分歧队列 29 条人工复核,26 条是我方 caption 错、客户原始标注对
(90%)——归类输入从"全员 caption"改为"标注优先"(instruction.strip() or caption)。
老交付是按旧策略跑的,人工裁决后也要能把单条重新归类,于是需要这一层:

- **绝不重跑 VLM caption、绝不重新归纳体系**:体系是客户已经看过的坐标系,重算只做
  文本对既有体系的再分配,成本近似为零;
- 归不进去的文本才(可选地)问一次 LLM 补漏(复用 taxonomy.repair_unassigned);
  没配 LLM 就诚实留「未归类」,绝不猜。

体系的"成员文本"从 details/skill_assignment.csv 反推:新交付有 grouping_text 列;
老交付(2026-08-16 之前)的归类输入就是 caption,caption 列即成员文本。
"""
from __future__ import annotations

from typing import Callable

#: 与 taxonomy.assign 的未映射产物同字面(那边是硬编码字符串,这里给个名字)。
UNASSIGNED = "未归类"
NO_SUBSKILL = "-"

#: 来源词表与 run.py 的 task_desc_source 同源(报告/CSV 两处必须说同一种话)。
SRC_LABEL = "原始标注"
SRC_CAPTION = "自产caption"
SRC_NONE = "无"


def grouping_text_and_source(instruction, caption) -> tuple[str, str]:
    """标注优先方针的唯一口径:归类文本 = instruction.strip() or caption。

    集中成一个函数是为了 run / rejudge / reprofile 三处不各写一遍三元表达式——
    口径一旦分叉,画像与 CSV 会安静地说两种话。
    """
    ins = str(instruction or "").strip()
    cap = str(caption or "").strip()
    if ins:
        return ins, SRC_LABEL
    if cap:
        return cap, SRC_CAPTION
    return "", SRC_NONE


def member_map_of(csv_rows: list[dict]) -> dict:
    """skill_assignment.csv 行 → {归类文本(小写): (family, subskill)}。

    未归类行不进映射(它不是体系的一部分);同文本冲突时首见为准(CSV 按
    episode_id 排序,结果确定)。
    """
    out: dict = {}
    for r in csv_rows:
        text = str(r.get("grouping_text") or r.get("caption") or "").strip().lower()
        fam = str(r.get("family") or "").strip()
        sub = str(r.get("subskill") or "").strip()
        if text and fam and fam != UNASSIGNED:
            out.setdefault(text, (fam, sub))
    return out


def taxonomy_from_profile(profile: dict) -> dict:
    """画像(passed.json 的 skills 段)→ repair_unassigned 能吃的体系骨架。

    补漏只需要族/子技能**名字**(它只允许指认既有类);members 留空即可。
    「未归类」不是真族,不进骨架——把它列成可指认目标等于教 LLM 偷懒。
    """
    fams = []
    for fname, f in (profile.get("families") or {}).items():
        if not fname or fname == UNASSIGNED:
            continue
        subs = [{"name": s, "members": []} for s in (f.get("subskills") or {}) if s]
        fams.append({"name": fname, "subskills": subs})
    return {"families": fams}


def reassign_texts(texts_by_eid: dict, member_map: dict, taxonomy: dict,
                   llm_ask: Callable | None = None) -> dict:
    """{episode_id: 归类文本} → {episode_id: (family, subskill)}。

    精确匹配(与 taxonomy.assign 同口径:strip+lower)优先;没命中的**整批**问一次
    LLM 补漏(llm_ask=None 或补漏失败 → 诚实留未归类,绝不猜)。空文本直接未归类。
    """
    out: dict = {}
    missed: dict = {}                    # 小写键 → (原文, [episode_id...])
    for eid, t in texts_by_eid.items():
        key = str(t or "").strip().lower()
        if not key:
            out[eid] = (UNASSIGNED, NO_SUBSKILL)
        elif key in member_map:
            out[eid] = member_map[key]
        else:
            orig, eids = missed.setdefault(key, (str(t).strip(), []))
            eids.append(eid)
    if missed and llm_ask is not None:
        from .taxonomy import repair_unassigned
        fix = repair_unassigned(sorted(o for o, _ in missed.values()),
                                taxonomy, llm_ask)
        for key, (orig, eids) in missed.items():
            if orig in fix:
                for eid in eids:
                    out[eid] = fix[orig]
                missed[key] = (orig, [])
    for orig, eids in missed.values():
        for eid in eids:
            out.setdefault(eid, (UNASSIGNED, NO_SUBSKILL))
    return out


def rebuild_profile(assignment: dict, old_profile: dict, caption_of: dict,
                    instruction_of: dict) -> dict:
    """{episode_id: (family, subskill)} → 重建两级画像(计数/百分比/名单全重算)。

    体系的"解释"不动:guideline 与各族/子技能的 criterion 从旧画像**按名回移植**
    ——重算只改归属,判据是当初 LLM 归纳时的产物,重算不产生新判据。
    captions_top/raw_labels_top 照旧陈列(caption 不再是归类输入,但仍是画像的
    "自产描述"一栏)。
    """
    from .profile import skill_profile_two_level
    eids = sorted(assignment)
    rows = [{"episode_id": e, "instruction": instruction_of.get(e, "")} for e in eids]
    fams = [assignment[e][0] for e in eids]
    subs = [assignment[e][1] for e in eids]
    caps = [str(caption_of.get(e, "") or "") for e in eids]
    prof = skill_profile_two_level(rows, fams, subs, caps)
    if old_profile.get("guideline"):
        prof["guideline"] = old_profile["guideline"]
    old_f = old_profile.get("families") or {}
    for fname, f in prof["families"].items():
        of = old_f.get(fname) or {}
        if of.get("criterion"):
            f["criterion"] = of["criterion"]
        for sname, s in f["subskills"].items():
            osub = (of.get("subskills") or {}).get(sname) or {}
            if osub.get("criterion"):
                s["criterion"] = osub["criterion"]
    return prof
