"""M7 第三步:原始标注审计——标注在整条管线里唯一的入口,身份=被审对象。

机制(8 次迭代定稿):原始标注经 LLM 映射进与 caption 同一套族体系 → 动词等价类修正后,
只有"高对比度跨族"(如 开合↔操纵搬放)与"乱码"进高置信档;同义动词跨族进人工复核档。
产出=按嫌疑排序的复核队列,**不是自动判决**(caption 物体级误差决定的产品边界)。
"""
from __future__ import annotations

import json
import re
from typing import Callable

LlmAsk = Callable[[str], str]

LABEL_MAP_PROMPT = (
    "Skill families: {families}. For each annotation below, output which family it belongs "
    "to, or 'garbage' if the text is meaningless. Return STRICT JSON: "
    '{{"map": [{{"label": str, "family": str}}]}}\n\nANNOTATIONS:\n')

# 动词等价类:操纵搬放一族(place/move/grab/pick/sweep 互不立案)
_EQUIV = ("place", "move", "grab", "pick", "sweep", "manipulat", "rearrang", "put")


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
            high.append({**entry, "reason": "garbage"})
        elif lf and cf not in ("未归类", None) and lf != cf:
            if _canon(lf) != _canon(cf):
                high.append({**entry, "reason": f"跨族: 标注={lf} vs 画面={cf}"})
            else:
                mid.append({**entry, "reason": f"同义动词跨族: {lf} vs {cf}"})
    return {"high": high, "mid_for_review": mid}
