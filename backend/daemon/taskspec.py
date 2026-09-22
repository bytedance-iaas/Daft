"""Validate and normalize a task's configuration (C4 ``TaskCreate`` / ``TaskPatch``).

``PATCH /tasks/{id}`` uses it now; W5's ``POST /tasks`` should reuse
:func:`resolve_config` so both paths accept exactly the same inputs. Names in
the request (access key, VLM backend and model) are resolved to ids here,
``tos://`` addresses are normalized, the episode expression is parsed with v1's
parser, module choices are checked against the registry (C1) and against the
preflight snapshot's availability (design doc 05, section 4).

What a task stores for a ``created`` (not yet started) task:
``task.preflight`` holds the preflight result (C2 ``preflight.schema.json``) the
configuration was checked against; starting re-checks it (W5, design doc 03 §3).

The input (C4 ``InputSpec``) is a registered dataset, ``{"dataset_id": ...}``,
or an address given in full; both end up as the same task fields (03 §12). A
full address is linked to the registration of that address when there is one;
registering a new address runs the CLI and is W5's (``POST /datasets``, and
``POST /tasks`` after its preflight).
"""
from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from curation import episode_select
from curation.contracts import modules as registry

from .errors import ApiError
from .repo import protocol as P
from .settings import Settings

#: POST /preflight results are kept 30 minutes (design doc 01, section 2.8).
PREFLIGHT_MAX_AGE_MS = 30 * 60 * 1000

#: After start only these may change (D20).
EDITABLE_AFTER_START = frozenset({"name", "note"})

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_MAX_EXPLICIT_EPISODES = episode_select.MAX_SPAN


def _bad(message: str, field_name: str | None = None) -> ApiError:
    details = {"errors": [{"field": field_name, "problem": message}]} if field_name else None
    return ApiError("validation_failed", message, details=details)


def normalize_tos_uri(uri: str, *, field_name: str = "uri") -> str:
    """``tos://Bucket//a/b/`` -> ``tos://bucket/a/b`` (same rules as v1 ``tos_store.parse_tos_url``)."""
    s = str(uri or "").strip()
    if not s.lower().startswith("tos://"):
        raise _bad(f"{field_name} 必须是 tos:// 地址（写法：tos://存储桶/前缀）", field_name)
    bucket, _, prefix = s[len("tos://"):].partition("/")
    bucket = bucket.lower()
    if not _BUCKET_RE.match(bucket):
        raise _bad(f"{field_name} 里的存储桶名不合法：{bucket!r}（3-63 位小写字母、数字、中划线）",
                   field_name)
    segs = [p for p in prefix.split("/") if p]
    if any(p in (".", "..") for p in segs) or "\\" in prefix:
        raise _bad(f"{field_name} 的前缀里不能有 .. 或反斜杠", field_name)
    return f"tos://{bucket}" + (f"/{'/'.join(segs)}" if segs else "")


def delivery_key(uri: str) -> str:
    """The normalized delivery directory; publishing is serialized per key (D29)."""
    return normalize_tos_uri(uri, field_name="output.uri")


def module_choices(items: list) -> list[tuple[str, dict | None]]:
    """``["a", {"id": "b", "params": {...}}]`` -> ``[("a", None), ("b", {...})]``, validated."""
    out: list[tuple[str, dict | None]] = []
    seen = set()
    for i, item in enumerate(items):
        mid, params = (item, None) if isinstance(item, str) else (item["id"], item.get("params"))
        where = f"modules.{i}"
        if mid not in registry.ids():
            raise _bad(f"没有叫 {mid} 的质检模块（可选：{', '.join(registry.ids())}）", where)
        if mid in seen:
            raise _bad(f"模块 {mid} 重复出现", where)
        seen.add(mid)
        try:
            registry.validate_params(mid, params)
        except jsonschema.ValidationError as err:
            raise _bad(f"模块 {mid} 的参数不对：{err.message}", f"{where}.params") from None
        out.append((mid, params))
    return out


def episode_selector(sel: dict, episode_count: int | None) -> dict:
    mode = sel["mode"]
    if mode == "all":
        return {"mode": "all"}
    if mode == "head":
        return {"mode": "head", "n": int(sel["n"])}
    expr = str(sel.get("expr") or "").strip()
    if expr.startswith("@"):
        raise _bad("@文件 的写法只在命令行里可用，这里请直接写编号，如 3,10-12", "episodes.expr")
    try:
        indices = episode_select.parse_episodes(expr)
    except ValueError as err:
        raise _bad(f"episode 表达式写法不对：{err}", "episodes.expr") from None
    if not indices:
        raise _bad("episode 表达式里没有任何编号", "episodes.expr")
    if len(indices) > _MAX_EXPLICIT_EPISODES:
        raise _bad(f"一次最多指定 {_MAX_EXPLICIT_EPISODES} 条 episode", "episodes.expr")
    if episode_count is not None:
        try:
            episode_select.reconcile_episodes(indices, range(episode_count))
        except episode_select.EpisodesOutOfRange as err:
            raise _bad(str(err), "episodes.expr") from None
    return {"mode": "explicit", "expr": expr, "indices": sorted(indices)}


