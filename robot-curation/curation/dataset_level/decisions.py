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

# 进程内写缓存 {csv绝对路径: 全量行列表}。FSX 上新写文件有 ~20-45s 可见延迟
# (读回是空的),而本模块是"读全量+整写"——不兜底的话,延迟窗口内连裁两条,
# 第二写读到空文件会把第一条冲掉(2026-08-06 实测推演)。取 max(文件, 缓存)
# 即可自愈:UI 常驻进程连点走缓存;rejudge 是新进程,届时文件早已可见。
_WRITE_CACHE: dict = {}


def load_label_decisions(delivery_path: str) -> dict:
    """→ {episode_id: {"decision","new_label","note","at"}}(文件不存在 = 空)。"""
    import csv as _csv
    path = os.path.join(delivery_path, "details", DECISIONS_CSV)
    rows: list = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
    cached = _WRITE_CACHE.get(os.path.abspath(path), [])
    if len(cached) > len(rows):        # FSX 可见延迟窗口内,以本进程写缓存为准
        rows = cached
    out: dict = {}
    for r in rows:
        out[r["episode_id"]] = {"decision": r.get("decision", ""),
                                "new_label": r.get("new_label", ""),
                                "note": r.get("note", ""), "at": r.get("at", "")}
    return out                              # 追加式语义,dict 天然后写覆盖前写


def record_label_decision(delivery_path: str, episode_id: str, decision: str,
                          new_label: str = "", note: str = "") -> str:
    """追加一条裁决;返回给 UI 的状态文案。改判(重复裁决同一条)= 再追加一行。

    物理写法是"读全量+整文件重写"而非 O_APPEND:交付目录在 TOS 的 FSX 挂载上,
    追加模式 open(..., "a") 直接报 EINVAL(2026-08-06 生产实锤);顺序整写没问题
    (三件套 JSON 一直这么写)。文件只有队列量级的几十行,整写零成本。
    """
    import csv as _csv
    import datetime as _dt
    if decision not in DECISION_CHOICES:
        return f"⚠️ 未记录:裁决必须是 {'/'.join(DECISION_CHOICES)}"
    if decision == "采纳建议改标" and not str(new_label).strip():
        return "⚠️ 未记录:采纳改标必须给出修正后的标注文本"
    det = os.path.join(delivery_path, "details")
    os.makedirs(det, exist_ok=True)
    path = os.path.join(det, DECISIONS_CSV)
    fields = ["episode_id", "decision", "new_label", "note", "at"]
    key = os.path.abspath(path)
    rows: list[dict] = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            rows = [{k: r.get(k, "") for k in fields} for r in _csv.DictReader(f)]
    cached = _WRITE_CACHE.get(key, [])
    if len(cached) > len(rows):        # 文件读回比缓存少 = FSX 可见延迟,以缓存为准
        rows = list(cached)
    rows.append({"episode_id": episode_id, "decision": decision,
                 "new_label": str(new_label).strip(), "note": note,
                 "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    _WRITE_CACHE[key] = list(rows)
    return (f"✅ 已记录:{episode_id} → {decision}"
            + (f"(新标注:{str(new_label).strip()[:40]})" if decision == "采纳建议改标" else "")
            + ";执行重判请在命令行跑 curation rejudge")
