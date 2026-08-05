"""标注裁决的落盘与读取(纯文件 IO,零管道/零 UI 依赖)。

UI 的裁决表单与 `curation rejudge` 命令共用本模块——同一份 schema 单一事实源。
schema: details/label_decisions.csv,列 episode_id/decision/new_label/note/at;
追加式,同一 episode 后写覆盖前写(改判=再追加一行)。
"""
from __future__ import annotations

import os


# 人工对分歧队列的三选一裁决,落 details/label_decisions.csv(追加式,后写覆盖前写)。
# 这是人工产生的新数据,不是对管线产物的改写;真正按裁决重判/搬移交付由
# `curation rejudge` 命令完成(UI 不 import 管道的红线不破)。
DECISIONS_CSV = "label_decisions.csv"
DECISION_CHOICES = ("采纳建议改标", "维持原标注", "弃用该条")


def load_label_decisions(delivery_path: str) -> dict:
    """→ {episode_id: {"decision","new_label","note","at"}}(文件不存在 = 空)。"""
    import csv as _csv
    path = os.path.join(delivery_path, "details", DECISIONS_CSV)
    out: dict = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            out[r["episode_id"]] = {"decision": r.get("decision", ""),
                                    "new_label": r.get("new_label", ""),
                                    "note": r.get("note", ""), "at": r.get("at", "")}
    return out                              # 追加式文件,dict 天然后写覆盖前写


def record_label_decision(delivery_path: str, episode_id: str, decision: str,
                          new_label: str = "", note: str = "") -> str:
    """追加一条裁决;返回给 UI 的状态文案。改判(重复裁决同一条)= 再追加一行。"""
    import csv as _csv
    import datetime as _dt
    if decision not in DECISION_CHOICES:
        return f"⚠️ 未记录:裁决必须是 {'/'.join(DECISION_CHOICES)}"
    if decision == "采纳建议改标" and not str(new_label).strip():
        return "⚠️ 未记录:采纳改标必须给出修正后的标注文本"
    det = os.path.join(delivery_path, "details")
    os.makedirs(det, exist_ok=True)
    path = os.path.join(det, DECISIONS_CSV)
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=["episode_id", "decision", "new_label",
                                           "note", "at"])
        if new_file:
            w.writeheader()
        w.writerow({"episode_id": episode_id, "decision": decision,
                    "new_label": str(new_label).strip(), "note": note,
                    "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    return (f"✅ 已记录:{episode_id} → {decision}"
            + (f"(新标注:{str(new_label).strip()[:40]})" if decision == "采纳建议改标" else "")
            + ";执行重判请在命令行跑 curation rejudge")
