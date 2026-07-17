"""技能分布画像(文档中的 M7 产出):分布统计 + 欠采样提示。

分组优先级(DESIGN 既定):①episode 自带 instruction(多数非空时) ②聚类标签(兜底)。
产出直接进 M8 报告(不删数据 → 低风险高价值)。
"""
from __future__ import annotations

import numpy as np


def instruction_grouping_available(rows: list[dict], min_ratio: float = 0.6) -> bool:
    """多数 episode 带非空 instruction 才用标签分组。"""
    if not rows:
        return False
    n_has = sum(1 for r in rows if str(r.get("instruction", "")).strip())
    return n_has / len(rows) >= min_ratio


def skill_profile_two_level(
    rows: list[dict],
    families: list[str],
    subskills: list[str],
    captions: list[str],
    undersampled_pct: float = 5.0,
) -> dict:
    """M7 终版两级画像:族→子技能→caption 细层;百分比精确两位小数;原始标注仅陈列。"""
    assert len(rows) == len(families) == len(subskills) == len(captions)
    n = len(rows)
    out: dict = {"n_episodes": n, "families": {}, "undersampled": []}
    for fam in sorted(set(families)):
        fidx = [i for i, f in enumerate(families) if f == fam]
        pct = 100.0 * len(fidx) / n
        subs: dict = {}
        for sub in sorted(set(subskills[i] for i in fidx)):
            sidx = [i for i in fidx if subskills[i] == sub]
            subs[sub] = {
                "count": len(sidx),
                "pct": round(100.0 * len(sidx) / n, 2),
                "captions_top": [c for c, _ in
                                 __import__("collections").Counter(
                                     captions[i] for i in sidx).most_common(3)],
                "raw_labels_top": [s for s, _ in
                                   __import__("collections").Counter(
                                       rows[i].get("instruction", "").strip()
                                       for i in sidx
                                       if rows[i].get("instruction", "").strip()
                                   ).most_common(3)],
            }
        out["families"][fam] = {"count": len(fidx), "pct": round(pct, 2), "subskills": subs}
        if pct < undersampled_pct:
            out["undersampled"].append(fam)
    out["n_families"] = len(out["families"])
    return out


def skill_profile(
    rows: list[dict],
    skill_of: list[str],
    undersampled_pct: float = 5.0,
) -> dict:
    """rows + 每条的技能名 → 画像 {skills: {名: {count,pct,avg_len_s}}, undersampled, n_skills}。"""
    assert len(rows) == len(skill_of), "rows 与 skill 标签数量不一致"
    n = len(rows)
    out: dict = {"n_episodes": n, "skills": {}, "undersampled": []}
    for skill in sorted(set(skill_of)):
        idx = [i for i, s in enumerate(skill_of) if s == skill]
        # 帧数优先取轻量元数据的 length(懒扫描管线不驻留 action);急切行退回 action 长度
        lens = [(rows[i].get("length") or len(rows[i]["action"])) / float(rows[i]["fps"])
                for i in idx]
        pct = 100.0 * len(idx) / n
        out["skills"][skill] = {
            "count": len(idx),
            "pct": round(pct, 1),
            "avg_len_s": round(float(np.mean(lens)), 2),
        }
        if pct < undersampled_pct:
            out["undersampled"].append(skill)
    out["n_skills"] = len(out["skills"])
    return out