def credential_id(repo: P.Repository, name: str, owner: str, where: str) -> str:
    try:
        cred = repo.get_credential_by_name(name, owner=owner)
    except P.NotFound:
        raise _bad(f"访问密钥「{name}」不存在，请先在「密钥与资源管理」里添加", where) from None
    if cred.kind != "tos":
        raise _bad(f"「{name}」不是 TOS 访问密钥", where)
    return cred.id


def local_path(settings: Settings, uri: str, field_name: str = "input.uri") -> str:
    """A local input path, which must stay under ``localDataRoot`` (the experimental source)."""
    root = settings.local_data_root
    if root is None:
        raise _bad("「本地挂载路径」这个数据来源没有开启（站点配置 localDataRoot）", "input.source")
    path = pathlib.Path(os.path.normpath(str(uri)))
    if not path.is_absolute():
        path = pathlib.Path(root) / path
    try:
        path.resolve().relative_to(pathlib.Path(root).resolve())
    except ValueError:
        raise _bad(f"本地路径必须在 {root} 之下", field_name) from None
    return str(path)


def _input_ref(repo: P.Repository, settings: Settings, ref: dict, owner: str) -> dict:
    source = ref["source"]
    fields: dict[str, Any] = {"input_source": source, "input_region": ref.get("region")}
    if source == "tos":
        fields["input_uri"] = normalize_tos_uri(ref["uri"], field_name="input.uri")
        fields["input_cred_id"] = credential_id(repo, ref["credential"], owner, "input.credential")
        return fields
    if ref.get("credential"):
        raise _bad("HuggingFace 缓存桶和本地路径不需要访问密钥", "input.credential")
    fields["input_cred_id"] = None
    if source == "public":
        fields["input_uri"] = normalize_tos_uri(ref["uri"], field_name="input.uri")
    else:
        fields["input_uri"] = local_path(settings, ref["uri"])
    return fields


def _registered_input(repo: P.Repository, settings: Settings, dataset_id: str, owner: str) -> dict:
    try:
        ds = repo.get_dataset(dataset_id, owner=owner)
    except P.NotFound:
        raise _bad(f"数据集 {dataset_id} 不存在（可能已删除登记）", "input.dataset_id") from None
    if ds.source == "tos" and not ds.credential_id:
        raise _bad(f"数据集「{ds.name}」登记时用的访问密钥已被删除；请直接填写地址并选一个访问密钥",
                   "input.dataset_id")
    uri = local_path(settings, ds.uri, "input.dataset_id") if ds.source == "local" else ds.uri
    return {"input_source": ds.source, "input_uri": uri, "input_region": ds.region,
            "input_cred_id": ds.credential_id if ds.source == "tos" else None,
            "dataset_id": ds.id}


def resolve_input(repo: P.Repository, settings: Settings, spec: dict, owner: str) -> dict:
    """C4 ``InputSpec`` -> task fields, ``dataset_id`` included (None: no registration)."""
    if "dataset_id" in spec:
        return _registered_input(repo, settings, spec["dataset_id"], owner)
    fields = _input_ref(repo, settings, spec, owner)
    found = repo.find_dataset(source=fields["input_source"], uri=fields["input_uri"],
                              region=fields["input_region"], owner=owner)
    fields["dataset_id"] = found.id if found is not None else None
    return fields


def resolve_output(repo: P.Repository, ref: dict, owner: str) -> dict:
    uri = normalize_tos_uri(ref["uri"], field_name="output.uri")
    return {"output_uri": uri, "output_region": ref.get("region"), "delivery_key": uri,
            "output_cred_id": credential_id(repo, ref["credential"], owner, "output.credential")}


def resolve_vlm(repo: P.Repository, choice: dict, owner: str) -> dict:
    try:
        backend = repo.get_vlm_backend_by_name(choice["backend"], owner=owner)
    except P.NotFound:
        raise _bad(f"模型服务「{choice['backend']}」不存在", "vlm.backend") from None
    model = next((m for m in backend.models if m.model_name == choice["model"]), None)
    if model is None:
        raise _bad(f"模型服务「{backend.name}」下没有模型 {choice['model']}，请先添加", "vlm.model")
    return {"vlm_model_id": model.id, "vlm_reasoning_effort": choice.get("reasoning_effort")}


def _availability(preflight: dict | None) -> dict[str, dict]:
    if not isinstance(preflight, dict):
        return {}
    return {m["id"]: m for m in preflight.get("modules") or [] if isinstance(m, dict) and "id" in m}


def _episode_count(preflight: dict | None) -> int | None:
    ds = preflight.get("dataset") if isinstance(preflight, dict) else None
    count = ds.get("episode_count") if isinstance(ds, dict) else None
    return count if isinstance(count, int) else None


@dataclass
class Resolved:
    fields: dict = field(default_factory=dict)        # for Repository.update_task_fields
    modules: list[P.TaskModule] | None = None          # full replacement of the task_module rows


