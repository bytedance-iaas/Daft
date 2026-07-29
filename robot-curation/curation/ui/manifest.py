"""交付目录 → UI 数据模型(纯函数层,2026-07-27 U1)。

架构红线:UI 只读交付目录,不 import 管道代码——本模块是"运行清单"契约的
读端,管道换底座 UI 不动。所有函数无副作用、不碰网络,Gradio 层只做渲染。

读的文件(U0 盘点定型的交付 schema):
  passed.json   数据集元信息 + dataset 统计 + skills + label_audit +
                config_effective + 通过条目(checks 含双重编码 detail)
  reject.json   被拒条目(+原因)
  review.json   待人工裁决条目 + 标注审计复核队列
  details/evidence/<ep>/*.jpg   task_success probe 证据帧
  details/plots/<ep>_sync.png   同步曲线证据图
"""
from __future__ import annotations

import glob
import json
import os


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_detail(detail) -> dict:
    """检查 detail:交付里是双重编码 JSON 字符串,UI 层统一解开。"""
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, str) and detail.strip():
        try:
            d = json.loads(detail)
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {"raw": detail}          # 解不开也不丢:原文进 raw
    return {}


def _norm_checks(checks: dict) -> dict:
    out = {}
    for name, c in (checks or {}).items():
        out[name] = {"state": c.get("结果", "?"), "score": c.get("score"),
                     "detail": parse_detail(c.get("detail"))}
    return out


def load_delivery(path: str) -> dict:
    """一个交付目录 → 统一 manifest。缺文件按空处理(老交付也能打开)。"""
    p = _load_json(os.path.join(path, "passed.json"))
    r = _load_json(os.path.join(path, "reject.json"))
    v = _load_json(os.path.join(path, "review.json"))

    episodes: dict = {}
    for eid, pe in (p.get("episodes") or {}).items():
        episodes[eid] = {"verdict": pe.get("判决", "通过"),
                         "soft_score": pe.get("综合软分"),
                         "reject_reason": None,
                         "checks": _norm_checks(pe.get("checks"))}
    for eid, re_ in (r.get("episodes") or {}).items():
        episodes[eid] = {"verdict": re_.get("判决", "拒绝"),
                         "soft_score": re_.get("综合软分"),
                         "reject_reason": re_.get("原因"),
                         "checks": _norm_checks(re_.get("checks"))}
    for eid, ve in (v.get("episodes") or {}).items():
        ep = episodes.setdefault(eid, {"verdict": ve.get("当前判决", "?"),
                                       "soft_score": None, "reject_reason": None,
                                       "checks": {}})
        ep["pending"] = ve.get("待裁决项") or []
        ep["abstain_reasons"] = ve.get("弃权原因") or {}

    det = os.path.join(path, "details")
    for eid, ep in episodes.items():
        ep.setdefault("pending", [])
        ep.setdefault("abstain_reasons", {})
        ep["evidence"] = sorted(glob.glob(os.path.join(det, "evidence", eid, "*.jpg")))
        plot = os.path.join(det, "plots", f"{eid}_sync.png")
        ep["plot"] = plot if os.path.exists(plot) else None

    return {"path": path,
            "name": p.get("数据集") or os.path.basename(path.rstrip("/")),
            "robot": p.get("机器人"),
            "generated_at": p.get("生成时间"), "code_version": p.get("代码版本"),
            "dataset": p.get("dataset") or {},
            "config_effective": p.get("config_effective"),
            "skills": p.get("skills") or {},
            "label_audit": p.get("label_audit"),
            "audit_queue": v.get("标注审计复核队列") or [],
            "episodes": episodes}


# ───────── 表格整形(Gradio Dataframe 直接吃)─────────

EPISODE_HEADERS = ["episode", "判决", "软分", "待裁决", "拒绝原因", "证据帧", "同步图"]


def episode_rows(m: dict) -> list[list]:
    rows = []
    for eid, ep in sorted(m["episodes"].items()):
        rows.append([eid, ep["verdict"],
                     round(ep["soft_score"], 3) if ep.get("soft_score") is not None else "",
                     "、".join(ep["pending"]) if ep.get("pending") else "",
                     ep.get("reject_reason") or "",
                     len(ep.get("evidence") or []),
                     "有" if ep.get("plot") else ""])
    return rows


FUNNEL_HEADERS = ["阶段", "数量"]


def funnel_rows(m: dict) -> list[list]:
    d = m["dataset"]
    fs = d.get("funnel_stats") or {}
    rows = [["输入 episode", d.get("input_episodes", fs.get("input", ""))],
            ["硬门中途拦截", d.get("hard_gate_filtered", "")],
            ["判决 keep", d.get("verdict_keep", "")],
            ["判决 drop", d.get("verdict_drop", "")],
            ["精确去重删除", d.get("dedup_removed", "")],
            ["交付", d.get("delivered", "")]]
    return [row for row in rows if row[1] != ""]


