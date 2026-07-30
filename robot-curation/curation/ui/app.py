"""Gradio Blocks 四 tab UI(薄壳层,2026-07-27 U1)。

分工:数据整形全在 manifest.py(纯函数,全测试);本文件只摆组件+接回调,
不做业务逻辑。gradio 是可选依赖——import 收在函数内,没装 gradio 时
`curation run`/`backends` 等一切照常。

四 tab:漏斗总览 / Episodes(点选看证据帧+VLM 理由=demo 高光页)/
技能画像 / 后端状态(复用 backends 探活)。
"""
from __future__ import annotations

import contextlib
import json
import os

from .manifest import (AUDIT_HEADERS, CHECK_HEADERS, DETAIL_LABELS,
                       EPISODE_HEADERS, FUNNEL_HEADERS, SKILL_HEADERS,
                       audit_rows, check_rows, discover_deliveries,
                       episode_rows, funnel_rows, list_detail_tables,
                       load_delivery, load_detail_table, load_timeline,
                       overview_markdown, skill_rows, timeline_html)



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


def build_app(delivery: str, config_path: str | None = None, probe_timeout: float = 5.0,
              terminal_url: str | None = None):
    """交付目录(或含多份交付的父目录)→ gr.Blocks。

    terminal_url 非空时套一层顶层导航:「终端」(ttyd 网页终端 iframe)+「质检报告」
    (= 本文件原有的全部内容),默认选中「质检报告」。缺省 None → 顶层导航整个不渲染,
    页面与加这层之前逐字一致(客户部署根本看不到终端入口)。
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
        # 详情面板随交付切换一起刷新:换目录后选中 eid 若恰好同名(ep000000 常见),
        # Dropdown 值不变→change 不触发→详情停留在上一份交付的陈旧渲染(实测踩过)
        return (m, overview_markdown(m), funnel_rows(m), _config_yaml(m),
                episode_rows(m), gr.update(choices=eids, value=first),
                skill_rows(m), audit_rows(m), *_detail(m, first),
                gr.update(choices=[DETAIL_LABELS[t] for t in tables],
                          value=(DETAIL_LABELS[t_first] if t_first else None)),
                note0, gr.update(value=r0, headers=h0),
                tl_note0, timeline_html(tl))

    # 系统字体:默认主题会向 fonts.googleapis.com 拉字体,国内网络挂起 15s+ 才放行
    # 首屏(实测),demo 一开场就是白屏等待——本 UI 场景里无网络字体的理由
    theme = gr.themes.Default(font=["system-ui", "sans-serif"],
                              font_mono=["ui-monospace", "Menlo", "monospace"])
    with gr.Blocks(title="Robot Data Curation", theme=theme,
               css=(_TOPNAV_CSS if terminal_url else None)) as app:
        gr.Markdown("# 机器人数据 Curation 质检台")
        # 双层导航(2026-07-28 U3):顶层「终端」在左、「质检报告」在右,但默认落在
        # 「质检报告」(selected= 指 Tab id)。terminal_url 缺省时 ExitStack 一个上下文
        # 都不进 → 页面结构与加这层之前完全一致,客户部署里看不到终端入口。
        with contextlib.ExitStack() as shell:
            if terminal_url:
                shell.enter_context(gr.Tabs(selected="report", elem_id="topnav"))
                with gr.Tab("终端", id="term"):
                    # ttyd 网页终端(pod 内 127.0.0.1:7681),iframe 由**浏览器**解析,
                    # 所以要能打开必须本机也能访问该地址(port-forward 7681)。
                    gr.HTML(f'<iframe src="{terminal_url}" style="width:100%;'
                            f'height:78vh;border:0"></iframe>')
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
                sk_table = gr.Dataframe(headers=SKILL_HEADERS, label="两级技能体系",
                                        interactive=False)
                au_table = gr.Dataframe(headers=AUDIT_HEADERS, label="标注审计复核队列",
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

            with gr.Tab("后端状态"):
                gr.Markdown("逐预设探活 + 列服务端模型(与 `curation backends` 同源)")
                be_btn = gr.Button("探活")
                be_table = gr.Dataframe(headers=["预设", "状态", "服务端模型"], interactive=False)

            outs = [state, ov_md, ov_funnel, ov_cfg, ep_table, ep_pick, sk_table, au_table,
                    ep_md, ep_checks, ep_gallery, dt_pick, dt_note, dt_table,
                    tl_note, tl_html]
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


def launch(delivery: str, config_path: str | None = None, host: str = "0.0.0.0",
           port: int = 7860, probe_timeout: float = 5.0,
           terminal_url: str | None = None) -> None:
    app = build_app(delivery, config_path, probe_timeout, terminal_url=terminal_url)
    # 允许 Gallery 直读交付目录下的证据文件(gradio 默认只许临时目录);
    # 只传三个跨版本稳定的参数(gradio 6.x 删了 show_api 等旧关键字)
    app.launch(server_name=host, server_port=port, allowed_paths=[delivery])
