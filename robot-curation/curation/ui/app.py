"""Gradio Blocks 四 tab UI(薄壳层,2026-07-27 U1)。

分工:数据整形全在 manifest.py(纯函数,全测试);本文件只摆组件+接回调,
不做业务逻辑。gradio 是可选依赖——import 收在函数内,没装 gradio 时
`curation run`/`backends` 等一切照常。

四 tab:漏斗总览 / Episodes(三桶 + 左清单右详情,详情以视频为主=demo 高光页)/
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

from .manifest import (AUDIT_HEADERS, BUCKET_ALL, DECISION_CHOICES,
                       DETAIL_LABELS, TASK_REVIEW_HEADERS, VERDICT_CHOICES,
                       WORKFLOW_GUIDE, audit_clip_paths, audit_note_md,
                       bucket_choices, bucket_ids,
                       load_label_decisions, load_task_verdicts,
                       record_label_decision, record_task_verdict,
                       FUNNEL_HEADERS, LATENCY_HEADERS,
                       LATENCY_KIND_NOTE, LATENCY_NOTE, LATENCY_PCTL_NOTE,
                       SKILL_HEADERS, SYNC_FILTER_ALL, SYNC_FILTERS,
                       audit_rows, check_table_html,
                       discover_deliveries, episode_card_html,
                       episode_list_view, episode_video_html, funnel_rows,
                       latency_bar_html, latency_rows, list_detail_tables,
                       load_delivery, load_detail_table, load_perf,
                       load_timeline, manual_hint_html, overview_markdown,
                       perf_backend_md,
                       perf_env_md, readings_text, skill_bar_html, skill_rows,
                       sync_camera_html, sync_conclusion_html, sync_health_html,
                       sync_view, SYNC_HOWTO,
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
/* 同步曲线卡片(2026-08-07 三轮反馈后定稿):一行一张(两列会把图压瘦)、
   卡片之间大间距、内部留白充足;点图在新标签页开原图(gradio 的全屏按钮
   在本环境不生效,用户实测)。 */
.sync-cards { display:flex; flex-direction:column; gap:22px; margin:14px 0 8px; }
/* 卡片整块居中(2026-08-07 用户最终定:靠左"还是不好看");内部曲线在左、诊断框在右 */
.sync-card { background:#fff; border:1px solid #e8ecf1; border-radius:14px;
             padding:6px 18px 14px; box-shadow:0 1px 3px rgba(15,23,42,.05);
             transition:box-shadow .18s ease, border-color .18s ease;
             max-width:1240px; width:100%; margin:0 auto; }
.sync-card-body { display:flex; gap:18px; align-items:flex-start; }
.sync-figure { flex:1 1 auto; min-width:0; display:block; }
/* 诊断框:定宽不参与压缩,窄屏时整块掉到图下面(flex-wrap 由 body 的换行控制) */
.sync-diag { flex:0 0 316px; align-self:stretch; border-left:1px solid #eef2f7;
             padding:2px 0 0 16px; font:12px/1.65 system-ui; color:#334155; }
.sync-diag-title { font-weight:800; color:#0f172a; font-size:.86rem;
                   margin:2px 0 8px; }
.sync-diag-row { padding:8px 0; border-bottom:1px dashed #eef2f7; }
.sync-diag-row:last-of-type { border-bottom:none; }
.sync-diag-head { display:flex; align-items:center; gap:7px; }
.sync-dot { width:8px; height:8px; border-radius:50%; flex:0 0 8px; }
.sync-diag-lag { margin-left:auto; font:600 .78rem ui-monospace,Menlo,monospace;
                 color:#475569; }
.sync-diag-label { font-weight:700; font-size:.78rem; margin:3px 0 2px; }
.sync-diag-text { color:#475569; }
.sync-diag-advice { color:#64748b; margin-top:4px; font-style:italic; }
.sync-diag-foot { margin-top:10px; padding-top:9px; border-top:1px solid #eef2f7;
                  color:#64748b; font-size:.78rem; }
@media (max-width:1100px) {
    .sync-card-body { flex-direction:column; }
    .sync-diag { flex:1 1 auto; border-left:none; padding:10px 0 0;
                 border-top:1px solid #eef2f7; }
}
.sync-card:hover { box-shadow:0 8px 26px rgba(15,23,42,.09); border-color:#dbe3ec; }
.sync-card-head { display:flex; align-items:center; gap:12px; padding:12px 0 12px 12px;
                  margin-bottom:6px; border-bottom:1px solid #f1f5f9; }
.sync-eid { font:700 .95rem ui-monospace,Menlo,monospace; color:#0f172a; }
.sync-badge { font:600 .82rem system-ui; }
.sync-open { margin-left:auto; font:.8rem system-ui; color:#64748b !important;
             text-decoration:none; border:1px solid #e2e8f0; border-radius:7px;
             padding:3px 11px; }
.sync-open:hover { color:#0f172a !important; border-color:#cbd5e1; background:#f8fafc; }
.sync-img { width:100%; display:block; border-radius:8px; cursor:zoom-in; }
/* 页内灯箱(纯 CSS):checkbox 选中 → 全屏遮罩显大图;点遮罩(同一 label)即关闭。
   ⚠️ 祖先若带 transform 会把 position:fixed 变成相对它定位——.sync-card 的
   hover 效果因此只动阴影不动 transform,别加回来。 */
.sync-lb-toggle { display:none; }
.sync-lb { display:none; position:fixed; inset:0; z-index:1000;
           background:rgba(15,23,42,.86); padding:2.5vh 2.5vw;
           align-items:center; justify-content:center; cursor:zoom-out; }
.sync-lb img { max-width:96vw; max-height:95vh; width:auto; height:auto;
               border-radius:10px; background:#fff; box-shadow:0 24px 80px rgba(0,0,0,.45); }
.sync-lb-toggle:checked + .sync-lb { display:flex; }

#audit-queue tbody tr, #task-queue tbody tr { cursor: pointer; }
#audit-queue tbody tr:hover td,
#task-queue tbody tr:hover td { background: rgba(255, 140, 0, 0.10) !important; }

/* Episodes 页(2026-08-11 改版):顶部三桶横排大按钮,左侧清单一列可滚。
   清单是**扫读**用的:等宽字体对齐 episode 号,单行不换行(几百条一换行整列就散),
   容器内滚动而不是把页面拉到几屏高。 */
#ep-buckets .wrap { display: flex; flex-direction: row; gap: 10px; flex-wrap: wrap; }
#ep-buckets label { font-size: 1.02rem !important; font-weight: 700; padding: 7px 16px; }
#ep-list { max-height: 68vh; overflow-y: auto; }
#ep-list .wrap { display: flex; flex-direction: column; gap: 1px; }
#ep-list label { font: 12px/1.5 ui-monospace, Menlo, monospace; padding: 4px 8px;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#ep-list label:hover { background: rgba(255, 140, 0, 0.10); }
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
              terminal: bool = False, review_dir: str | None = None):
    """交付目录(或含多份交付的父目录)→ gr.Blocks。

    terminal=True 时套一层顶层导航:「终端」(内嵌 xterm.js,后端是本服务的
    `/ws/term`)+「质检报告」(= 本文件原有的全部内容),默认选中「质检报告」。
    缺省 False → 顶层导航整个不渲染,页面与加这层之前逐字一致(客户部署根本看不到
    终端入口),`/ws/term` 路由也不注册。

    review_dir = 审片站根目录(与 `/review` 静态路由同一个),Episodes 页的视频
    来源链第一档指着它;不给就只剩交付集内的视频。
    """
    import gradio as gr

    choices = discover_deliveries(delivery)
    if not choices:
        raise SystemExit(f"目录里找不到交付(无 passed.json):{delivery}")

    def _ep_list(m, bucket, page, selected):
        """左清单的一屏(装配顺序 = _ep_list_outs)。分页口径全在 manifest。"""
        v = episode_list_view(m or {}, bucket or BUCKET_ALL, page or 0, selected)
        multi = gr.update(visible=v["pages"] > 1)
        return (v["page"], selected,
                gr.update(choices=v["choices"], value=v["value"]),
                v["pos"], multi, multi)

    def _detail(m, eid):
        """选中 episode → 判决卡 / 视频区 / 待人工指路 / 检查明细。返回顺序 = _ep_outs。

        层级是定死的(2026-08-11 用户拍板):判决与理由在最上,**视频是主角**,
        逐维读数退到默认折叠的明细里。静态证据帧整块撤掉(用户原话:体验太差)。
        """
        if not m or not eid:
            return (episode_card_html(m or {}, ""), "", "", "", "",
                    gr.update(value=None, visible=False))
        plot = (m["episodes"].get(eid) or {}).get("plot")
        return (episode_card_html(m, eid), episode_video_html(m, eid, review_dir),
                manual_hint_html(m, eid), check_table_html(m, eid),
                sync_camera_html(m, eid),
                gr.update(value=plot, visible=bool(plot)))

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

    def _sync_view(m, mode, page):
        """同步曲线页的一屏(装配顺序 = _sy_outs)。分页/筛选逻辑全在 manifest。"""
        v = sync_view(m or {}, mode or SYNC_FILTER_ALL, page or 0)
        # items = [(路径, 标题)];逐槽位填充,多出来的槽位隐藏(不留空框)
        multi = gr.update(visible=v["pages"] > 1)
        return v["page"], v["note"], v["pos"], multi, multi, v["cards"]

    def _load(path):
        m = load_delivery(path)
        eids = bucket_ids(m, BUCKET_ALL)
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
                # 桶随交付切换复位到「全部」:停在「拒绝」而新交付一条都没被拒,
                # 看到的是空清单 + 一个还亮着的桶,等于骗人
                gr.update(choices=bucket_choices(m), value=BUCKET_ALL),
                *_ep_list(m, BUCKET_ALL, 0, first),
                skill_bar_html(m), skill_rows(m), audit_note_md(m),
                # 两块裁决面板都从第 0 条重新起(换交付不复位 = 停在上一份交付的
                # 条目上,按钮状态还是旧的,实测踩过)
                audit_rows(m), *_au_render(m, 0),
                task_review_hint_md(m), task_review_rows(m), *_tv_render(m, 0),
                *_detail(m, first),
                gr.update(choices=[DETAIL_LABELS[t] for t in tables],
                          value=(DETAIL_LABELS[t_first] if t_first else None)),
                note0, gr.update(value=r0, headers=h0),
                # 同步曲线页:筛选与页码一起复位(理由同上)
                gr.update(value=SYNC_FILTER_ALL),
                *_sync_view(m, SYNC_FILTER_ALL, 0),
                sync_conclusion_html(m), sync_health_html(m),
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

            # ── Episodes 页(2026-08-11 整页改版):顶部三桶 + 左清单右详情。
            #    客户只关心"哪些过了/被拒/待人工",所以三桶就是主导航;骨架照
            #    lerobot visualize_dataset 的左导航右详情,视频是详情的主角。
            #    桶口径与清单文案全在 manifest 的纯函数里,这里只摆组件。
            with gr.Tab("Episodes"):
                # 选项(带计数)由 _load 现算填入:交付一换,计数就得跟着换
                ep_bucket = gr.Radio([], label="", elem_id="ep-buckets",
                                     container=False)
                with gr.Row():
                    with gr.Column(scale=1, min_width=250):
                        ep_pick = gr.Radio([], label="episode", elem_id="ep-list",
                                           interactive=True, container=True)
                        # 分页(2026-08-11):两百行单选框一次渲染就到极限。翻页件
                        # 一页放得下时整排隐藏(与同步曲线页同一手法)
                        with gr.Row():
                            ep_prev = gr.Button("← 上一页", scale=1, visible=False,
                                                size="sm")
                            ep_pos = gr.Markdown("")
                            ep_next = gr.Button("下一页 →", scale=1, visible=False,
                                                size="sm")
                        ep_page = gr.State(0)
                        # 当前正在看的那条:清单翻页后它可能不在本页,右侧详情
                        # 照旧显示它,翻回来仍是选中态
                        ep_sel = gr.State(None)
                    with gr.Column(scale=3):
                        ep_card = gr.HTML()
                        ep_video = gr.HTML()
                        ep_hint = gr.HTML()
                        # 明细默认折叠(2026-08-11 用户定):看片的人九成不需要读数,
                        # 但要读时一点就开——不是删掉,是让路
                        with gr.Accordion("检查明细(逐维读数)", open=False):
                            ep_checks = gr.HTML()
                            ep_sync = gr.HTML()
                            # 同步曲线是超宽长图,给整幅宽度(整页也有专门的曲线页)
                            ep_plot = gr.Image(label="同步曲线(右上角可全屏放大)",
                                               visible=False, interactive=False,
                                               buttons=["fullscreen", "download"],
                                               height=380)

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

            # ── 同步曲线页(2026-08-07 新增):details/plots/<ep>_sync.png 的画廊。
            #    与「Stuck 时间线」相邻 = 两页都是图形化诊断(一个看时间轴、一个看
            #    相关曲线),放一起找得到。分页而不是懒加载:曲线是宽长图,一屏 24 张
            #    已经够翻,且分页零 JS(本 UI 一贯做法)。
            with gr.Tab("同步曲线"):
                # 结论先行(2026-08-07 用户点名:光有图没有提示)——建议原先埋在
                # 整页曲线之后,滚不到等于没有。现在顶部先说"这份数据同步得怎么样、
                # 该怎么办",曲线退居证据位。
                sy_conclusion = gr.HTML()
                # 全库逐相机概览紧跟结论(2026-08-07 用户:"建议放到页面开头")——
                # 它是结论的量化底稿,原先压在整页曲线之后,谁也滚不到。
                sy_health = gr.HTML()
                with gr.Accordion("怎么看这些图(展开)", open=False):
                    gr.Markdown(SYNC_HOWTO)
                sy_filter = gr.Radio(SYNC_FILTERS, value=SYNC_FILTER_ALL,
                                     label="筛选")
                with gr.Row():
                    # 平铺优先:一页放得下就整排隐藏翻页件(2026-08-07 用户定),
                    # 只有图多到一页塞不下时才露出来兜底
                    sy_prev = gr.Button("← 上一页", scale=1, visible=False)
                    sy_pos = gr.Markdown("")
                    sy_next = gr.Button("下一页 →", scale=1, visible=False)
                sy_note = gr.Markdown()
                # 一张一行、整幅宽度(不用 Gallery:宽幅长图塞进方格必然上下留白,
                # 中间两张图周围全是空——2026-08-07 用户实见)。固定槽位 + visible
                # 开关,数量随分页变化;每张自带全屏/下载按钮,点开即原尺寸。
                sy_cards = gr.HTML()
                sy_page = gr.State(0)

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
            # Episodes 详情的六个槽(判决卡 / 视频区 / 待人工指路 / 检查表 /
            # 逐相机同步 / 同步曲线):_detail 与 _ep_bucket_change 都按这个顺序装配。
            _ep_outs = [ep_card, ep_video, ep_hint, ep_checks, ep_sync, ep_plot]
            # 左清单的六个槽(页码 / 选中项 / 单选框 / 页码文字 / 两个翻页键):
            # _ep_list 按这个顺序装配
            _ep_list_outs = [ep_page, ep_sel, ep_pick, ep_pos, ep_prev, ep_next]
            _sy_outs = [sy_page, sy_note, sy_pos, sy_prev, sy_next, sy_cards]

            outs = [state, ov_md, ov_funnel, ov_cfg, ep_bucket, *_ep_list_outs,
                    sk_html, sk_table, sk_audit_note,
                    au_table,
                    au_idx, au_pos, au_info, au_origlab, au_newlab, au_note, *au_vids,
                    au_adopt, au_keep, au_drop,
                    tv_hint, tv_table,
                    tv_idx, tv_pos, tv_info, tv_readings, *tv_vids,
                    tv_pass, tv_fail, tv_hold,
                    *_ep_outs, dt_pick, dt_note, dt_table,
                    sy_filter, *_sy_outs, sy_conclusion, sy_health,
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

            def _ep_select(m, eid):
                """点清单某条 → 记住它 + 换右侧详情。"""
                return (eid, *_detail(m, eid))

            # 用 .input 不用 .change:后端回填清单(翻页/换桶/换交付)也会改 value,
            # 走 .change 就会被自己的回填触发一遍——翻页时把详情冲掉,正是"跨页
            # 保持选中"要防的事。.input 只认用户点击。
            ep_pick.input(_ep_select, [state, ep_pick], [ep_sel, *_ep_outs])

            def _ep_bucket_change(m, bucket):
                """点桶 → 左清单换成该桶的条目(回第 1 页),详情跳到该桶第一条。

                清单与详情必须一起换:选中项停在被筛掉的旧 eid 上,右边就成了
                "清单里看不见的那条"的详情——实测最容易骗人。
                """
                ids = bucket_ids(m or {}, bucket or BUCKET_ALL)
                first = ids[0] if ids else None
                return (*_ep_list(m, bucket, 0, first), *_detail(m, first))

            ep_bucket.input(_ep_bucket_change, [state, ep_bucket],
                            [*_ep_list_outs, *_ep_outs])

            # 翻页只动清单,不动右侧详情(正在看的那条翻页时不该被冲掉)
            ep_prev.click(lambda m, b, p, s: _ep_list(m, b, (p or 0) - 1, s),
                          [state, ep_bucket, ep_page, ep_sel], _ep_list_outs)
            ep_next.click(lambda m, b, p, s: _ep_list(m, b, (p or 0) + 1, s),
                          [state, ep_bucket, ep_page, ep_sel], _ep_list_outs)

            # ── 同步曲线页:筛选换档回第 0 页,翻页越界回绕(与裁决卡片同款) ──
            sy_filter.change(lambda m, mode: _sync_view(m, mode, 0),
                             [state, sy_filter], _sy_outs)
            sy_prev.click(lambda m, mode, p: _sync_view(m, mode, (p or 0) - 1),
                          [state, sy_filter, sy_page], _sy_outs)
            sy_next.click(lambda m, mode, p: _sync_view(m, mode, (p or 0) + 1),
                          [state, sy_filter, sy_page], _sy_outs)

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

    blocks = build_app(delivery, config_path, probe_timeout, terminal=terminal,
                       review_dir=review_dir)
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
    # allowed_paths:允许页面直读交付目录下的证据文件(gradio 默认只许临时目录);
    # 审片站目录同样要放行——Episodes 页的视频第一来源就在那儿,不放行会 403。
    allowed = [delivery] + ([review_dir] if review_dir else [])
    return gr.mount_gradio_app(api, blocks, path="/", allowed_paths=allowed,
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
