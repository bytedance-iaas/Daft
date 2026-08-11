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
        # review 条目自带 checks 的情况(rejudge 搬移过的条目会写上):只在
        # passed/reject 那边没有时才用,不覆盖主视图的读数。
        if not ep.get("checks") and ve.get("checks"):
            ep["checks"] = _norm_checks(ve.get("checks"))

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
            "task_review": task_review_queue(v, episodes),
            "episodes": episodes}


#: 任务成败检查在交付里的中文名(report.py 的 CHECK_CN 单一事实源;此处是读端的
#: 常量副本——UI 不 import 管道代码的红线,不许 from ..export.report import)。
TASK_CHECK_CN = "任务成败判定"

#: 任务成败弃权队列里,人要看的那两个读数(VLM 判定的中间量,不是我们现算的)。
#: voc = 打乱帧排序能否还原时序(任务在不在推进);末态分 = 终态完成度。
TASK_READING_KEYS = (("voc", "voc"), ("completion_final", "末态分"))


def _deep_detail(raw) -> dict:
    """detail 解码,容忍**双重编码**(JSON 字符串里又套一层 JSON 字符串)。

    交付里 detail 本来就是 JSON 字符串;经过 rejudge 搬移 / 老版本管道时,曾出现
    再被 json.dumps 一次的条目——只解一层拿到的是 str,读数就全丢了。这里最多剥
    两层,仍不是 dict 就交给 parse_detail 的原文兜底(不丢信息)。
    """
    d = raw
    for _ in range(2):
        if isinstance(d, dict):
            return d
        if isinstance(d, str) and d.strip():
            try:
                d = json.loads(d)
                continue
            except Exception:  # noqa: BLE001
                break
        break
    return parse_detail(d if isinstance(d, (dict, str)) else raw)


def task_readings(check: dict) -> dict:
    """任务成败检查条目 → {"voc":…, "末态分":…}(取不到的键不出现)。

    check 既吃 manifest 归一化后的 {"detail": dict},也吃交付原文 {"detail": "…"}。
    """
    d = check.get("detail") if isinstance(check, dict) else None
    d = _deep_detail(d)
    out = {}
    for key, label in TASK_READING_KEYS:
        if d.get(key) is not None:
            out[label] = d[key]
    return out


def task_review_queue(review_json: dict, episodes: dict) -> list:
    """review.json + 已合并的 episodes → 任务成败待裁决队列(纯函数)。

    只收**待裁决项含「任务成败判定」**的条目:review 里还有别的维度的弃权
    (如同步/运动学),那些不是人看视频就能拍板的,不该混进成败裁决面板。
    """
    out = []
    for eid, ve in sorted((review_json.get("episodes") or {}).items()):
        if TASK_CHECK_CN not in (ve.get("待裁决项") or []):
            continue
        ep = episodes.get(eid) or {}
        check = (ep.get("checks") or {}).get(TASK_CHECK_CN) or {}
        out.append({"id": eid,
                    "current": ve.get("当前判决", "?"),
                    "reason": (ve.get("弃权原因") or {}).get(TASK_CHECK_CN, ""),
                    "readings": task_readings(check),
                    "state": check.get("state", "")})
    return out


# ───────── 表格整形(Gradio Dataframe 直接吃)─────────

EPISODE_HEADERS = ["episode", "判决", "软分", "待裁决", "拒绝原因", "证据帧", "同步图"]

#: Episodes 页的列表筛选(2026-08-07 用户点名:被拒条目是重点工作对象,得能只看
#: 它们)。取值直接当界面文案用——再建一层「英文键 → 中文标签」的映射,只会多一处
#: 能对不上的地方。
EPISODE_FILTER_ALL = "全部"
EPISODE_FILTER_REJECTED = "只看被拒"
EPISODE_FILTER_PENDING = "只看待裁决"
EPISODE_FILTERS = [EPISODE_FILTER_ALL, EPISODE_FILTER_REJECTED,
                   EPISODE_FILTER_PENDING]


def filter_episode_ids(m: dict, mode: str = EPISODE_FILTER_ALL) -> list[str]:
    """按筛选档挑 episode id(id 升序,稳定)。

    未知档位一律当「全部」:前端能塞任意字符串进来,不该因为一个陌生值就空表
    (空表看起来像"这份交付没数据",是最糟的误导)。
    """
    eps = m.get("episodes") or {}
    eids = sorted(eps.keys())
    if mode == EPISODE_FILTER_REJECTED:
        return [e for e in eids if (eps[e] or {}).get("verdict") == "拒绝"]
    if mode == EPISODE_FILTER_PENDING:
        return [e for e in eids if (eps[e] or {}).get("pending")]
    return eids


def episode_rows(m: dict, mode: str = EPISODE_FILTER_ALL) -> list[list]:
    rows = []
    for eid in filter_episode_ids(m, mode):
        ep = m["episodes"][eid]
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


# ───────── 技能分布图(2026-07-30):技能画像页的横向条形图 ─────────
#
# 设计定稿(用户拍板 + 两条实现纪律),改之前先读完:
# ① **不截断**:全部条目都画。画像的价值恰恰在长尾——bridge 那 130 项里绝大多数
#    只有 1 条数据,截掉尾巴看到的是"数据很集中"的假象。行高压紧(一条一行、
#    条间 2px)让长列表也能扫读。
# ② **单色**:所有条同一个蓝。所有条测的是同一个量(条数),按族上色是彩虹图
#    反模式——颜色不携带任何信息,只增加视觉噪声,还会被误读成"类别有好坏"。
#    单蓝已过调色板校验(亮度带 / 彩度下限 / CVD 分离 / 常视分离 / 对比度,五项
#    全过)。**别改成多色。**
# ③ **样本偏少**:名单直接用画像自带的 undersampled 字段(不是本图现算的,图下
#    注明出处)。标记方式 = 条尾一枚**带文字**的琥珀 chip,不靠颜色单独表意;
#    **不换条的填充色**——条本来就短,再变个色是冗余编码。
# ④ **下钻用原生 <details>/<summary>,零 JavaScript**:族条本身就是 summary
#    (行首 ▸/▾ 由 CSS 的 details[open] 切换,同样零脚本),展开露出子技能条。
#    只有 **≥2 个子技能**的族才折叠,单子技能族退化成普通行(点开只看到自己的
#    复读没有意义);普通行占一个等宽的空箭头位,保证所有条起点对齐。
# ⑤ **共用全局尺子**:子技能条与族条按**同一个**全局最大 count 归一。若按族内
#    最大值重缩放,只有 1 条数据的族其子技能会画得和 102 条的 Put 一样长——那是
#    骗人的。
#
#: 唯一的条色(见上 ②)。
SKILL_BAR_COLOR = "#2a78d6"

#: 形状 B(VLM 不可用 → 退回按原始标注分组)必须挂的前提说明。不写清楚,客户会
#: 把一堆原始指令当成系统归纳出的技能体系。形状 A 不显示这句。
SKILL_FALLBACK_NOTE = ("未经 VLM 审计的原始标注分组(VLM 不可用时的降级路径),仅供参考")

#: 悬停详情里判据截断长度(判据是 LLM 写的一整句,全塞进 title 会糊一屏)。
_SKILL_CRIT_CAP = 60


def _esc(s) -> str:
    """进 HTML 前转义。技能名来自 LLM 归纳或数据集原始指令,不能当可信片段拼。"""
    import html
    return html.escape(str(s), quote=True)


def skill_chart_items(m: dict) -> tuple[str, list[dict]]:
    """skills 块 → (形状, 条目列表)。纯数据整形,不产 HTML(方便单测)。

    形状三选一:
      "two_level" 两级画像(正常路径):families → 每族带 subskills
      "flat"      扁平降级画像(VLM 不可用,按原始标注分组):skills 一层
      "empty"     未启用 / 空
    条目一律按条数降序;子技能同样降序。
    """
    sk = m.get("skills") or {}
    fams = sk.get("families") or {}
    flat = sk.get("skills") or {}
    if fams:
        items = [{"name": name, "count": f.get("count") or 0, "pct": f.get("pct"),
                  "criterion": f.get("criterion") or "",
                  "subs": sorted(
                      ({"name": sn, "count": s.get("count") or 0, "pct": s.get("pct"),
                        "criterion": s.get("criterion") or "", "subs": []}
                       for sn, s in (f.get("subskills") or {}).items()),
                      key=lambda x: (-x["count"], x["name"]))}
                 for name, f in fams.items()]
        shape = "two_level"
    elif flat:
        items = [{"name": name, "count": s.get("count") or 0, "pct": s.get("pct"),
                  "criterion": "", "subs": []} for name, s in flat.items()]
        shape = "flat"
    else:
        return "empty", []
    items.sort(key=lambda x: (-x["count"], x["name"]))
    return shape, items


