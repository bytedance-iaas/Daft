"""任务面板(跑质检 · 当前任务):issue #56「停止没反应」/ #57「一直停在运行中」(2026-08-21)。

#56 的真相:任务早已落终态,红色「停止」却还亮着;#57 的真相:自动刷新押在 gr.Timer 上,
而它在这套部署里不跳,状态条就停在最后一次手动刷新的样子。
"""
from __future__ import annotations

import pytest


def _btn(app, value):
    import gradio as gr
    return next(b for b in app.blocks.values()
                if isinstance(b, gr.Button) and str(b.value) == value)


def _fn_on(app, block, event="click"):
    for fn in app.fns.values():
        for tgt in (getattr(fn, "targets", None) or []):
            cid, ev = (tgt if isinstance(tgt, tuple) else (tgt.block._id, tgt.event_name))
            if cid == block._id and ev == event:
                return fn
    raise AssertionError(f"{block} 没接 {event}")


@pytest.fixture
def app(tmp_path, monkeypatch):
    pytest.importorskip("gradio")
    monkeypatch.delenv("CURATION_CONFIG", raising=False)
    from curation.ui.app import build_app
    root = tmp_path / "deliveries"
    root.mkdir()
    return build_app(str(root), data_root=str(tmp_path / "datasets"))


def test_stop_button_starts_disabled_and_rides_every_status_refresh(app):
    """停止按钮默认不可点,且每条刷新状态区的路都带着它(与 #55 的「开始质检」同一条不变量)。"""
    import gradio as gr
    stop = _btn(app, "停止")
    assert stop.interactive is False
    tk_log = next(b for b in app.blocks.values() if isinstance(b, gr.Textbox) and b.label == "日志")
    riders = [f for f in app.fns.values()
              if any(getattr(o, "_id", None) == tk_log._id for o in (f.outputs or []))]
    assert riders
    for f in riders:
        assert stop._id in {getattr(o, "_id", None) for o in (f.outputs or [])}, \
            f"{getattr(f.fn, '__name__', f)} 刷新了状态区却没带上「停止」按钮"


def test_stop_button_follows_busy_state(app, monkeypatch):
    from curation.ui import runner
    fn = _fn_on(app, _btn(app, "刷新")).fn
    # 没有任务:停止不可点、开始可点
    monkeypatch.setattr(runner, "active_run", lambda root: None)
    monkeypatch.setattr(runner, "list_runs", lambda root, limit=50, **kw: [])
    out = fn("提示要带回去")
    assert out[2] == "提示要带回去", "刷新不许抹掉当前提示(2026-08-13 教训)"
    assert out[3]["interactive"] is True and out[4]["interactive"] is False
    # 有任务在跑:停止可点、开始置灰
    st = {"run_id": "r1", "state": "running", "label": "质检 x", "command": "run",
          "started_at": "2026-08-21 00:00:00", "pid": 1}
    monkeypatch.setattr(runner, "active_run", lambda root: st)
    monkeypatch.setattr(runner, "list_runs", lambda root, limit=50, **kw: [st])
    monkeypatch.setattr(runner, "tail_log", lambda root, rid: "[curation] 数值检查 1/2")
    out = fn("")
    assert out[3]["interactive"] is False and out[4]["interactive"] is True
    assert "运行中" in out[0]


def test_refresh_is_driven_by_page_script_not_gr_timer(app):
    """自动刷新 = 页面脚本点「刷新」按钮(真事件),不再有 gr.Timer 对着任务面板。"""
    import gradio as gr
    from curation.ui import app as ui_app
    head = ui_app.presentation()["head"]
    for needle in ("tk-refresh", "tk-status", "运行中", "正在停止", "visibilitychange"):
        assert needle in head, f"页面脚本里少了 {needle}"
    assert str(ui_app.TASK_POLL_ACTIVE_MS) in head and str(ui_app.TASK_POLL_IDLE_MS) in head
    tk_log = next(b for b in app.blocks.values() if isinstance(b, gr.Textbox) and b.label == "日志")
    timers = {b._id for b in app.blocks.values() if type(b).__name__ == "Timer"}
    for f in app.fns.values():
        if any(getattr(o, "_id", None) == tk_log._id for o in (f.outputs or [])):
            for tgt in (getattr(f, "targets", None) or []):
                cid = tgt[0] if isinstance(tgt, tuple) else tgt.block._id
                assert cid not in timers, "任务面板不许再押在 gr.Timer 上"
    assert _btn(app, "刷新").elem_id == "tk-refresh"


def test_refresh_does_not_flash_loading_state(app):
    """轮询刷新不许带 Gradio 的加载态(2026-08-22 用户实见"一秒一闪"):每 2 秒把日志框 /
    状态卡打成灰色再填回来,看着像不停刷新。show_progress 必须是 hidden。"""
    fn = _fn_on(app, _btn(app, "刷新"))
    assert str(getattr(fn, "show_progress", "")) == "hidden", getattr(fn, "show_progress", None)
