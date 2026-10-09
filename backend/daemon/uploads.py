"""Input files of module parameters (registry 1.5 ``format: upload``; design doc 12 §11.1, F5.5).

``POST /uploads`` validates the file on arrival and keeps only valid ones:

* ``eef_trajectory`` - a ``trajectory-bundle/1.0`` read by the module's own loader (the shared
  library, like the planner): container and section Schemas, cross-section semantics, the
  provided-vs-recomputed projection check, evaluation keys rejected; media are checked later,
  when a task binds the file to a dataset;
* ``eef_observation_seeds`` - a JSON array of observation rows (C2 ``eef/observation.schema.json``);
* ``eef_gripper_template`` - a ``gripper-template/1.0`` file (C2 ``eef/gripper_template.schema.json``), read by
  the module's own loader: patches decoded, features counted, entries with too few features reported;
* ``eef_record_mapping`` - the record mapping of the EEF module's record comparison (``eef-mapping/1.1``
  ``record``, or a 1.0 exporter mapping whose ``eef`` block is the pose record; design doc 12 §8.7), parsed by
  the module's own parser; whether its columns / topics exist is checked when a task binds it to a dataset;
* ``eef_gripper_calibration`` - a handheld gripper's ``umi-calibration/2`` (C2 ``eef/umi_calibration.schema.json``,
  design doc 22 §5.4): the Schema and the rigid, proper transforms the exporter checks.

An error is reported with its location (JSON path, sample, episode, frame, camera, point). Files
live on the data volume as ``uploads/<owner key>/<upload_id>/{file, meta.json}``: no database row,
the metadata is the sidecar. Uploads are owner-scoped; a task refers to one by its handle
``upload:<upload_id>`` and gets a copy in its run directory when it starts. Upload ids are
``upl-<9 lowercase letters>`` (D45); uploads made before keep their ``upl_<hex>`` ids.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import threading
from typing import Any

from curation.contracts import modules as registry

from .errors import ApiError
from .util import ID_ATTEMPTS, id_regex, new_id

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
KINDS = ("eef_trajectory", "eef_observation_seeds", "eef_gripper_template", "eef_record_mapping",
         "eef_gripper_calibration", "viz_annotations")
_ID_RE = re.compile(rf"^{id_regex('upl', r'upl_[0-9a-z]{10,40}')}$")
_MAX_ERRORS = 50


def _owner_key(owner: str) -> str:
    return hashlib.sha256(owner.encode()).hexdigest()[:16]


def _invalid(message: str, errors: list[dict]) -> ApiError:
    return ApiError("validation_failed", message, details={"errors": errors[:_MAX_ERRORS]})


def _warning(problem: str, code: str, field: str | None = None) -> dict:
    """A warning as C4 ``UploadIssue`` has it."""
    return {"field": field, "problem": problem, "code": code, "severity": "warning"}


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
        # frame numbers count from 1 on screen (design doc 18 §4.4); frame_index is the data's, from 0
        shown = {**first, "frame_index": first["frame_index"] + 1} if isinstance(first.get("frame_index"), int) else first
        where = "、".join(label.format(shown[k]) for k, label in (("sample_id", "样本 {}"), ("frame_index", "第 {} 帧"),
                                                                  ("camera_id", "相机 {}"), ("point_id", "点 {}"))
                         if shown.get(k) is not None)
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


def validate_template(data: bytes) -> dict:
    """The ``validation`` of a gripper-template upload, or raises validation_failed (400)."""
    from curation.extensions.eef_consistency import template as TP

    try:
        t = TP.load_template(data)
    except TP.TemplateError as err:
        raise _invalid(f"夹爪外观模板不合格：{err}", [{"field": None, "problem": str(err), "code": "template_invalid",
                                                   "severity": "error"}]) from None
    weak = [e.entry_id for e in t.entries if len(e.descriptors) < t.matching.min_inliers]
    summary = {"entries": len(t.entries), "usable_entries": len(t.entries) - len(weak),
               "cameras": sorted({e.camera_id for e in t.entries if e.camera_id}), "points": list(t.point_ids),
               "methods": list(t.methods), "masked_entries": sum(e.mask is not None for e in t.entries)}
    warnings = [_warning(f"{len(weak)} entries have fewer than {t.matching.min_inliers} features and will never match: "
                         + ", ".join(weak[:5]), "entries_weak", "entries")] if weak else []
    if "synthetic_fixture" in t.methods:
        warnings.append(_warning("entries are synthetic_fixture (DEMO): not for visual-accuracy acceptance",
                                 "synthetic_fixture", "entries"))
    return {"valid": True, "summary": summary, "warnings": warnings}


def validate_record_mapping(data: bytes) -> dict:
    """The ``validation`` of a record-mapping upload (JSON), or raises validation_failed (400)."""
    from curation.extensions.eef_consistency import record as RC

    try:
        doc = json.loads(data.decode("utf-8"))
        m = RC.parse_mapping(doc, sha256=hashlib.sha256(data).hexdigest())
    except (ValueError, UnicodeDecodeError, RC.RecordMappingError) as err:
        code = "robot_model_unknown" if isinstance(err, RC.RobotModelUnknown) else "record_mapping_invalid"
        raise _invalid(f"数据集记录映射不合格：{err}", [{"field": None, "problem": str(err),
                                                   "code": code, "severity": "error"}]) from None
    src = m.sources
    summary = {"sources": sorted(src), "robot": src["joints"].robot if "joints" in src else None,
               "columns": sorted({c for s in src.values() for c in (s.key, s.quaternion_key) if c}),
               "topics": sorted({s.topic for s in src.values() if s.topic}), "declared_frames": sorted(m.frames)}
    warnings = [] if doc.get("record") else [_warning("no record block: the exporter's eef block is the pose record",
                                                       "record_block_missing", "record")]
    return {"valid": True, "summary": summary, "warnings": warnings}


def validate_gripper_calibration(data: bytes) -> dict:
    """The ``validation`` of a gripper calibration upload (``umi-calibration/2``), or raises validation_failed (400)."""
    from curation.extensions.eef_consistency.adapters import umi_mcap as X

    try:
        doc = json.loads(data.decode("utf-8"))
        cfg = X.check_calibration(doc, "夹爪标定")
    except (ValueError, UnicodeDecodeError, X.ExportError) as err:
        raise _invalid(f"夹爪标定不合格：{err}", [{"field": None, "problem": str(err), "code": "calibration_invalid",
                                               "severity": "error"}]) from None
    assumed = X.assumed(cfg)
    summary = {"gripper": cfg["gripper"], "pose_frame": cfg["pose_frame"], "assurance": cfg["provenance"]["assurance"],
               "assumed": assumed, "intrinsics_fallback": sorted((cfg.get("intrinsics_fallback") or {})),
               "pairing_tolerance_s": cfg["pairing_tolerance_s"]}
    warnings = [_warning(f"assumed, not declared by the gripper's maker: {', '.join(assumed)}", "assumed",
                         "provenance")] if assumed else []
    return {"valid": True, "summary": summary, "warnings": warnings}


def validate_annotations(data: bytes, name: str = "") -> dict:
    """The ``validation`` of an external annotation file (design doc 18 §4.5): a zip of per-episode
    Argus-style JSON named by episode index, a JSON list / mapping of them, or one JSON for the
    episode its file name names; or raises validation_failed (400) with every bad file located."""
    from curation.viz.annotations import AnnotationFileError, argus_annotations, read_label_file

    try:
        docs = read_label_file(data, name)
    except AnnotationFileError as err:
        raise _invalid(f"外部标注文件不合格：{err}", [{"field": e.get("field"), "problem": e["problem"],
                                                   "code": e.get("code", "annotation_invalid"), "severity": "error",
                                                   **({"episode_index": e["episode_index"]} if "episode_index" in e else {})}
                                                  for e in err.errors]) from None
    segments = events = 0
    for doc in docs.values():
        ann = argus_annotations(doc)
        segments += sum(len(t["segments"]) for t in ann.tracks)
        events += len(ann.events)
    episodes = sorted(docs)
    summary = {"format": "argus", "episodes": len(episodes), "first": episodes[0], "last": episodes[-1],
               "segments": segments, "events": events, "zip": data[:2] == b"PK"}
    warnings = [] if segments else ["no timeline / event_labels segment in any episode"]
    return {"valid": True, "summary": summary, "warnings": warnings}


VALIDATORS = {"eef_trajectory": validate_trajectory, "eef_observation_seeds": validate_seeds,
              "eef_gripper_template": validate_template, "eef_record_mapping": validate_record_mapping,
              "eef_gripper_calibration": validate_gripper_calibration, "viz_annotations": validate_annotations}


class UploadStore:
    def __init__(self, root: os.PathLike | str, clock):
        self.root = pathlib.Path(root)
        self.clock = clock
        self._lock = threading.Lock()

    def _dir(self, owner: str, upload_id: str) -> pathlib.Path:
        if not _ID_RE.match(upload_id or ""):
            raise ApiError("not_found", "没有这个上传件", details={"upload_id": upload_id})
        return self.root / _owner_key(owner) / upload_id

    def _claim(self, owner: str) -> tuple[str, pathlib.Path]:
        """A new upload id and its directory, created here so no other upload can take it; a
        random id that is already there is drawn again (D45)."""
        for _ in range(ID_ATTEMPTS):
            upload_id = new_id("upl")
            d = self._dir(owner, upload_id)
            try:
                d.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            return upload_id, d
        raise RuntimeError(f"no free upload id after {ID_ATTEMPTS} draws")

    def put(self, owner: str, kind: str, name: str, data: bytes) -> dict:
        if kind not in KINDS:
            raise ApiError("validation_failed", f"不认识的上传类型 {kind!r}（可选：{'、'.join(KINDS)}）",
                           details={"errors": [{"field": "kind", "problem": "unknown kind"}]})
        if len(data) > MAX_UPLOAD_BYTES:
            raise ApiError("validation_failed", f"文件太大（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB）")
        clean = pathlib.PurePath(name).name.strip() or "upload.json"
        validation = validate_annotations(data, clean) if kind == "viz_annotations" else VALIDATORS[kind](data)
        upload_id, d = self._claim(owner)
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