#: 图内样式:只有"折叠箭头"这一件事非 CSS 不可(纯内联样式写不出 details[open]
#: 与伪元素)。选择器全部挂在 .sk-chart 下,不污染 Gradio 页面其余部分;零 JS。
_SKILL_CSS = """<style>
.sk-chart details > summary{list-style:none;cursor:pointer}
.sk-chart details > summary::-webkit-details-marker{display:none}
.sk-chart .sk-caret::before{content:"\\25B8";color:#888}
.sk-chart details[open] > summary .sk-caret::before{content:"\\25BE"}
</style>"""


def _skill_bar_row(it: dict, top: int, undersampled: set, *,
                   sub: bool = False, caret: bool = False) -> str:
    """一根条(族 / 子技能共用)。宽度按**全局** top 归一(见上 ⑤)。"""
    name, count, pct = it["name"], it["count"], it["pct"]
    width = max(0.6, count / top * 100) if top else 0.6
    meta = f"{count} 条" + (f" · {pct}%" if pct is not None else "")
    title = f"{name} · {meta}"
    if it.get("criterion"):
        title += f" · 判据:{str(it['criterion'])[:_SKILL_CRIT_CAP]}"
    # 「样本偏少」:带文字的 chip,颜色只是陪衬(色盲/黑白打印下仍读得出)。
    chip = ('<span style="background:#fdf1dc;color:#8a6d3b;border:1px solid #eec98a;'
            'border-radius:3px;padding:0 4px;margin-left:6px;font-size:10px;'
            'white-space:nowrap">样本偏少</span>') if name in undersampled else ""
    return (
        f'<div title="{_esc(title)}" style="display:flex;align-items:center;gap:8px;'
        f'margin:1px 0">'
        f'<span class="{"sk-caret" if caret else ""}" style="flex:0 0 '
        f'{14 if not sub else 30}px;font-size:11px"></span>'
        f'<div style="flex:0 0 {204 if sub else 218}px;font:12px/1.4 system-ui;'
        f'color:#333;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
        f'{_esc(name)}</div>'
        f'<div style="flex:1;min-width:60px;background:#eef1f5;border-radius:4px;'
        f'height:12px">'
        f'<div style="width:{width:.2f}%;height:100%;background:{SKILL_BAR_COLOR};'
        f'border-radius:0 4px 4px 0"></div></div>'
        f'<div style="flex:0 0 150px;font:11px/1.4 system-ui;color:#666;'
        f'text-align:right">{_esc(meta)}{chip}</div></div>')


def skill_bar_html(m: dict) -> str:
    """技能画像 → 横向条形图 HTML(自包含、零 JS)。纯函数(可测)。

    两种画像形状都吃(见 skill_chart_items),没画像就一句说明、不占位。
    """
    shape, items = skill_chart_items(m)
    if shape == "empty":
        return ('<p style="color:#777">此交付未生成技能画像——需要跑过技能画像'
                '阶段(全员 caption → 归纳技能体系)的交付。</p>')
    sk = m.get("skills") or {}
    undersampled = set(sk.get("undersampled") or [])
    top = max((it["count"] for it in items), default=0) or 1
    n_eps = sk.get("n_episodes")

    head = []
    if shape == "flat":
        head.append(f'<p style="margin:0 0 6px 0;color:#8a6d3b;background:#fcf8e3;'
                    f'border-left:3px solid #f1c40f;padding:6px 10px">'
                    f'{SKILL_FALLBACK_NOTE}</p>')
    unit = "个技能族" if shape == "two_level" else "项技能(按原始标注分组)"
    n_kind = sk.get("n_families") if shape == "two_level" else sk.get("n_skills")
    line = (f'共 {n_kind if n_kind is not None else len(items)} {unit}'
            + (f",覆盖 {n_eps} 条数据" if n_eps is not None else "")
            + ";按条数降序,全部列出(长尾本身就是画像的信息量,不截断)。")
    n_drill = sum(1 for it in items if len(it["subs"]) >= 2)
    if n_drill:
        line += f"其中 {n_drill} 个族有多个子技能,点族名可展开。"
    head.append(f'<div style="font:12px/1.6 system-ui;color:#555;margin-bottom:6px">'
                f'{line}</div>')

    rows = []
    for it in items:
        drill = len(it["subs"]) >= 2        # 只有 ≥2 子技能才值得折叠(见上 ④)
        bar = _skill_bar_row(it, top, undersampled, caret=drill)
        if drill:
            subs = "".join(_skill_bar_row(s, top, undersampled, sub=True)
                           for s in it["subs"])
            rows.append(f'<details><summary>{bar}</summary>{subs}</details>')
        else:
            rows.append(bar)

    foot = ('<div style="font:11px/1.6 system-ui;color:#777;margin-top:8px">'
            '琥珀「样本偏少」标记来自画像自带的名单(生成画像时判定),不是本图现算的。'
            '</div>') if undersampled else ""
    return ('<div class="sk-chart" style="max-width:960px">' + _SKILL_CSS
            + "".join(head) + "".join(rows) + foot + '</div>')


AUDIT_HEADERS = ["操作", "档位", "episode", "原始标注", "自产描述(VLM 生成)", "成败线判定", "分歧说明", "裁决"]


def audit_rows(m: dict) -> list[list]:
    """档位=重点(成败线同时不利,两线同报警)/参考(成败线放行,多为描述噪声)。
    裁决列回显 details/label_decisions.csv(空=待人工)。"""
    dec = load_label_decisions(m)
    return [["裁决 ▶", a.get("priority", "参考"), a.get("id", ""), a.get("label", ""),
             a.get("caption", ""), a.get("task_verdict", ""), a.get("reason", ""),
             dec.get(a.get("id", ""), {}).get("decision", "")]
            for a in m["audit_queue"]]


TASK_REVIEW_HEADERS = ["操作", "episode", "当前判决", "弃权原因", "关键读数", "裁决"]

#: 弃权原因是 VLM 写的一整句,表里截断,全文在下方卡片里给。
_REASON_CAP = 60


def readings_text(readings: dict) -> str:
    """{"voc":0.87,"末态分":0.3} → "voc=0.87 · 末态分=0.3"(没有读数给一句人话)。"""
    if not readings:
        return "(无读数)"
    return " · ".join(f"{k}={v}" for k, v in readings.items())


def task_review_rows(m: dict) -> list[list]:
    """任务成败弃权队列 → 表格行。裁决列回显 details/task_verdicts.csv(空=待人工)。"""
    dec = load_task_verdicts(m)
    rows = []
    for t in m.get("task_review") or []:
        reason = str(t.get("reason") or "")
        rows.append(["裁决 ▶", t.get("id", ""), t.get("current", ""),
                     reason[:_REASON_CAP] + ("…" if len(reason) > _REASON_CAP else ""),
                     readings_text(t.get("readings") or {}),
                     dec.get(t.get("id", ""), {}).get("verdict", "")])
    return rows


def audit_pending_count(m: dict) -> int:
    """标注分歧队列里**还没裁**的条数(裁过的不该再催人去看)。"""
    dec = load_label_decisions(m)
    return sum(1 for a in (m.get("audit_queue") or [])
               if not dec.get(a.get("id", ""), {}).get("decision"))


def task_pending_count(m: dict) -> int:
    """任务成败弃权队列里还没裁的条数(搁置**算未裁**:它是"待定"不是结论)。"""
    dec = load_task_verdicts(m)
    return sum(1 for t in (m.get("task_review") or [])
               if dec.get(t.get("id", ""), {}).get("verdict", "") in ("", "搁置"))


#: 「人工裁决」页顶的工序引导。顺序不是洁癖:改标重判会让一部分弃权自动有结论,
#: 先裁成败等于白裁——这句话就是防止用户白干一遍。
WORKFLOW_GUIDE = (
    "**建议顺序**:先裁下方「标注分歧」 → 跑 `curation rejudge`"
    "(改标重判后,部分任务成败弃权会自动解决)→ 再裁剩余的「任务成败弃权」 → "
    "再跑一次 `curation rejudge` 生效。\n\n"
    "_两块都只**记录**裁决(落交付目录 details/ 下的 CSV),可随时改判;"
    "真正改交付的是命令行的 `curation rejudge`。_")


