"""人工裁决的落盘与读取(纯文件 IO,零管道/零 UI 依赖)。

UI 的裁决表单与 `curation rejudge` 命令共用本模块——同一份 schema 单一事实源。
三条裁决线,三张表,互不相扰(2026-08-14 起住在**交付根**的 human-decisions/,
理由与新旧位置的回退链见 curation/delivery.py):
  ① 标注分歧  human-decisions/label_decisions.csv 列 episode_id/decision/new_label/note/at
  ② 任务成败  human-decisions/task_verdicts.csv   列 episode_id/verdict/note/at
  ③ 被拒复议  human-decisions/reject_appeals.csv  列 episode_id/appeal/note/at
三张表都是追加式,同一 episode 后写覆盖前写(改判=再追加一行)。
③ 单独一张表而不是并进 ②:两者的对象完全不同(② 是系统弃权的条目,③ 是系统已经
杀掉的条目),同一条 episode 理论上可以先后落在两张表里,合表会按 episode_id 互相
覆盖 —— 那就成了"复议一按,把上一轮的成败裁决抹了"。

2026-08-16 起本模块还是「裁决落库了没有 / 是不是沿用自此前」判据的**单一事实源**
(*_applied_in / decisions_view):rejudge 的幂等跳过与 UI 的已应用/未应用计数都
必须走这几个函数 —— 两边各写一套比对的话,迟早一边说"已应用"一边说"没应用"。
"""
from __future__ import annotations

import datetime
import json
import os
import re


# 人工对分歧队列的三选一裁决,落 human-decisions/label_decisions.csv(追加式,后写覆盖前写)。
# 这是人工产生的新数据,不是对管线产物的改写;真正按裁决重判/搬移交付由
# `curation rejudge` 命令完成(UI 不 import 管道的红线不破)。
DECISIONS_CSV = "label_decisions.csv"
DECISION_CHOICES = ("采纳建议改标", "维持原标注", "弃用该条")

# 人工对"任务成败弃权"条目的三选一裁决,落 human-decisions/task_verdicts.csv。
# 与标注裁决的关键差别:**成败裁决不重判**——系统已经诚实说了"我判不了",
# 再问一次 VLM 只会得到同样的弃权;人说了算,rejudge 只负责按裁决搬交付。
VERDICTS_CSV = "task_verdicts.csv"
VERDICT_CHOICES = ("判成功", "判失败", "搁置")

# 人工对"已被任务成败判定拒掉"条目的二选一复议,落 human-decisions/reject_appeals.csv。
# 存在意义 = 保险丝:判定从"拿不准就转人工"升级成"证据够就杀"之后,杀错的那几条
# 必须有人看得见、捞得回来,否则一次误杀就是客户永久少一条数据。
APPEALS_CSV = "reject_appeals.csv"
APPEAL_CHOICES = ("维持拒绝", "捞回")

# 可复议的边界(用户 2026-08-11 拍板):**只有语义判定的杀可以复议**。
# 时间戳/残段/运动学极限/同步判废这些物理与结构问题是终局 —— 它们是测出来的事实,
# 不因人的意见而消失;放进复议区就等于给"物理证据"开后门。
# 判据放在本模块(而不是 UI 或 rejudge 里)是因为两边都要用同一个:界面能复议、
# 闭环不认账(或反过来),都是事故。检查中文名的**单一事实源**是 export/report.py 的
# CHECK_CN,这里是读端常量副本(UI 不 import 管道的红线照旧)。
TASK_CHECK_CN = "任务成败判定"
_CHECK_NAMES_CN = ("时间戳检查", "运动学极限", "运动质量", "视觉质量",
                   "视频-动作同步", "视频动作同步", TASK_CHECK_CN)
_CHECK_KEYS_EN = ("timestamp_check", "kinematic_limits", "motion_quality",
                  "visual_quality", "video_action_sync", "task_success")
_QUOTED_NAME = re.compile(r"「([^」]*)」")


def is_task_success_reject(reason) -> bool:
    """这条拒因是否**只**归因于任务成败判定(= 可复议的语义判定)。

    两种记法都认:新交付的「未通过「任务成败判定」:…」与老交付的
    「硬门违规: 「任务成败判定」」;英文检查名(极老的交付)兜底。
    一条同时踩了成败判定和某个物理硬门时返回 False —— 物理硬门是终局,
    捞回它等于把一条时间戳残段塞回交付。
    """
    s = str(reason or "")
    named = {n for n in _QUOTED_NAME.findall(s) if n in _CHECK_NAMES_CN}
    if named:
        return named == {TASK_CHECK_CN}
    hit = {k for k in _CHECK_KEYS_EN if k in s}
    return hit == {"task_success"}

