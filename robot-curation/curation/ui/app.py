"""Gradio Blocks 四 tab UI(薄壳层,2026-07-27 U1)。

分工:数据整形全在 manifest.py(纯函数,全测试);本文件只摆组件+接回调,
不做业务逻辑。gradio 是可选依赖——import 收在函数内,没装 gradio 时
`curation run`/`backends` 等一切照常。

四 tab:漏斗总览 / Episodes(点选看证据帧+VLM 理由=demo 高光页)/
技能画像 / 后端状态(复用 backends 探活)。

启动方式(2026-07-29 U4 改):不再 `blocks.launch()`,而是
`gr.mount_gradio_app(FastAPI(), blocks, "/")` + uvicorn 自跑——因为要在同一个端口上
挂自定义路由(`/ws/term` 内嵌终端 + `/term-static/` 前端资产 + `/healthz`)。
外部行为(端口/主题/页签/CSS/allowed_paths)与 launch() 时代逐项一致。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os

from .manifest import (AUDIT_HEADERS, CHECK_HEADERS, DECISION_CHOICES,
                       DETAIL_LABELS, TASK_REVIEW_HEADERS, VERDICT_CHOICES,
                       WORKFLOW_GUIDE, audit_clip_paths, audit_note_md,
                       load_label_decisions, load_task_verdicts,
                       record_label_decision, record_task_verdict,
                       EPISODE_HEADERS, FUNNEL_HEADERS, LATENCY_HEADERS,
                       LATENCY_KIND_NOTE, LATENCY_NOTE, LATENCY_PCTL_NOTE,
                       SKILL_HEADERS, audit_rows, check_rows,
                       discover_deliveries, episode_rows, funnel_rows,
                       latency_bar_html, latency_rows, list_detail_tables,
                       load_delivery, load_detail_table, load_perf,
                       load_timeline, overview_markdown, perf_backend_md,
                       perf_env_md, readings_text, skill_bar_html, skill_rows,
                       task_review_hint_md, task_review_rows, timeline_html)

log = logging.getLogger("curation.ui")

#: vendored 的 xterm.js 资产 + 我们的 term.js,由 `/term-static/` 静态目录服务。
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

#: 终端页签的前端装配:资产从**本服务**取(pod 内无 CDN 通路),只在开了终端时注入。
_TERMINAL_HEAD = """
<link rel="stylesheet" href="/term-static/xterm.css" />
<script src="/term-static/xterm.js"></script>
<script src="/term-static/addon-fit.js"></script>
<script src="/term-static/term.js"></script>
"""

_TERMINAL_CSS = """
#curation-term-screen {
  height: 78vh; width: 100%;
  background: #0b0f17; border-radius: 8px; padding: 8px 6px;
}
/* 2026-07-30 用户反馈:终端右侧有一条刺眼的白条——那是 xterm 滚动区
   (.xterm-viewport)的**浏览器默认滚动条**,白色轨道贴在深色终端上。
   改成与终端同色系:轨道融入背景,滑块深灰、悬停略亮。Firefox 走
   scrollbar-color,WebKit 系走 ::-webkit-scrollbar 三件套。 */
#curation-term-screen .xterm-viewport {
  scrollbar-color: #3a4556 #0b0f17;
  scrollbar-width: thin;
}
#curation-term-screen .xterm-viewport::-webkit-scrollbar { width: 10px; background: #0b0f17; }
#curation-term-screen .xterm-viewport::-webkit-scrollbar-track { background: #0b0f17; }
#curation-term-screen .xterm-viewport::-webkit-scrollbar-thumb {
  background: #3a4556; border-radius: 5px; border: 2px solid #0b0f17;
}
#curation-term-screen .xterm-viewport::-webkit-scrollbar-thumb:hover { background: #55627a; }
/* "字太淡"的真凶(2026-07-30 JS 实测):gradio 的 `.prose *` 把终端里**每一层**
   后代(行 div、字符 span)全染成 var(--body-text-color)(rgb(39,39,42) 深灰),
   压过 xterm 主题的继承——无论前景设什么,默认文字都是深灰。
   修法必须覆盖**整棵子树**(第一版只改 span 不够:span 的 inherit 会从紧邻的
   父级行 div 继承,而行 div 还是灰的——继承链断在中间)。带 xterm-fg- 类或
   内联色的 span 保持 ANSI 原色,不碰。 */
#curation-term-screen .xterm-rows *:not([class*="xterm-fg-"]) { color: inherit; }
"""



# 顶层导航按钮样式(2026-07-29 用户定:大、明显、立体)。只作用于 elem_id=topnav
# 的外层两页签,内层六个报告 tab 不受影响。立体感=渐变+外阴影(凸起),选中态=
# 橙色渐变+内阴影(按下)。选中类名在 gradio 版本间摇摆,selected/aria-selected 双保。
_TOPNAV_CSS = """
/* 只杀 gradio 给选中页签画的 ::after 橙色指示条(2px 绝对定位,贴在圆角
   按钮底部像条怪线);页签条自带的灰色分隔线保留(用户确认好看)。 */