def audit_note_md(m: dict) -> str:
    """技能画像页留的一行指路(裁决面板已搬去「人工裁决」页)。"""
    q = m.get("audit_queue") or []
    if not q:
        return "_本次未检出标注-画面分歧条目。_"
    n = audit_pending_count(m)
    if not n:
        return (f"标注-画面分歧队列共 **{len(q)}** 条,已全部裁决 → "
                "详见「**人工裁决**」页")
    return (f"**{n}** 条标注分歧待裁(队列共 {len(q)} 条)→ "
            "去「**人工裁决**」页处理")


def task_review_hint_md(m: dict) -> str:
    """区块②标题下的提示:本块进度 + "上面还有标注分歧没裁,建议先清"的工序提醒。"""
    q = m.get("task_review") or []
    if not q:
        return "_本次没有任务成败弃权条目(系统对每条数据都给出了判定)。_"
    lines = [f"共 **{len(q)}** 条弃权,其中 **{task_pending_count(m)}** 条待裁"
             "(搁置算待裁:它是「待定」不是结论)。"]
    n_audit = audit_pending_count(m)
    if n_audit:
        lines.append(f"⚠️ 上方还有 **{n_audit}** 条标注分歧未裁,建议先清"
                     "(改标重判后,部分弃权会自动解决,省得白裁)")
    return "\n\n".join(lines)


def overview_markdown(m: dict) -> str:
    d = m["dataset"]
    ss = d.get("summary_stats") or {}
    # 机器人字段自 2026-07 起是 dict(型号+规格表+质量),概览一直在渲染原始
    # dict(2026-08-10 发现)——按报告身份行的同款人话格式化;老交付是纯字符串,原样。
    rb = m.get("robot")
    if isinstance(rb, dict):
        _q = f",质量 {rb.get('quality')}" if rb.get("quality") else ""
        rb = f"{rb.get('robot_type')}(规格表 {rb.get('registry_profile')}{_q})"
    lines = [f"# {m['name']}",
             f"机器人 **{rb}** · 生成于 {m['generated_at']} · 代码版本 {m['code_version']}",
             "",
             f"- 交付 **{d.get('delivered', '?')}** / 输入 {d.get('input_episodes', '?')} 条"
             f"(通过率 {ss.get('pass_rate_pct', '?')}%,平均软分 {ss.get('avg_soft_score', '?')})"]
    hb = d.get("hard_fail_breakdown") or {}
    if hb:
        lines.append("- 硬门拒绝:" + ",".join(f"{k} {v} 条" for k, v in hb.items()))
    # 数据包完整性(2026-08-10):容器缺了什么、按什么补的,概览一行带过,
    # 详细说明在质检报告的「数据包完整性」节。有 findings 才出,不占位。
    cf = (d.get("container") or {}).get("findings") or []
    if cf:
        _ic = {"正常": "✅", "缺失(已补)": "⚠️", "缺失(已溯源补全)": "✅",
               "降级": "⚠️", "缺失": "❌"}
        lines.append("- 数据包完整性:" + ";".join(
            f"{f.get('项')} {_ic.get(str(f.get('状态')), '')}{f.get('状态')}"
            for f in cf) + "(缺什么、按什么补的,详见质检报告)")
    n_pending = sum(1 for e in m["episodes"].values() if e.get("pending"))
    if n_pending:
        lines.append(f"- 待人工裁决 **{n_pending}** 条(见 Episode 页「待裁决」列;"
                     "任务成败弃权可在「人工裁决」页逐条裁定)")
    if m["audit_queue"]:
        lines.append(f"- 标注-画面分歧复核队列 {len(m['audit_queue'])} 条"
                     "(见「人工裁决」页;双方都可能错,供人工判定)")
    return "\n".join(lines)


def discover_deliveries(root: str, max_depth: int = 3) -> list[str]:
    """root 本身是交付目录 → [root];否则递归扫子目录(默认 3 层)找含 passed.json 的。

    2026-08-06 从"只扫一层"改递归:用户把交付放在嵌套目录(如 experiments/run1/)
    时曾整个不可见,看起来像 UI 坏了。找到交付目录即不再往其内部钻(details/ 等
    子目录里不会再有交付)。"""
    root = os.path.abspath(root)
    if os.path.exists(os.path.join(root, "passed.json")):
        return [root]
    out = []

    def _walk(d: str, depth: int):
        if depth > max_depth:
            return
        try:
            subs = sorted(os.listdir(d))
        except OSError:
            return
        for name in subs:
            p = os.path.join(d, name)
            if not os.path.isdir(p):
                continue
            if os.path.exists(os.path.join(p, "passed.json")):
                out.append(p)                 # 是交付:收下,不再往里钻
            else:
                _walk(p, depth + 1)

    _walk(root, 1)
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
    "- **体系归纳**:文本大模型把全部打标通读一遍,归纳出这份数据集的两级技能"
    "分类树(哪些族、每族哪些子技能),并自查合并分错的类。整个数据集只需几次"
    "调用,但每次都要读完全部打标——所以延时表里它**次数少、单次久**是正常形态,"
    "不是卡顿。",
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
    lat = (m.get("dataset") or {}).get("vlm_latency") or {}
    fresh = _recompute_latency(os.path.join(m.get("path") or "", "details",
                                            "vlm_latency.csv"))
    if fresh:
        lat = fresh          # 逐请求明细在手就现场复算——快照口径旧了也能自愈
    return {"backend": be, "env": env, "legacy": legacy, "latency": lat,
            "total_wall_s": rt.get("total_wall_s")}


def _pctl_(xs: list, q: float) -> float:
    i = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return xs[i]