# 进程内写缓存 {csv绝对路径: 全量行列表}。FSX 上新写文件有 ~20-45s 可见延迟
# (读回是空的),而本模块是"读全量+整写"——不兜底的话,延迟窗口内连裁两条,
# 第二写读到空文件会把第一条冲掉(2026-08-06 实测推演)。取 max(文件, 缓存)
# 即可自愈:UI 常驻进程连点走缓存;rejudge 是新进程,届时文件早已可见。
# 两张表按绝对路径分键,共用一个缓存字典不会串味。
_WRITE_CACHE: dict = {}


#: 已经就"读到了老位置"提示过的路径(同一进程里只说一次,别刷屏)。
_LEGACY_NOTED: set = set()


def _read_path(delivery_path: str, filename: str) -> str:
    """读裁决 CSV 的实际路径:新位置(交付根 human-decisions/)优先,老位置兜底。

    老交付**不自动迁移**(不动别人的数据),但真读到老位置时留一行日志 —— 否则
    "为什么裁决还在旧地方"这种事只能靠猜。写缓存里已有新位置的内容时也走新位置:
    FSX 新写文件有可见延迟,文件还看不见不代表这条裁决没记(见 _WRITE_CACHE)。
    """
    from ..delivery import decisions_read_path, decisions_write_path
    new = decisions_write_path(delivery_path, filename)
    if os.path.abspath(new) in _WRITE_CACHE:
        return new
    path, legacy = decisions_read_path(delivery_path, filename)
    if legacy and path not in _LEGACY_NOTED:
        _LEGACY_NOTED.add(path)
        print(f"[curation] 裁决记录读自老位置 {path}"
              f"(2026-08-14 之前的布局);新裁决会写到 {new}", flush=True)
    return path


def _write_path(delivery_path: str, filename: str) -> str:
    """写裁决 CSV 的路径:**一律**交付根的 human-decisions/(老交付也写这儿)。"""
    from ..delivery import decisions_write_path
    return decisions_write_path(delivery_path, filename)


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

    from ..export.safe_write import delivery_file
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = _read_rows(path, fields)
    rows.append(row)
    with delivery_file(path, newline="") as f:
        w = _csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    _WRITE_CACHE[os.path.abspath(path)] = list(rows)


def load_label_decisions(delivery_path: str) -> dict:
    """→ {episode_id: {"decision","new_label","note","at"}}(文件不存在 = 空)。"""
    fields = ["episode_id", "decision", "new_label", "note", "at"]
    path = _read_path(delivery_path, DECISIONS_CSV)
    out: dict = {}
    for r in _read_rows(path, fields):
        out[r["episode_id"]] = {"decision": r.get("decision", ""),
                                "new_label": r.get("new_label", ""),
                                "note": r.get("note", ""), "at": r.get("at", "")}
    return out                              # 追加式语义,dict 天然后写覆盖前写


def load_task_verdicts(delivery_path: str) -> dict:
    """→ {episode_id: {"verdict","note","at"}}(文件不存在 = 空)。"""
    fields = ["episode_id", "verdict", "note", "at"]
    path = _read_path(delivery_path, VERDICTS_CSV)
    out: dict = {}
    for r in _read_rows(path, fields):
        out[r["episode_id"]] = {"verdict": r.get("verdict", ""),
                                "note": r.get("note", ""), "at": r.get("at", "")}
    return out


def load_reject_appeals(delivery_path: str) -> dict:
    """→ {episode_id: {"appeal","note","at"}}(文件不存在 = 空)。"""
    fields = ["episode_id", "appeal", "note", "at"]
    path = _read_path(delivery_path, APPEALS_CSV)
    out: dict = {}
    for r in _read_rows(path, fields):
        out[r["episode_id"]] = {"appeal": r.get("appeal", ""),
                                "note": r.get("note", ""), "at": r.get("at", "")}
    return out


