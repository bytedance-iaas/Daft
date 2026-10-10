"""A registration's dataset declaration (C7 ``dataset-declaration/1.0``, design doc 25 §3, D83).

Kept where the mcap field mapping was (``Dataset.viz_mapping``, one document and one version counter): a mapping
confirmed before is a declaration of its first layer. ``GET`` gives the confirmed one, a fresh draft (from the
registration's preflight - ``dataset.features``, ``camera_info``, ``robot_type`` - and, for an mcap dataset, a probe
of its first file) and what the EEF module would make of it; ``PUT`` checks a new version against the dataset
(errors stop it; suspects are kept on it) and takes the registration's preflight again, whose EEF entry follows
the declaration.
"""
from __future__ import annotations

import json
from typing import Any

from curation import declaration as DCL
from curation.declaration import checks as K
from curation.declaration import draft as DD

from ..errors import ApiError
from ..repo import protocol as P
from .source import dataset_source
from .status import is_mcap


def _preflight(ds: P.Dataset) -> dict:
    return ds.preflight if isinstance(ds.preflight, dict) else {}


def format_of(ds: P.Dataset) -> str:
    kind = str((_preflight(ds).get("format") or {}).get("kind") or "")
    return kind if kind in ("lerobot", "mcap", "lance", "umi_session") else "unknown"


def _info(ds: P.Dataset) -> dict:
    """An ``info.json``-like view of the registration's preflight: features with their component names, the robot."""
    d = _preflight(ds).get("dataset") or {}
    feats = {f["key"]: {"dtype": f.get("dtype"), "shape": f.get("shape"), "names": f.get("names")}
             for f in d.get("features") or [] if isinstance(f, dict) and f.get("key")}
    return {"features": feats, "robot_type": d.get("robot_type"), "fps": d.get("fps")}


def _listing(svc, src) -> list[str]:
    try:
        return list(src.listing() or [])
    except Exception:  # noqa: BLE001 - no kept listing: meta files are not looked for
        return []


def _reader(svc, src):
    from .source import Access

    def read(key: str) -> bytes:
        with Access(svc.rt, src).storage() as st:
            return st.read_bytes(key)
    return read


def cameras_of(svc, ds: P.Dataset, decl: dict | None) -> list[dict]:
    """The dataset's cameras: source, display name, video size."""
    fmt = format_of(ds)
    if fmt == "mcap":
        out = []
        mapping = DCL.mapping_of(decl) or {}
        sizes = {}
        try:
            pr, _, _ = svc.mcap.probe_source(dataset_source(svc.rt, ds, ds.owner_id))
            sizes = {t: (tp.width, tp.height) for t, tp in pr.topics.items() if tp.kind == "camera"}
        except ApiError:
            pass
        for c in mapping.get("cameras") or []:
            w, h = sizes.get(c["topic"], (None, None))
            out.append({"source": c["topic"], "name": c.get("name") or DCL.short_name(c["topic"]), "width": w, "height": h})
        return out
    info = _preflight(ds).get("dataset") or {}
    shown = {c.get("key"): c for c in info.get("camera_info") or [] if isinstance(c, dict)}
    out = []
    for key, f in _info(ds)["features"].items():
        if f.get("dtype") not in ("video", "image") or "depth" in key.lower():
            continue
        c = shown.get(key) or {}
        out.append({"source": key, "name": DCL.short_name(key), "width": c.get("width"), "height": c.get("height")})
    return out


def facts_of(svc, ds: P.Dataset, decl: dict | None) -> K.Facts:
    cams = cameras_of(svc, ds, decl)
    sizes = {c["source"]: ([c["width"], c["height"]] if c.get("width") and c.get("height") else None) for c in cams}
    if format_of(ds) == "mcap":
        topics: dict[str, Any] = {}
        try:
            pr, _, _ = svc.mcap.probe_source(dataset_source(svc.rt, ds, ds.owner_id))
            topics = {t: tp.schema for t, tp in pr.topics.items()}
        except ApiError:
            topics = {}
        return K.Facts.mcap(topics, sizes, cameras=list(sizes))
    facts = K.Facts.lerobot(_info(ds), kind=format_of(ds))
    facts.cameras = sizes
    return facts