def _recompute_latency(csv_path: str) -> dict:
    """details/vlm_latency.csv → 按类汇总(与 vlm_client.latency_summary 同款算法,
    在 UI 侧独立实现——UI 不 import 管道的红线;两实现有对拍测试钉住)。

    wall_s = 忙碌区间并集(空档不计):旧快照用"首发→末返"跨度,分段跑的类别
    (caption 补标+画像两波)会把中间隔的别的阶段全灌进来,条形图严重失真
    (2026-08-06 droid-30 实锤)。读不到/读坏 CSV → 返回 {},上层退回快照。
    """
    import csv as _csv
    try:
        rows = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                st = (r.get("started_at") or "").strip()
                rows.append((r["call_type"], float(r["seconds"]),
                             bool(int(r["ok"])), float(st) if st else None))
    except (OSError, KeyError, ValueError):
        return {}
    out: dict = {}
    for tag in sorted({r[0] for r in rows}):
        mine = [r for r in rows if r[0] == tag]
        oks = sorted(r[1] for r in mine if r[2])
        entry = {"n": len(oks), "errors": sum(1 for r in mine if not r[2])}
        if oks:
            entry.update({"mean_s": round(sum(oks) / len(oks), 2),
                          "p50_s": round(_pctl_(oks, 0.50), 2),
                          "p90_s": round(_pctl_(oks, 0.90), 2),
                          "p99_s": round(_pctl_(oks, 0.99), 2),
                          "max_s": round(oks[-1], 2)})
        stamped = [r for r in mine if r[3] is not None]
        if stamped:
            ivs = sorted((r[3], r[3] + r[1]) for r in stamped)
            busy, cs, ce = 0.0, ivs[0][0], ivs[0][1]
            for s, e in ivs[1:]:
                if s > ce:
                    busy += ce - cs
                    cs, ce = s, e
                else:
                    ce = max(ce, e)
            busy += ce - cs
            entry["wall_s"] = round(busy, 2)
        out[tag] = entry
    return out


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

    ★ 口径只有一个:墙钟 = 忙碌区间并集(该类调用真实在飞的净时长,阶段间空档
    不计;见 vlm_client.latency_summary 的 wall_s)。**不存在**"次数 ×
    均值"的回退画法:我们是并发跑的,那个乘积是几十倍的高估,画出来就是误导。
    没有墙钟数据(老交付)= 不画图 + 一句说明。
    """
    lat = perf["latency"]
    # 总墙钟(2026-08-06 用户点名):整次 run 从启动到交付可用的真实流逝时间,
    # 含 CPU 检查/VLM/导出/落盘回验——回答"这批到底跑了多久"的唯一口径。
    tw = perf.get("total_wall_s")
    total_line = (f"<br>整次运行总墙钟 <b>{human_duration(tw)}</b>"
                  f"(含全部阶段与交付落盘)" if tw else "")
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
            '各类调用的墙钟耗时(忙碌区间并集:该类调用真实在飞的净时长,空档不计)'
        + total_line + '</div>'
            + "".join(bars)
            + '<div style="font:12px system-ui;color:#777;margin-top:6px">'
              '各类调用之间在时间上可能重叠,各条墙钟相加 ≠ 整次运行总时长。</div>'
            '</div>')


# ── 人工裁决:实现在 dataset_level/decisions.py(与 rejudge 命令共用同一份)。
#    那是纯文件 IO 层,不是管道——UI 不 import 管道的红线在此不破。 ──
from ..dataset_level.decisions import (DECISION_CHOICES, DECISIONS_CSV,  # noqa: F401
                                       VERDICT_CHOICES, VERDICTS_CSV,
                                       record_label_decision, record_task_verdict)
from ..dataset_level.decisions import load_label_decisions as _load_decisions
from ..dataset_level.decisions import load_task_verdicts as _load_verdicts


def load_label_decisions(m: dict) -> dict:
    return _load_decisions(m["path"])


def load_task_verdicts(m: dict) -> dict:
    return _load_verdicts(m["path"])


def audit_clip_paths(m: dict, episode_id: str) -> list[str]:
    """该分歧条目的视频片段(details/audit_clips/<ep>__<cam>.mp4,按相机名排序)。"""
    d = os.path.join(m["path"], "details", "audit_clips")
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d)
                  if f.startswith(episode_id + "__") and f.endswith(".mp4"))


# ───────── Episodes 页重做 + 同步曲线页(2026-08-07)─────────
#
# 起因(用户原话):被拒展示"目前的显示很差"。根因是小尺寸证据帧和**超宽的同步
# 曲线长图**被塞进同一个 4 列画廊,曲线被压成四分之一格 → 必然糊成一条。
# 重做的三条硬规矩:
#   ① 证据分家:小图走多列画廊,长条曲线图走**整幅宽度**的独立组件,永不混排;
#   ② 判决先行:被拒条目最该一眼看到的是"哪一维把它毙了、为什么",不是一堆缩略图;
#   ③ 一切降级:老交付没有逐相机同步数据,缺字段时说一句人话,绝不崩、绝不编。
#
# 同步语义(用户拍板,展示文案与之严格一致,别自造说法):
#   · 同步检查**永不废弃相机** —— 发现异常只**标注**;判废只在 episode 层面,
#     且仅当"所有可信相机一致指向同一个偏移"(verdict=misaligned_all);
#   · 弃权/测不准(undecidable)**不进人工裁决队列**、**不参与综合软分**,是个标注。

#: 交付里同步检查的中文名。report.py 的 CHECK_CN 写「视频-动作同步」,进度条等处
#: 出现过无连字符的写法 —— 读端两个都认(UI 不 import 管道,只能在此留常量副本;
#: 与 TASK_CHECK_CN 同一套办法)。
SYNC_CHECK_NAMES = ("视频-动作同步", "视频动作同步")

#: 老交付(2026-08-07 之前)的 detail 只有平铺的 lag_s/corr_peak,没有 per_camera。
#: 缺字段时统一这一句 —— 让人知道是交付旧,而不是系统坏了。
LEGACY_SYNC_NOTE = "此交付无逐相机同步数据(旧版本)"

#: 同步判决 → (徽章文字, 主色, 底色, 一句人话)。四态的措辞就是上面那段语义的
#: 界面化,改这里等于改对客户的承诺,改前先看那段注释。
SYNC_VERDICT_TEXT = {
    "aligned": ("同步正常", "#166534", "#dcfce7",
                "各可信相机与动作时序对齐,未发现异常。"),
    "misaligned_all": ("整体错位(判废)", "#991b1b", "#fee2e2",
                       "所有可信相机一致指向同一个偏移 —— 这是判废的唯一条件,"
                       "发生在 episode 层面(不是废掉某个相机)。"),
    "annotated": ("已标注异常(不判废)", "#92400e", "#fef3c7",
                  "个别相机读数异常,已标注;同步检查永不废弃相机,也不因此判废这条数据。"),
    # verdict 仍是 annotated,但成因是"疑似错位、证据不足"时换个说法——
    # 用户看曲线时最想区分的正是这两者(2026-08-07)
    "_annotated_suspect": ("疑似错位(证据不足)", "#92400e", "#fef3c7",
                           "有相机的互相关峰明显偏离 0,但峰形不够可信,不足以定论:"
                           "只标注,不判废、不进人工裁决队列。"),
    "undecidable": ("测不准(弃权)", "#3730a3", "#e0e7ff",
                    "信号不足以判定同步,只作标注:不进人工裁决队列,也不参与综合软分。"),
}

#: 老交付没有 verdict 字段,只有检查三态 —— 退回讲整体结论,并注明是旧版本。
_SYNC_STATE_TEXT = {
    "pass": ("同步通过", "#166534", "#dcfce7", "旧版本交付只有整体结论。"),
    "拒绝": ("同步不通过", "#991b1b", "#fee2e2", "旧版本交付只有整体结论。"),
    "弃权": ("弃权", "#3730a3", "#e0e7ff",
             "旧版本交付只有整体结论;弃权不参与综合软分。"),
}
_SYNC_UNKNOWN = ("同步结论未知", "#374151", "#f3f4f6", "此条没有同步检查读数。")

#: 判决 → (徽章文字, 主色, 底色, 边色)。绿=通过、红=拒绝、琥珀=待裁决;
#: 与「人工裁决」页的 ① 橙 ② 蓝区块色错开,不会被误读成同一套分类。
VERDICT_STYLES = {
    "通过": ("✅ 通过", "#166534", "#dcfce7", "#86efac"),
    "拒绝": ("⛔ 拒绝", "#991b1b", "#fee2e2", "#fca5a5"),
    "待裁决": ("⏳ 待裁决", "#92400e", "#fef3c7", "#fcd34d"),
}
_VERDICT_UNKNOWN = ("判决未知", "#374151", "#f3f4f6", "#d1d5db")


def _fmt_num(v, nd: int = 3) -> str:
    """读数格式化。缺测 → 「—」;**不写 0**:0 是个有意义的滞后值,与"没测出来"
    是两回事,混在一起会让人以为对齐得很好。"""
    if v is None or isinstance(v, bool):
        return "—"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _sync_check_of(ep: dict) -> dict:
    """episode → 同步检查条目(名字两种写法都认;没有返回 {})。"""
    checks = (ep or {}).get("checks") or {}
    for name in SYNC_CHECK_NAMES:
        if name in checks:
            return checks[name] or {}
    return {}


def sync_check(m: dict, eid: str) -> dict:
    return _sync_check_of((m.get("episodes") or {}).get(eid or "") or {})


def sync_detail(m: dict, eid: str) -> dict:
    """同步检查的 detail(容忍双重编码;没有该检查 → {})。"""
    chk = sync_check(m, eid)
    return _deep_detail(chk.get("detail")) if chk else {}


def sync_badge(detail: dict, state: str = "") -> tuple[str, str, str, str]:
    """(徽章文字, 主色, 底色, 一句人话)。新契约的 verdict 优先,老交付退回三态。"""
    d = detail or {}
    v = d.get("verdict")
    if v == "annotated" and d.get("suspect_cameras") and not d.get("flagged_cameras"):
        return SYNC_VERDICT_TEXT["_annotated_suspect"]
    if v in SYNC_VERDICT_TEXT:
        return SYNC_VERDICT_TEXT[v]
    if state in _SYNC_STATE_TEXT:
        return _SYNC_STATE_TEXT[state]
    return _SYNC_UNKNOWN


SYNC_CAM_HEADERS = ["相机", "标注", "滞后(秒)", "相关峰值", "零偏相关",
                    "峰值比", "峰宽(秒)", "可信", "说明"]


def sync_camera_rows(m: dict, eid: str) -> list[list]:
    """逐相机同步读数 → 表格行。老交付无 per_camera → 空列表(上层给降级说明)。"""
    d = sync_detail(m, eid)
    per = d.get("per_camera")
    if not isinstance(per, dict) or not per:
        return []
    flagged = set(d.get("flagged_cameras") or [])
    rows = []
    for cam in sorted(per):
        c = per[cam] if isinstance(per[cam], dict) else {}
        note = c.get("note") or c.get("code") or ""
        rows.append([cam, "⚠ 已标注" if cam in flagged else "",
                     _fmt_num(c.get("lag_s")), _fmt_num(c.get("corr_peak")),
                     _fmt_num(c.get("corr_at_zero")), _fmt_num(c.get("peak_ratio"), 2),
                     _fmt_num(c.get("peak_width_s"), 2),
                     "可信" if c.get("trusted") else "不可信", str(note)])
    return rows


def _cell(txt: str, *, bold: bool = False, color: str = "#333",
          align: str = "left") -> str:
    return (f'<td style="padding:5px 9px;border-bottom:1px solid #eceff3;color:{color};'
            f'text-align:{align}{";font-weight:700" if bold else ""}">{_esc(txt)}</td>')


def _table_html(headers: list, rows: list[list], marks: list[bool],
                mark_color: str = "#fef3c7") -> str:
    """通用小表:marks[i] 为真的行整行着色 + 左侧色条(被标注/被拒的那行要跳出来)。"""
    head = "".join(f'<th style="padding:5px 9px;text-align:left;font-weight:700;'
                   f'color:#475569;border-bottom:2px solid #cbd5e1;white-space:nowrap">'
                   f'{_esc(h)}</th>' for h in headers)
    body = []
    for i, r in enumerate(rows):
        hit = i < len(marks) and marks[i]
        style = (f'background:{mark_color};box-shadow:inset 3px 0 0 0 #f59e0b'
                 if hit else "")
        body.append(f'<tr style="{style}">'
                    + "".join(_cell(c, bold=(hit and j == 0)) for j, c in enumerate(r))
                    + "</tr>")
    return ('<table style="border-collapse:collapse;width:100%;max-width:960px;'
            'font:12px/1.6 system-ui;margin:4px 0 10px">'
            f'<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>')


def sync_camera_html(m: dict, eid: str) -> str:
    """Episodes 页的逐相机同步读数块(徽章 + 摘要 + 表;老交付一句降级说明)。"""
    chk = sync_check(m, eid)
    if not chk:
        return ('<p style="color:#777;font:12px/1.6 system-ui">'
                '此条没有视频-动作同步读数(该检查未启用,或漏斗更早的硬门已短路)。</p>')
    d = sync_detail(m, eid)
    txt, fg, bg, why = sync_badge(d, chk.get("state", ""))
    bits = []
    if d.get("consensus_lag_s") is not None:
        bits.append(f"一致偏移 {_fmt_num(d.get('consensus_lag_s'))} 秒")
    if d.get("n_cameras") is not None:
        bits.append(f"相机 {d.get('n_cameras')} 路"
                    + (f"(可信 {d.get('n_trusted')} 路)"
                       if d.get("n_trusted") is not None else ""))
    if d.get("flagged_cameras"):
        bits.append("已标注相机:" + "、".join(str(c) for c in d["flagged_cameras"]))
    if d.get("reason"):
        bits.append(str(d["reason"]))
    summary = (f'<div style="font:12px/1.6 system-ui;color:#555;margin:2px 0 6px">'
               f'{_esc(" · ".join(bits))}</div>' if bits else "")
    rows = sync_camera_rows(m, eid)
    if rows:
        flagged = set(d.get("flagged_cameras") or [])
        table = _table_html(SYNC_CAM_HEADERS, rows, [r[0] in flagged for r in rows])
    else:
        # 老交付:只有平铺读数(lag_s/corr_peak),照实摊开,并说清为什么没有逐相机
        flat = " · ".join(f"{k}={_fmt_num(d[k])}" for k in ("lag_s", "corr_peak")
                          if d.get(k) is not None)
        table = (f'<div style="font:12px/1.6 system-ui;color:#777;background:#f8fafc;'
                 f'border-left:3px solid #cbd5e1;padding:6px 10px;max-width:960px">'
                 f'{LEGACY_SYNC_NOTE}'
                 + (f'。本条整体读数:{_esc(flat)}' if flat else "") + '</div>')
    return ('<div style="margin-top:6px">'
            '<div style="font:13px/1.6 system-ui;font-weight:700;color:#334155;'
            'margin-bottom:4px">视频-动作同步(逐相机读数)</div>'
            f'<span style="background:{bg};color:{fg};border-radius:999px;'
            f'padding:2px 12px;font:12px/1.8 system-ui;font-weight:700">{_esc(txt)}</span>'
            f'<span style="color:#64748b;font:12px/1.8 system-ui;margin-left:8px">'
            f'{_esc(why)}</span>'
            + summary + table + '</div>')


def episode_verdict_label(ep: dict) -> str:
    """卡片头上的那三个字。待裁决优先于通过/拒绝 —— 系统还没定论时,先告诉人
    "该你上了",而不是显示一个随时会变的当前判决。"""
    if (ep or {}).get("pending"):
        return "待裁决"
    return (ep or {}).get("verdict") or "?"


def fatal_checks(m: dict, eid: str) -> list[str]:
    """把这条数据毙掉的检查(state == 拒绝)。可能不止一维,按 checks 顺序返回。"""
    ep = (m.get("episodes") or {}).get(eid or "") or {}
    return [name for name, c in (ep.get("checks") or {}).items()
            if (c or {}).get("state") == "拒绝"]


def episode_reason_text(m: dict, eid: str) -> str:
    """一句人话原因:优先用致命检查自己写的 reason,其次交付里的拒绝原因。"""
    ep = (m.get("episodes") or {}).get(eid or "") or {}
    for name in fatal_checks(m, eid):
        d = (ep.get("checks") or {}).get(name, {}).get("detail") or {}
        d = _deep_detail(d)
        why = d.get("reason") or d.get("verdict")
        if why:
            return str(why)
    return str(ep.get("reject_reason") or "")


def episode_card_html(m: dict, eid: str) -> str:
    """选中 episode 的判决卡:判决 / 致命项是哪一维 / 一句人话 / 综合软分。

    被拒条目的第一屏就该回答"谁毙的、为什么",所以这张卡在证据之前。
    """
    ep = (m.get("episodes") or {}).get(eid or "") or {}
    if not eid or not ep:
        return ('<div style="border:1px dashed #cbd5e1;border-radius:10px;padding:14px 18px;'
                'color:#64748b;font:13px/1.6 system-ui">'
                '在上方列表里选一条 episode,这里会显示它的判决、致命项与证据。</div>')
    label = episode_verdict_label(ep)
    txt, fg, bg, bd = VERDICT_STYLES.get(label, _VERDICT_UNKNOWN)
    score = ep.get("soft_score")
    score_txt = _fmt_num(score) if score is not None else "未记录"
    fatal = fatal_checks(m, eid)
    if fatal:
        fatal_html = "".join(
            f'<span style="background:#fee2e2;color:#991b1b;border:1px solid #fca5a5;'
            f'border-radius:6px;padding:1px 9px;margin-right:6px;font-weight:700">'
            f'{_esc(n)}</span>' for n in fatal)
        fatal_line = f'<b style="color:#991b1b">致命项</b> {fatal_html}'
    elif label == "待裁决":
        fatal_line = ('<b style="color:#92400e">待裁决项</b> '
                      + "".join(
                          f'<span style="background:#fef3c7;color:#92400e;'
                          f'border:1px solid #fcd34d;border-radius:6px;padding:1px 9px;'
                          f'margin-right:6px;font-weight:700">{_esc(n)}</span>'
                          for n in (ep.get("pending") or [])))
    else:
        fatal_line = '<span style="color:#166534">没有任何一维投拒绝(未被硬门拦下)</span>'
    reason = episode_reason_text(m, eid)
    reason_html = (f'<div style="margin-top:8px;font:13px/1.7 system-ui;color:#334155">'
                   f'<b>原因</b>:{_esc(reason)}</div>' if reason else "")
    abstain = "".join(
        f'<div style="margin-top:4px;font:12px/1.6 system-ui;color:#78716c">'
        f'「{_esc(chk)}」弃权:{_esc(why)}</div>'
        for chk, why in (ep.get("abstain_reasons") or {}).items())
    # 同步弃权是个标注:它既不进裁决队列也不进软分,卡片上讲一句,免得客户
    # 看到"弃权"二字以为这条数据被扣了分。
    footnote = ""
    if sync_detail(m, eid).get("verdict") == "undecidable":
        footnote = ('<div style="margin-top:8px;font:12px/1.6 system-ui;color:#3730a3;'
                    'background:#eef2ff;border-radius:6px;padding:5px 10px">'
                    '同步测不准仅作标注:不进人工裁决队列,也不参与综合软分。</div>')
    return (f'<div style="background:{bg};border:1px solid {bd};border-left:6px solid {fg};'
            f'border-radius:10px;padding:14px 18px;margin:4px 0 10px">'
            f'<div style="display:flex;flex-wrap:wrap;align-items:baseline;gap:12px">'
            f'<span style="font-size:1.25rem;font-weight:800;color:{fg}">{_esc(txt)}</span>'
            f'<span style="font:14px/1.6 ui-monospace,monospace;color:#334155;'
            f'font-weight:700">{_esc(eid)}</span>'
            f'<span style="margin-left:auto;background:#fff;border:1px solid #e2e8f0;'
            f'border-radius:999px;padding:2px 12px;font:12px/1.8 system-ui;color:#475569">'
            f'综合软分 <b style="color:#0f172a">{_esc(score_txt)}</b></span></div>'
            f'<div style="margin-top:9px;font:13px/1.9 system-ui">{fatal_line}</div>'
            + reason_html + abstain + footnote + '</div>')


def check_table_html(m: dict, eid: str) -> str:
    """逐维检查读数表(数据仍来自 check_rows),投「拒绝」的那一维整行标红。

    为什么不用 Dataframe:Dataframe 没法把某一行做视觉强调,而"被拒的是哪一维"
    正是这页最该一眼看到的信息。
    """
    rows = check_rows(m, eid) if eid else []
    if not rows:
        return ('<p style="color:#777;font:12px/1.6 system-ui">'
                '此条没有逐维检查读数(老交付,或漏斗更早阶段已短路)。</p>')
    marks = [r[1] == "拒绝" for r in rows]
    return ('<div style="font:13px/1.6 system-ui;font-weight:700;color:#334155;'
            'margin-top:4px">各维检查读数(标红=把这条毙掉的那一维)</div>'
            + _table_html(CHECK_HEADERS, [[str(c) for c in r] for r in rows],
                          marks, mark_color="#fee2e2"))


# ── 同步曲线页(2026-08-07 新增):details/plots/<ep>_sync.png 的画廊 ──

SYNC_FILTER_ALL = "全部"
SYNC_FILTER_FLAGGED = "只看有标注/异常的"
SYNC_FILTERS = [SYNC_FILTER_ALL, SYNC_FILTER_FLAGGED]

#: 一页多少张。曲线是宽长图,一屏塞太多既看不清也拖慢首屏;分页比懒加载简单且
#: 零 JS(本模块的一贯做法)。
SYNC_PAGE_SIZE = 20      # 每页张数:曲线是整幅宽度的长图,平铺往下滚最顺手
                         # (2026-08-07 用户定)。翻页只是"图多到塞不下"时的兜底,
                         # 一页放得下时 UI 会把翻页件整排隐藏

#: 交付里没有 plots 时的说明。必须点名开关,否则客户会以为功能坏了。
NO_PLOTS_NOTE = (
    "此交付没有同步曲线图(`details/plots/` 为空)。曲线画不画由配置 "
    "`pipeline.sync_plots` 控制:`flagged`=只给非「过」的条目画(默认)、"
    "`all`=全画、`off`=不画。")


def _sync_flagged(ep: dict, detail: dict, state: str) -> bool:
    """这条曲线值不值得人看:新契约看 verdict/flagged_cameras,老交付退回检查三态
    与 episode 判决(老交付本来也只在非「过」时才画图)。"""
    v = detail.get("verdict")
    if v in SYNC_VERDICT_TEXT:
        # 判 aligned 但有相机被诊断出毛病的(假峰/疑似错位/无信号),同样要进筛选:
        # ep4 就是这种——整条结论没问题,可那一路的峰肉眼可见地偏了,用户第一个
        # 想复查的就是它(2026-08-07)。
        return (v != "aligned"
                or bool(detail.get("flagged_cameras"))
                or bool(detail.get("suspect_cameras"))
                or bool(detail.get("noisy_cameras")))
    if detail.get("flagged_cameras"):
        return True
    if state in ("拒绝", "弃权"):
        return True
    return ep.get("verdict") == "拒绝" or bool(ep.get("pending"))


#: 诊断标签 → 圆点颜色。对齐=绿、错位=红、测不准=琥珀(需注意但不是罪证)、
#: 无信号=灰。与页面其它徽章同一套语义,不另造配色。
_DIAG_COLOR = {"aligned": "#16a34a", "misaligned": "#dc2626",
               "false_peak": "#f59e0b", "blurry_motion": "#f59e0b",
               "rival_lags": "#f59e0b", "weak_signal": "#f59e0b",
               "partial_visibility": "#f59e0b",
               "no_motion": "#94a3b8"}


def _diag_rows(detail: dict) -> list[dict]:
    """同步 detail → 逐相机诊断行(纯数据)。老交付没有 diagnosis → 退回 note。"""
    per = (detail or {}).get("per_camera")
    if not isinstance(per, dict):
        return []
    rows = []
    for cam in sorted(per):
        r = per[cam] if isinstance(per[cam], dict) else {}
        dg = r.get("diagnosis") if isinstance(r.get("diagnosis"), dict) else {}
        cause = str(dg.get("cause") or ("aligned" if r.get("trusted") else ""))
        lag = r.get("lag_s")
        rows.append({
            "cam": cam,
            "lag": "—" if lag is None else f"{float(lag):+.2f}s",
            "label": str(dg.get("label") or ("对齐" if r.get("trusted") else "测不准")),
            "text": str(dg.get("text") or r.get("note") or ""),
            "advice": str(dg.get("advice") or ""),
            "color": _DIAG_COLOR.get(cause, "#64748b"),
        })
    return rows


def sync_diag_html(items: list) -> str:
    """逐相机诊断框(每张曲线右侧)。**说病因,不说笼统结论**——
    用户 2026-08-07:"你不是 instruction 都说'又矮又胖'的情况是啥了嘛,
    难道这种情况不是'又矮又胖'背后的问题吗?你得给出正确的诊断啊"。"""
    if not items:
        return ""
    out = ['<div class="sync-diag-title">逐相机诊断</div>']
    for r in items:
        out.append(
            f'<div class="sync-diag-row">'
            f'<div class="sync-diag-head">'
            f'<span class="sync-dot" style="background:{r["color"]}"></span>'
            f'<b>{_esc(r["cam"])}</b>'
            f'<span class="sync-diag-lag">{_esc(r["lag"])}</span></div>'
            f'<div class="sync-diag-label" style="color:{r["color"]}">'
            f'{_esc(r["label"])}</div>'
            f'<div class="sync-diag-text">{_md_bold(r["text"])}</div>'
            + (f'<div class="sync-diag-advice">→ {_esc(r["advice"])}</div>'
               if r["advice"] else "")
            + "</div>")
    return "".join(out)


