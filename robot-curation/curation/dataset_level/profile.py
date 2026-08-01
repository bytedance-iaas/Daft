"""技能分布画像(文档中的 M7 产出):分布统计 + 欠采样提示。

分组优先级(DESIGN 既定):①episode 自带 instruction(多数非空时) ②聚类标签(兜底)。
产出直接进 M8 报告(不删数据 → 低风险高价值)。
"""
from __future__ import annotations

import numpy as np

#: details/skill_assignment.csv 的列(英文小写下划线,与 kinematic_details.csv 等一致)。
SKILL_ASSIGNMENT_COLUMNS = ["episode_id", "family", "subskill", "caption"]


def _episode_ids(rows: list[dict], idx: list[int]) -> list[str]:
    """下标 → episode_id 列表,按 id 排序(排序=为了两次交付能直接 diff)。"""
    return sorted(str(rows[i].get("episode_id", "")) for i in idx)


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
    """M7 终版两级画像:族→子技能→caption 细层;百分比精确两位小数;原始标注仅陈列。

    每个子技能带 `episodes`(属于它的 episode_id,已排序)——"非抓取有 5 条"必须
    答得出"是哪 5 条",否则画像没法追溯到数据(2026-07-31 补)。族级**不重复存**
    (子技能的并集就是族);只有"族有成员却一个子技能都没有"的边界情况才在族级
    落 `episodes`,保证并集不丢条。
    """
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
                "episodes": _episode_ids(rows, sidx),
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
        if not subs and fidx:
            # 边界:族有成员但无子技能层(理论上 assign 总给子技能名,此处兜底)——
            # 族级补 episodes,否则这些条目在"所有子技能的并集"里凭空消失。
            out["families"][fam]["episodes"] = _episode_ids(rows, fidx)
        if pct < undersampled_pct:
            out["undersampled"].append(fam)
    out["n_families"] = len(out["families"])
    return out


def skill_profile(
    rows: list[dict],
    skill_of: list[str],
    undersampled_pct: float = 5.0,
) -> dict:
    """rows + 每条的技能名 → 画像 {skills: {名: {count,pct,avg_len_s,episodes}}, ...}。

    `episodes` 与两级画像同口径(排好序的 episode_id),两条路径字段一致。
    """
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
            "episodes": _episode_ids(rows, idx),
        }
        if pct < undersampled_pct:
            out["undersampled"].append(skill)
    out["n_skills"] = len(out["skills"])
    return out


def skill_assignment_rows(profile: dict, caption_of: dict | None = None) -> list[dict]:
    """画像 → 逐 episode 归属行(details/skill_assignment.csv 的原料),按 id 排序。

    **从画像本身反推**而不是另攒一份数据:CSV 与 JSON 同源,不可能漂移。
    口径(两条路径自洽):
      - 两级画像:family=族名,subskill=子技能名;族无子技能的边界情况 subskill 留空。
      - 扁平降级画像(VLM 不可用、按原始标注分组):**没有族层 → family 留空串**,
        不拿子技能名冒充族名,免得下游误以为这份交付有两级体系。
      - caption 由 caption_of({episode_id: caption})提供;降级路径没有 caption
        (根本没调 VLM)→ 留空串。
    """
    cap = caption_of or {}
    rows: list[dict] = []
    for fam, f in (profile.get("families") or {}).items():
        for sub, s in (f.get("subskills") or {}).items():
            for e in (s.get("episodes") or []):
                rows.append({"episode_id": e, "family": fam, "subskill": sub,
                             "caption": cap.get(e, "")})
        for e in (f.get("episodes") or []):          # 族无子技能的兜底成员
            rows.append({"episode_id": e, "family": fam, "subskill": "",
                         "caption": cap.get(e, "")})
    if not profile.get("families"):
        for skill, s in (profile.get("skills") or {}).items():
            for e in (s.get("episodes") or []):
                rows.append({"episode_id": e, "family": "", "subskill": skill,
                             "caption": cap.get(e, "")})
    rows.sort(key=lambda r: r["episode_id"])
    return rows


def write_skill_assignment_csv(details_dir: str, profile: dict,
                               caption_of: dict | None = None) -> str | None:
    """逐 episode 归属 → `<details_dir>/skill_assignment.csv`,返回路径(没归属则 None)。

    与画像同源(见 skill_assignment_rows),所以 CSV 与 report.json 不会漂移。
    没画像时**不生成空文件**:空表会被误读成"跑了但 0 条数据",而真相是没跑画像。
    显式 utf-8:caption/原始标注可能带非 ASCII,而容器 locale 常是 POSIX。
    """
    import csv as _csv
    import os as _os

    data = skill_assignment_rows(profile, caption_of)
    if not data:
        return None
    path = _os.path.join(details_dir, "skill_assignment.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=SKILL_ASSIGNMENT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(data)
    return path
