"""``curation task ...`` - a thin client of the Daemon's REST API (design doc 02 §3.11, D22).

For Agents and scripts. Each subcommand is one call of C4
(``docs/contracts/openapi.yaml``); with ``--json`` the response body is printed
as the Daemon sent it, ``links`` included, so an Agent can hand a person the
page that needs them. The atomic commands never produce links; only these do.

Connection: ``--url`` or ``$CURATOR_URL`` (the Daemon's address including its
mount prefix, e.g. ``https://host/curation``; ``/api/v1`` is appended), and
``$CURATOR_USER`` / ``$CURATOR_PASSWORD`` for HTTP Basic. The password is read
from the environment only. Nothing is retried.

Errors keep the CLI's exit codes; the REST error body travels in
``error.details.rest_error``:

* cannot connect, 401/403, 5xx, ``precheck_failed`` -> 3 (``input_unreachable``)
* ``source_changed`` -> 6
* any other 4xx (``not_found``, ``task_state_conflict``, ``validation_failed``, ...) -> 2
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

from .errors import CliError, InputUnreachable, SourceChanged, UsageError
from .framework import Context, Result

TERMINAL_STATES = frozenset({"succeeded", "completed_with_errors", "failed", "stopped"})
ACTIONS = ("start", "pause", "resume", "stop")
DEFAULT_POLL_S = 5.0                     # P8: the list page polls every 5 s as well


class DaemonClient:
    def __init__(self, base_url: str, user: str | None = None, password: str | None = None,
                 timeout: float = 30.0):
        base = base_url.strip().rstrip("/")
        if base.endswith("/api/v1"):
            base = base[: -len("/api/v1")]
        self.base = base
        self.timeout = timeout
        self._auth = (user, password) if user and password else None

    @classmethod
    def from_env(cls, args: argparse.Namespace, env=None) -> DaemonClient:
        env = os.environ if env is None else env
        url = (getattr(args, "url", None) or env.get("CURATOR_URL") or "").strip()
        if not url:
            raise UsageError("no Daemon address: set CURATOR_URL or pass --url")
        if not url.startswith(("http://", "https://")):
            raise UsageError(f"the Daemon address must start with http:// or https://, "
                             f"got {url!r}")
        user = (env.get("CURATOR_USER") or "").strip()
        password = env.get("CURATOR_PASSWORD") or ""
        if bool(user) != bool(password):
            missing = "CURATOR_PASSWORD" if user else "CURATOR_USER"
            raise UsageError(f"{missing} is not set while its partner is; set both or neither")
        return cls(url, user or None, password or None,
                   timeout=float(getattr(args, "http_timeout", None) or 30.0))

    def call(self, method: str, path: str, *, params: dict | None = None,
             body: Any = None, idempotency_key: str | None = None) -> Any:
        import requests

        url = f"{self.base}/api/v1{path}"
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            resp = requests.request(method, url, params=_clean(params), data=data,
                                    headers=headers, auth=self._auth, timeout=self.timeout)
        except requests.RequestException as e:
            raise InputUnreachable(f"cannot reach the Curator daemon at {self.base}: "
                                   f"{type(e).__name__}", {"url": self.base}) from None
        try:
            payload = resp.json() if resp.content else None
        except ValueError:
            payload = None
        if resp.status_code >= 400:
            raise _http_error(method, path, resp.status_code, payload, resp.text)
        if resp.content and payload is None:
            raise InputUnreachable(f"the Daemon at {self.base} did not answer JSON "
                                   f"({method} {path}, HTTP {resp.status_code})",
                                   {"url": self.base, "http_status": resp.status_code})
        return payload


def _clean(params: dict | None) -> dict | None:
    return {k: v for k, v in (params or {}).items() if v is not None} or None


def _http_error(method: str, path: str, status: int, payload: Any, text: str) -> CliError:
    err = payload.get("error") if isinstance(payload, dict) else None
    err = err if isinstance(err, dict) else None
    code = str(err.get("code") or "") if err else ""
    message = str(err.get("message") or "") if err else (text or "").strip()[:200]
    details: dict[str, Any] = {"http_status": status, "method": method, "path": path}
    if err:
        details["rest_error"] = err
    summary = f"the Daemon refused {method} {path}: HTTP {status}"
    if code:
        summary += f" {code}"
    if message:
        summary += f" - {message}"
    if code == "source_changed":
        return SourceChanged(summary, details)
    if status in (401, 403) or status >= 500 or code == "precheck_failed":
        return InputUnreachable(summary, details)
    return UsageError(summary, details)


# ---------------------------------------------------------------- parser


def add_parser(sub, json_parent) -> None:
    client = argparse.ArgumentParser(add_help=False)
    g = client.add_argument_group("connection")
    g.add_argument("--url", metavar="URL",
                   help="Daemon address incl. mount prefix (default: $CURATOR_URL); "
                        "credentials come from $CURATOR_USER / $CURATOR_PASSWORD")
    g.add_argument("--http-timeout", type=float, default=30.0, metavar="S",
                   help="seconds to wait for one response (default 30)")
    parents = [json_parent, client]
    idem = argparse.ArgumentParser(add_help=False)
    idem.add_argument("--idempotency-key", metavar="KEY",
                      help="reuse the same key when retrying so the Daemon does not act twice")
    waiting = argparse.ArgumentParser(add_help=False)
    waiting.add_argument("--timeout", type=float, default=None, metavar="S",
                         help="stop waiting after S seconds and print the task as it is")
    waiting.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_S, metavar="S",
                         help=f"seconds between polls (default {DEFAULT_POLL_S:g})")

    task = sub.add_parser("task", help="talk to the Curator daemon: create, list, wait, ...",
                          description="Client of the Curator daemon's REST API for Agents and "
                                      "scripts. With --json the Daemon's response is printed "
                                      "as is, links included.")
    tsub = task.add_subparsers(dest="task_command", metavar="<subcommand>")
    tsub.required = True

    p = tsub.add_parser("create", parents=parents + [idem, waiting],
                        help="create a task from a JSON file (POST /tasks)")
    p.add_argument("--file", required=True, metavar="FILE",
                   help="task JSON as in the REST API (TaskCreate); - reads stdin")
    p.add_argument("--wait", action="store_true",
                   help="then wait until the task ends and print it")
    p.set_defaults(func=cmd_create)

    p = tsub.add_parser("list", parents=parents, help="list tasks, newest first (GET /tasks)")
    p.add_argument("--state", help="only tasks in this state (or deleted)")
    p.add_argument("--page", type=int, default=None, metavar="N")
    p.add_argument("--page-size", type=int, default=None, choices=(10, 20, 50, 100))
    p.add_argument("--query", "-q", dest="q", metavar="TEXT", help="search name or id")
    p.add_argument("--delivery", metavar="URI", help="tasks writing to this delivery directory")
    p.set_defaults(func=cmd_list)

    p = tsub.add_parser("get", parents=parents, help="show one task (GET /tasks/{id})")
    p.add_argument("task_id", metavar="ID")
    p.set_defaults(func=cmd_get)

    p = tsub.add_parser("wait", parents=parents + [waiting],
                        help="poll until the task and its subtask are finished")
    p.add_argument("task_id", metavar="ID")
    p.set_defaults(func=cmd_wait)

    for action in ACTIONS:
        p = tsub.add_parser(action, parents=parents + [idem],
                            help=f"{action} the task (POST /tasks/{{id}}/actions/{action})")
        p.add_argument("task_id", metavar="ID")
        p.set_defaults(func=cmd_action, action=action)

    p = tsub.add_parser("retry", parents=parents + [idem],
                        help="re-run only the episodes that errored (POST /tasks/{id}/retry)")
    p.add_argument("task_id", metavar="ID")
    p.add_argument("--modules", metavar="IDS",
                   help="comma-separated modules (default: every module with errors)")
    p.set_defaults(func=cmd_retry)

    p = tsub.add_parser("continue", parents=parents + [idem],
                        help="continue a stopped or failed task (POST /tasks/{id}/continue)")
    p.add_argument("task_id", metavar="ID")
    p.set_defaults(func=cmd_continue)

    p = tsub.add_parser("report", parents=parents,
                        help="the QA report (GET /tasks/{id}/report)")
    p.add_argument("task_id", metavar="ID")
    p.add_argument("--rev", type=int, default=None, metavar="N", help="an older result revision")
    p.set_defaults(func=cmd_report)

    p = tsub.add_parser("adjudication", parents=parents,
                        help="how many episodes wait for a person, and the page to judge them")
    p.add_argument("task_id", metavar="ID")
    p.set_defaults(func=cmd_adjudication)


# ---------------------------------------------------------------- commands


def _path_id(task_id: str) -> str:
    from urllib.parse import quote

    task_id = str(task_id or "").strip()
    if not task_id:
        raise UsageError("a task id is required")
    return quote(task_id, safe="")


def cmd_create(ctx: Context, args: argparse.Namespace) -> Result:
    body = _read_task_file(args.file)
    client = DaemonClient.from_env(args)
    created = client.call("POST", "/tasks", body=body, idempotency_key=args.idempotency_key)
    ctx.log("info", f"created task {created.get('id')} ({created.get('state')})")
    if not args.wait:
        return Result(created, human=_render_created(created))
    return _wait(ctx, client, str(created.get("id")), args)


def _read_task_file(path: str) -> dict:
    import sys

    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        body = json.loads(text)
    except OSError as e:
        raise UsageError(f"--file {path}: cannot read it: {e}") from None
    except ValueError as e:
        raise UsageError(f"--file {path}: not valid JSON: {e}") from None
    if not isinstance(body, dict):
        raise UsageError(f"--file {path}: a task is a JSON object")
    return body


def cmd_list(ctx: Context, args: argparse.Namespace) -> Result:
    client = DaemonClient.from_env(args)
    page = client.call("GET", "/tasks", params={
        "page": args.page, "page_size": args.page_size, "state": args.state, "q": args.q,
        "delivery": args.delivery})
    return Result(page, human=_render_list(page))


def cmd_get(ctx: Context, args: argparse.Namespace) -> Result:
    client = DaemonClient.from_env(args)
    task = client.call("GET", f"/tasks/{_path_id(args.task_id)}")
    return Result(task, human=_render_task(task))


def cmd_wait(ctx: Context, args: argparse.Namespace) -> Result:
    return _wait(ctx, DaemonClient.from_env(args), args.task_id, args)


def finished(task: dict) -> bool:
    """A task is done when it is in a terminal state and runs no subtask."""
    return task.get("state") in TERMINAL_STATES and not task.get("active_subtask")


def _wait(ctx: Context, client: DaemonClient, task_id: str, args) -> Result:
    if args.poll_interval <= 0:
        raise UsageError("--poll-interval must be positive")
    started = time.monotonic()
    last_state = None
    told_created = False
    while True:
        task = client.call("GET", f"/tasks/{_path_id(task_id)}")
        state = task.get("state")
        if state != last_state:
            ctx.log("info", f"task {task_id}: {state}")
            last_state = state
        _report_progress(ctx, task)
        if finished(task):
            return Result(task, human=_render_task(task))
        if state == "created" and not told_created:
            ctx.log("warn", f"task {task_id} is waiting to be started: "
                            f"curation task start {task_id}")
            told_created = True
        if args.timeout is not None and time.monotonic() - started >= args.timeout:
            ctx.log("warn", f"stopped waiting after {args.timeout:g}s; task {task_id} is "
                            f"still {state}")
            return Result(task, human=_render_task(task))
        pause = args.poll_interval
        if args.timeout is not None:
            pause = min(pause, max(0.0, args.timeout - (time.monotonic() - started)))
        ctx.sleep(pause)


def _report_progress(ctx: Context, task: dict) -> None:
    stages = ((task.get("progress") or {}).get("stages")) or []
    for st in stages:
        if isinstance(st, dict) and st.get("state") == "running":
            kw = {"eta_s": st["eta_s"]} if isinstance(st.get("eta_s"), (int, float)) else {}
            ctx.progress(f"task:{st.get('id')}", int(st.get("done") or 0),
                         int(st.get("total") or 0), **kw)


def cmd_action(ctx: Context, args: argparse.Namespace) -> Result:
    client = DaemonClient.from_env(args)
    task = client.call("POST", f"/tasks/{_path_id(args.task_id)}/actions/{args.action}",
                       idempotency_key=args.idempotency_key)
    return Result(task, human=_render_task(task))


def cmd_retry(ctx: Context, args: argparse.Namespace) -> Result:
    body = None
    if args.modules:
        mods = [m.strip() for m in args.modules.split(",") if m.strip()]
        if not mods:
            raise UsageError("--modules lists no module")
        body = {"modules": mods}
    client = DaemonClient.from_env(args)
    created = client.call("POST", f"/tasks/{_path_id(args.task_id)}/retry", body=body,
                          idempotency_key=args.idempotency_key)
    return Result(created, human=_render_subtask(created))


def cmd_continue(ctx: Context, args: argparse.Namespace) -> Result:
    client = DaemonClient.from_env(args)
    created = client.call("POST", f"/tasks/{_path_id(args.task_id)}/continue",
                          idempotency_key=args.idempotency_key)
    return Result(created, human=_render_subtask(created))


def cmd_report(ctx: Context, args: argparse.Namespace) -> Result:
    client = DaemonClient.from_env(args)
    report = client.call("GET", f"/tasks/{_path_id(args.task_id)}/report",
                         params={"rev": args.rev})
    return Result(report, human=_render_report(report))


def cmd_adjudication(ctx: Context, args: argparse.Namespace) -> Result:
    """Counts from the queue plus the task's adjudication links; judging needs the web page."""
    client = DaemonClient.from_env(args)
    tid = _path_id(args.task_id)
    queue = client.call("GET", f"/tasks/{tid}/adjudication",
                        params={"status": "pending", "limit": 1})
    task = client.call("GET", f"/tasks/{tid}")
    counts = (queue or {}).get("counts") or {}
    links = [ln for ln in task.get("links") or [] if ln.get("rel") == "adjudication"]
    doc = {"task_id": task.get("id", args.task_id), "pending": counts.get("pending", 0),
           "counts": counts, "links": links}
    return Result(doc, human=_render_adjudication(doc))


