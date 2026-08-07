"""人工裁决的落盘与读取(纯文件 IO,零管道/零 UI 依赖)。

UI 的裁决表单与 `curation rejudge` 命令共用本模块——同一份 schema 单一事实源。
两条裁决线,两张表,互不相扰:
  ① 标注分歧  details/label_decisions.csv 列 episode_id/decision/new_label/note/at
  ② 任务成败  details/task_verdicts.csv   列 episode_id/verdict/note/at
两张表都是追加式,同一 episode 后写覆盖前写(改判=再追加一行)。
"""
from __future__ import annotations

import os


# 人工对分歧队列的三选一裁决,落 details/label_decisions.csv(追加式,后写覆盖前写)。
# 这是人工产生的新数据,不是对管线产物的改写;真正按裁决重判/搬移交付由
# `curation rejudge` 命令完成(UI 不 import 管道的红线不破)。
DECISIONS_CSV = "label_decisions.csv"
DECISION_CHOICES = ("采纳建议改标", "维持原标注", "弃用该条")

# 人工对"任务成败弃权"条目的三选一裁决,落 details/task_verdicts.csv。
# 与标注裁决的关键差别:**成败裁决不重判**——系统已经诚实说了"我判不了",
# 再问一次 VLM 只会得到同样的弃权;人说了算,rejudge 只负责按裁决搬交付。
VERDICTS_CSV = "task_verdicts.csv"
VERDICT_CHOICES = ("判成功", "判失败", "搁置")

# 进程内写缓存 {csv绝对路径: 全量行列表}。FSX 上新写文件有 ~20-45s 可见延迟
# (读回是空的),而本模块是"读全量+整写"——不兜底的话,延迟窗口内连裁两条,
# 第二写读到空文件会把第一条冲掉(2026-08-06 实测推演)。取 max(文件, 缓存)
# 即可自愈:UI 常驻进程连点走缓存;rejudge 是新进程,届时文件早已可见。
# 两张表按绝对路径分键,共用一个缓存字典不会串味。
_WRITE_CACHE: dict = {}


def _read_rows(path: str, fields: list) -> list:
    """读全量行(缺列补空串)。文件读回比本进程写缓存少 = FSX 可见延迟,以缓存为准。"""
    import csv as _csv
    rows: list = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            rows = [{k: (r.get(k) or "") for k in fields} for r in _csv.DictReader(f)]
    cached = _WRITE_CACHE.get(os.path.abspath(path), [])
    if len(cached) > len(rows):
        rows = list(cached)
    return rows


def _append_row(path: str, fields: list, row: dict) -> None:
    """追加一行 = 读全量 + 整文件重写(见 record_label_decision 里的 FSX 血泪注释)。"""
    import csv as _csv
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = _read_rows(path, fields)
    rows.append(row)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    _WRITE_CACHE[os.path.abspath(path)] = list(rows)


def load_label_decisions(delivery_path: str) -> dict:
    """→ {episode_id: {"decision","new_label","note","at"}}(文件不存在 = 空)。"""
    fields = ["episode_id", "decision", "new_label", "note", "at"]
    path = os.path.join(delivery_path, "details", DECISIONS_CSV)
    out: dict = {}
    for r in _read_rows(path, fields):
        out[r["episode_id"]] = {"decision": r.get("decision", ""),
                                "new_label": r.get("new_label", ""),
                                "note": r.get("note", ""), "at": r.get("at", "")}
    return out                              # 追加式语义,dict 天然后写覆盖前写


def load_task_verdicts(delivery_path: str) -> dict:
    """→ {episode_id: {"verdict","note","at"}}(文件不存在 = 空)。"""
    fields = ["episode_id", "verdict", "note", "at"]
    path = os.path.join(delivery_path, "details", VERDICTS_CSV)
    out: dict = {}
    for r in _read_rows(path, fields):
        out[r["episode_id"]] = {"verdict": r.get("verdict", ""),
                                "note": r.get("note", ""), "at": r.get("at", "")}
    return out


def record_task_verdict(delivery_path: str, episode_id: str, verdict: str,
                        note: str = "") -> str:
    """追加一条成败裁决;返回给 UI 的状态文案。改判 = 再追加一行(后写覆盖前写)。

    防御与标注裁决完全同款(整写而非 O_APPEND、进程内写缓存兜 FSX 可见延迟),
    原因见 record_label_decision 的注释——同一块挂载,同一批坑。
    """
    import datetime as _dt
    if verdict not in VERDICT_CHOICES:
        return f"⚠️ 未记录:裁决必须是 {'/'.join(VERDICT_CHOICES)}"
    if not str(episode_id).strip():
        return "⚠️ 未记录:没有选中任何 episode"
    path = os.path.join(delivery_path, "details", VERDICTS_CSV)
    _append_row(path, ["episode_id", "verdict", "note", "at"],
                {"episode_id": episode_id, "verdict": verdict, "note": note,
                 "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    return (f"✅ 已记录:{episode_id} → {verdict}"
            ";执行生效请在命令行跑 curation rejudge"
            "(裁决文件落 TOS 约需 1 分钟可见,裁完稍候再跑)")


def record_label_decision(delivery_path: str, episode_id: str, decision: str,
                          new_label: str = "", note: str = "") -> str:
    """追加一条裁决;返回给 UI 的状态文案。改判(重复裁决同一条)= 再追加一行。

    物理写法是"读全量+整文件重写"而非 O_APPEND:交付目录在 TOS 的 FSX 挂载上,
    追加模式 open(..., "a") 直接报 EINVAL(2026-08-06 生产实锤);顺序整写没问题
    (三件套 JSON 一直这么写)。文件只有队列量级的几十行,整写零成本。
    """
    import datetime as _dt
    if decision not in DECISION_CHOICES:
        return f"⚠️ 未记录:裁决必须是 {'/'.join(DECISION_CHOICES)}"
    if decision == "采纳建议改标" and not str(new_label).strip():
        return "⚠️ 未记录:采纳改标必须给出修正后的标注文本"
    path = os.path.join(delivery_path, "details", DECISIONS_CSV)
    _append_row(path, ["episode_id", "decision", "new_label", "note", "at"],
                {"episode_id": episode_id, "decision": decision,
                 "new_label": str(new_label).strip(), "note": note,
                 "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    return (f"✅ 已记录:{episode_id} → {decision}"
            + (f"(新标注:{str(new_label).strip()[:40]})" if decision == "采纳建议改标" else "")
            + ";执行重判请在命令行跑 curation rejudge"
              "(裁决文件落 TOS 约需 1 分钟可见,裁完稍候再跑)")
