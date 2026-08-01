"""交付目录 → UI 数据模型(纯函数层,2026-07-27 U1)。

架构红线:UI 只读交付目录,不 import 管道代码——本模块是"运行清单"契约的
读端,管道换底座 UI 不动。所有函数无副作用、不碰网络,Gradio 层只做渲染。

读的文件(U0 盘点定型的交付 schema):
  passed.json   数据集元信息 + dataset 统计 + skills + label_audit +
                config_effective + runtime(后端/硬件/容器配额)+
                dataset.vlm_latency(分桶延时)+ 通过条目(checks 含双重编码 detail)
  reject.json   被拒条目(+原因)
  review.json   待人工裁决条目 + 标注-画面分歧复核队列(旧交付键名:标注审计复核队列)
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
            "runtime": p.get("runtime") or {},
            "skills": p.get("skills") or {},
            "label_audit": p.get("label_audit"),
            # 双键兼容(2026-07-31 键名中性化):新交付写"标注-画面分歧复核队列",
            # 老交付写"标注审计复核队列"——两个都认,否则老交付打不开。
            "audit_queue": (v.get("标注-画面分歧复核队列")
                            or v.get("标注审计复核队列") or []),
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


AUDIT_HEADERS = ["episode", "原始标注", "自产描述(VLM 生成)", "分歧说明"]


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
        lines.append(f"- 标注-画面分歧复核队列 {len(m['audit_queue'])} 条"
                     "(见 技能画像 页;双方都可能错,供人工判定)")
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
    # 老交付没有这张表 → list_detail_tables 只列实际存在的文件,自然降级不报错
    "skill_assignment.csv": "技能归属明细(逐 episode 属于哪个技能)",
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
    """details/episodes_timeline.json → {episodes, 口径, 数据集注记};
    无文件返回空(老交付)。dataset_note 来自数据集 profile 的 extras.note,原样
    透传(如 bridge 的 state 由 action 合成),老交付/无注记的数据集为空串。"""
    d = _load_json(os.path.join(m["path"], "details", "episodes_timeline.json"))
    return {"episodes": d.get("episodes") or {}, "note": d.get("口径", ""),
            "dataset_note": d.get("dataset_note", "")}


def timeline_html(tl: dict, cap: int = 200, only_flagged: bool = True) -> str:
    """时间线 → HTML 彩条列表。纯函数(可测)。

    2026-07-28 用户定稿:①默认只列有 stuck 或 idle 的 episode(全绿的不占屏,
    被藏条数在页脚注明);②段界时间直接标注在条下方(悬停仍有精确起止;标签
    间距 <4% 条宽的自动跳过防挤,右端时长恒标)。排序:stuck 降序,次 idle 降序。"""
    eps = tl.get("episodes") or {}
    if not eps:
        return ("<p>此交付无时间线数据(episodes_timeline.json)——需要跑过"
                "运动质量检查的新版交付。</p>")
    # 数据集注记(2026-07-29 用户定):看彩条前必须知道的前提(如 bridge 的 state
    # 由 action 累加合成 → 指令-实际无独立信息,stuck 只能弃权,黄条的含义随之变)。
    # 有才渲染,没有不占位;内容原样来自数据集 profile 的 extras.note,UI 不做判断。
    note_html = (f'<p style="margin:0 0 6px 0;color:#8a6d3b;background:#fcf8e3;'
                 f'border-left:3px solid #f1c40f;padding:6px 10px">'
                 f'数据集注记:{tl["dataset_note"]}</p>'
                 if tl.get("dataset_note") else "")
    flagged = {e: t for e, t in eps.items()
               if (t.get("totals", {}).get("stuck", 0) > 0
                   or t.get("totals", {}).get("idle", 0) > 0)}
    shown_eps = flagged if only_flagged else eps
    if not shown_eps:
        return (note_html
                + f"<p>全部 {len(eps)} 条 episode 均无 stuck/idle——录制卫生良好 ✅</p>")
    order = sorted(shown_eps, key=lambda e: (-(eps[e].get("totals", {}).get("stuck", 0)),
                                             -(eps[e].get("totals", {}).get("idle", 0)), e))
    legend = " ".join(
        f'<span style="display:inline-block;width:12px;height:12px;'
        f'background:{TL_COLORS[s]};margin-right:4px;vertical-align:middle"></span>'
        f'<span style="margin-right:16px">{TL_LABELS[s]}</span>'
        for s in ("stuck", "idle", "normal"))
    rows = ([note_html] if note_html else []) + [
        f'<div style="margin:6px 0 14px 0">{legend}</div>']
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


# ───────── 性能剖析(P1,2026-07-30):VLM 后端 / 运行环境 / 延时剖析 ─────────
#
# ★ 界面红线:**绝不出现预设代号**(那是机房黑话,客户看不懂也不该看懂)。
#   后端一律用「服务端点 URL + 模型名 + 服务类型 + 硬件型号」四件套表述。
#   硬件型号只能来自交付记录本身(runtime.vlm_backend.hardware,源头是站点配置的
#   vlm_backends.*.hardware)——本模块**没有也不许有**"端点→型号"的硬编码映射表:
#   那种表会在换机器后继续输出旧型号,拿 A 机的规格解释 B 机的延时。

#: 交付里没记这一项时的统一措辞(老交付走这条)。
NOT_RECORDED = "未记录(旧版本交付)"

#: VLM 调用类型 → 语义化中文标签。左边是实现内部的埋点标签(vlm_client.latency_record
#: 的 tag),只作字典键;界面上只出现右边。顺序 = 展示顺序(按漏斗发生的先后)。
LATENCY_LABELS = {
    "probe": "任务判定探针",
    "endstate": "终态复核",
    "caption": "技能打标",
    "llm": "体系归纳",
}

#: 延时口径(一句话,跟着表格一起显示)——不写清楚会被当成服务端推理耗时。
LATENCY_NOTE = (
    "口径:**客户端视角**的单次调用耗时(发出请求 → 收完整响应),含网络往返、"
    "服务端排队与推理。下表统计的是**单次调用**的分布;整类调用实际占了多久,"
    "看下面的墙钟条形图(并发下多次调用在时间上重叠,把单次耗时乘以次数会严重高估)。")

#: 分位数怎么读(表下小字)。客户不是做性能的,P90 不解释就只是个符号。
LATENCY_PCTL_NOTE = (
    "_P50 = 一半的调用不超过此耗时;P90/P99 同理,越靠后越反映最慢的少数调用。_")

#: 四类调用各是干什么的(表下说明,一行一条)。语义化名字必须配得上解释,
#: 否则"任务判定探针 1583 次"这种数字客户没法判断合不合理。
LATENCY_KIND_NOTE = "\n".join([
    "**调用类型说明**",
    "",
    "- **任务判定探针**:判断任务是否完成的主力——抽 8 帧打乱后让视觉模型为每帧打"
    "「完成度」分,排序能还原时间顺序即任务在推进;每帧一次调用,故次数最多。",
    "- **终态复核**:主判拿不准时的二审——同样抽 8 帧,分成早期组与晚期组对照,"
    "用两个互为反问的是非题确认任务终态;仅一审未通过的数据触发,最多复核 4 路相机。",
    "- **技能打标**:视觉模型为每条数据写一句「它在做什么」,供技能画像;每条一次。",
    "- **体系归纳**:文本大模型通读全部打标,归纳数据集的两级技能体系;"
    "次数极少、单次长。",
])

#: 横条图配色(四类各一色;与判决用的红/绿色系错开,避免被误读成"好坏")。
_BAR_COLORS = {"probe": "#4a7fd4", "endstate": "#7d5ba6",
               "caption": "#2a9d8f", "llm": "#e08a3c"}


def infer_service_type(endpoint: str | None) -> str:
    """endpoint → 服务类型(仅当交付里没显式声明 service_type 时的兜底)。

    纯字符串判断、零网络。只粗判"部署形态",判不出就老实写「OpenAI 兼容服务」——
    宁可说不知道,也不编一个具体的服务名。
    """
    if not endpoint:
        return "未记录"
    from urllib.parse import urlparse
    host = (urlparse(endpoint).hostname or "").lower()
    if not host:
        return "OpenAI 兼容服务"
    if (host.endswith(".svc.cluster.local") or host in ("localhost", "127.0.0.1")
            or host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18."))):
        return "自托管推理服务(集群内)"
    if host.endswith("volces.com"):
        return "方舟 MaaS(托管服务)"
    return "OpenAI 兼容服务"


def load_perf(m: dict) -> dict:
    """交付 → 性能剖析三块数据(后端 / 运行环境 / 延时)。纯函数。

    降级策略(老交付):runtime 块是 2026-07-30 之后的新交付才有。缺块时从
    config_effective **尽力**还原端点/模型/三个并发值(那是配置快照里本来就有的),
    硬件与运行环境则如实标"未记录"——绝不从端点反查型号。
    """
    rt = m.get("runtime") or {}
    be = dict(rt.get("vlm_backend") or {})
    env = dict(rt.get("environment") or {})
    legacy = not be
    if legacy:                                   # 老交付:退回配置快照能给的部分
        ce = m.get("config_effective") or {}
        vlm = ((ce.get("checks") or {}).get("task_success") or {}).get("vlm") or {}
        be = {"endpoint": vlm.get("endpoint"), "model": vlm.get("model"),
              "hardware": None, "service_type": None,
              "episode_concurrency": (ce.get("pipeline") or {}).get("vlm_episode_concurrency"),
              "frame_concurrency": vlm.get("max_concurrency"),
              "caption_concurrency": (ce.get("skill_profile") or {}).get("caption_concurrency")}
    if not be.get("service_type"):
        be["service_type"] = infer_service_type(be.get("endpoint"))
    return {"backend": be, "env": env, "legacy": legacy,
            "latency": (m.get("dataset") or {}).get("vlm_latency") or {}}


def _hardware_text(perf: dict) -> str:
    """硬件型号的展示文本。只有两个来源:交付记录里写了,或托管服务本来就不可见。"""
    hw = perf["backend"].get("hardware")
    if hw:
        return str(hw)
    st = perf["backend"].get("service_type") or ""
    # ⚠️「自托管」里含「托管」二字:必须先排除,否则自建的 GPU 服务会被说成
    # "硬件不可见"(2026-07-30 测试当场抓到的字串陷阱)。
    if "MaaS" in st or ("托管" in st and "自托管" not in st):
        return "托管服务,硬件不可见(由服务商调度)"
    return NOT_RECORDED if perf["legacy"] else "未记录(站点配置未声明 hardware)"


def _val(v) -> str:
    return "未记录" if v in (None, "") else str(v)


def perf_backend_md(perf: dict) -> str:
    """第一块:VLM 后端卡片(Markdown 表)。"""
    b = perf["backend"]
    ep = b.get("endpoint")
    rows = [
        ("服务端点", f"`{ep}`" if ep else "未记录"),
        ("模型名", _val(b.get("model"))),
        ("服务类型", _val(b.get("service_type"))),
        ("硬件型号", _hardware_text(perf)),
        ("episode 并发(同时处理几条数据)", _val(b.get("episode_concurrency"))),
        ("单条内帧并发(一条数据内同时问几帧)", _val(b.get("frame_concurrency"))),
        ("打标并发(技能打标同时跑几条)", _val(b.get("caption_concurrency"))),
    ]
    head = "### VLM 后端\n\n| 项 | 本次运行取值 |\n|---|---|\n"
    body = "\n".join(f"| {k} | {v} |" for k, v in rows)
    tail = "\n\n_硬件型号来自本次交付的运行记录(站点配置声明),不是界面推测的。_"
    return head + body + tail


def perf_env_md(perf: dict) -> str:
    """第二块:运行环境(质检管线所在容器的 CPU 侧)。老交付整块"未记录"。"""
    env = perf["env"]
    if not env:
        return ("### 运行环境(质检管线容器)\n\n" + NOT_RECORDED
                + "——运行环境是 2026-07-30 之后的新交付才记录的字段,"
                  "此交付跑批时管线尚未采集。")
    cpu = env.get("cpu_limit_cores")
    mem = env.get("memory_limit_bytes")
    node = env.get("node")
    src = env.get("node_source")
    node_txt = _val(node)
    if node and src == "hostname":
        node_txt += "(容器 hostname;未注入 NODE_NAME,故非节点名)"
    elif node and src == "NODE_NAME":
        node_txt += "(取自调度注入的节点名)"
    rows = [("CPU 配额", f"{cpu} 核" if cpu else "未记录(非容器环境或未设限)"),
            ("内存配额", f"{mem / (1 << 30):.1f} GiB" if mem else "未记录(非容器环境或未设限)"),
            ("运行节点", node_txt)]
    head = "### 运行环境(质检管线容器)\n\n| 项 | 值 |\n|---|---|\n"
    tail = ("\n\n_这里是**管线自己**的 CPU 侧资源(抽帧解码/数值检查在此消耗);"
            "VLM 推理的算力在上面那张卡片的服务端。_")
    return head + "\n".join(f"| {k} | {v} |" for k, v in rows) + tail


LATENCY_HEADERS = ["调用类型", "调用次数", "平均响应时间(秒)",
                   "P50 响应时间(秒)", "P90 响应时间(秒)", "P99 响应时间(秒)"]


def latency_rows(perf: dict) -> list[list]:
    """第三块:延时表。按 LATENCY_LABELS 顺序,缺的桶不占行;未知标签兜底排在最后。"""
    lat = perf["latency"]
    order = [t for t in LATENCY_LABELS if t in lat]
    order += [t for t in sorted(lat) if t not in LATENCY_LABELS]
    rows = []
    for tag in order:
        s = lat.get(tag) or {}
        rows.append([LATENCY_LABELS.get(tag, tag), s.get("n", 0),
                     _val(s.get("mean_s")), _val(s.get("p50_s")),
                     _val(s.get("p90_s")), _val(s.get("p99_s"))])
    return rows


#: 老交付(2026-07-30 之前)没记调用发出时刻 → 算不出墙钟。此时**不画图**:
#: 退回"次数 × 均值"那种条形图会把并发跑的 8 分钟说成 8 小时,宁可空着。
NO_WALL_NOTE = ("此交付未记录调用时刻(旧版本),无法计算墙钟;新交付起提供。")


def human_duration(sec: float) -> str:
    """秒 → 人读的时长。<60s 保留一位小数,再往上按 分/小时 拆(演示要能念出来)。"""
    if sec < 60:
        return f"{sec:.1f} 秒"
    total = int(round(sec))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h} 小时 {m} 分 {s} 秒" if h else f"{m} 分 {s} 秒"


def latency_bar_html(perf: dict) -> str:
    """第三块配图:纯 HTML/CSS 横条图,条长 = 该类调用的**墙钟**。

    ★ 口径只有一个:墙钟 = 第一次发出 → 最后一次返回的真实时长(run 收割时按类
    算好写进交付,见 vlm_client.latency_summary 的 wall_s)。**不存在**"次数 ×
    均值"的回退画法:我们是并发跑的,那个乘积是几十倍的高估,画出来就是误导。
    没有墙钟数据(老交付)= 不画图 + 一句说明。
    """
    lat = perf["latency"]
    if not lat:
        return ('<p style="color:#777">本次运行没有 VLM 调用(例如只跑了数值类检查),'
                '因此没有延时数据。</p>')
    items = [(tag, float(s["wall_s"]), s.get("n") or 0, s.get("errors") or 0)
             for tag, s in lat.items() if s.get("wall_s") is not None]
    if not items:
        return f'<p style="color:#777">{NO_WALL_NOTE}</p>'
    items.sort(key=lambda x: -x[1])
    top = max(items[0][1], 1e-9)
    bars = []
    for tag, wall, n, errs in items:
        pct = max(1.0, wall / top * 100)
        label = LATENCY_LABELS.get(tag, tag)
        cnt = f"{n} 次调用并发执行" if n > 1 else f"{n} 次调用"
        err_txt = f' · <span style="color:#c0392b">失败 {errs}</span>' if errs else ""
        bars.append(
            f'<div style="margin:8px 0">'
            f'<div style="font:12px/1.5 system-ui;margin-bottom:2px">{label}'
            f' — 墙钟 <b>{human_duration(wall)}</b>({cnt}){err_txt}</div>'
            f'<div style="background:#eceff3;border-radius:4px;overflow:hidden;height:14px">'
            f'<div style="width:{pct:.2f}%;height:100%;'
            f'background:{_BAR_COLORS.get(tag, "#888")}"></div></div></div>')
    return ('<div style="max-width:760px">'
            '<div style="font:12px system-ui;color:#555;margin-bottom:4px">'
            '各类调用的墙钟耗时(第一次发出 → 最后一次返回的真实时长)</div>'
            + "".join(bars)
            + '<div style="font:12px system-ui;color:#777;margin-top:6px">'
              '各类调用之间在时间上可能重叠,各条墙钟相加 ≠ 整次运行总时长。</div>'
            '</div>')
