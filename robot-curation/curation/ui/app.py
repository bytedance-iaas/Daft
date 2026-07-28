"""Gradio Blocks 四 tab UI(薄壳层,2026-07-27 U1)。

分工:数据整形全在 manifest.py(纯函数,全测试);本文件只摆组件+接回调,
不做业务逻辑。gradio 是可选依赖——import 收在函数内,没装 gradio 时
`curation run`/`backends` 等一切照常。

四 tab:漏斗总览 / Episodes(点选看证据帧+VLM 理由=demo 高光页)/
技能画像 / 后端状态(复用 backends 探活)。
"""
from __future__ import annotations

import json
import os

from .manifest import (AUDIT_HEADERS, CHECK_HEADERS, EPISODE_HEADERS,
                       FUNNEL_HEADERS, SKILL_HEADERS, audit_rows, check_rows,
                       discover_deliveries, episode_rows, funnel_rows,
                       load_delivery, overview_markdown, skill_rows)


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


def build_app(delivery: str, config_path: str | None = None, probe_timeout: float = 5.0):
    """交付目录(或含多份交付的父目录)→ gr.Blocks。"""
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

    def _load(path):
        m = load_delivery(path)
        eids = sorted(m["episodes"].keys())
        first = eids[0] if eids else None
        # 详情面板随交付切换一起刷新:换目录后选中 eid 若恰好同名(ep000000 常见),
        # Dropdown 值不变→change 不触发→详情停留在上一份交付的陈旧渲染(实测踩过)
        return (m, overview_markdown(m), funnel_rows(m), _config_yaml(m),
                episode_rows(m), gr.update(choices=eids, value=first),
                skill_rows(m), audit_rows(m), *_detail(m, first))

    # 系统字体:默认主题会向 fonts.googleapis.com 拉字体,国内网络挂起 15s+ 才放行
    # 首屏(实测),demo 一开场就是白屏等待——本 UI 场景里无网络字体的理由
    theme = gr.themes.Default(font=["system-ui", "sans-serif"],
                              font_mono=["ui-monospace", "Menlo", "monospace"])
    with gr.Blocks(title="Robot Data Curation", theme=theme) as app:
        gr.Markdown("# 机器人数据 Curation 质检台")
        with gr.Row():
            picker = gr.Dropdown(choices=choices, value=choices[0], label="交付目录",
                                 scale=4, interactive=True)
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

        with gr.Tab("后端状态"):
            gr.Markdown("逐预设探活 + 列服务端模型(与 `curation backends` 同源)")
            be_btn = gr.Button("探活")
            be_table = gr.Dataframe(headers=["预设", "状态", "服务端模型"], interactive=False)

        outs = [state, ov_md, ov_funnel, ov_cfg, ep_table, ep_pick, sk_table, au_table,
                ep_md, ep_checks, ep_gallery]
        picker.change(_load, picker, outs)
        reload_btn.click(_load, picker, outs)
        ep_pick.change(_detail, [state, ep_pick], [ep_md, ep_checks, ep_gallery])
        be_btn.click(lambda: _probe_backends(config_path, probe_timeout), None, be_table)
        app.load(_load, picker, outs)
    return app


def launch(delivery: str, config_path: str | None = None, host: str = "0.0.0.0",
           port: int = 7860, probe_timeout: float = 5.0) -> None:
    app = build_app(delivery, config_path, probe_timeout)
    # 允许 Gallery 直读交付目录下的证据文件(gradio 默认只许临时目录);
    # 只传三个跨版本稳定的参数(gradio 6.x 删了 show_api 等旧关键字)
    app.launch(server_name=host, server_port=port, allowed_paths=[delivery])