# ---------------------------------------------------------------- rendering


def _render_links(links) -> list[str]:
    out = []
    for ln in links or []:
        rel = "" if ln.get("absolute", True) else " (relative: the Daemon has no public URL)"
        out.append(f"  {ln.get('title') or ln.get('rel')}: {ln.get('url')}{rel}")
    return out


def _render_created(created: dict) -> str:
    lines = [f"created {created.get('id')} ({created.get('state')})"]
    lines += [f"  warning: {w}" for w in created.get("warnings") or []]
    return "\n".join(lines + _render_links(created.get("links")))


def _render_task(task: dict) -> str:
    lines = [f"{task.get('id')}  {task.get('state')}  {task.get('name', '')}"]
    summary = task.get("summary")
    if summary:
        lines.append(f"  total {summary.get('total')}: passed {summary.get('passed')}, "
                     f"rejected {summary.get('rejected')}, held {summary.get('held')}, "
                     f"review {summary.get('review')}")
    if task.get("pending_adjudication"):
        lines.append(f"  {task['pending_adjudication']} episodes wait for a person")
    if task.get("active_subtask"):
        sub = task["active_subtask"]
        lines.append(f"  subtask {sub.get('id')} ({sub.get('kind')}): {sub.get('state')}")
    return "\n".join(lines + _render_links(task.get("links")))


def _render_list(page: dict) -> str:
    lines = [f"page {page.get('page')} of {page.get('total')} tasks"]
    for item in page.get("items") or []:
        lines.append(f"  {item.get('id')}  {item.get('state'):<22}  {item.get('name')}")
    return "\n".join(lines)


def _render_subtask(created: dict) -> str:
    sub = created.get("subtask") or {}
    return "\n".join([f"subtask {sub.get('id')} ({sub.get('kind')}): {sub.get('state')}"]
                     + _render_links(created.get("links")))


def _render_report(report: dict) -> str:
    counts = ((report.get("report") or {}).get("overview") or {}).get("counts") or {}
    head = f"report revision {report.get('revision')}"
    if counts:
        head += (f": total {counts.get('total')}, passed {counts.get('passed')}, "
                 f"rejected {counts.get('rejected')}, held {counts.get('held')}")
    return "\n".join([head] + _render_links(report.get("links")))


def _render_adjudication(doc: dict) -> str:
    lines = [f"{doc['task_id']}: {doc['pending']} episodes wait for a person"]
    if not doc["links"]:
        lines.append("  (no adjudication page to open)")
    return "\n".join(lines + _render_links(doc["links"]))
