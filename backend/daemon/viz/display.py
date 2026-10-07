"""The display configuration of a registered dataset (design doc 21 §6, C4 2.7.0 ``VizDisplay``).

How a dataset is shown by default, one configuration per registration that everyone who opens it
shares: the layout, the cameras' order, names and which ones the layout templates leave out, curve
groups instead of the automatic ones (LeRobot and Lance - an mcap dataset's curves come from its
field mapping), the subtitle track, speed and looping.

It is kept in the registration's ``display_config`` column as ``{"version", "updated_at",
"config"}``; a bare configuration (written before there was an envelope) counts as version 0. The
models apply it on every request on top of the cached reader metadata, so no cache keys on it: the
LeRobot / Lance readers draw the configured curve groups (:func:`override_groups`), the service
reorders, renames and hides cameras and makes the track's source primary (:func:`apply_model`,
:func:`apply_episode`); the layout, the hidden lines and playback are the player's. What a later
change of the dataset left behind is skipped when read, never refused; a save is checked against the
dataset's current model (:func:`check`).
"""
from __future__ import annotations

import dataclasses
from typing import Any

from ..errors import ApiError

#: readers whose curve groups a configuration may replace (mcap: the field mapping makes them)
EDITABLE = ("lerobot", "lance")


def envelope(raw: Any) -> tuple[dict | None, int, int | None]:
    """``(config, version, updated_at)`` of a registration's ``display_config`` column."""
    if not isinstance(raw, dict):
        return None, 0, None
    if "version" in raw and "config" in raw:
        cfg, version, at = raw.get("config"), raw.get("version"), raw.get("updated_at")
        return (cfg if isinstance(cfg, dict) else None,
                version if isinstance(version, int) and version >= 0 else 0,
                at if isinstance(at, int) else None)
    return raw, 0, None


def for_task(cfg: dict | None) -> dict | None:
    """What a task's mini player takes of the registration's configuration: cameras, curve groups
    and the track (its layout comes from the finding, design doc 18 §4.6)."""
    if not cfg:
        return None
    curves = cfg.get("curves") if isinstance(cfg.get("curves"), dict) else {}
    groups = curves.get("groups") if isinstance(curves.get("groups"), list) else None
    out = {"cameras": cfg.get("cameras") if isinstance(cfg.get("cameras"), list) else None,
           "curves": {"groups": groups} if groups else None,
           "track": cfg.get("track") if isinstance(cfg.get("track"), str) else None}
    return out if any(v is not None for v in out.values()) else None


# ---------------------------------------------------------------- applying

def override_groups(cfg: dict | None, dims: set[tuple[str, int]], taken: set[str]) -> list[dict] | None:
    """The configured curve groups still drawable - lines on dimensions the dataset has, keys no other
    stream uses - or None for the automatic ones (none configured, or none left)."""
    curves = (cfg or {}).get("curves")
    groups = curves.get("groups") if isinstance(curves, dict) else None
    if not isinstance(groups, list) or not groups:
        return None
    out, keys = [], set(taken)
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("key"), str) or g["key"] in keys:
            continue
        lines = [ln for ln in g.get("lines") or []
                 if isinstance(ln, dict) and (ln.get("source"), ln.get("dim")) in dims]
        if lines:
            keys.add(g["key"])
            out.append({**g, "lines": lines})
    return out or None


def apply_model(model: dict, cfg: dict | None) -> dict:
    """The presentation model with the configuration's cameras and track applied; every camera says
    whether the templates leave it out (``hidden``), and ``display`` is the configuration in effect."""
    out = dict(model)
    out["cameras"] = _cameras(model.get("cameras") or [], (cfg or {}).get("cameras"))
    track = (cfg or {}).get("track")
    sources = model.get("annotation_sources") or []
    if isinstance(track, str) and any(s.get("key") == track and s.get("kind") == "segments" for s in sources):
        out["annotation_sources"] = [dict(s, primary=s["key"] == track) if s.get("kind") == "segments" else s
                                     for s in sources]
    out["display"] = cfg
    return out


def _cameras(cams: list[dict], entries: Any) -> list[dict]:
    by_key = {c["key"]: c for c in cams}
    out, seen = [], set()
    for e in entries if isinstance(entries, list) else []:
        c = by_key.get(e.get("key")) if isinstance(e, dict) else None
        if c is None or c["key"] in seen:
            continue
        seen.add(c["key"])
        name = e.get("name")
        out.append({**c, "name": name if isinstance(name, str) and name else c["name"], "hidden": bool(e.get("hidden"))})
    return out + [{**c, "hidden": False} for c in cams if c["key"] not in seen]


