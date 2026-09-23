"""Repository rows -> C4 response bodies (``Task``, ``TaskListItem``, ``Subtask``, ``UsageReport``...).

Everything a route returns goes through here, and the tests validate each body
against its OpenAPI schema (C4 1.2: ``Task.vlm`` is a ``TaskVlm`` with the
snapshot frozen at start; a deleted access key shows as ``credential: null``).
"""
from __future__ import annotations

import pathlib
from typing import Any
from urllib.parse import quote

from curation.contracts import modules as registry

from .repo import protocol as P
from .repo.extras import dataset_format

_STAGE_KEYS = ("id", "state", "done", "total", "elapsed_s", "eta_s", "note", "pipeline")
_SUMMARY_KEYS = ("total", "passed", "rejected", "held", "review", "pass_rate")
_PARAM_KEYS = ("start_now", "export", "batch_size", "vlm_retry", "vlm_hedge",
               "vlm_timeouts_s", "clips", "limits")
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens",
               "requests", "requests_unknown_usage")
_MODULE_ORDER = {mid: i for i, mid in enumerate(registry.ids())}


class Links:
    """Links for agents (D22): absolute with ``publicBaseUrl``, else relative + ``absolute: false``."""

    def __init__(self, base_path: str, public_base_url: str = ""):
        self.base_path = base_path
        self.public_base_url = public_base_url

    def url(self, route: str) -> dict:
        path = f"{self.base_path}{route}"
        if self.public_base_url:
            return {"url": f"{self.public_base_url}{path}"}
        return {"url": path, "absolute": False}

    def link(self, rel: str, title: str, route: str) -> dict:
        return {"rel": rel, "title": title, **self.url(route)}

    def for_task(self, task_id: str, *, result_rev: int, pending_adjudication: int) -> list[dict]:
        tid = quote(task_id, safe="")
        out = [self.link("task", "Open task", f"/tasks/{tid}")]
        if result_rev >= 1:
            out.append(self.link("report", "Open QA report", f"/tasks/{tid}/report"))
        if pending_adjudication > 0:
            noun = "episode needs" if pending_adjudication == 1 else "episodes need"
            out.append(self.link("adjudication", f"{pending_adjudication} {noun} human judgement",
                                 f"/tasks/{tid}/adjudication?status=pending"))
        return out


def dataset_name(uri: str) -> str:
    """The last path segment of the input (``tos://b/datasets/droid_100`` -> ``droid_100``)."""
    s = (uri or "").rstrip("/")
    if "://" in s:
        s = s.split("://", 1)[1]
    name = s.rsplit("/", 1)[-1] if "/" in s else s
    return name or uri


def stage_progress(progress: dict | None) -> dict:
    stages = []
    for st in (progress or {}).get("stages") or []:
        if isinstance(st, dict) and {"id", "state", "done", "total"} <= set(st):
            stages.append({k: st[k] for k in _STAGE_KEYS if k in st})
    return {"stages": stages}


def summary(value: dict | None) -> dict | None:
    if not isinstance(value, dict) or not set(_SUMMARY_KEYS) <= set(value):
        return None
    out = {k: value[k] for k in _SUMMARY_KEYS}
    if _count(value.get("skipped")):                  # C4 1.4: only when some were (D40)
        out["skipped"] = value["skipped"]
    return out


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def pending_adjudication(task: P.Task) -> int:
    """W5 keeps ``summary.pending_adjudication`` current; before that, every review item counts.
    (``daemon.repo.extras.adjudication_backlog`` counts the same way.)"""
    s = task.summary if isinstance(task.summary, dict) else {}
    for key in ("pending_adjudication", "review"):
        if _count(s.get(key)) is not None:
            return s[key]
    return 0


def params(value: dict | None) -> dict:
    return {k: v for k, v in (value or {}).items() if k in _PARAM_KEYS}


def usage_totals(buckets: list[P.UsageBucket]) -> dict:
    return {k: sum(int(getattr(b, k)) for b in buckets) for k in _USAGE_KEYS}


def usage_row(b: P.UsageBucket) -> dict:
    return {"subtask_id": b.subtask_id, "module_id": b.module_id, "call_kind": b.call_kind,
            "model_name": b.model_name, **{k: int(getattr(b, k)) for k in _USAGE_KEYS}}


def usage_report(actual: list[P.UsageBucket], attributed: list[P.UsageBucket]) -> dict:
    return {"totals": usage_totals(actual), "actual": [usage_row(b) for b in actual],
            "attributed": [usage_row(b) for b in attributed]}