def record_reject_appeal(delivery_path: str, episode_id: str, appeal: str,
                         note: str = "") -> str:
    """追加一条被拒复议;返回给 UI 的状态文案。改判 = 再追加一行(后写覆盖前写)。

    防御与另两条裁决线完全同款(整写而非 O_APPEND、进程内写缓存兜 FSX 可见延迟)。
    """
    import datetime as _dt
    if appeal not in APPEAL_CHOICES:
        return f"⚠️ 未记录:复议结论必须是 {'/'.join(APPEAL_CHOICES)}"
    if not str(episode_id).strip():
        return "⚠️ 未记录:没有选中任何 episode"
    path = _write_path(delivery_path, APPEALS_CSV)
    _append_row(path, ["episode_id", "appeal", "note", "at"],
                {"episode_id": episode_id, "appeal": appeal, "note": note,
                 "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    return (f"✅ 已记录:{episode_id} → {appeal}"
            ";执行生效请在命令行跑 curation rejudge"
            "(裁决文件落 TOS 约需 1 分钟可见,裁完稍候再跑)")


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
    path = _write_path(delivery_path, VERDICTS_CSV)
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
    path = _write_path(delivery_path, DECISIONS_CSV)
    _append_row(path, ["episode_id", "decision", "new_label", "note", "at"],
                {"episode_id": episode_id, "decision": decision,
                 "new_label": str(new_label).strip(), "note": note,
                 "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    return (f"✅ 已记录:{episode_id} → {decision}"
            + (f"(新标注:{str(new_label).strip()[:40]})" if decision == "采纳建议改标" else "")
            + ";执行重判请在命令行跑 curation rejudge"
              "(裁决文件落 TOS 约需 1 分钟可见,裁完稍候再跑)")


# ═════════ 裁决落库与新旧的判据(rejudge 幂等跳过 & UI 计数的同一套)═════════
#
# 判据一句话:**以三件套里落库的溯源为准,不看旁路文件**。一条裁决"应用了没有" =
# 条目(或分歧队列)上是否已有与 CSV 这一行完全一致的溯源(裁决时间 + 内容);
# "是不是沿用" = 裁决时间是否早于本次跑批开始(裁决 CSV 在交付根跨跑批共用,新跑
# 一批之后老裁决会被 rejudge 自动再应用 —— 不标出来,用户会以为这批全是机器判的)。

#: 三件套条目上的人工溯源键(单一事实源;pipeline/rejudge.py 从这里 import,
#: 别在别处再写一遍字面量)。标注线两个键、成败线一个、复议线一个,互不覆盖。
PROV_LABEL = "标注裁决"
PROV_RELABEL = "标注修正"
PROV_TASK = "人工裁决"
PROV_APPEAL = "人工复议"

#: 分歧队列在 review.json 里的两个键名(新交付 / 老交付),读端都认。
QUEUE_KEYS = ("标注-画面分歧复核队列", "标注审计复核队列")

#: 改变交付结果的裁决词 vs 只留痕不改数据的裁决词。计数必须分开报(用户点名):
#: 把 38 条混成一个数,读者会以为 38 条结论被人动过,而实际只有其中改结果的那几条。
RESULT_CHANGING = ("采纳建议改标", "弃用该条", "判成功", "判失败", "捞回")
MARK_ONLY = ("维持原标注", "搁置", "维持拒绝")


def _entries(files: dict, eid: str):
    """三件套里该 episode 的全部现存副本。**三边都看**而不是首个命中即收手:
    FSX 改写延迟造出过"无溯源僵尸副本在前、真身在后"的排列(2026-08-06 droid-30),
    只看第一份会把已落库的裁决误判成没应用。"""
    for name in ("passed", "review", "reject"):
        entry = ((files.get(name) or {}).get("episodes") or {}).get(eid)
        if isinstance(entry, dict):
            yield name, entry


def label_decision_applied_in(files: dict, eid: str, dec: dict) -> str:
    """标注裁决这一行落库了没有 → 落在哪个文件("passed"/"review"/"reject",
    没落库返回空串)。files = {"passed":…, "review":…, "reject":…}(已加载的 JSON)。

    三种裁决词三种落库形态,判据都是「裁决时间 + 内容」逐字相等:
    - 采纳建议改标:条目上的「标注修正」溯源(时间 + 新标注都没变才算,改了标注
      文本必须重新重判 —— 这正是 rejudge 不重复付 VLM 钱的那道幂等);
    - 弃用该条:reject 条目上的「标注裁决」溯源;
    - 维持原标注:它不搬条目,只在分歧队列上留 decision/decided_at 标记,以队列
      标记为落库证据(没有更强的了)。
    """
    kind = dec.get("decision")
    at = dec.get("at")
    if kind == "采纳建议改标":
        new_label = str(dec.get("new_label", "")).strip()
        if not new_label:
            return ""                    # 没给新标注的采纳压根应用不了,谈不上落库
        for name, entry in _entries(files, eid):
            prov = entry.get(PROV_RELABEL) or {}
            if (isinstance(prov, dict) and prov.get("裁决时间") == at
                    and prov.get("新标注") == new_label):
                return name
        return ""
    if kind == "弃用该条":
        for name, entry in _entries(files, eid):
            prov = entry.get(PROV_LABEL) or {}
            if (isinstance(prov, dict) and prov.get("裁决时间") == at
                    and prov.get("裁决") == kind):
                return name
        return ""
    if kind == "维持原标注":
        review = files.get("review") or {}
        for key in QUEUE_KEYS:
            for q in review.get(key) or []:
                if (q.get("id") == eid and q.get("decision") == kind
                        and q.get("decided_at") == at):
                    return "review"
        return ""
    return ""                            # 未知裁决词:应用不了,自然也没落库


def task_verdict_applied_in(files: dict, eid: str, v: dict) -> str:
    """成败裁决这一行落库了没有(判据同上:条目上「人工裁决」溯源的时间 + 裁决词)。"""
    for name, entry in _entries(files, eid):
        prov = entry.get(PROV_TASK) or {}
        if (isinstance(prov, dict) and prov.get("裁决时间") == v.get("at")
                and prov.get("裁决") == v.get("verdict")):
            return name
    return ""


def reject_appeal_applied_in(files: dict, eid: str, a: dict) -> str:
    """复议这一行落库了没有(条目上「人工复议」溯源的时间 + 结论)。"""
    for name, entry in _entries(files, eid):
        prov = entry.get(PROV_APPEAL) or {}
        if (isinstance(prov, dict) and prov.get("复议时间") == a.get("at")
                and prov.get("复议结论") == a.get("appeal")):
            return name
    return ""


#: 裁决时间的两种合法写法:CSV 里的钟面时间(2026-08-15 05:29:43,分钟精度也认,
#: 老手写记录有过),与跑批目录名的紧凑时间戳(20260815-052943)。
_CLOCK_TIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")
_COMPACT_TIME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})")