def apply_episode(ep: dict, cfg: dict | None) -> dict:
    """An episode whose subtitle track is the configured one (when it has that track)."""
    track = (cfg or {}).get("track")
    ann = ep.get("annotations")
    if not isinstance(track, str) or not isinstance(ann, dict):
        return ep
    tracks = ann.get("tracks") or []
    if not any(t.get("key") == track for t in tracks):
        return ep
    return {**ep, "annotations": {**ann, "tracks": [dict(t, primary=t.get("key") == track) for t in tracks]}}


# ---------------------------------------------------------------- the document

def _dims(model: dict) -> dict[tuple[str, int], dict]:
    """The model's curve dimensions (its automatic groups' lines): ``(feature, dim) -> line``."""
    out: dict[tuple[str, int], dict] = {}
    for s in model.get("streams") or []:
        if s.get("kind") != "series":
            continue
        for ln in s.get("lines") or []:
            src, dim = ln.get("source"), ln.get("dim")
            if isinstance(src, str) and isinstance(dim, int):
                out.setdefault((src, dim), {"source": src, "dim": dim, "name": ln["name"], "role": ln["role"]})
    return out


def defaults(model: dict, reader: str | None) -> dict:
    """What the visualizer does without a configuration (``VizDisplay.defaults``), from the model
    made without one."""
    editable = reader in EDITABLE
    groups = []
    if editable:
        for s in model.get("streams") or []:
            if s.get("kind") != "series" or not s.get("available"):
                continue
            lines = [{"source": ln["source"], "dim": ln["dim"], "name": ln["name"], "role": ln["role"]}
                     for ln in s.get("lines") or [] if isinstance(ln.get("source"), str) and isinstance(ln.get("dim"), int)]
            if lines:
                groups.append({"key": s["key"], "name": s["name"], "unit": s.get("unit"), "smart": bool(s.get("smart")),
                               "lines": lines})
    return {"cameras": [{"key": c["key"], "name": c["name"], "source": c["source"]} for c in model.get("cameras") or []],
            "groups": groups, "dimensions": list(_dims(model).values()) if editable else [],
            "tracks": [{"key": s["key"], "name": s["name"]} for s in model.get("annotation_sources") or []
                       if s.get("kind") == "segments" and s.get("supported")],
            "groups_editable": editable}


def base_model(svc, src) -> tuple[dict, str | None]:
    """The source's model made without its configuration, and its reader."""
    return svc.dataset(dataclasses.replace(src, display_config=None)), svc.reader_of(src)


def doc(svc, ds, owner: str, *, base: tuple[dict, str | None] | None = None) -> dict:
    """``VizDisplay`` of a registration."""
    from .source import dataset_source

    model, reader = base if base is not None else base_model(svc, dataset_source(svc.rt, ds, owner))
    cfg, version, at = envelope(ds.display_config)
    return {"dataset_id": ds.id, "config": cfg, "version": version, "updated_at": at,
            "defaults": defaults(model, reader)}


def save(svc, dataset_id: str, who, config: dict | None) -> dict:
    """Replaces the configuration (None: restores the defaults) after checking it against the
    dataset's current model; the version goes up either way. Returns ``VizDisplay``."""
    from .source import dataset_source

    rt = svc.rt
    ds = rt.repo.get_dataset(dataset_id, owner=who.owner_id)
    base = base_model(svc, dataset_source(rt, ds, who.owner_id))
    if config is not None:
        problems = check(config, *base)
        if problems:
            raise ApiError("validation_failed", f"展示配置有 {len(problems)} 处对不上这个数据集",
                           details={"errors": problems})
    now = rt.clock()
    with rt.repo.transaction():
        ds = rt.repo.get_dataset(dataset_id, owner=who.owner_id)
        _, version, _ = envelope(ds.display_config)
        ds = rt.repo.update_dataset(dataset_id, owner=who.owner_id,
                                    display_config={"version": version + 1, "updated_at": now, "config": config})
        rt.repo.append_event(actor=who.display_name, action="dataset.update", resource=dataset_id, at=now,
                             owner=who.owner_id, detail={"fields": ["display_config"], "version": version + 1,
                                                         "restored": config is None})
    return doc(svc, ds, who.owner_id, base=base)