def draft_of(svc, ds: P.Dataset) -> dict:
    """``{"declaration", "unresolved"}`` drafted from the dataset now (never stored)."""
    fmt = format_of(ds)
    src = dataset_source(svc.rt, ds, ds.owner_id)
    if fmt in ("lerobot", "lance"):
        return DD.draft_lerobot(_info(ds), listing=_listing(svc, src), read=_reader(svc, src),
                                base="builtin:lerobot" if fmt == "lerobot" else None)
    if fmt == "mcap":
        from curation.viz import mcap_mapping as MMAP

        try:
            pr, _, _ = svc.mcap.probe_source(src)
        except ApiError:
            return {"declaration": None, "unresolved": [{"field": "<dataset>", "code": "probe_failed"}]}
        site = [{"id": t.id, "name": t.name, "mapping": DCL.mapping_of(t.mapping)}
                for t in svc.rt.repo.list_viz_templates(owner=ds.owner_id) if DCL.mapping_of(t.mapping)]
        layer1 = DCL.mapping_of(ds.viz_mapping) or MMAP.draft(pr, site_templates=site)[0]
        calibrations = {t: tp.calibration for t, tp in pr.topics.items() if getattr(tp, "calibration", None)}
        return DD.draft_mcap(layer1, {t: tp.schema for t, tp in pr.topics.items()},
                             robot_type=(_preflight(ds).get("dataset") or {}).get("robot_type"),
                             calibrations=calibrations)
    return {"declaration": None, "unresolved": []}


def _trajectory(svc, ds: P.Dataset, decl: dict | None, drafted: bool, cameras: list[str]) -> dict:
    """What the EEF module would do with the declaration (design doc 25 §4.1), without the dataset's listing:
    a session or a handheld export is told by the preflight's format and profile."""
    from curation.extensions.eef_consistency import declared, derive

    fmt = format_of(ds)
    d = _preflight(ds).get("dataset") or {}
    if fmt == "umi_session":
        return {"kind": derive.SESSION}
    handheld = fmt == "mcap" and (d.get("profile") == "umi_das" or (decl or {}).get("base") == "builtin:umi")
    if handheld:
        return {"kind": derive.MCAP_DERIVE}
    if fmt == "lerobot" and str(d.get("robot_type") or "").startswith("umi"):
        return {"kind": derive.GENERATE}
    if decl is None:
        return {"kind": derive.MISSING_POSE}
    ready = declared.readiness(decl, cameras, kind=fmt)
    if ready["ready"] and not drafted:
        return {"kind": derive.GENERATE, "cameras": ready["cameras"]}
    if ready["pose"]:
        missing = ready["missing"] if not (ready["ready"] and drafted) else \
            [{"field": "<declaration>", "code": derive.DECLARATION_UNCONFIRMED}]
        return {"kind": derive.MISSING_DECLARATION, "missing": missing, "cameras": ready["cameras"]}
    return {"kind": derive.MISSING_POSE, "cameras": ready["cameras"]}


def info_of(ds: P.Dataset) -> dict:
    """C4 ``DatasetDeclarationInfo`` of a registration (the list and the detail)."""
    decl = DCL.normalize(ds.viz_mapping)
    has = isinstance(decl, dict) and any(k in decl for k in DCL.LAYER2_3)
    if not decl:
        return {"state": "none", "version": int(ds.viz_mapping_version or 0), "updated_at": None, "name": None,
                "layers": [], "assumed": 0, "suspects": 0}
    layers = (["sources"] if DCL.has_layer1(decl) else []) + (["semantics", "calibration"] if has else [])
    return {"state": "confirmed", "version": int(ds.viz_mapping_version or 0), "updated_at": ds.viz_mapping_updated_at,
            "name": decl.get("name") if isinstance(decl.get("name"), str) else None, "layers": layers,
            "assumed": len(DCL.assumed(decl)), "suspects": len(decl.get("suspects") or [])}


