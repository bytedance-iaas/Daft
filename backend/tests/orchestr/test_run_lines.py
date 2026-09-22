"""A run turns a command's C3 stderr lines into task logs, SSE events, progress and usage."""
from __future__ import annotations

import json

from daemon.orchestr.runbase import Run

from .conftest import seed_task

LINES = (
    '{"kind": "progress", "stage": "vlm", "done": 2, "total": 4, "eta_s": 3.5}',
    '{"kind": "log", "level": "warn", "msg": "ep000001 模型超时，重试", "episode_index": 1}',
    '{"kind": "usage", "module": "task_success", "call_kind": "probe", "model": "fake-vlm",'
    ' "prompt_tokens": 10, "completion_tokens": 2, "requests": 1, "ledger": "actual"}',
    '{"kind": "throttle", "backend": "fake", "limit": 8, "reason": "429"}',
    "Traceback (most recent call last):",
)


def test_c3_lines_become_logs_events_progress_and_usage(daemon):
    d = daemon()
    rt = d.rt
    task = seed_task(rt.repo, state="created")
    run = Run(d.orch, task)
    run.plan_progress(["vlm"])
    run.progress("vlm", state="running", total=4)
    handle = run._line_handler("vlm")
    for line in LINES:
        handle(line)
    run.close()                                          # flushes usage and progress

    stage = rt.repo.get_task(task.id).progress["stages"][0]
    assert (stage["id"], stage["state"], stage["done"], stage["total"]) == ("vlm", "running", 2, 4)
    assert stage["eta_s"] == 3.5 and stage["elapsed_s"] is not None
    usage = rt.repo.usage_buckets(task.id, ledger="actual")
    assert [(b.module_id, b.call_kind, b.model_name, b.prompt_tokens, b.requests)
            for b in usage] == [("task_success", "probe", "fake-vlm", 10, 1)]

    with open(rt.logs.log_path(task.id, "vlm"), encoding="utf-8") as fh:
        stored = [json.loads(line) for line in fh]
    shown = [(line["level"], line["msg"]) for line in stored if line["kind"] == "log"]
    assert ("warn", "ep000001 模型超时，重试") in shown
    assert ("warn", "模型服务 fake 限流：并发降到 8（429）") in shown       # kept on the logs page
    assert ("warn", "Traceback (most recent call last):") in shown       # not C3, not lost
    body = d.api("GET", f"/tasks/{task.id}/logs", params={"stage": "vlm"}).json()
    assert {line["msg"] for line in body["items"]} >= {m for _, m in shown}

    rt.hub.flush()
    events = [(e.event, e.data) for e in rt.hub.buffered(task.id)]
    kinds = {name for name, _ in events}
    assert {"progress", "log", "usage"} <= kinds
    logs = [data for name, data in events if name == "log"]
    assert any(data.get("episode_index") == 1 for data in logs)
    usage_totals = [data for name, data in events if name == "usage"][-1]
    assert usage_totals["prompt_tokens"] == 10 and usage_totals["requests"] == 1
