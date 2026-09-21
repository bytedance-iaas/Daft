"""The execution timeline of a task (``GET /tasks/{id}/timeline``, C4 ``TimelineEntry``).

Built from three sources, oldest first:

* the task row - ``created`` always; ``started`` / the final outcome only as a
  fallback when no ``task.state`` events exist (audit events are purged after
  90 days, design doc 08 §7);
* ``task.state`` / ``subtask.state`` / ``task.revision`` audit events written by
  :mod:`daemon.transitions` - system and user pauses and resumes, stops,
  failures, the main run's end, new result revisions;
* the subtask rows - when each retry, resume, adjudication or re-export started
  and how it ended.
"""
from __future__ import annotations

from .repo import protocol as P

STATE_ZH = {"created": "待启动", "queued": "排队中", "running": "运行中", "pausing": "暂停中",
            "paused": "已暂停", "stopping": "停止中", "stopped": "已停止", "succeeded": "已完成",
            "completed_with_errors": "部分错误", "failed": "失败"}
KIND_ZH = {"retry": "重试", "resume": "继续运行", "apply_adjudication": "执行裁决",
           "reexport": "重新导出"}


def _entry(at: int, kind: str, text: str, *, state=None, subtask_id=None, revision=None) -> dict:
    return {"at": int(at), "kind": kind, "subtask_id": subtask_id, "revision": revision,
            "state": state, "text": text}


def _with_reason(text: str, reason: str | None) -> str:
    return f"{text}：{reason}" if reason else text


def _state_event(ev: P.Event, started: list[bool]) -> dict | None:
    d = ev.detail or {}
    frm, to, reason = d.get("from"), d.get("to"), d.get("reason")
    sub = d.get("subtask_id") if ev.action == "subtask.state" else None
    who = f"{KIND_ZH.get(d.get('kind'), '子任务')}" if sub else "任务"
    if to == "paused":
        if d.get("pause_reason") == "user":
            return _entry(ev.at, "user_pause", f"{who}被用户暂停", state=to, subtask_id=sub)
        text = f"{who}被系统暂停：{reason}，将自动恢复" if reason else f"{who}被系统暂停，将自动恢复"
        return _entry(ev.at, "system_pause", text, state=to, subtask_id=sub)
    if frm == "paused" and to == "queued":
        if d.get("prev_pause_reason") == "user":
            return _entry(ev.at, "user_resume", f"用户恢复{who}，重新排队", state=to, subtask_id=sub)
        return _entry(ev.at, "system_resume", f"系统自动恢复{who}，重新排队", state=to,
                      subtask_id=sub)
    if sub:
        return None                       # subtask start and end come from the subtask rows
    if to == "running" and not started[0]:
        started[0] = True
        return _entry(ev.at, "started", "开始运行", state=to)
    if to == "stopped":
        return _entry(ev.at, "stopped", _with_reason("任务已停止", reason), state=to)
    if to == "failed":
        return _entry(ev.at, "failed", _with_reason("任务失败", reason), state=to)
    if to in ("succeeded", "completed_with_errors") and frm == "running":
        return _entry(ev.at, "finished", f"主流程结束：{STATE_ZH[to]}", state=to)
    if to in ("succeeded", "completed_with_errors") and frm in ("stopped", "failed"):
        # a resume subtask finished the main run (C5 1.2 edges, 01 §3.1)
        return _entry(ev.at, "finished", f"继续运行后主流程结束：{STATE_ZH[to]}", state=to)
    return None


def build(task: P.Task, subtasks: list[P.Subtask], events: list[P.Event]) -> list[dict]:
    """``events``: every audit event of the task, any order."""
    events = sorted(events, key=lambda e: e.id)
    out: list[tuple[int, int, dict]] = []

    def add(entry: dict | None) -> None:
        if entry is not None:
            out.append((entry["at"], len(out), entry))

    add(_entry(task.created_at, "created", "任务创建"))
    started = [False]
    for ev in events:
        if ev.action in ("task.state", "subtask.state"):
            add(_state_event(ev, started))
    # the row fills what the events do not say (purged after 90 days, or never written)
    if not started[0] and task.started_at is not None:
        add(_entry(task.started_at, "started", "开始运行", state="running"))
    ended = any(e["kind"] in ("finished", "stopped", "failed") for _, _, e in out)
    if not ended and task.finished_at is not None and task.state in P.TERMINAL_STATES:
        kind = {"stopped": "stopped", "failed": "failed"}.get(task.state, "finished")
        text = {"stopped": _with_reason("任务已停止", task.state_reason),
                "failed": _with_reason("任务失败", task.state_reason)}.get(
            kind, f"主流程结束：{STATE_ZH[task.state]}")
        add(_entry(task.finished_at, kind, text, state=task.state))
    for ev in events:
        if ev.action == "task.revision":
            rev = (ev.detail or {}).get("revision")
            if isinstance(rev, int):
                add(_entry(ev.at, "revision", f"结果版本 r{rev:04d} 生效", revision=rev,
                           subtask_id=(ev.detail or {}).get("subtask_id")))
    for s in subtasks:
        name = KIND_ZH.get(s.kind, s.kind)
        if s.started_at is not None:
            add(_entry(s.started_at, "subtask_started", f"{name}开始", state="running",
                       subtask_id=s.id))
        if s.finished_at is not None and s.state in P.TERMINAL_STATES:
            add(_entry(s.finished_at, "subtask_finished",
                       _with_reason(f"{name}结束：{STATE_ZH[s.state]}", s.state_reason),
                       state=s.state, subtask_id=s.id, revision=s.result_rev))
    return [entry for _, _, entry in sorted(out, key=lambda x: (x[0], x[1]))]