def sync_plot_items(m: dict, mode: str = SYNC_FILTER_ALL) -> list[dict]:
    """有曲线图的 episode → [{id, path, badge, color, flagged}](纯数据,不产 HTML)。"""
    out = []
    for eid, ep in sorted((m.get("episodes") or {}).items()):
        if not ep.get("plot"):
            continue
        chk = _sync_check_of(ep)
        d = _deep_detail(chk.get("detail")) if chk else {}
        txt, fg, _bg, _why = sync_badge(d, chk.get("state", ""))
        flagged = _sync_flagged(ep, d, chk.get("state", ""))
        out.append({"id": eid, "path": ep["plot"], "badge": txt,
                    "color": fg, "flagged": flagged,
                    "cameras": _diag_rows(d), "reason": str(d.get("reason") or "")})
    if mode == SYNC_FILTER_FLAGGED:
        return [it for it in out if it["flagged"]]
    return out


def sync_plots_mode(m: dict) -> str:
    """本次质检画曲线的范围:all / flagged / off(读不到 → "")。"""
    pl = ((m.get("config_effective") or {}).get("pipeline") or {})
    return str(pl.get("sync_plots") or "")


def sync_checked_count(m: dict) -> int:
    """做过同步检查的 episode 条数(不管有没有画曲线)。"""
    return sum(1 for ep in (m.get("episodes") or {}).values() if _sync_check_of(ep))


