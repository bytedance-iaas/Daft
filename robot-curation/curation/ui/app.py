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

from .manifest import (AUDIT_HEADERS, CHECK_HEADERS, DETAIL_LABELS,
                       EPISODE_HEADERS, FUNNEL_HEADERS, LATENCY_HEADERS,
                       LATENCY_KIND_NOTE, LATENCY_NOTE, LATENCY_PCTL_NOTE,
                       SKILL_HEADERS, audit_rows, check_rows,
                       discover_deliveries, episode_rows, funnel_rows,
                       latency_bar_html, latency_rows, list_detail_tables,
                       load_delivery, load_detail_table, load_perf,
                       load_timeline, overview_markdown, perf_backend_md,
                       perf_env_md, skill_bar_html, skill_rows, timeline_html)

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


def presentation(terminal: bool = False) -> dict:
    """theme/css/head 三件套(gradio 6 起只认 launch()/mount_gradio_app() 上的这三个
    关键字,传给 `gr.Blocks()` 会被静默丢弃——2026-07-29 实测,顺手修掉的老 bug)。"""
    import gradio as gr

    return {
        # 系统字体:默认主题会向 fonts.googleapis.com 拉字体,国内网络挂起 15s+ 才放行
        # 首屏(实测),demo 一开场就是白屏等待——本 UI 场景里无网络字体的理由
        "theme": gr.themes.Default(font=["system-ui", "sans-serif"],
                                   font_mono=["ui-monospace", "Menlo", "monospace"]),
        "css": (_TOPNAV_CSS + _TERMINAL_CSS) if terminal else None,
        "head": _TERMINAL_HEAD if terminal else None,
    }


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
                skill_bar_html(m), skill_rows(m), audit_rows(m), *_detail(m, first),
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
                                     info="可直接输入任意交付目录路径;「重新加载」会重扫列表")
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

            with gr.Tab("技能画像"):
                # 图在表上方(2026-07-30):先看分布(谁多谁少、长尾有多长),
                # 再下去查具体判据。表格原样不动。
                sk_html = gr.HTML()
                sk_table = gr.Dataframe(headers=SKILL_HEADERS, label="两级技能体系",
                                        interactive=False)
                au_table = gr.Dataframe(headers=AUDIT_HEADERS,
                                        label="标注-画面分歧复核队列(双方都可能错,供人工判定)",
                                        interactive=False)

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

            outs = [state, ov_md, ov_funnel, ov_cfg, ep_table, ep_pick,
                    sk_html, sk_table, au_table,
                    ep_md, ep_checks, ep_gallery, dt_pick, dt_note, dt_table,
                    tl_note, tl_html,
                    perf_backend, perf_env, perf_note, perf_table, perf_bar]
            picker.change(_load, picker, outs)

            def _reload(path):
                # 重扫根目录(2026-07-28 用户问"不能自己设定吗":新交付目录从此免重启;
                # 手输的路径不在扫描列表里也保留为合法选项)
                fresh = discover_deliveries(delivery)
                if path and path not in fresh:
                    fresh = fresh + [path]
                return (gr.update(choices=fresh, value=path), *_load(path))

            reload_btn.click(_reload, picker, [picker, *outs])
            ep_pick.change(_detail, [state, ep_pick], [ep_md, ep_checks, ep_gallery])

            def _table_change(m, label):
                name = {v: k for k, v in DETAIL_LABELS.items()}.get(label)
                note, headers, rows = _detail_table_md(m, name)
                return note, gr.update(value=rows, headers=headers)

            dt_pick.change(_table_change, [state, dt_pick], [dt_note, dt_table])
            tl_all.change(lambda m, a: timeline_html(load_timeline(m), only_flagged=not a),
                          [state, tl_all], tl_html)
            be_btn.click(lambda: _probe_backends(config_path, probe_timeout), None, be_table)
            app.load(_load, picker, outs)
    return app


def create_asgi_app(delivery: str, config_path: str | None = None,
                    probe_timeout: float = 5.0, terminal: bool = False):
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

    auth.apply(api, terminal_enabled=terminal)
    # allowed_paths:允许 Gallery 直读交付目录下的证据文件(gradio 默认只许临时目录)
    return gr.mount_gradio_app(api, blocks, path="/", allowed_paths=[delivery],
                               **presentation(terminal))


def launch(delivery: str, config_path: str | None = None, host: str = "0.0.0.0",
           port: int = 7860, probe_timeout: float = 5.0,
           terminal: bool = False) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_asgi_app(delivery, config_path, probe_timeout, terminal=terminal)
    log.info("质检台 UI 监听 http://%s:%s(交付根目录 %s)", host, port, delivery)
    uvicorn.run(app, host=host, port=port)
