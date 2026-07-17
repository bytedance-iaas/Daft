"""裁决:硬门短路 + 软分加权(DESIGN.md §4 混合机制)。纯函数,权值全部来自 config。"""
from __future__ import annotations


def episode_verdict(checks: dict[str, dict], cfg: dict) -> dict:
    """checks: {检查名: {"passed":bool|None, "score":float|None, ...}} → 判决。

    - 任一启用的硬门 passed=False → drop(短路,不可补偿);
    - passed=None(不可判)记录,不参与判决(诚实缺省);
    - 软分加权均值 < soft_threshold → drop;
    - 其余 keep。
    """
    hard_fails, undecidable = [], []
    wsum, wtotal = 0.0, 0.0

    for name, c in cfg["checks"].items():
        if not c.get("enable", True) or name not in checks:
            continue
        r = checks[name]
        if c.get("gate", "soft") == "hard":
            if r.get("passed") is False:
                hard_fails.append(name)
            elif r.get("passed") is None:
                undecidable.append(name)
        else:
            score = r.get("score")
            if score is None:
                undecidable.append(name)
            else:
                w = float(c.get("weight", 1.0))
                wsum += w * float(score)
                wtotal += w

    soft_score = (wsum / wtotal) if wtotal > 0 else None
    out = {"soft_score": soft_score, "hard_fails": hard_fails, "undecidable": undecidable}

    if hard_fails:
        out["verdict"] = "drop"
        out["reason"] = f"硬门违规: {','.join(hard_fails)}"
    elif soft_score is not None and soft_score < cfg["verdict"]["soft_threshold"]:
        out["verdict"] = "drop"
        out["reason"] = f"软分 {soft_score:.3f} < {cfg['verdict']['soft_threshold']}"
    else:
        out["verdict"] = "keep"
        out["reason"] = ""
    return out
