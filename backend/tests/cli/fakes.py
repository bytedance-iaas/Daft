"""Offline stand-ins: an in-memory TOS and a stub of the Daemon's REST API.

Both are standard library only. The stub Daemon validates every response it
sends (and every request body it receives) against ``docs/contracts/openapi.yaml``
and records violations in ``problems``; tests assert that list stays empty, so
the client is exercised against contract-true answers.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from curation.contracts import schemas

# ---------------------------------------------------------------- TOS


class FakeTosError(Exception):
    def __init__(self, status_code: int, code: str, message: str = ""):
        super().__init__(f"{status_code} {code} {message}")
        self.status_code, self.code, self.message = status_code, code, message


class FakeCloud:
    """Buckets of ``key -> bytes``; ``acl`` limits which access keys may touch a bucket."""

    def __init__(self):
        self.buckets: dict[str, dict[str, bytes]] = {}
        self.acl: dict[str, set[str]] = {}
        self.clients: list[dict] = []         # one entry per client built: which key, where
        self.calls: list[tuple] = []
        #: (bucket, key) -> how many more GETs fail although the object is listed
        self.invisible: dict[tuple[str, str], int] = {}

    def bucket(self, name: str, *, readers=None) -> dict[str, bytes]:
        self.buckets.setdefault(name, {})
        if readers is not None:
            self.acl[name] = set(readers)
        return self.buckets[name]

    def upload_dir(self, local_root: str, bucket: str, prefix: str) -> None:
        b = self.bucket(bucket)
        for dirpath, _, files in os.walk(local_root):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, local_root).replace(os.sep, "/")
                with open(full, "rb") as fh:
                    b[f"{prefix}/{rel}" if prefix else rel] = fh.read()

    @staticmethod
    def etag(data: bytes) -> str:
        return '"' + hashlib.md5(data).hexdigest() + '"'

    def factory(self, creds, endpoint, region):
        self.clients.append({"access_key": creds.access_key, "source": creds.source,
                             "endpoint": endpoint, "region": region})
        return FakeTos(self, creds.access_key)


class FakeTos:
    """The five TosClientV2 methods the CLI uses."""

    def __init__(self, cloud: FakeCloud, access_key: str):
        self.cloud, self.ak = cloud, access_key

    def _objects(self, bucket: str) -> dict[str, bytes]:
        if bucket not in self.cloud.buckets:
            raise FakeTosError(404, "NoSuchBucket", "The specified bucket does not exist.")
        allowed = self.cloud.acl.get(bucket)
        if allowed is not None and self.ak not in allowed:
            raise FakeTosError(403, "AccessDenied", "Access Denied")
        return self.cloud.buckets[bucket]

    def list_objects_type2(self, bucket, prefix="", continuation_token=None, max_keys=1000,
                           **kw):
        self.cloud.calls.append(("list", bucket, prefix))
        objs = self._objects(bucket)
        keys = sorted(k for k in objs if k.startswith(prefix))
        start = int(continuation_token or 0)
        page = keys[start:start + 3]          # tiny pages: exercise pagination
        more = start + 3 < len(keys)
        return SimpleNamespace(
            contents=[SimpleNamespace(key=k, size=len(objs[k]), etag=FakeCloud.etag(objs[k]))
                      for k in page],
            is_truncated=more, next_continuation_token=str(start + 3) if more else None)

    def get_object(self, bucket, key, range_start=None, range_end=None, **kw):
        self.cloud.calls.append(("get", bucket, key, range_start, range_end))
        objs = self._objects(bucket)
        hidden = self.cloud.invisible.get((bucket, key), 0)
        if hidden:
            self.cloud.invisible[(bucket, key)] = hidden - 1
        if key not in objs or hidden:
            raise FakeTosError(404, "NoSuchKey", "The specified key does not exist.")
        data = objs[key]
        if range_start is not None:
            data = data[range_start:(range_end + 1) if range_end is not None else None]
        return SimpleNamespace(read=lambda: data)

    def head_object(self, bucket, key, **kw):
        self.cloud.calls.append(("head", bucket, key))
        objs = self._objects(bucket)
        if key not in objs:
            raise FakeTosError(404, "NoSuchKey")
        return SimpleNamespace(content_length=len(objs[key]), etag=FakeCloud.etag(objs[key]))

    def put_object(self, bucket, key, content=b"", **kw):
        self.cloud.calls.append(("put", bucket, key))
        self._objects(bucket)[key] = bytes(content)

    def delete_object(self, bucket, key, **kw):
        self.cloud.calls.append(("delete", bucket, key))
        self._objects(bucket).pop(key, None)


# ---------------------------------------------------------------- Daemon

API = "/api/v1"
OAS = "openapi.yaml#"
REF_TASK = OAS + "/components/schemas/Task"
REF_CREATED = OAS + "/components/schemas/TaskCreated"
REF_SUBTASK_CREATED = OAS + "/components/schemas/SubtaskCreated"
REF_ERROR = OAS + "/components/schemas/Error"
REF_TASK_CREATE = OAS + "/components/schemas/TaskCreate"
REF_LIST = OAS + "/paths/~1tasks/get/responses/200/content/application~1json/schema"
REF_REPORT = OAS + "/paths/~1tasks~1{id}~1report/get/responses/200/content/application~1json/schema"
REF_ADJ = (OAS + "/paths/~1tasks~1{id}~1adjudication/get/responses/200/content/"
           "application~1json/schema")
REF_RETRY_BODY = (OAS + "/paths/~1tasks~1{id}~1retry/post/requestBody/content/"
                  "application~1json/schema")

PUBLIC = "https://curator.example.com/curation"


def links(task_id: str, pending: int = 0) -> list[dict]:
    out = [{"rel": "task", "title": "Open task", "url": f"{PUBLIC}/tasks/{task_id}"},
           {"rel": "report", "title": "Open QA report", "url": f"{PUBLIC}/tasks/{task_id}/report"}]
    if pending:
        out.append({"rel": "adjudication", "title": f"{pending} episodes need human judgement",
                    "url": f"{PUBLIC}/tasks/{task_id}/adjudication?status=pending"})
    return out


def make_task(task_id: str = "task_01", state: str = "running", *, pending: int = 0,
              active_subtask: dict | None = None) -> dict:
    done = state in ("succeeded", "completed_with_errors")
    return {
        "id": task_id, "name": "droid first 50", "note": None, "state": state,
        "state_reason": None, "pause_reason": None,
        "input": {"source": "tos", "uri": "tos://bucket/datasets/droid_lerobot",
                  "region": "cn-beijing", "credential": "prod-tos"},
        "output": {"uri": "tos://bucket/deliveries/droid-50", "region": "cn-beijing",
                   "credential": "prod-tos"},
        "run_id": "20260921-101500", "episodes": {"mode": "head", "n": 50},
        "embodiment_id": "franka",
        "vlm": {"backend": "ark-prod", "model": "doubao-seed-2-0-pro-260215",
                "reasoning_effort": None},
        "params": {"export": True},
        "source": {"objects": 204, "bytes": 1520331122, "digest": "sha256:" + "0" * 64},
        "progress": {"stages": [
            {"id": "numeric", "state": "succeeded", "done": 50, "total": 50, "elapsed_s": 1},
            {"id": "vlm", "state": "succeeded" if done else "running", "done": 49 if done else 12,
             "total": 49, "elapsed_s": 40, "eta_s": None if done else 120}]},
        "modules": [{"id": "task_success", "name": "任务成败判定", "selected": True,
                     "availability": "available", "state": "succeeded" if done else "running",
                     "episodes_total": 49, "episodes_error": 0}],
        "summary": ({"total": 50, "passed": 41, "rejected": 7, "held": 2, "review": 10,
                     "pass_rate": 0.82} if done else None),
        "result_rev": 1 if done else 0,
        "usage": {"prompt_tokens": 1820, "completion_tokens": 64, "reasoning_tokens": 0,
                  "cached_tokens": 0, "requests": 3, "requests_unknown_usage": 0},
        "pending_adjudication": pending, "delivery_stale": False,
        "active_subtask": active_subtask,
        "created_at": 1758300000000, "updated_at": 1758300001000,
        "started_at": 1758300000500, "finished_at": 1758300009000 if done else None,
        "links": links(task_id, pending),
    }


def make_subtask(task_id: str, kind: str, modules=None) -> dict:
    return {"id": "sub_01", "task_id": task_id, "kind": kind,
            "scope": {"modules": list(modules or []), "episodes": "errors"},
            "state": "queued", "created_at": 1758300010000}


def report_doc() -> dict:
    path = schemas.contracts_dir() / "examples" / "report.json"
    return json.loads(path.read_text(encoding="utf-8"))["valid"][0]


class StubDaemon:
    """``ThreadingHTTPServer`` on 127.0.0.1 answering a slice of C4 from canned data."""

    ACTION_FROM = {"start": {"created"}, "pause": {"running", "queued"},
                   "resume": {"paused"}, "stop": {"running", "queued", "paused", "pausing"}}
    ACTION_TO = {"start": "queued", "pause": "pausing", "resume": "queued", "stop": "stopping"}

    def __init__(self, user: str = "agent", password: str = "s3cret-pw", port: int = 0):
        self.user, self.password, self.port = user, password, port
        self.tasks: dict[str, dict] = {}
        #: states GET /tasks/{id} walks through, one per request, the last one repeats
        self.state_script: dict[str, list[str]] = {}
        self.requests: list[dict] = []
        self.problems: list[str] = []
        self._server = None

    # lifecycle
    def __enter__(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):               # keep pytest output clean
                pass

            def do_GET(self):
                stub._handle(self, "GET")

            def do_POST(self):
                stub._handle(self, "POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/curation"

    # helpers
    def check(self, ref: str, doc, what: str) -> None:
        errs = schemas.errors(ref, doc)
        if errs:
            self.problems.append(f"{what}: {errs[:3]}")

    def _send(self, h, status: int, doc, ref: str | None) -> None:
        if ref:
            self.check(ref, doc, f"response {h.command} {h.path}")
        body = json.dumps(doc, ensure_ascii=False).encode("utf-8") if doc is not None else b""
        h.send_response(status)
        if body:
            h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _error(self, h, status: int, code: str, message: str, details=None) -> None:
        err = {"code": code, "message": message}
        if details:
            err["details"] = details
        self._send(h, status, {"error": err}, REF_ERROR)

    def _task_now(self, task_id: str) -> dict:
        task = self.tasks[task_id]
        script = self.state_script.get(task_id)
        if script:
            step = script.pop(0) if len(script) > 1 else script[0]
            kw = dict(step) if isinstance(step, dict) else {"state": step}
            kw.setdefault("pending", task.get("pending_adjudication", 0))
            self.tasks[task_id] = task = make_task(task_id, **kw)
        return task

    # routing
    def _handle(self, h, method: str) -> None:
        parsed = urlparse(h.path)
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        self.requests.append({"method": method, "path": parsed.path,
                              "query": parse_qs(parsed.query),
                              "headers": {k.lower(): v for k, v in h.headers.items()},
                              "body": body})
        auth = h.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(
            f"{self.user}:{self.password}".encode()).decode()
        if auth != expected:
            return self._error(h, 401, "unauthorized", "需要登录")
        path = parsed.path
        if not path.startswith("/curation" + API):
            return self._error(h, 404, "not_found", "没有这个接口")
        if method != "GET":
            # C4 1.2 (as the Daemon does it): a write carries JSON, body or not
            ctype = (h.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return self._error(h, 400, "validation_failed",
                                   "写接口只接受 JSON：请带上 Content-Type: application/json"
                                   "（没有请求体也要带）")
        path = path[len("/curation" + API):]
        q = parse_qs(parsed.query)
        if method == "POST" and re.fullmatch(r"/tasks/[^/]+", path):
            return self._error(h, 405, "method_not_allowed", "这个地址不支持该请求方法")

        if method == "POST" and path == "/tasks":
            self.check(REF_TASK_CREATE, body, "request POST /tasks")
            task = make_task("task_new", "queued")
            self.tasks["task_new"] = task
            return self._send(h, 201, {"id": "task_new", "state": "queued",
                                       "created_at": task["created_at"], "warnings": [],
                                       "links": links("task_new")}, REF_CREATED)
        if method == "GET" and path == "/tasks":
            items = [_list_item(t) for t in self.tasks.values()]
            state = q.get("state", [None])[0]
            if state:
                items = [i for i in items if i["state"] == state]
            page = int(q.get("page", ["1"])[0])
            size = int(q.get("page_size", ["20"])[0])
            return self._send(h, 200, {"items": items[(page - 1) * size: page * size],
                                       "page": page, "page_size": size,
                                       "total": len(items)}, REF_LIST)
        m = re.fullmatch(r"/tasks/([^/]+)(/.*)?", path)
        if not m:
            return self._error(h, 404, "not_found", "没有这个接口")
        task_id, rest = m.group(1), m.group(2) or ""
        if task_id == "task_boom":
            return self._error(h, 500, "internal", "服务内部错误")
        if task_id not in self.tasks:
            return self._error(h, 404, "not_found", "任务不存在", {"id": task_id})
        if method == "GET" and rest == "":
            return self._send(h, 200, self._task_now(task_id), REF_TASK)
        m2 = re.fullmatch(r"/actions/(\w+)", rest)
        if method == "POST" and m2:
            action = m2.group(1)
            task = self.tasks[task_id]
            if task["state"] not in self.ACTION_FROM.get(action, set()):
                return self._error(h, 409, "task_state_conflict",
                                   f"任务当前是 {task['state']}，不能 {action}",
                                   {"state": task["state"]})
            task = make_task(task_id, self.ACTION_TO[action])
            self.tasks[task_id] = task
            return self._send(h, 200, task, REF_TASK)
        if method == "POST" and rest == "/retry":
            if body is not None:
                self.check(REF_RETRY_BODY, body, "request POST retry")
            mods = (body or {}).get("modules")
            return self._send(h, 202, {"subtask": make_subtask(task_id, "retry", mods),
                                       "links": links(task_id)}, REF_SUBTASK_CREATED)
        if method == "POST" and rest == "/continue":
            if self.tasks[task_id].get("state_reason") == "source_changed":
                return self._error(h, 409, "source_changed", "源数据已变化，请重新预检后新建任务")
            return self._send(h, 202, {"subtask": make_subtask(task_id, "resume"),
                                       "links": links(task_id)}, REF_SUBTASK_CREATED)
        if method == "GET" and rest == "/report":
            rev = int(q.get("rev", ["2"])[0])
            report = copy.deepcopy(report_doc())
            report["revision"] = rev
            return self._send(h, 200, {"revision": rev, "report": report,
                                       "links": links(task_id)}, REF_REPORT)
        if method == "GET" and rest == "/adjudication":
            pending = self.tasks[task_id].get("pending_adjudication", 0)
            return self._send(h, 200, {"items": [], "next_cursor": None, "has_more": False,
                                       "counts": {"decided": 1, "pending": pending,
                                                  "unapplied": 1}}, REF_ADJ)
        return self._error(h, 404, "not_found", "没有这个接口")


def _list_item(task: dict) -> dict:
    return {"id": task["id"], "name": task["name"], "state": task["state"],
            "pause_reason": None, "dataset": task["input"]["uri"],
            "created_at": task["created_at"], "progress": task["progress"],
            "summary": task["summary"], "pending_adjudication": task["pending_adjudication"],
            "delivery_stale": task["delivery_stale"], "active_subtask": None,
            "modules": [m["id"] for m in task["modules"]], "dataset_id": task.get("dataset_id"),
            "module_counts": {"succeeded": 1}, "usage": task["usage"]}
