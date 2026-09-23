"""Input files of module parameters (registry 1.5 ``format: upload``; design doc 12 §11.1, F5.5).

``POST /uploads`` validates the file on arrival and keeps only valid ones:

* ``eef_trajectory`` - a ``trajectory-bundle/1.0`` read by the module's own loader (the shared
  library, like the planner): container and section Schemas, cross-section semantics, the
  provided-vs-recomputed projection check, evaluation keys rejected; media are checked later,
  when a task binds the file to a dataset;
* ``eef_observation_seeds`` - a JSON array of observation rows (C2 ``eef/observation.schema.json``).

An error is reported with its location (JSON path, sample, episode, frame, camera, point). Files
live on the data volume as ``uploads/<owner key>/<upload_id>/{file, meta.json}``: no database row,
the metadata is the sidecar. Uploads are owner-scoped; a task refers to one by its handle
``upload:<upload_id>`` and gets a copy in its run directory when it starts.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import secrets
import threading
from typing import Any

from curation.contracts import modules as registry

from .errors import ApiError

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
KINDS = ("eef_trajectory", "eef_observation_seeds")
_ID_RE = re.compile(r"^upl_[0-9a-z]{10,40}$")
_MAX_ERRORS = 50


def _owner_key(owner: str) -> str:
    return hashlib.sha256(owner.encode()).hexdigest()[:16]


def _new_id() -> str:
    return "upl_" + secrets.token_hex(10)


def _invalid(message: str, errors: list[dict]) -> ApiError:
    return ApiError("validation_failed", message, details={"errors": errors[:_MAX_ERRORS]})


def _issue(i) -> dict:
    d = i.as_dict()
    out = {"field": d.pop("path", None), "problem": d.pop("message"), "code": d.pop("code"),
           "severity": d.pop("severity")}
    out.update({k: v for k, v in d.items() if v is not None})
    return out


def validate_trajectory(data: bytes) -> dict:
    """The ``validation`` of a trajectory.json upload, or raises validation_failed (400) with located errors."""
    from curation.extensions.eef_consistency import load

    result = load.load_bundle(data, check_media=False)
    if not result.ok:
        errors = [_issue(i) for i in result.errors]
        first = errors[0]
        where = "、".join(label.format(first[k]) for k, label in (("sample_id", "样本 {}"), ("frame_index", "第 {} 帧"),
                                                                  ("camera_id", "相机 {}"), ("point_id", "点 {}"))
                         if first.get(k) is not None)
        raise _invalid(f"trajectory.json 没有通过校验：{first['problem']}"
                       + (f"（{where}）" if where else "") + (f"，共 {len(errors)} 处" if len(errors) > 1 else ""),
                       errors)
    rep = result.report
    samples = list(result.samples.values())
    summary = {"dataset": result.meta.get("dataset"), "samples": len(samples),
               "episodes": sorted(result.samples), "frames": rep["total_frames"],
               "cameras": sorted({c for s in samples for c in s.cameras}),
               "points_checked": rep["total_points_checked"],
               "max_reprojection_difference_px": rep["max_reprojection_difference_px"]}
    return {"valid": True, "summary": summary, "warnings": [_issue(i) for i in result.warnings[:_MAX_ERRORS]]}


def validate_seeds(data: bytes) -> dict:
    """The ``validation`` of an observation-seed upload (a JSON array of rows), or raises validation_failed."""
    from curation.contracts import schemas

    try:
        rows = json.loads(data, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except ValueError as err:
        raise _invalid(f"种子文件不是合法的 JSON：{err}", [{"field": None, "problem": str(err)}]) from None
    if isinstance(rows, dict):
        rows = rows.get("rows")
    if not isinstance(rows, list) or not rows:
        raise _invalid("种子文件应是 observation 行的 JSON 数组（控制台会把 .jsonl 转成它）",
                       [{"field": None, "problem": "expected a non-empty JSON array of observation rows"}])
    validator = schemas.validator("eef/observation.schema.json")
    errors = []
    for i, row in enumerate(rows):
        for e in validator.iter_errors(row):
            loc = "/".join(map(str, e.absolute_path))
            item = {"field": f"{i}" + (f"/{loc}" if loc else ""), "problem": e.message, "code": "schema",
                    "severity": "error"}
            if isinstance(row, dict):
                for k in ("sample_id", "frame_index", "camera_id"):
                    if isinstance(row.get(k), (str, int)):
                        item[k] = row[k]
            errors.append(item)
            break
    if errors:
        raise _invalid(f"种子文件有 {len(errors)} 行不合格式：第 {errors[0]['field'].split('/')[0]} 行 "
                       f"{errors[0]['problem']}", errors)
    summary = {"rows": len(rows), "samples": len({r["sample_id"] for r in rows}),
               "cameras": sorted({r["camera_id"] for r in rows}),
               "points": sorted({p for r in rows for p in r["points"]}),
               "methods": sorted({r["method"] for r in rows})}
    return {"valid": True, "summary": summary, "warnings": []}


VALIDATORS = {"eef_trajectory": validate_trajectory, "eef_observation_seeds": validate_seeds}


class UploadStore:
    def __init__(self, root: os.PathLike | str, clock):
        self.root = pathlib.Path(root)
        self.clock = clock
        self._lock = threading.Lock()

    def _dir(self, owner: str, upload_id: str) -> pathlib.Path:
        if not _ID_RE.match(upload_id or ""):
            raise ApiError("not_found", "没有这个上传件", details={"upload_id": upload_id})
        return self.root / _owner_key(owner) / upload_id

    def put(self, owner: str, kind: str, name: str, data: bytes) -> dict:
        if kind not in KINDS:
            raise ApiError("validation_failed", f"不认识的上传类型 {kind!r}（可选：{'、'.join(KINDS)}）",
                           details={"errors": [{"field": "kind", "problem": "unknown kind"}]})
        if len(data) > MAX_UPLOAD_BYTES:
            raise ApiError("validation_failed", f"文件太大（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB）")
        clean = pathlib.PurePath(name).name.strip() or "upload.json"
        validation = VALIDATORS[kind](data)
        upload_id = _new_id()
        d = self._dir(owner, upload_id)
        d.mkdir(parents=True, exist_ok=False)
        (d / clean).write_bytes(data)
        meta = {"upload_id": upload_id, "handle": registry.UPLOAD_PREFIX + upload_id, "kind": kind, "name": clean,
                "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data), "created_at": self.clock(),
                "validation": validation}
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, d / "meta.json")
        return meta

    def get(self, owner: str, upload_id: str) -> dict:
        path = self._dir(owner, upload_id) / "meta.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ApiError("not_found", "没有这个上传件（或它不是你的）", details={"upload_id": upload_id}) from None

    def path(self, owner: str, upload_id: str) -> pathlib.Path:
        meta = self.get(owner, upload_id)
        return self._dir(owner, upload_id) / meta["name"]

    def resolve(self, owner: str, value: Any, *, kind: str, field: str) -> tuple[pathlib.Path, dict]:
        """``upload:<id>`` of the right kind -> (file, meta); anything else is refused (a task may
        not point the Daemon at a path of its own choosing)."""
        if not isinstance(value, str) or not value.startswith(registry.UPLOAD_PREFIX):
            raise ApiError("validation_failed", f"{field} 要先上传文件，填上传返回的句柄（upload:…）",
                           details={"errors": [{"field": field, "problem": "not an upload handle"}]})
        upload_id = value[len(registry.UPLOAD_PREFIX):]
        try:
            meta = self.get(owner, upload_id)
        except ApiError:
            raise ApiError("validation_failed", f"{field} 引用的上传件不存在（或不是你的）：{upload_id}",
                           details={"errors": [{"field": field, "problem": "unknown upload"}]}) from None
        if meta["kind"] != kind:
            raise ApiError("validation_failed", f"{field} 需要 {kind} 类型的文件，给的是 {meta['kind']}",
                           details={"errors": [{"field": field, "problem": "wrong upload kind"}]})
        return self._dir(owner, upload_id) / meta["name"], meta