def check_modules(selected: list[str], availability: dict[str, dict], *,
                  embodiment_id: str | None, has_vlm: bool) -> None:
    """The rules of design doc 05 §4 for the modules a task will run."""
    for mid in selected:
        spec = registry.get(mid)
        entry = availability.get(mid, {})
        state = entry.get("availability", "available")
        if state == "unsupported":
            raise _bad(f"「{spec.name_zh}」在这个数据集上不可用：{entry.get('reason', '')}".rstrip("："),
                       "modules")
        if state == "needs_input":
            hint = entry.get("input_hint") or {}
            if hint.get("field") == "embodiment_id":
                options = hint.get("options") or []
                if not embodiment_id:
                    raise _bad(f"「{spec.name_zh}」需要补充机器人型号（embodiment_id），"
                               "或者不勾选这个模块", "embodiment_id")
                if options and embodiment_id not in options:
                    raise _bad(f"机器人型号 {embodiment_id} 不在规格库里（可选：{', '.join(options)}）",
                               "embodiment_id")
            elif hint.get("field") == "vlm" and not has_vlm:
                raise _bad(f"「{spec.name_zh}」需要先选择 VLM 后端和模型", "vlm")
        if "vlm" in spec.needs and not has_vlm:
            raise _bad(f"勾选了「{spec.name_zh}」，需要选择 VLM 后端和模型", "vlm")


def module_rows(task_id: str, choices: list[tuple[str, dict | None]],
                availability: dict[str, dict]) -> list[P.TaskModule]:
    chosen = dict(choices)
    rows = []
    for spec in registry.MODULES:
        entry = availability.get(spec.id, {})
        rows.append(P.TaskModule(
            task_id=task_id, module_id=spec.id, selected=spec.id in chosen,
            availability=entry.get("availability", "available"),
            unavailable_reason=entry.get("reason"), params=chosen.get(spec.id)))
    return rows


def resolve_config(repo: P.Repository, settings: Settings, task: P.Task, body: dict, *,
                   now: int, owner: str) -> Resolved:
    """``body`` already fits ``TaskPatch``; returns what to write, or raises :class:`ApiError`.

    ``task`` is the current row (for PATCH) - its values fill in whatever ``body``
    leaves out when cross-field rules are checked.
    """
    if task.state != "created":
        locked = sorted(set(body) - EDITABLE_AFTER_START)
        if locked:
            raise ApiError("task_state_conflict",
                           "任务已经启动，只能改名称和备注；要换数据、模块、模型或参数，请复制为新任务",
                           details={"state": task.state, "fields": locked})
    out = Resolved()
    for key in ("name", "note"):
        if key in body:
            out.fields[key] = body[key]

    new_input = None
    if "input" in body:
        new_input = resolve_input(repo, settings, body["input"], owner)
        changed = (new_input["input_source"], new_input["input_uri"]) != \
            (task.input_source, task.input_uri)
        if changed and "preflight_id" not in body:
            raise _bad("换了数据集之后要重新预检：请同时提交新的 preflight_id", "preflight_id")
        out.fields.update(new_input)
    if "output" in body:
        out.fields.update(resolve_output(repo, body["output"], owner))

    preflight = task.preflight
    if "preflight_id" in body:
        preflight = repo.get_preflight(body["preflight_id"], max_age_ms=PREFLIGHT_MAX_AGE_MS,
                                       now=now, owner=owner)
        if preflight is None:
            raise ApiError("preflight_expired", details={"preflight_id": body["preflight_id"]})
        out.fields["preflight"] = preflight
    if "episodes" in body:
        out.fields["episode_selector"] = episode_selector(body["episodes"], _episode_count(preflight))
    elif "preflight_id" in body and task.episode_selector.get("mode") == "explicit":
        episode_selector(task.episode_selector, _episode_count(preflight))   # still in range?

    if "embodiment_id" in body:
        out.fields["embodiment_id"] = body["embodiment_id"]
    if "vlm" in body:
        out.fields.update(resolve_vlm(repo, body["vlm"], owner))
    if "params" in body:
        out.fields["params"] = {k: v for k, v in body["params"].items() if k != "start_now"}

    touches_modules = bool({"modules", "preflight_id", "embodiment_id", "vlm", "input"} & set(body))
    if touches_modules:
        availability = _availability(preflight)
        if "modules" in body:
            choices = module_choices(body["modules"])
        else:
            current = repo.get_task_modules(task.id)
            known = set(registry.ids())
            choices = [(m.module_id, m.params) for m in current
                       if m.selected and m.module_id in known]
        embodiment = out.fields.get("embodiment_id", task.embodiment_id)
        has_vlm = bool(out.fields.get("vlm_model_id", task.vlm_model_id))
        check_modules([mid for mid, _ in choices], availability, embodiment_id=embodiment,
                      has_vlm=has_vlm)
        if "modules" in body or "preflight_id" in body:
            out.modules = module_rows(task.id, choices, availability)
    return out