#topnav > .tab-wrapper button::after,
#topnav > .tab-container button::after {
  display: none !important;
}
#topnav > .tab-wrapper, #topnav > .tab-container {
  border-bottom: 1px solid #d9d9d9 !important;   /* 用户确认要的灰色分隔线 */
}
#topnav > .tab-container button, #topnav > .tab-wrapper button {
  font-size: 1.3rem !important; font-weight: 700 !important;
  padding: 12px 34px !important; margin: 14px 10px 14px 0 !important;
  border: 1px solid #cfcfcf !important; border-radius: 12px !important;
  background: linear-gradient(180deg, #ffffff 0%, #e9e9e9 100%) !important;
  box-shadow: 0 3px 7px rgba(0,0,0,.20), inset 0 1px 0 rgba(255,255,255,.9) !important;
  color: #444 !important;
}
#topnav > .tab-container button.selected,
#topnav > .tab-wrapper button.selected,
#topnav button[aria-selected="true"] {
  background: linear-gradient(180deg, #ffd9b3 0%, #ff9e5e 100%) !important;
  border-color: #e8722a !important; color: #7c2d12 !important;
  box-shadow: inset 0 2px 5px rgba(0,0,0,.22) !important;
}
"""


def _config_yaml(m: dict) -> str:
    ce = m.get("config_effective")
    if not ce:
        return "(此交付无 config_effective 快照——U0 之前的老交付)"
    try:
        import yaml
        return yaml.safe_dump(ce, allow_unicode=True, sort_keys=False)
    except Exception:  # noqa: BLE001
        return json.dumps(ce, ensure_ascii=False, indent=1)


def _probe_backends(config_path: str | None, timeout: float) -> list[list]:
    """后端状态 tab 的数据源:复用 backends 子命令同款探活(config+list_models)。"""
    from ..adapters.vlm_client import list_models
    from ..pipeline.config import load_config

    rows = []
    try:
        cfg = load_config(config_path)
    except Exception as e:  # noqa: BLE001
        return [["(配置加载失败)", str(e), ""]]
    for name, b in sorted((cfg.get("vlm_backends") or {}).items()):
        try:
            models = list_models(b["endpoint"], b.get("api_key_env"), timeout_s=timeout)
            shown = ", ".join(models[:3]) + (f" …(共{len(models)}个)" if len(models) > 3 else "")
            rows.append([name, "✅在线", shown])
        except Exception as e:  # noqa: BLE001
            rows.append([name, "❌不可达", type(e).__name__])
    return rows



# 分歧队列的可点性提示(2026-08-05 用户反馈:怕用户不知道行能点):
# 悬停变手型 + 行高亮,配合首列「裁决 ▶」操作列,双保险。
_AUDIT_CSS = """
#audit-queue tbody tr, #task-queue tbody tr { cursor: pointer; }
#audit-queue tbody tr:hover td,
#task-queue tbody tr:hover td { background: rgba(255, 140, 0, 0.10) !important; }
"""

def presentation(terminal: bool = False) -> dict:
    """theme/css/head 三件套(gradio 6 起只认 launch()/mount_gradio_app() 上的这三个
    关键字,传给 `gr.Blocks()` 会被静默丢弃——2026-07-29 实测,顺手修掉的老 bug)。"""
    import gradio as gr

    return {
        # 系统字体:默认主题会向 fonts.googleapis.com 拉字体,国内网络挂起 15s+ 才放行
        # 首屏(实测),demo 一开场就是白屏等待——本 UI 场景里无网络字体的理由
        "theme": gr.themes.Default(font=["system-ui", "sans-serif"],
                                   font_mono=["ui-monospace", "Menlo", "monospace"]),
        "css": _AUDIT_CSS + ((_TOPNAV_CSS + _TERMINAL_CSS) if terminal else ""),
        "head": _TERMINAL_HEAD if terminal else None,
    }


# 「人工裁决」页的视觉件(2026-08-07 用户反馈:页面太平淡,引导和区块头要一眼看到)。
# 全部内联样式:不依赖主题变量,浅色页面直出;步骤链和区块头是"人要按顺序干活"
# 的导航件,值得比正文重一个视觉量级。①橙 ②蓝:两块任务性质不同,用色系分开。
_ADJ_GUIDE_HTML = """
<div style="background:linear-gradient(135deg,#fff7ed,#ffedd5);border:1px solid #fdba74;
            border-left:6px solid #ea580c;border-radius:10px;padding:14px 18px;margin:2px 0 6px">
  <div style="font-weight:700;color:#9a3412;font-size:1.05rem;margin-bottom:9px">
    📋 建议工作顺序(顺序错了会白裁)</div>
  <div style="display:flex;flex-wrap:wrap;gap:7px;align-items:center;font-size:.95rem;color:#431407">
    <span style="background:#fff;border:1px solid #fdba74;border-radius:999px;padding:3px 13px">
      <b style="color:#ea580c">1</b> 裁「标注分歧」</span><span style="color:#c2410c">→</span>
    <span style="background:#fff;border:1px solid #fdba74;border-radius:999px;padding:3px 13px">
      <b style="color:#ea580c">2</b> 跑 <code>curation rejudge</code>
      <span style="color:#9a3412">(部分弃权自动解决)</span></span><span style="color:#c2410c">→</span>
    <span style="background:#fff;border:1px solid #fdba74;border-radius:999px;padding:3px 13px">
      <b style="color:#ea580c">3</b> 裁剩余「任务成败弃权」</span><span style="color:#c2410c">→</span>
    <span style="background:#fff;border:1px solid #fdba74;border-radius:999px;padding:3px 13px">
      <b style="color:#ea580c">4</b> 再跑一次 <code>rejudge</code> 生效</span>
  </div>
  <div style="margin-top:9px;font-size:.86rem;color:#7c2d12">
    两块都只<b>记录</b>裁决(落交付目录 details/ 下的 CSV),可随时改判;
    真正修改交付的是命令行的 <code>curation rejudge</code>。</div>