# ---------------------------------------------------------------- checking a save

def check(cfg: dict, model: dict, reader: str | None) -> list[dict]:
    """Where a configuration does not fit the dataset's current model (made without a configuration):
    ``[{field, problem}]``, empty when it fits. The Schema (C4 ``VizDisplayConfig``) is checked before."""
    out: list[dict] = []

    def bad(field: str, problem: str) -> None:
        out.append({"field": field, "problem": problem})

    cams = {c["key"] for c in model.get("cameras") or []}
    seen: set[str] = set()
    for i, c in enumerate(cfg.get("cameras") or []):
        if c["key"] not in cams:
            bad(f"cameras.{i}.key", f"数据集里没有相机 {c['key']}")
        elif c["key"] in seen:
            bad(f"cameras.{i}.key", f"相机 {c['key']} 写了两次")
        seen.add(c["key"])

    streams = model.get("streams") or []
    depth = {s["key"] for s in streams if s.get("kind") == "depth" and s.get("available")}
    others = {s["key"] for s in streams if s.get("kind") != "series"}
    drawn = {s["key"]: [ln["name"] for ln in s.get("lines") or []] for s in streams if s.get("kind") == "series"}
    curves = cfg.get("curves") or {}
    groups = curves.get("groups") or []
    if groups:
        if reader not in EDITABLE:
            bad("curves.groups", "mcap 数据集的曲线分组由字段映射决定，在「mcap 配置」里改")
        else:
            dims = _dims(model)
            keys: set[str] = set()
            used: dict[tuple[str, int], str] = {}
            for i, g in enumerate(groups):
                if g["key"] in keys or g["key"] in others:
                    bad(f"curves.groups.{i}.key", f"组的键 {g['key']} 重复了")
                keys.add(g["key"])
                names: set[str] = set()
                for j, ln in enumerate(g["lines"]):
                    d = (ln["source"], ln["dim"])
                    if d not in dims:
                        bad(f"curves.groups.{i}.lines.{j}", f"数据集里没有 {ln['source']} 的第 {ln['dim']} 维")
                    elif d in used:
                        bad(f"curves.groups.{i}.lines.{j}", f"{ln['source']} 的第 {ln['dim']} 维已经在组 {used[d]} 里")
                    else:
                        used[d] = g["key"]
                    if ln["name"] in names:
                        bad(f"curves.groups.{i}.lines.{j}.name", f"组里已经有一条线叫 {ln['name']}")
                    names.add(ln["name"])
            drawn = {g["key"]: [ln["name"] for ln in g["lines"]] for g in groups}
    for key, names in (curves.get("hidden") or {}).items():
        if key not in drawn:
            bad(f"curves.hidden.{key}", f"没有曲线组 {key}")
            continue
        for n in names:
            if n not in drawn[key]:
                bad(f"curves.hidden.{key}", f"曲线组 {key} 里没有线 {n}")

    track = cfg.get("track")
    tracks = {s["key"] for s in model.get("annotation_sources") or [] if s.get("kind") == "segments" and s.get("supported")}
    if track is not None and track not in tracks:
        bad("track", f"没有能作字幕轨的标注来源 {track}")

    layout = cfg.get("layout")
    if isinstance(layout, dict) and layout.get("template") == "custom":
        cols, rows, cells = layout.get("cols"), layout.get("rows"), layout.get("cells")
        if cols is None or rows is None or cells is None:
            bad("layout", "自定义布局要写 cols、rows 与 cells")
        elif len(cells) != cols * rows:
            bad("layout.cells", f"{cols} × {rows} 的布局应有 {cols * rows} 格，写了 {len(cells)} 格")
        else:
            for i, c in enumerate(cells):
                kind, key = c["kind"], c.get("key")
                if kind == "empty":
                    continue
                if not key:
                    bad(f"layout.cells.{i}.key", "要写这一格放什么")
                elif kind == "video" and key not in cams:
                    bad(f"layout.cells.{i}.key", f"数据集里没有相机 {key}")
                elif kind == "curve" and key not in drawn:
                    bad(f"layout.cells.{i}.key", f"没有曲线组 {key}")
                elif kind == "depth" and key not in depth:
                    bad(f"layout.cells.{i}.key", f"数据集里没有深度图 {key}")
                if "view" in c and kind != "depth":
                    bad(f"layout.cells.{i}.view", "只有深度图格子有上色与叠放")
    return out