def module_state(m: P.TaskModule, now: int) -> dict:
    try:
        spec = registry.get(m.module_id)
    except KeyError:                     # a module retired from the registry after the task ran
        spec = None
    elapsed = None
    if m.started_at is not None:
        elapsed = round(((m.finished_at or now) - m.started_at) / 1000.0, 3)
    return {"id": m.module_id, "name": spec.name_zh if spec else m.module_id,
            "selected": bool(m.selected), "availability": m.availability,
            "unavailable_reason": m.unavailable_reason, "state": m.state,
            "episodes_total": int(m.episodes_total), "episodes_error": int(m.episodes_error),
            "elapsed_s": elapsed, "error": m.error}


def sorted_modules(rows: list[P.TaskModule]) -> list[P.TaskModule]:
    return sorted(rows, key=lambda m: (_MODULE_ORDER.get(m.module_id, len(_MODULE_ORDER)),
                                       m.module_id))


def module_counts(rows: list[P.TaskModule]) -> dict:
    """``selected`` plus, per module state, how many selected modules are in it."""
    selected = [m for m in rows if m.selected]
    out: dict[str, int] = {"selected": len(selected)}
    for m in selected:
        out[m.state] = out.get(m.state, 0) + 1
    return out


def subtask(s: P.Subtask) -> dict:
    scope = {k: v for k, v in (s.scope or {}).items()
             if k in ("modules", "episodes", "relabel_rerun")}
    return {"id": s.id, "task_id": s.task_id, "kind": s.kind, "scope": scope, "state": s.state,
            "state_reason": s.state_reason, "progress": s.progress, "created_at": s.created_at,
            "started_at": s.started_at, "finished_at": s.finished_at, "result_rev": s.result_rev}


class Names:
    """Resolves credential and model ids to the names the API speaks, cached per request."""

    def __init__(self, repo: P.Repository, owner: str):
        self._repo, self._owner = repo, owner
        self._creds: dict[str, str | None] = {}
        self._models: dict[str, tuple[str, str]] | None = None

    def credential(self, cred_id: str | None) -> str | None:
        """The key's name; None when there is none or it was deleted (the task keeps its
        report, rebind-credentials gives it a new one)."""
        if not cred_id:
            return None
        if cred_id not in self._creds:
            try:
                self._creds[cred_id] = self._repo.get_credential(cred_id, owner=self._owner).name
            except P.NotFound:
                self._creds[cred_id] = None
        return self._creds[cred_id]

    def model(self, model_id: str | None) -> tuple[str, str] | None:
        if not model_id:
            return None
        if self._models is None:
            self._models = {m.id: (b.name, m.model_name)
                            for b in self._repo.list_vlm_backends(owner=self._owner) for m in b.models}
        return self._models.get(model_id)


def input_ref(task: P.Task, names: Names) -> dict:
    out: dict[str, Any] = {"source": task.input_source, "uri": task.input_uri}
    if task.input_region:
        out["region"] = task.input_region
    if task.input_source == "tos" or task.input_cred_id:
        out["credential"] = names.credential(task.input_cred_id)
    return out


def output_ref(task: P.Task, names: Names) -> dict:
    out: dict[str, Any] = {"uri": task.output_uri, "credential": names.credential(task.output_cred_id)}
    if task.output_region:
        out["region"] = task.output_region
    return out


def task_vlm(task: P.Task, names: Names) -> dict | None:
    """C4 ``TaskVlm``: the chosen backend and model by name, and what start froze (P17).
    A backend deleted after the task finished leaves the names in the snapshot."""
    snap = task.vlm_snapshot if isinstance(task.vlm_snapshot, dict) else None
    pair = names.model(task.vlm_model_id)
    if pair is None:
        backend, model = (snap or {}).get("backend"), (snap or {}).get("model")
        if not (isinstance(backend, str) and isinstance(model, str)):
            return None
        pair = (backend, model)
    return {"backend": pair[0], "model": pair[1], "reasoning_effort": task.vlm_reasoning_effort,
            "snapshot": snap}


def source_summary(fp: dict | None) -> dict | None:
    if not isinstance(fp, dict):
        return None
    return {k: fp[k] for k in ("objects", "bytes", "digest") if k in fp}