def parse_decision_time(s) -> datetime.datetime | None:
    """裁决时间/跑批时间戳 → datetime;认不出**返回 None,绝不猜**(测试 fixture
    里的 "t1" 这类占位串必须判成"无法判定",不能悄悄当成某个时刻)。"""
    text = str(s or "").strip()
    for rx in (_CLOCK_TIME_RE, _COMPACT_TIME_RE):
        m = rx.match(text)
        if m:
            try:
                return datetime.datetime(*[int(x or 0) for x in m.groups()])
            except ValueError:
                return None
    return None


def run_started_at(run_path: str) -> datetime.datetime | None:
    """一次跑批的开始时间。权威来源优先:事实卡 run.json 的「跑批」字段(= 跑批
    开局分配的目录名),缺了退目录名本身的时间戳;都解析不出 → None,由调用方
    如实降级为「无法判定新旧」。老布局交付(目录名不是时间戳)天然是 None。"""
    from ..delivery import RUN_FACTS_NAME
    name = ""
    try:
        with open(os.path.join(str(run_path or ""), RUN_FACTS_NAME),
                  encoding="utf-8") as f:
            facts = json.load(f)
        if isinstance(facts, dict):
            name = str(facts.get("跑批") or "")
    except (OSError, ValueError):
        name = ""
    t = parse_decision_time(name)
    if t is not None:
        return t
    return parse_decision_time(os.path.basename(str(run_path or "").rstrip("/")))