CHECK_HEADERS = ["检查", "结果", "分数", "要点"]


def check_rows(m: dict, eid: str) -> list[list]:
    ep = m["episodes"].get(eid) or {}
    rows = []
    for name, c in (ep.get("checks") or {}).items():
        d = c["detail"]
        gist = d.get("reason") or d.get("verdict") or ""
        if "voc" in d:
            gist = f"voc={d['voc']} 末态={d.get('completion_final')} {gist}"
        rows.append([name, c["state"],
                     c["score"] if c["score"] is not None else "", str(gist)[:120]])
    return rows


SKILL_HEADERS = ["技能族", "子技能", "条数", "占比%", "判据"]


def skill_rows(m: dict) -> list[list]:
    rows = []
    for fam, f in (m["skills"].get("families") or {}).items():
        subs = f.get("subskills") or {}
        if not subs:
            rows.append([fam, "", f.get("count", ""), f.get("pct", ""),
                        str(f.get("criterion", ""))[:100]])
        for sub, s in subs.items():
            rows.append([fam, sub, s.get("count", ""), s.get("pct", ""),
                        str(s.get("criterion", ""))[:100]])
    return rows


AUDIT_HEADERS = ["episode", "原始标注", "自产 caption", "存疑原因"]


def audit_rows(m: dict) -> list[list]:
    return [[a.get("id", ""), a.get("label", ""), a.get("caption", ""),
             a.get("reason", "")] for a in m["audit_queue"]]


def overview_markdown(m: dict) -> str:
    d = m["dataset"]
    ss = d.get("summary_stats") or {}
    lines = [f"# {m['name']}",
             f"机器人 **{m['robot']}** · 生成于 {m['generated_at']} · 代码版本 {m['code_version']}",
             "",
             f"- 交付 **{d.get('delivered', '?')}** / 输入 {d.get('input_episodes', '?')} 条"
             f"(通过率 {ss.get('pass_rate_pct', '?')}%,平均软分 {ss.get('avg_soft_score', '?')})"]
    hb = d.get("hard_fail_breakdown") or {}
    if hb:
        lines.append("- 硬门拒绝:" + ",".join(f"{k} {v} 条" for k, v in hb.items()))
    n_pending = sum(1 for e in m["episodes"].values() if e.get("pending"))
    if n_pending:
        lines.append(f"- 待人工裁决 **{n_pending}** 条(见 Episode 页「待裁决」列)")
    if m["audit_queue"]:
        lines.append(f"- 标注审计复核队列 {len(m['audit_queue'])} 条(见 技能画像 页)")
    return "\n".join(lines)


def discover_deliveries(root: str) -> list[str]:
    """root 本身是交付目录 → [root];否则扫一层子目录找含 passed.json 的。"""
    if os.path.exists(os.path.join(root, "passed.json")):
        return [root]
    out = []
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "passed.json")):
            out.append(d)
    return out


# ───────── 明细表(D1,2026-07-28):details/ 下 CSV 的只读渲染 ─────────

DETAIL_LABELS = {                      # 语义化标签(纪律:界面不出现实现名)
    "motion_details.csv": "运动质量明细(逐子项)",
    "visual_details.csv": "视觉质量明细(逐相机)",
    "kinematic_details.csv": "运动学违规明细",
    "stuck_details.csv": "卡死事件明细",
    "vlm_latency.csv": "VLM 调用延时明细(逐请求)",
}


def list_detail_tables(m: dict) -> list[str]:
    """交付里实际存在的明细 CSV(按 DETAIL_LABELS 顺序;缺的不列)。"""
    det = os.path.join(m["path"], "details")
    return [f for f in DETAIL_LABELS if os.path.exists(os.path.join(det, f))]


def load_detail_table(m: dict, name: str, cap: int = 2000):
    """CSV → (表头, 行, 总行数)。行数封顶 cap 防大数据集拖垮页面(总数照报)。"""
    import csv as _csv
    path = os.path.join(m["path"], "details", name)
    if name not in DETAIL_LABELS or not os.path.exists(path):
        return [], [], 0
    with open(path, newline="", encoding="utf-8") as f:
        reader = _csv.reader(f)
        headers = next(reader, [])
        rows, total = [], 0
        for row in reader:
            total += 1
            if total <= cap:
                rows.append(row)
    return headers, rows, total


# ───────── Stuck 时间线(D2,2026-07-28):三态彩条 HTML 渲染 ─────────

TL_COLORS = {"stuck": "#c0392b", "idle": "#f1c40f", "normal": "#1abc9c"}
TL_LABELS = {"stuck": "stuck(指令在推而不动)", "idle": "idle(无指令静止)",
             "normal": "正常(在干活)"}


def load_timeline(m: dict) -> dict:
    """details/episodes_timeline.json → {episodes, 口径};无文件返回空(老交付)。"""
    d = _load_json(os.path.join(m["path"], "details", "episodes_timeline.json"))
    return {"episodes": d.get("episodes") or {}, "note": d.get("口径", "")}