def task_detail(task: P.Task, *, repo: P.Repository, names: Names, links: Links, now: int) -> dict:
    mods = sorted_modules(repo.get_task_modules(task.id))
    active = repo.active_subtask(task.id)
    pending = pending_adjudication(task)
    return {
        "id": task.id, "name": task.name, "note": task.note, "state": task.state,
        "state_reason": task.state_reason, "pause_reason": task.pause_reason,
        "input": input_ref(task, names), "dataset_id": task.dataset_id, "output": output_ref(task, names),
        "run_id": task.run_id, "episodes": task.episode_selector,
        "embodiment_id": task.embodiment_id, "vlm": task_vlm(task, names),
        "params": params(task.params), "source": source_summary(task.source_fingerprint),
        "progress": stage_progress(task.progress),
        "modules": [module_state(m, now) for m in mods],
        "summary": summary(task.summary), "result_rev": task.result_rev,
        "usage": usage_totals(repo.usage_buckets(task.id, ledger="actual")),
        "pending_adjudication": pending, "delivery_stale": bool(task.delivery_stale),
        "active_subtask": subtask(active) if active else None,
        "created_at": task.created_at, "updated_at": task.updated_at,
        "started_at": task.started_at, "finished_at": task.finished_at,
        "deleted_at": task.deleted_at,
        "links": links.for_task(task.id, result_rev=task.result_rev, pending_adjudication=pending),
    }


def task_list_item(task: P.Task, *, repo: P.Repository) -> dict:
    active = repo.active_subtask(task.id)
    mods = sorted_modules(repo.get_task_modules(task.id))
    return {
        "id": task.id, "name": task.name, "state": task.state, "pause_reason": task.pause_reason,
        "dataset": dataset_name(task.input_uri), "created_at": task.created_at,
        "progress": stage_progress(task.progress), "summary": summary(task.summary),
        "pending_adjudication": pending_adjudication(task),
        "delivery_stale": bool(task.delivery_stale),
        "active_subtask": active.id if active else None,
        "modules": [m.module_id for m in mods if m.selected],
        "dataset_id": task.dataset_id,
        "module_counts": module_counts(mods),
        "usage": usage_totals(repo.usage_buckets(task.id, ledger="actual")),
        "deleted_at": task.deleted_at,
    }


def etag(task: P.Task) -> str:
    return f'"{task.updated_at}"'


# ---------------------------------------------------------------------------
# datasets (D36, D37)
# ---------------------------------------------------------------------------

def task_ref(task: P.Task) -> dict:
    return {"id": task.id, "name": task.name, "state": task.state, "created_at": task.created_at}


def dataset_item(ds: P.Dataset, last_task: P.Task | None) -> dict:
    """C4 ``DatasetItem``; format, episode count and robot type come from the kept preflight."""
    pf = ds.preflight if isinstance(ds.preflight, dict) else {}
    info = pf.get("dataset") if isinstance(pf.get("dataset"), dict) else {}
    robot = info.get("robot_type")
    return {"id": ds.id, "name": ds.name, "source": ds.source, "uri": ds.uri, "region": ds.region,
            "format": dataset_format(pf), "episode_count": _count(info.get("episode_count")),
            "robot_type": robot if isinstance(robot, str) else None,
            "check_state": ds.check_state, "checked_at": ds.checked_at,
            "preflighted_at": ds.preflighted_at, "created_at": ds.created_at,
            "last_task": task_ref(last_task) if last_task is not None else None}


def dataset_check(check: P.DatasetCheck) -> dict:
    change = check.change if isinstance(check.change, dict) else None
    return {"at": check.at, "trigger": check.trigger, "result": check.result, "change": change}


def listing(fp: dict | None) -> dict:
    """The kept file listing's summary; C2 ``source-manifest`` calls the object count ``count``."""
    fp = fp if isinstance(fp, dict) else {}
    objects = fp.get("objects", fp.get("count"))
    digest = fp.get("digest")
    return {"objects": _count(objects) or 0, "bytes": _count(fp.get("bytes")) or 0,
            "digest": digest if isinstance(digest, str) else ""}


def dataset_detail(ds: P.Dataset, *, tasks: list[P.Task], checks: list[P.DatasetCheck],
                   names: Names) -> dict:
    """C4 ``DatasetDetail``: ``tasks`` newest first (the first is ``last_task``).

    ``links`` stays empty: C4 ``Link.rel`` has no value for a dataset page yet.
    """
    return {**dataset_item(ds, tasks[0] if tasks else None),
            "note": ds.note, "credential": names.credential(ds.credential_id),
            "preflight": ds.preflight, "meta_fingerprint": ds.meta_fingerprint,
            "listing": listing(ds.source_fingerprint),
            "checks": [dataset_check(c) for c in checks],
            "tasks": [task_ref(t) for t in tasks], "links": []}


def is_under(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