def decisions_view(files: dict, decisions: dict, verdicts: dict, appeals: dict,
                   run_started: datetime.datetime | None = None) -> list[dict]:
    """三张裁决表 × 三件套落库溯源 → 逐条状态(纯函数,报告小节与 UI 计数共用)。

    每条 = {"line","id","kind","at","applied_in","status","when","changes_result"}:
    - status:applied(已落库)/ unapplied(还没执行 rejudge)/ orphaned(该
      episode 压根不在本次跑批里 —— 硬门早杀 / 只跑了前 N 条,**无处可施**;
      单独一类,混进 unapplied 那个数字就永远消不掉,变成狼来了);
    - when:carryover(裁决时间早于本次跑批开始 = 沿用此前的裁决)/ fresh(本轮
      新裁)/ unknown(两个时间有一个解析不出,如实降级不猜)。
    """
    present: set = set()
    for name in ("passed", "review", "reject"):
        present |= set(((files.get(name) or {}).get("episodes") or {}))
    for key in QUEUE_KEYS:
        present |= {q.get("id") for q in (files.get("review") or {}).get(key) or []
                    if q.get("id")}

    out: list[dict] = []

    def _add(line: str, eid: str, kind: str, at, applied_in: str) -> None:
        status = ("applied" if applied_in
                  else ("unapplied" if eid in present else "orphaned"))
        t = parse_decision_time(at)
        when = ("unknown" if (run_started is None or t is None)
                else ("carryover" if t < run_started else "fresh"))
        out.append({"line": line, "id": eid, "kind": kind, "at": str(at or ""),
                    "applied_in": applied_in, "status": status, "when": when,
                    "changes_result": kind in RESULT_CHANGING})

    for eid, d in sorted((decisions or {}).items()):
        _add("label", eid, str(d.get("decision") or ""), d.get("at"),
             label_decision_applied_in(files, eid, d))
    for eid, v in sorted((verdicts or {}).items()):
        _add("verdict", eid, str(v.get("verdict") or ""), v.get("at"),
             task_verdict_applied_in(files, eid, v))
    for eid, a in sorted((appeals or {}).items()):
        _add("appeal", eid, str(a.get("appeal") or ""), a.get("at"),
             reject_appeal_applied_in(files, eid, a))
    return out


def application_counts(records: list) -> dict:
    """decisions_view 的输出 → 各类计数(改变结果的与仅标记的**分开数**)。"""
    applied = [r for r in records if r["status"] == "applied"]
    carry = [r for r in applied if r["when"] == "carryover"]
    return {"total": len(records),
            "applied": len(applied),
            "unapplied": sum(1 for r in records if r["status"] == "unapplied"),
            "orphaned": sum(1 for r in records if r["status"] == "orphaned"),
            "carryover": len(carry),
            "carryover_changed": sum(1 for r in carry if r["changes_result"]),
            "fresh": sum(1 for r in applied if r["when"] == "fresh"),
            "undated": sum(1 for r in applied if r["when"] == "unknown")}


#: run_decision_records 的进程内缓存 {跑批目录绝对路径: (文件签名, records)}。
#: 任务台的状态条 2 秒轮询一次,passed.json 在 FSX 上有几 MB,不缓存等于每两秒
#: 拖一次挂载;签名 = 六个相关文件的 mtime+size(外加写缓存行数 —— CSV 刚写完
#: 在 FSX 上有可见延迟,文件没变不代表裁决没变)。
_RECORDS_CACHE: dict = {}


def run_decision_records(run_path: str) -> list[dict]:
    """跑批目录 → decisions_view 的逐条状态(读盘版,带 mtime 签名缓存)。

    给拿不到 manifest 的场景用(任务台的下拉旁计数、跑完的任务卡片);报告页
    走 manifest 的 prov_files,不重复读大 JSON。读不到的文件按空处理,不炸。
    """
    run_path = str(run_path or "")
    if not run_path:
        return []
    json_paths = [os.path.join(run_path, n + ".json")
                  for n in ("passed", "review", "reject")]
    csv_paths = [_read_path(run_path, f)
                 for f in (DECISIONS_CSV, VERDICTS_CSV, APPEALS_CSV)]

    def _sig(p):
        try:
            st = os.stat(p)
            return (p, st.st_mtime_ns, st.st_size)
        except OSError:
            return (p, None, None)

    sig = (tuple(_sig(p) for p in json_paths + csv_paths)
           + tuple(len(_WRITE_CACHE.get(os.path.abspath(c), ()))
                   for c in csv_paths))
    key = os.path.abspath(run_path)
    hit = _RECORDS_CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    files = {}
    for name, p in zip(("passed", "review", "reject"), json_paths):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            files[name] = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            files[name] = {}
    records = decisions_view(files,
                             load_label_decisions(run_path),
                             load_task_verdicts(run_path),
                             load_reject_appeals(run_path),
                             run_started=run_started_at(run_path))
    _RECORDS_CACHE[key] = (sig, records)
    return records