def declaration_doc(svc, ds: P.Dataset) -> dict:
    """C4 ``DatasetDeclaration``."""
    decl = DCL.normalize(ds.viz_mapping)
    fmt = format_of(ds)
    if fmt == "unknown":
        raise ApiError("validation_failed", "这个数据集的格式认不出来，没有声明可写", details={"reason": "format_unknown"})
    drafted = draft_of(svc, ds)
    cams = cameras_of(svc, ds, decl or drafted.get("declaration"))
    sources = [c["source"] for c in cams]
    start = DD.merge(drafted.get("declaration"), decl) if decl is not None else drafted.get("declaration")
    # what tasks would use: the confirmed declaration when it says more than a mapping, else the draft (never used)
    confirmed = decl is not None and any(decl.get(k) for k in DCL.LAYER2_3)
    trajectory = _trajectory(svc, ds, decl if confirmed else start, not confirmed, sources)
    unresolved = DD.unresolved_of(decl, cameras=sources) if confirmed else (drafted.get("unresolved") or [])
    return {"dataset_id": ds.id, "format": fmt, "state": "confirmed" if decl else "none", "declaration": decl,
            "version": int(ds.viz_mapping_version or 0), "updated_at": ds.viz_mapping_updated_at,
            "draft": start, "unresolved": unresolved, "cameras": cams, "trajectory": trajectory,
            "assumed": DCL.assumed(decl) if decl else [], "suspects": list((decl or {}).get("suspects") or []),
            "warnings": []}


def check_for(svc, ds: P.Dataset, doc: dict) -> dict:
    """``{"declaration", "suspects"}``: the document checked against the dataset; ``validation_failed`` with every
    located error."""
    decl = DCL.normalize(doc)
    if format_of(ds) == "mcap" and not DCL.has_layer1(decl):
        raise ApiError("validation_failed", "mcap 数据集的声明要有第一层（哪些 topic 是相机与曲线）",
                       details={"errors": [{"field": "declaration.cameras", "problem": "mcap 数据集要映射相机与曲线"}]})
    got = K.check(decl, facts_of(svc, ds, decl))
    if got["errors"]:
        first = got["errors"][0]
        raise ApiError("validation_failed", f"声明不合格：{first['field']} {first['problem']}"
                       + (f"（另有 {len(got['errors']) - 1} 处）" if len(got["errors"]) > 1 else ""),
                       details={"errors": [{"field": f"declaration.{e['field']}", "problem": e["problem"]}
                                           for e in got["errors"][:50]]})
    decl["suspects"] = got["suspects"]
    if not decl["suspects"]:
        decl.pop("suspects")
    return {"declaration": decl, "suspects": got["suspects"]}


def put_declaration(svc, dataset_id: str, owner: str, doc: dict) -> P.Dataset:
    ds = svc.rt.repo.get_dataset(dataset_id, owner=owner)
    if format_of(ds) == "unknown":
        raise ApiError("validation_failed", "这个数据集的格式认不出来，没有声明可写", details={"reason": "format_unknown"})
    checked = check_for(svc, ds, doc)
    return svc.rt.repo.set_dataset_viz_mapping(dataset_id, checked["declaration"], owner=owner)


def for_cli(decl: Any) -> dict | None:
    """The declaration a CLI command is given: one that says more than an mcap mapping (the readers get that one
    as ``ingest.mcap_mapping``); None otherwise."""
    d = DCL.normalize(decl)
    if not isinstance(d, dict) or not any(d.get(k) for k in DCL.LAYER2_3):
        return None
    return d


def dumps(decl: dict) -> str:
    return json.dumps(decl, ensure_ascii=False, sort_keys=True)
