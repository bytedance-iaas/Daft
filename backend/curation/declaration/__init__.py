"""The dataset declaration (C7 ``dataset-declaration/1.0``, design doc 25 §3, D83).

One document per dataset says what its records are and what they mean, for the data visualizer and the checks
alike. Its first layer is the field mapping of an mcap dataset (``viz-mapping/1.x``, design doc 18 §6), kept as it
was: which topics are cameras, depth pictures, curves, the task text and the segments. The second says what the
pose, joint and gripper records mean (column or topic, layout, units, frames); the third how they become pixels
(each camera's mount, owner, intrinsics and extrinsics; the tool on the arm; a handheld gripper's calibration).
Every item of the last two says how sure it is (``assurance``) and which drafting assumptions it rests on.

A ``viz-mapping/1.x`` document is a declaration with only its first layer (:func:`normalize`), so a dataset
confirmed before keeps its version and its meaning; the mcap readers keep reading a mapping (:func:`mapping_of`).
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

SCHEMA_VERSION = "dataset-declaration/1.0"
SCHEMA_FILE = "dataset-declaration.schema.json"
#: the C7 documents that came before: a declaration with its first layer only
LEGACY_VERSIONS = ("viz-mapping/1.0", "viz-mapping/1.1")
MAPPING_VERSION = "viz-mapping/1.1"
#: the first layer (sources -> roles), as viz-mapping/1.x names it
LAYER1 = ("name", "base", "timeline", "cameras", "depths", "series", "task", "segments", "ignore")
LAYER2_3 = ("semantics", "calibration", "timing")
DECLARED, ASSUMED, UNKNOWN = "declared", "model_assumed", "unknown"
#: the mounts a camera can have (design doc 25 §2.1)
FIXED_EXTERNAL, WRIST, MOVING = "fixed_external", "wrist", "moving"


def version_of(doc: Any) -> str | None:
    return doc.get("schema_version") if isinstance(doc, dict) else None


def normalize(doc: Any) -> dict | None:
    """The declaration a stored or imported document is: a declaration as it is, a ``viz-mapping/1.x`` mapping
    as a declaration of its first layer only; None for nothing. Never modifies ``doc``."""
    if not isinstance(doc, dict):
        return None
    out = copy.deepcopy(doc)
    if version_of(doc) in LEGACY_VERSIONS:
        out["schema_version"] = SCHEMA_VERSION
    return out


def has_layer1(doc: Any) -> bool:
    """Whether the declaration maps sources to roles (an mcap dataset's cameras and curves)."""
    return isinstance(doc, dict) and isinstance(doc.get("cameras"), list) and isinstance(doc.get("series"), list)


def mapping_of(doc: Any) -> dict | None:
    """The ``viz-mapping/1.1`` view of a declaration's first layer, what the mcap readers and the check reader's
    derivation read (``curation.viz.mcap_mapping``); None when it maps nothing (LeRobot, Lance)."""
    if not has_layer1(doc):
        return None
    out = {"schema_version": MAPPING_VERSION if version_of(doc) not in LEGACY_VERSIONS else doc["schema_version"]}
    for k in LAYER1:
        if k in doc:
            out[k] = copy.deepcopy(doc[k])
    if out["schema_version"] == "viz-mapping/1.0" and out.get("depths"):
        out["schema_version"] = MAPPING_VERSION
    return out


def with_mapping(doc: Any, mapping: dict) -> dict:
    """``doc`` with its first layer replaced by ``mapping`` (a ``viz-mapping/1.x`` or a declaration): what
    ``PUT /datasets/{id}/mapping`` keeps doing for one more version - the other layers stay."""
    out = normalize(doc) or {"schema_version": SCHEMA_VERSION}
    for k in LAYER1:
        out.pop(k, None)
    for k in LAYER1:
        if k in mapping:
            out[k] = copy.deepcopy(mapping[k])
    return out


def schema_errors(doc: Any) -> list[dict]:
    """The C7 Schema's complaints as ``[{field, problem}]`` (a legacy mapping is checked as the declaration it is)."""
    from ..contracts import schemas

    target = normalize(doc) if version_of(doc) in LEGACY_VERSIONS else doc
    return [{"field": e.split(":", 1)[0] or "<root>", "problem": e.split(": ", 1)[-1]}
            for e in schemas.errors(SCHEMA_FILE, target)]


def load(path: str) -> dict:
    """A declaration file (JSON; a ``viz-mapping/1.x`` file too), checked against the Schema and normalized;
    ``ValueError`` names the first problem."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    problems = schema_errors(doc)
    if problems:
        raise ValueError(f"{problems[0]['field']}: {problems[0]['problem']}")
    return normalize(doc)


def sha256(doc: Any) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def cameras(doc: Any) -> dict[str, dict]:
    """``calibration.cameras``: per camera source, its calibration entry."""
    return dict(((doc or {}).get("calibration") or {}).get("cameras") or {}) if isinstance(doc, dict) else {}


def short_name(source: str) -> str:
    """A camera's short name from its source: ``observation.images.exterior_1_left`` -> ``exterior_1_left``,
    ``/robot0/sensor/camera0/compressed`` -> ``robot0_camera0``."""
    s = str(source)
    if s.startswith("observation.images."):
        return s[len("observation.images."):]
    if s.startswith("/"):
        parts = [p for p in s.strip("/").split("/") if p not in ("compressed", "image_raw", "image", "sensor", "rgb",
                                                                  "color", "image_rect", "image_rect_color")]
        return "_".join(parts) or s.strip("/").replace("/", "_")
    return s.rsplit(".", 1)[-1]


def assumed(doc: Any) -> list[dict]:
    """Every item the declaration only assumes (``assurance: model_assumed`` or ``unknown``), with its path and the
    assumption codes: what reports and overlays note as 按假设值."""
    out: list[dict] = []
    if not isinstance(doc, dict):
        return out

    def note(path: str, item: Any) -> None:
        if isinstance(item, dict) and item.get("assurance") in (ASSUMED, UNKNOWN):
            out.append({"field": path, "assurance": item["assurance"],
                        "codes": [a.get("code") for a in item.get("assumptions") or [] if isinstance(a, dict)]})

    sem = doc.get("semantics") or {}
    for k in ("pose", "joints", "gripper"):
        note(f"semantics.{k}", sem.get(k))
    cal = doc.get("calibration") or {}
    for src, c in sorted((cal.get("cameras") or {}).items()):
        note(f"calibration.cameras.{src}", c)
        for part in ("intrinsics", "extrinsics"):
            note(f"calibration.cameras.{src}.{part}", (c or {}).get(part))
    note("calibration.tool", cal.get("tool"))
    hh = cal.get("handheld")
    if isinstance(hh, dict):
        items = ((hh.get("calibration") or {}).get("provenance") or {}).get("items") or {}
        for name, it in sorted(items.items()):
            if isinstance(it, dict) and it.get("assurance") not in (DECLARED, "independently_calibrated"):
                out.append({"field": f"calibration.handheld.{name}", "assurance": ASSUMED,
                            "codes": [c.get("code") for c in hh.get("assumptions") or [] if isinstance(c, dict)]})
    note("timing", doc.get("timing"))
    return out