</div>"""


def _adj_section_html(num: str, title: str, subtitle: str, color: str, dark: str) -> str:
    """区块头:色块序号 + 加粗标题 + 弱化副题,底部同色粗线把区块"框"出来。"""
    return (f'<div style="display:flex;align-items:baseline;gap:10px;margin:20px 0 4px;'
            f'padding-bottom:7px;border-bottom:3px solid {color}">'
            f'<span style="background:{color};color:#fff;font-weight:800;border-radius:8px;'
            f'padding:2px 13px;font-size:1.05rem">{num}</span>'
            f'<span style="font-size:1.18rem;font-weight:800;color:{dark}">{title}</span>'
            f'<span style="color:#78716c;font-size:.9rem">{subtitle}</span></div>')


def build_app(delivery: str, config_path: str | None = None, probe_timeout: float = 5.0,
              terminal: bool = False):
    """交付目录(或含多份交付的父目录)→ gr.Blocks。

    terminal=True 时套一层顶层导航:「终端」(内嵌 xterm.js,后端是本服务的
    `/ws/term`)+「质检报告」(= 本文件原有的全部内容),默认选中「质检报告」。
    缺省 False → 顶层导航整个不渲染,页面与加这层之前逐字一致(客户部署根本看不到
    终端入口),`/ws/term` 路由也不注册。
    """
    import gradio as gr

    choices = discover_deliveries(delivery)
    if not choices:
        raise SystemExit(f"目录里找不到交付(无 passed.json):{delivery}")

    def _detail(m, eid):
        if not m or not eid:
            return "", [], []
        ep = m["episodes"].get(eid) or {}
        md = [f"### {eid} — {ep.get('verdict', '?')}"]
        if ep.get("reject_reason"):
            md.append(f"**拒绝原因**:{ep['reject_reason']}")
        for chk, why in (ep.get("abstain_reasons") or {}).items():
            md.append(f"**{chk} 弃权**:{why}")
        gallery = [(f, os.path.basename(f)) for f in ep.get("evidence") or []]
        if ep.get("plot"):
            gallery.append((ep["plot"], "同步曲线"))
        return "\n\n".join(md), check_rows(m, eid), gallery

    def _detail_table_md(m, name):
        if not m or not name:
            return "(此交付无明细表)", ["(无)"], []
        headers, rows, total = load_detail_table(m, name)
        note = f"**{DETAIL_LABELS.get(name, name)}** · 共 {total} 行" + \
               (f"(仅显示前 {len(rows)} 行,完整文件见交付目录 details/{name})"
                if total > len(rows) else "")
        return note, headers or ["(空)"], rows

    # ── 裁决卡片的公共装配(三键状态 / 视频槽 / 逐条渲染):两块裁决面板形态
    #    完全一样(队列表 + 翻页卡片 + 三键),差别只在字段与裁决词,所以抽在
    #    这里共用——复制粘贴两份的话,下次改按钮反馈必然只改一边。──

    def _btns(choices, current):
        """三键状态:未裁决=全中性灰;已裁决=只有选中的那个高亮(2026-08-06 用户定:
        按钮各有颜色时按下没反馈,分不清成没成功;改为"同色待选、选中变色",
        且翻页/跳行回到已裁决条目时按钮状态跟着该条的落盘裁决走)。
        choices 的顺序 = 界面上三个按钮的排列顺序。"""
        return [gr.update(variant=("primary" if c == current else "secondary"))
                for c in choices]

    def _au_btns(decision):
        return _btns(DECISION_CHOICES, decision)

    def _tv_btns(verdict):
        return _btns(VERDICT_CHOICES, verdict)

    def _vids(m, eid):
        """三路视频槽:有几路给几路;一路都没有时保留一个可见占位(不然卡片塌掉)。"""
        clips = audit_clip_paths(m, eid) if eid else []
        return [gr.update(value=(clips[i] if i < len(clips) else None),
                          visible=(i < len(clips) or not clips)) for i in range(3)]

    def _au_render(m, idx):
        """渲染第 idx 条标注分歧卡片(越界回绕)。装配顺序 = _au_outs。"""
        q = (m or {}).get("audit_queue") or []
        if not q:
            return (idx, "(无分歧条目)", "", "", "", "",
                    *[gr.update(value=None, visible=False)] * 3, *_au_btns(None))
        idx = idx % len(q)
        a = q[idx]
        dec = load_label_decisions(m).get(a.get("id", ""), {})
        info = (f"**{a.get('id','')}** · 档位 **{a.get('priority','参考')}** · "
                f"成败线判定:{a.get('task_verdict') or '—'}"
                + (f" · 已裁决:**{dec['decision']}**" if dec.get("decision") else "")
                + f"\n\n分歧说明:{a.get('reason','')}")
        return (idx, f"第 {idx + 1} / {len(q)} 条", info, a.get("label", ""),
                a.get("caption", ""), "", *_vids(m, a.get("id", "")),
                *_au_btns(dec.get("decision")))

    def _tv_render(m, idx):
        """渲染第 idx 条任务成败弃权卡片(越界回绕)。装配顺序 = _tv_outs。"""
        q = (m or {}).get("task_review") or []
        if not q:
            return (idx, "(无待裁决的任务成败弃权条目)", "", "",
                    *[gr.update(value=None, visible=False)] * 3, *_tv_btns(None))
        idx = idx % len(q)
        t = q[idx]
        v = load_task_verdicts(m).get(t.get("id", ""), {})
        info = (f"**{t.get('id','')}** · 当前判决:{t.get('current','?')}"
                + (f" · 已裁决:**{v['verdict']}**" if v.get("verdict") else "")
                + f"\n\n**系统弃权原因**:{t.get('reason') or '未注明'}")
        readings = f"关键读数:{readings_text(t.get('readings') or {})}"
        return (idx, f"第 {idx + 1} / {len(q)} 条", info, readings,
                *_vids(m, t.get("id", "")), *_tv_btns(v.get("verdict")))

    def _load(path):
        m = load_delivery(path)
        eids = sorted(m["episodes"].keys())
        first = eids[0] if eids else None
        tables = list_detail_tables(m)
        t_first = tables[0] if tables else None
        note0, h0, r0 = _detail_table_md(m, t_first)
        tl = load_timeline(m)
        tl_note0 = f"口径:{tl['note']}" if tl.get("note") else ""
        perf = load_perf(m)
        # 详情面板随交付切换一起刷新:换目录后选中 eid 若恰好同名(ep000000 常见),
        # Dropdown 值不变→change 不触发→详情停留在上一份交付的陈旧渲染(实测踩过)
        return (m, overview_markdown(m), funnel_rows(m), _config_yaml(m),
                episode_rows(m), gr.update(choices=eids, value=first),
                skill_bar_html(m), skill_rows(m), audit_note_md(m),
                # 两块裁决面板都从第 0 条重新起(换交付不复位 = 停在上一份交付的
                # 条目上,按钮状态还是旧的,实测踩过)
                audit_rows(m), *_au_render(m, 0),
                task_review_hint_md(m), task_review_rows(m), *_tv_render(m, 0),
                *_detail(m, first),
                gr.update(choices=[DETAIL_LABELS[t] for t in tables],
                          value=(DETAIL_LABELS[t_first] if t_first else None)),
                note0, gr.update(value=r0, headers=h0),
                tl_note0, timeline_html(tl),
                perf_backend_md(perf), perf_env_md(perf), LATENCY_NOTE,
                latency_rows(perf), latency_bar_html(perf))

    # theme/css/head 不在这里传:gradio 6 把它们从 Blocks() 挪到了 launch()/
    # mount_gradio_app()(传给 Blocks 只换来一条 UserWarning,值被丢掉)。见 presentation()。
    with gr.Blocks(title="Robot Data Curation") as app:
        gr.Markdown("# 机器人数据 Curation 质检台")
        # 双层导航(2026-07-28 U3):顶层「终端」在左、「质检报告」在右,但默认落在
        # 「质检报告」(selected= 指 Tab id)。terminal 关闭时 ExitStack 一个上下文
        # 都不进 → 页面结构与加这层之前完全一致,客户部署里看不到终端入口。
        with contextlib.ExitStack() as shell:
            if terminal:
                shell.enter_context(gr.Tabs(selected="report", elem_id="topnav"))
                with gr.Tab("终端", id="term"):
                    # 内嵌终端(2026-07-29 U4,替代 ttyd iframe):xterm.js 画屏 +
                    # 本服务的 /ws/term(forkpty 起 bash)。装配全在 term.js 里,
                    # 这里只放它要挂载的容器 div;term.js 等这个 div **可见**才连,
                    # 所以不点终端页签就不会在服务端 fork 出 shell。
                    gr.HTML('<div id="curation-term-screen"></div>')
                shell.enter_context(gr.Tab("质检报告", id="report"))
            with gr.Row():
                picker = gr.Dropdown(choices=choices, value=choices[0], label="交付目录",
                                     scale=4, interactive=True, allow_custom_value=True,
                                     info="可输入任意**交付目录**或其**父目录**后按回车+「重新加载」:"
                                          "父目录会自动展开其下全部交付;新跑完的目录点「重新加载」即出现")
                reload_btn = gr.Button("重新加载", scale=1)
            state = gr.State()

            with gr.Tab("漏斗总览"):
                ov_md = gr.Markdown()
                ov_funnel = gr.Dataframe(headers=FUNNEL_HEADERS, label="漏斗", interactive=False)
                with gr.Accordion("本次运行生效配置(config_effective)", open=False):
                    ov_cfg = gr.Code(language="yaml")

            with gr.Tab("Episodes"):
                ep_table = gr.Dataframe(headers=EPISODE_HEADERS, label="全部 episode",
                                        interactive=False)
                ep_pick = gr.Dropdown(label="查看单条详情", interactive=True)
                ep_md = gr.Markdown()
                ep_checks = gr.Dataframe(headers=CHECK_HEADERS, label="各维检查", interactive=False)
                ep_gallery = gr.Gallery(label="证据(probe 帧 + 同步曲线)", columns=4, height=320)

            # ── 人工裁决页(2026-08-06):把"人要做决定"的事全收到一处。
            #    此前分歧裁决藏在技能画像页底部,而任务成败弃权只能在 Episodes 页
            #    看见"待裁决"三个字、没有任何下手的地方。位置放在 Episodes 与
            #    技能画像之间 = 看完数据紧接着做决定的自然工序。
            with gr.Tab("人工裁决"):
                # 页签已写「人工裁决」,页内不再重复大标题(2026-08-07 用户定)
                gr.HTML(_ADJ_GUIDE_HTML)

                gr.HTML(_adj_section_html("1", "标注分歧",
                                          "原始标注 vs 画面自产描述",
                                          "#ea580c", "#9a3412"))
                au_table = gr.Dataframe(headers=AUDIT_HEADERS,
                                        label="标注-画面分歧队列(审计检出;重点档排最前;"
                                              "点任意一行 → 下方裁决卡片跳到该条)",
                                        interactive=False, elem_id="audit-queue",
                                        max_height=420,     # 容器内滚动(表头吸顶),条数多不挤爆页面
                                        wrap=True,          # 长文本换行显示全文(原始标注/分歧说明不截断)
                                        column_widths=["7%", "6%", "9%", "24%", "19%",
                                                       "9%", "18%", "8%"])
                # ── 裁决标注分歧(逐条翻页卡片,参考 7862 人工审片的形态):
                #    看视频 → 对照原始标注/建议描述 → 三按钮直接裁决。
                #    UI 只记录裁决(details/label_decisions.csv);
                #    重判 = 命令行 curation rejudge(人工确认是自产自证的断路器)。
                # 默认展开(2026-08-05 用户定:折叠着没人知道能点开)——有分歧队列
                # 的交付,裁决面板就是这个页签的主工作区,不该藏
                with gr.Accordion("裁决标注分歧(点按钮=记草稿,可随时改判;"
                                  "跑 curation rejudge 才生效)", open=True):
                    au_idx = gr.State(0)
                    with gr.Row():
                        au_prev = gr.Button("← 上一条", scale=1)
                        au_pos = gr.Markdown("", elem_id="au-pos")
                        au_next = gr.Button("下一条 →", scale=1)
                    au_info = gr.Markdown()
                    with gr.Row():
                        au_vids = [gr.Video(label=f"机位 {i+1}", interactive=False,
                                            autoplay=False, scale=1) for i in range(3)]
                    au_origlab = gr.Textbox(label="原始标注(只读)", interactive=False)
                    au_newlab = gr.Textbox(label="修正后标注(可编辑;预填 VLM 建议描述,"
                                                 "仅「采纳改标」使用)")
                    au_note = gr.Textbox(label="备注(可选)")
                    with gr.Row():
                        # 三键同色待选、选中变色(见 _au_btns);语义靠图标与文字
                        au_adopt = gr.Button("✅ 采纳改标", variant="secondary")
                        au_keep = gr.Button("↩️ 维持原标注", variant="secondary")
                        au_drop = gr.Button("🗑 弃用该条", variant="secondary")
                    au_status = gr.Markdown()

                # ── ② 任务成败弃权:系统诚实说"我判不了"的条目。这里**不重判**,
                #    人看视频直接给结论(判成功/判失败/搁置),rejudge 只负责搬交付。
                gr.HTML(_adj_section_html("2", "任务成败弃权",
                                          "系统弃权,需人工审核",
                                          "#2563eb", "#1e3a8a"))
                tv_hint = gr.Markdown()
                tv_table = gr.Dataframe(headers=TASK_REVIEW_HEADERS,
                                        label="任务成败待裁决队列(点任意一行 → "
                                              "下方裁决卡片跳到该条)",
                                        interactive=False, elem_id="task-queue",
                                        max_height=420, wrap=True,
                                        column_widths=["7%", "11%", "10%", "38%",
                                                       "22%", "12%"])
                with gr.Accordion("裁决任务成败(点按钮=记草稿,可随时改判;"
                                  "跑 curation rejudge 才生效)", open=True):
                    tv_idx = gr.State(0)
                    with gr.Row():
                        tv_prev = gr.Button("← 上一条", scale=1)
                        tv_pos = gr.Markdown("", elem_id="tv-pos")
                        tv_next = gr.Button("下一条 →", scale=1)
                    tv_info = gr.Markdown()
                    tv_readings = gr.Markdown()
                    with gr.Row():
                        tv_vids = [gr.Video(label=f"机位 {i+1}", interactive=False,
                                            autoplay=False, scale=1) for i in range(3)]
                    tv_note = gr.Textbox(label="备注(可选;写清依据,复盘时是唯一线索)")
                    with gr.Row():
                        # 顺序与 VERDICT_CHOICES 严格对应(_tv_btns 按序点亮)
                        tv_pass = gr.Button("✅ 判成功", variant="secondary")
                        tv_fail = gr.Button("❌ 判失败", variant="secondary")
                        tv_hold = gr.Button("⏸ 搁置", variant="secondary")
                    tv_status = gr.Markdown()

            with gr.Tab("技能画像"):
                # 图在表上方(2026-07-30):先看分布(谁多谁少、长尾有多长),
                # 再下去查具体判据。表格原样不动。
                sk_html = gr.HTML()
                sk_table = gr.Dataframe(headers=SKILL_HEADERS, label="两级技能体系",
                                        interactive=False)
                # 分歧队列与裁决卡片 2026-08-06 整体搬去「人工裁决」页,这里只留指路:
                # 画像页是"看数据"的,裁决是"做决定"的,混在一页两边都做不好。
                sk_audit_note = gr.Markdown()

            with gr.Tab("Stuck 时间线"):
                gr.Markdown("每条 episode 一根彩条(0 → 结束秒),段界标秒、悬停看精确"
                            "起止;按 stuck 总时长降序 = 图形化人工复查队列")
                tl_all = gr.Checkbox(label="显示全部 episode(含无 stuck/idle 的干净条目)",
                                     value=False)
                tl_note = gr.Markdown()
                tl_html = gr.HTML()

            with gr.Tab("明细"):
                dt_pick = gr.Dropdown(label="选择明细表(交付目录 details/ 下的 CSV)",
                                      interactive=True)
                dt_note = gr.Markdown()
                dt_table = gr.Dataframe(label="明细", interactive=False)

            with gr.Tab("性能剖析"):
                # 三块(2026-07-30):① 这次用的什么服务/什么硬件 ② 管线自己跑在
                # 什么容器里 ③ 时间花在哪一类 VLM 调用上。数据全部来自交付记录,
                # **界面不出现任何后端预设代号**(那是机房黑话,见 manifest 顶部红线)。
                perf_backend = gr.Markdown()
                perf_env = gr.Markdown()
                gr.Markdown("### 延时剖析")
                perf_note = gr.Markdown()
                perf_table = gr.Dataframe(headers=LATENCY_HEADERS, label="分类延时",
                                          interactive=False)
                # 两段常量说明(与交付无关,不进 _load 的输出列表):分位数怎么读、
                # 四类调用各是干什么的。第二轮反馈:光有语义化名字客户仍读不懂。
                gr.Markdown(LATENCY_PCTL_NOTE)
                gr.Markdown(LATENCY_KIND_NOTE)
                perf_bar = gr.HTML()

            with gr.Tab("后端状态"):
                gr.Markdown("逐预设探活 + 列服务端模型(与 `curation backends` 同源)")
                be_btn = gr.Button("探活")
                be_table = gr.Dataframe(headers=["预设", "状态", "服务端模型"], interactive=False)

            # ⚠️ 顺序必须与 _load 的返回值逐项对齐(错位是运行期才炸的接线错误,
            #    有测试直接比 len(_load(...)) == len(outputs) 钉住)。
            outs = [state, ov_md, ov_funnel, ov_cfg, ep_table, ep_pick,
                    sk_html, sk_table, sk_audit_note,
                    au_table,
                    au_idx, au_pos, au_info, au_origlab, au_newlab, au_note, *au_vids,
                    au_adopt, au_keep, au_drop,
                    tv_hint, tv_table,
                    tv_idx, tv_pos, tv_info, tv_readings, *tv_vids,
                    tv_pass, tv_fail, tv_hold,
                    ep_md, ep_checks, ep_gallery, dt_pick, dt_note, dt_table,
                    tl_note, tl_html,
                    perf_backend, perf_env, perf_note, perf_table, perf_bar]
            picker.change(_load, picker, outs)

            def _reload(path):
                # 重扫根目录(2026-07-28;2026-08-06 增强):
                # - 手输的是**交付目录本体** → 保留为选项并加载;
                # - 手输的是**装着交付的父目录** → 自动展开,把它下面的交付并入列表
                #   (此前这种输入静默加载失败,看起来像"其它目录无法显示");
                # - 都不是 → 仍保留输入(可能是尚未跑完的目录),加载报什么算什么。
                fresh = discover_deliveries(delivery)
                sel = path
                if path and path not in fresh and os.path.isdir(path):
                    found = discover_deliveries(path)
                    if found and path not in found:
                        fresh = fresh + [d for d in found if d not in fresh]
                        sel = found[0]                    # 父目录 → 跳到其中第一个交付
                if sel and sel not in fresh:
                    fresh = fresh + [sel]
                return (gr.update(choices=fresh, value=sel), *_load(sel))

            reload_btn.click(_reload, picker, [picker, *outs])
            ep_pick.change(_detail, [state, ep_pick], [ep_md, ep_checks, ep_gallery])

            def _table_change(m, label):
                name = {v: k for k, v in DETAIL_LABELS.items()}.get(label)
                note, headers, rows = _detail_table_md(m, name)
                return note, gr.update(value=rows, headers=headers)

            dt_pick.change(_table_change, [state, dt_pick], [dt_note, dt_table])

            # ── ① 标注分歧:翻页 / 点行跳转 / 三键裁决 ──
            _au_outs = [au_idx, au_pos, au_info, au_origlab, au_newlab, au_note, *au_vids,
                        au_adopt, au_keep, au_drop]

            au_prev.click(lambda m, i: _au_render(m, (i or 0) - 1),
                          [state, au_idx], _au_outs)
            au_next.click(lambda m, i: _au_render(m, (i or 0) + 1),
                          [state, au_idx], _au_outs)

            def _au_jump(m, evt):
                """点队列表任意一行 → 卡片跳到该条(挑着裁/回头重看都靠它)。"""
                return _au_render(m, evt.index[0])

            def _tv_jump(m, evt):
                return _tv_render(m, evt.index[0])

            # gradio 靠注解识别"要注入 SelectData";本文件开了 future annotations,
            # 字符串注解会在模块全局被 eval(gr 是函数内导入)→ NameError。
            # 塞真实类对象绕开字符串求值。
            _au_jump.__annotations__ = {"evt": gr.SelectData}
            _tv_jump.__annotations__ = {"evt": gr.SelectData}

            au_table.select(_au_jump, state, _au_outs)

            def _au_decide(m, idx, newlab, note, decision):
                q = (m or {}).get("audit_queue") or []
                if not q:
                    return ("⚠️ 无条目可裁决", gr.update(), gr.update(),
                            *[gr.update()] * 3, gr.update(), gr.update())
                a = q[(idx or 0) % len(q)]
                msg = record_label_decision(m["path"], a.get("id", ""), decision,
                                            newlab or "", note or "")
                if msg.startswith("✅"):
                    msg = msg.replace("✅ 已记录:", "✅ 已记录(草稿,可随时改判):")
                    btns = _au_btns(decision)                # 记录成功才点亮所选键
                else:
                    btns = [gr.update()] * 3                 # 校验失败:按钮不动
                info = _au_render(m, idx)[2]                 # 卡片头同步"已裁决"状态
                # 顺带刷新两处"还剩几条没裁"的提示(裁完最后一条,催办语就该消失)
                return (msg, gr.update(value=audit_rows(m)), info, *btns,
                        task_review_hint_md(m), audit_note_md(m))

            _dec_outs = [au_status, au_table, au_info, au_adopt, au_keep, au_drop,
                         tv_hint, sk_audit_note]
            au_adopt.click(lambda m, i, nl, nt: _au_decide(m, i, nl, nt, "采纳建议改标"),
                           [state, au_idx, au_newlab, au_note], _dec_outs)
            au_keep.click(lambda m, i, nl, nt: _au_decide(m, i, nl, nt, "维持原标注"),
                          [state, au_idx, au_newlab, au_note], _dec_outs)
            au_drop.click(lambda m, i, nl, nt: _au_decide(m, i, nl, nt, "弃用该条"),
                          [state, au_idx, au_newlab, au_note], _dec_outs)

            # ── ② 任务成败弃权:同一套形态(翻页 / 点行跳转 / 三键裁决)──
            _tv_outs = [tv_idx, tv_pos, tv_info, tv_readings, *tv_vids,
                        tv_pass, tv_fail, tv_hold]

            tv_prev.click(lambda m, i: _tv_render(m, (i or 0) - 1),
                          [state, tv_idx], _tv_outs)
            tv_next.click(lambda m, i: _tv_render(m, (i or 0) + 1),
                          [state, tv_idx], _tv_outs)
            tv_table.select(_tv_jump, state, _tv_outs)

            def _tv_decide(m, idx, note, verdict):
                q = (m or {}).get("task_review") or []
                if not q:
                    return ("⚠️ 无条目可裁决", gr.update(), gr.update(),
                            *[gr.update()] * 3)
                t = q[(idx or 0) % len(q)]
                msg = record_task_verdict(m["path"], t.get("id", ""), verdict,
                                          note or "")
                if msg.startswith("✅"):
                    msg = msg.replace("✅ 已记录:", "✅ 已记录(草稿,可随时改判):")
                    btns = _tv_btns(verdict)
                else:
                    btns = [gr.update()] * 3
                info = _tv_render(m, idx)[2]
                return msg, gr.update(value=task_review_rows(m)), info, *btns

            _tv_dec_outs = [tv_status, tv_table, tv_info, tv_pass, tv_fail, tv_hold]
            tv_pass.click(lambda m, i, nt: _tv_decide(m, i, nt, "判成功"),
                          [state, tv_idx, tv_note], _tv_dec_outs)
            tv_fail.click(lambda m, i, nt: _tv_decide(m, i, nt, "判失败"),
                          [state, tv_idx, tv_note], _tv_dec_outs)
            tv_hold.click(lambda m, i, nt: _tv_decide(m, i, nt, "搁置"),
                          [state, tv_idx, tv_note], _tv_dec_outs)
            tl_all.change(lambda m, a: timeline_html(load_timeline(m), only_flagged=not a),
                          [state, tl_all], tl_html)
            be_btn.click(lambda: _probe_backends(config_path, probe_timeout), None, be_table)
            app.load(_load, picker, outs)
    return app


def create_asgi_app(delivery: str, config_path: str | None = None,
                    probe_timeout: float = 5.0, terminal: bool = False,
                    review_dir: str | None = None):
    """→ FastAPI 应用(gradio 挂在 `/`,自定义路由挂在它前面)。

    为什么不再用 `blocks.launch()`:launch() 自己造 FastAPI + 自己跑 uvicorn,拿不到
    那个 app 的引用,也就挂不上 `/ws/term`。改成我们造 app、gradio 往上挂,单端口
    同时提供 UI + 终端 + 静态资产 + 健康检查。

    路由注册顺序有讲究:starlette 按注册顺序匹配,gradio 的 `/` 是 catch-all mount,
    必须最后挂,否则它会吃掉 `/ws/term` 和 `/term-static/*`。
    """
    import gradio as gr
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    from fastapi.staticfiles import StaticFiles

    from . import auth

    blocks = build_app(delivery, config_path, probe_timeout, terminal=terminal)
    api = FastAPI()

    # 探针端点(鉴权豁免,见 auth.EXEMPT_PATHS):k8s readinessProbe 现在指 /(整页
    # 渲染),配了 Basic 之后会被 401 打红 → 留一个不设防的轻量端点给探针用。
    @api.get("/healthz", response_class=PlainTextResponse)
    def _healthz() -> str:                     # noqa: ANN202
        return "ok"

    if terminal:
        from . import terminal as term
        api.mount("/term-static", StaticFiles(directory=STATIC_DIR), name="term-static")
        api.add_api_websocket_route("/ws/term", term.term_endpoint)
        log.info("终端:已开启(/ws/term,shell=%s,cwd=%s)",
                 term.resolve_shell(), term.resolve_workdir())

    # 静态审片站(curation review-page 的产出):挂在 gradio catch-all 之前。
    # html=True → /review 直接出 index.html;目录缺失只警告不拦启动(先起 UI 后生成站点
    # 是合法顺序,StaticFiles 每次请求现场解析路径,后补的目录立即可用)。
    if review_dir:
        if not os.path.isdir(review_dir):
            log.warning("审片站目录尚不存在:%s(生成后无需重启即可访问)", review_dir)
            os.makedirs(review_dir, exist_ok=True)
        api.mount("/review", StaticFiles(directory=review_dir, html=True), name="review")
        log.info("审片站:已挂 /review → %s", review_dir)

    auth.apply(api, terminal_enabled=terminal)
    # allowed_paths:允许 Gallery 直读交付目录下的证据文件(gradio 默认只许临时目录)
    return gr.mount_gradio_app(api, blocks, path="/", allowed_paths=[delivery],
                               **presentation(terminal))


def launch(delivery: str, config_path: str | None = None, host: str = "0.0.0.0",
           port: int = 7860, probe_timeout: float = 5.0,
           terminal: bool = False, review_dir: str | None = None) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_asgi_app(delivery, config_path, probe_timeout, terminal=terminal,
                          review_dir=review_dir)
    log.info("质检台 UI 监听 http://%s:%s(交付根目录 %s)", host, port, delivery)
    uvicorn.run(app, host=host, port=port)