def timeline_html(tl: dict, cap: int = 200, only_flagged: bool = True) -> str:
    """时间线 → HTML 彩条列表。纯函数(可测)。

    2026-07-28 用户定稿:①默认只列有 stuck 或 idle 的 episode(全绿的不占屏,
    被藏条数在页脚注明);②段界时间直接标注在条下方(悬停仍有精确起止;标签
    间距 <4% 条宽的自动跳过防挤,右端时长恒标)。排序:stuck 降序,次 idle 降序。"""
    eps = tl.get("episodes") or {}
    if not eps:
        return ("<p>此交付无时间线数据(episodes_timeline.json)——需要跑过"
                "运动质量检查的新版交付。</p>")
    flagged = {e: t for e, t in eps.items()
               if (t.get("totals", {}).get("stuck", 0) > 0
                   or t.get("totals", {}).get("idle", 0) > 0)}
    shown_eps = flagged if only_flagged else eps
    if not shown_eps:
        return (f"<p>全部 {len(eps)} 条 episode 均无 stuck/idle——录制卫生良好 ✅</p>")
    order = sorted(shown_eps, key=lambda e: (-(eps[e].get("totals", {}).get("stuck", 0)),
                                             -(eps[e].get("totals", {}).get("idle", 0)), e))
    legend = " ".join(
        f'<span style="display:inline-block;width:12px;height:12px;'
        f'background:{TL_COLORS[s]};margin-right:4px;vertical-align:middle"></span>'
        f'<span style="margin-right:16px">{TL_LABELS[s]}</span>'
        for s in ("stuck", "idle", "normal"))
    rows = [f'<div style="margin:6px 0 14px 0">{legend}</div>']
    for eid in order[:cap]:
        t = eps[eid]
        dur = t.get("duration_s") or 0
        if dur <= 0:
            continue
        tot = t.get("totals", {})
        segs = t.get("segments") or []
        segs_html = "".join(
            f'<div title="{TL_LABELS.get(s["state"], s["state"])} '
            f'{s["start_s"]}–{s["end_s"]}s" '
            f'style="width:{max(0.2, (s["end_s"] - s["start_s"]) / dur * 100):.2f}%;'
            f'background:{TL_COLORS.get(s["state"], "#999")}"></div>'
            for s in segs)
        # 段界时间标注(2026-07-28 用户二次定稿:全部分界都标;**默认同一水平线
        # (条下方)**,与同行前一标签间距 <4% 条宽会重叠时,该标签放到 **bar 上方**
        # 的溢出行;上方也挤则挑更宽松的一行,宁可微叠不丢标)
        marks_below, marks_above = [], []
        last_below, last_above = -10.0, -10.0
        bounds = [0.0] + [seg["end_s"] for seg in segs]
        for j, b in enumerate(bounds):
            pct = min(b / dur * 100, 100.0)
            txt = f"{b:g}s" if j == len(bounds) - 1 else f"{b:g}"
            pos = ('right:0' if pct > 97 else
                   f'left:{pct:.2f}%;transform:translateX(-50%)')
            span = f'<span style="position:absolute;{pos}">{txt}</span>'
            if pct - last_below >= 4:
                marks_below.append(span); last_below = pct
            elif pct - last_above >= 4:
                marks_above.append(span); last_above = pct
            elif pct - last_below >= pct - last_above:
                marks_below.append(span); last_below = pct
            else:
                marks_above.append(span); last_above = pct
        above_html = (f'<div class="tl-above" style="position:relative;height:12px;'
                      f'font:10px monospace;color:#777">{"".join(marks_above)}</div>'
                      if marks_above else "")
        label = (f'{eid} · {dur:.1f}s'
                 + (f' · stuck {tot.get("stuck", 0)}s' if tot.get("stuck") else "")
                 + (f' · idle {tot.get("idle", 0)}s' if tot.get("idle") else ""))
        rows.append(
            f'<div style="margin:4px 0 10px 0">'
            f'<div style="font:12px monospace;margin-bottom:2px">{label}</div>'
            f'{above_html}'
            f'<div style="display:flex;height:16px;border-radius:3px;'
            f'overflow:hidden;border:1px solid #ddd">{segs_html}</div>'
            f'<div style="position:relative;height:13px;font:10px monospace;'
            f'color:#777">{"".join(marks_below)}</div></div>')
    if len(order) > cap:
        rows.append(f"<p>…共 {len(order)} 条,仅显示 stuck/idle 最多的前 {cap} 条</p>")
    if only_flagged and len(eps) > len(flagged):
        rows.append(f'<p style="color:#777">另有 {len(eps) - len(flagged)} 条无 '
                    f'stuck/idle 的干净 episode 未列出(勾选「显示全部」可见)</p>')
    return "\n".join(rows)