def sync_coverage_note(m: dict, n_plots: int) -> str:
    """「全部」到底是全部什么?—— 覆盖范围说明。

    2026-08-07 用户问:"假如跑质检时没设 all-plots,只有有问题的 episode 有图,
    我点了「全部」会发生什么?" 答案是只会看到那几张,而页面从前只说"共 N 张曲线",
    读起来像全库只有 N 条 —— 会让人误以为其余 episode 没被检查。这里说破。
    """
    mode = sync_plots_mode(m)
    n_ck = sync_checked_count(m)
    if mode == "flagged" or (mode != "all" and n_ck > n_plots > 0):
        miss = max(0, n_ck - n_plots)
        return (f"\n\n⚠️ 本次质检**只为需要留意的条目画了曲线**"
                f"(配置 `pipeline.sync_plots = flagged`):{n_ck} 条 episode 都做了"
                f"同步检查,但只有 {n_plots} 条有图,其余 {miss} 条同步正常、未出图。"
                f"这里的「全部」= 全部**已出图**的曲线,不是全部 episode。"
                f"要逐条都看,请加 `--set pipeline.sync_plots=all` 重跑。")
    return ""


def sync_view(m: dict, mode: str = SYNC_FILTER_ALL, page: int = 0,
              page_size: int = SYNC_PAGE_SIZE) -> dict:
    """同步曲线页的一屏:{page, pages, items, note, pos}。

    items = [(图片路径, "epXXXXXX · 徽章")] —— 直接喂 gr.Gallery(自带点击放大),
    每张的标题就是 episode 号 + 该条的同步判定徽章。页码越界回绕(与裁决卡片一致)。
    """
    items = sync_plot_items(m, mode)  # [{id,path,badge,color,flagged}]
    n_all = len(sync_plot_items(m, SYNC_FILTER_ALL))
    pages = max(1, (len(items) + page_size - 1) // page_size)
    page = (page or 0) % pages
    shown = items[page * page_size:(page + 1) * page_size]
    if not n_all:
        note = NO_PLOTS_NOTE
    elif not items:
        note = (f"本交付的 {n_all} 张曲线里没有被标注/异常的条目 —— "
                "切到「全部」可以逐条看。")
    else:
        note = f"共 **{len(items)}** 张曲线"
        if mode == SYNC_FILTER_FLAGGED and n_all > len(items):
            note += f"(另有 {n_all - len(items)} 张同步正常的未列出)"
        note += ";点任意一张放大,标题里的徽章是该条的同步判定。"
        note += sync_coverage_note(m, n_all)
    pos = f"第 {page + 1} / {pages} 页" if pages > 1 else ""
    return {"page": page, "pages": pages,
            "items": [(it["path"], f'{it["id"]} · {it["badge"]}') for it in shown],
            "cards": sync_cards_html(shown),      # 自绘卡片(一行一张,点图开原图)
            "note": note, "pos": pos}


#: 表头一律说人话(2026-08-07 用户:"四分位距是个啥?能改不")。"四分位距"是统计
#: 黑话,它在这里的意思就是"这一路的滞后在各条之间跳得厉害不厉害"——直接写那个意思。
SYNC_HEALTH_HEADERS = ["相机", "有效读数", "典型滞后(秒)", "逐条波动(秒)",
                       "疑似错位", "测不准", "已标注"]


def sync_health_rows(m: dict) -> list[list]:
    """数据集级 lag 分布(dataset.sync_health.per_camera)→ 表格行;老交付空表。"""
    h = (m.get("dataset") or {}).get("sync_health") or {}
    per = h.get("per_camera")
    if not isinstance(per, dict):
        return []
    rows = []
    for cam in sorted(per):
        s = per[cam] if isinstance(per[cam], dict) else {}
        rows.append([cam, s.get("n", "—"), _fmt_num(s.get("median_lag_s")),
                     _fmt_num(s.get("iqr_s")),
                     s.get("n_suspect", "—"), s.get("n_abstained", "—"),
                     s.get("n_flagged", "—")])
    return rows


def sync_health_marks(m: dict) -> list[bool]:
    """哪几行该高亮:典型滞后超容差,或有疑似错位的条目。"""
    h = (m.get("dataset") or {}).get("sync_health") or {}
    per = h.get("per_camera")
    if not isinstance(per, dict):
        return []
    marks = []
    for cam in sorted(per):
        s = per[cam] if isinstance(per[cam], dict) else {}
        med = s.get("median_lag_s")
        marks.append(bool(s.get("n_suspect"))
                     or (med is not None and abs(float(med)) > 0.25))
    return marks


#: 「同步曲线」页顶部的结论横幅 + 读图指南(2026-08-07 用户点名:
#: "用户就看到一堆曲线,能得到什么提示呢")。原来的建议埋在整页图之后,
#: 滚不到 = 等于没有。现在结论先行:先说这份数据集同步得怎么样、**该怎么办**,
#: 曲线退居为证据。
SYNC_HOWTO = (
    "**怎么看这些图**:左边每格一路相机——蓝线=画面在动的程度,红线=机械臂在动的"
    "程度,两条同步起伏才对;右边一格是所有相机的互相关曲线,**峰落在绿带(0±0.25s)"
    "内就是对齐**。峰又矮又胖 = 这一路测不准(背景有人走动/相机晃动都会这样),"
    "系统不会拿它下结论。")


def sync_conclusion(m: dict) -> dict:
    """数据集级同步结论 → {level, title, points}(纯数据,便于单测)。

    level: ok / notice / attention —— 只影响配色,不代表判废(同步永不废相机,
    判废只在"所有可信相机一致指向同一偏移"这一种情形,且已在漏斗里发生过了)。
    """
    eps = m.get("episodes") or {}
    h = (m.get("dataset") or {}).get("sync_health") or {}
    n_plot = n_flag = n_undec = n_killed = n_suspect = n_noisy = 0
    for eid, ep in eps.items():
        chk = _sync_check_of(ep)
        if not chk:
            continue
        d = _deep_detail(chk.get("detail")) or {}
        v = d.get("verdict")
        if v == "undecidable":
            n_undec += 1
        if v == "misaligned_all" or chk.get("state") == "拒绝":
            n_killed += 1
        if d.get("suspect_cameras"):
            n_suspect += 1
        if d.get("noisy_cameras"):
            n_noisy += 1
        # 「被标注异常」只算**真有问题**的路(可信且错位、或完全无信号)。
        # 假峰/测不准另有说法,混进来会让横幅自相矛盾:标题说"同步正常"、
        # 条目却说"有相机被标注异常"(2026-08-07 实见)。
        if d.get("flagged_cameras") or (not d and _sync_flagged(ep, d,
                                                               chk.get("state", ""))):
            n_flag += 1
        if ep.get("plot"):
            n_plot += 1
    n_all = sum(1 for ep in eps.values() if _sync_check_of(ep))
    points: list[str] = []
    level, title = "ok", "同步正常:未发现视频与动作错位"
    if n_noisy:
        title = (f"同步正常:未发现错位;{n_noisy} 条有相机因画面干扰测不准"
                 f"(证据仍偏向对齐)")
    if n_flag:
        level, title = "notice", f"{n_flag} 条有相机被标注异常(不判废)"
    if n_suspect:
        level, title = "notice", (f"{n_suspect} 条有相机疑似错位(证据不足,"
                                  f"不判废)")

    if n_killed:
        level, title = "attention", f"{n_killed} 条因整条错位被判废"
        points.append("判废条件极严:**所有可信相机一致指向同一个偏移**才杀——"
                      "这通常意味着录制管线的时间轴出了问题,而不是某一路相机的毛病。")
    if h.get("negative_lag_episodes"):
        level = "attention"
        points.append("出现**负滞后**(画面早于动作)。这在物理上没有良性解释,"
                      "多半来自数据装配环节(格式转换错行、episode 边界切错、开头掉帧)"
                      "——建议回查转换流程。")
    if n_flag:
        level = "notice" if level == "ok" else level
        points.append(f"**{n_flag} 条**有相机被标注异常。**视频一路没删、数据照常交付**;"
                      "如果要拿这些条目做逐帧对齐敏感的训练,建议对被标注的那一路降权或不用。")
    if n_noisy:
        points.append(
            f"**{n_noisy} 条**的某一路测出了偏移,但把画面与动作错开和**完全不错开**"
            "几乎同样像 —— 这是画面干扰造成的**假峰**,证据其实偏向对齐,不是错位。"
            "逐条曲线右侧的诊断框写明了是哪一路、本条实测到的成因、该怎么改善。")
    if n_suspect:
        level = "notice" if level == "ok" else level
        points.append(
            f"**{n_suspect} 条**有相机**疑似错位但证据不足**(测出的偏移明显不在"
            "零点,但证据还不够硬)。系统**不判废也不进人工队列**,但这类条目值得"
            "抽查:如果同一路反复出现,多半是真延时。")
    if n_undec:
        points.append(f"**{n_undec} 条测不准**(背景干扰大、动作幅度小或静止段长)。"
                      "测不准 **不是** 质量问题、不进人工队列、不参与打分——"
                      "只是这条数据上本方法没有判别力。")
    if h.get("advice"):
        points.append(str(h["advice"]))
    if not points:
        points.append("逐相机测量结果一致且都落在容差内,这份数据可直接用于"
                      "对时序精度敏感的训练(如模仿学习的逐帧配对)。")
    points.append(f"本页共 {n_plot} 张曲线(检查了 {n_all} 条 episode)——曲线是**证据**,"
                  "结论已写在上面,正常情况下不必逐张看。")
    return {"level": level, "title": title, "points": points}


_SYNC_LEVEL_STYLE = {
    "ok": ("#f0fdf4", "#16a34a", "#14532d", "✅"),
    "notice": ("#fffbeb", "#f59e0b", "#78350f", "🔎"),
    "attention": ("#fef2f2", "#dc2626", "#7f1d1d", "⚠️"),
}


def sync_conclusion_html(m: dict) -> str:
    """结论横幅(页面最顶):一句结论 + 该怎么办的要点。"""
    c = sync_conclusion(m)
    bg, line, fg, icon = _SYNC_LEVEL_STYLE.get(c["level"], _SYNC_LEVEL_STYLE["ok"])
    lis = "".join(f'<li style="margin:3px 0">{_md_bold(p)}</li>' for p in c["points"])
    return (f'<div style="background:{bg};border:1px solid {line};border-left:6px solid {line};'
            f'border-radius:10px;padding:13px 18px;margin:4px 0 10px">'
            f'<div style="font-weight:800;font-size:1.08rem;color:{fg};margin-bottom:6px">'
            f'{icon} {_esc(c["title"])}</div>'
            f'<ul style="margin:0;padding-left:20px;font:13px/1.75 system-ui;color:{fg}">'
            f"{lis}</ul></div>")


def _md_bold(s: str) -> str:
    """把 **粗体** 转成 <b>(结论文案里手写的强调),其余一律转义。"""
    import re as _re
    parts = _re.split(r"\*\*(.+?)\*\*", str(s))
    out = []
    for i, seg in enumerate(parts):
        out.append(f"<b>{_esc(seg)}</b>" if i % 2 else _esc(seg))
    return "".join(out)


def _file_url(path: str) -> str:
    """本地文件 → gradio 的静态文件 URL(交付目录已在 allowed_paths 里)。

    带 ?v=<mtime> 版本号:重画曲线是原地重写同名 PNG,FSX 重写有短暂读不到的
    空窗,页面赶上空窗会把破图缓存住(2026-08-07 用户实见 ep1/ep2 空白)。
    mtime 进 URL 后,文件一变 URL 就变,缓存天然失效。
    """
    from urllib.parse import quote
    p = str(path)
    try:
        ver = f"?v={int(os.stat(p).st_mtime)}"
    except OSError:
        ver = ""
    return "/gradio_api/file=" + quote(p) + ver

# 传输抖动自愈:失败后退避重试,最多 8 次(≈9s)。只改 ?v= 的值,不引入 & 字符
# ——属性里的 & 会被 HTML 解析器当实体开头,踩过一次不再踩。
_IMG_RETRY = (
    ' onerror="var n=+(this.dataset.n||0);if(n<8){this.dataset.n=n+1;var i=this;'
    "setTimeout(function(){i.src=i.src.replace(/[?]v=.*$/,'?v='+Date.now())},"
    '260*n+180)}"')


def sync_cards_html(items: list) -> str:
    """曲线卡片(一行一张,2026-08-07 用户定:两列太挤、图被压瘦)。

    自绘而非 gr.Gallery/gr.Image,原因两条:①要精细控制边框/间距/留白;
    ②放大要用**页内灯箱**——最初做成 <a target=_blank> 开新标签页,用户反馈
    "点开放大后就回不去了",改成 checkbox 灯箱:点图 → 全屏遮罩看大图,
    点遮罩任意处关掉,不离开页面。纯 CSS(label+checkbox),不依赖 JS——
    gr.HTML 走 innerHTML 注入,<script> 不执行,CSS 机关永远好使。

    图片加载防线(2026-08-07 实锤,三张图长期空白):同页多张 200KB+ 的图并发拉,
    `kubectl port-forward` 会掐掉部分流(转发日志 broken pipe / connection reset),
    浏览器把失败结果记死,刷新也不好。两道防线:
      · 灯箱大图 loading=lazy —— 藏着不发请求,并发请求量直接减半;
      · onerror 自动重试(换 v 值绕缓存,退避到 8 次)—— 传输抖动自愈,
        不再需要人去硬刷新。内联事件属性经 innerHTML 注入是执行的(<script> 才不执行)。
    """
    if not items:
        return ""
    out = []
    for k, it in enumerate(items):
        url = _file_url(it["path"])
        lb = f"sync-lb-{k}"                       # 灯箱开关 id,页内唯一即可
        flag = it.get("flagged")
        accent = "#f59e0b" if flag else "#e2e8f0"
        out.append(
            f'<div class="sync-card">'
            f'<div class="sync-card-head" style="border-left:4px solid {accent}">'
            f'<span class="sync-eid">{_esc(it["id"])}</span>'
            f'<span class="sync-badge" style="color:{it.get("color") or "#475569"}">'
            f'{_esc(it.get("badge") or "")}</span>'
            f'<a class="sync-open" href="{url}" target="_blank" rel="noopener">'
            f"原图 ↗</a></div>"
            f'<div class="sync-card-body">'
            f'<label class="sync-figure" for="{lb}" title="点击放大">'
            f'<img class="sync-img" src="{url}" alt="{_esc(it["id"])}"{_IMG_RETRY}>'
            f"</label>"
            f'<div class="sync-diag">{sync_diag_html(it.get("cameras") or [])}'
            + (f'<div class="sync-diag-foot">{_md_bold(it["reason"])}</div>'
               if it.get("reason") else "")
            + "</div></div>"
            f'<input type="checkbox" id="{lb}" class="sync-lb-toggle">'
            f'<label for="{lb}" class="sync-lb" title="点击任意处关闭">'
            f'<img src="{url}" loading="lazy" alt="{_esc(it["id"])}"{_IMG_RETRY}>'
            f"</label></div>")
    return '<div class="sync-cards">' + "".join(out) + "</div>"


def sync_health_html(m: dict) -> str:
    """数据集级同步健康度:逐相机 lag 分布 + 系统给的建议。老交付整块降级一句话。"""
    h = (m.get("dataset") or {}).get("sync_health") or {}
    rows = sync_health_rows(m)
    if not rows and not h.get("advice"):
        return ('<p style="color:#777;font:12px/1.6 system-ui">'
                f'{LEGACY_SYNC_NOTE}——数据集级 lag 分布是新版本交付才统计的。</p>')
    parts = ['<div style="font:13px/1.6 system-ui;font-weight:700;color:#334155;'
             'margin-top:2px">全库逐相机同步概览</div>',
             '<div style="font:12px/1.7 system-ui;color:#64748b;margin:2px 0 6px">'
             '<b>典型滞后</b>=这一路画面比动作晚多少(正=画面晚,负=画面早,'
             '越接近 0 越好);<b>逐条波动</b>=各条 episode 之间这个数跳得厉不厉害'
             '(小=录制稳定,大=时快时慢);<b>疑似错位</b>=峰明显偏了但证据不够硬,'
             '不判废、只提醒;<b>测不准</b>=这一路信号不适合做此项判定。</div>']
    if rows:
        marks = sync_health_marks(m)
        parts.append(_table_html(SYNC_HEALTH_HEADERS,
                                 [[str(c) for c in r] for r in rows], marks))
    if h.get("advice"):
        parts.append('<div style="background:#fcf8e3;border-left:3px solid #f1c40f;'
                     'padding:7px 11px;max-width:960px;font:12px/1.7 system-ui;'
                     'color:#8a6d3b">建议:' + _md_bold(str(h["advice"])) + '</div>')
    neg = h.get("negative_lag_episodes") or []
    if neg:
        head = "、".join(str(e) for e in neg[:10])
        parts.append('<div style="font:12px/1.7 system-ui;color:#555;margin-top:6px">'
                     f'负滞后(画面先于动作)的条目 {len(neg)} 条:{_esc(head)}'
                     + ("…" if len(neg) > 10 else "") + '</div>')
    return "".join(parts)
