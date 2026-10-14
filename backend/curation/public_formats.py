"""The public cache bucket's datasets in every format the platform reads (requester 2026-10-12).

``ingest.public_catalog`` (A class, v1's) lists only LeRobot datasets - a ``meta/info.json`` at the root of a
mirrored repository: 7 of the mirror's 411 on 2026-10-12. v2 reads more: LeRobot v2 / v3, Lance (``frames.lance``
next to its LeRobot v3 ``meta/``), mcap (``.mcap`` files in the directory) and a raw UMI session
(``dataset_plan.pkl`` and ``demos/``; the EEF module only). This reads the same manifest - ``{name: [object
keys]}`` at the bucket's root, with ``public_catalog``'s configuration - and tells a directory's format from the
file list alone, no request per dataset to classify it. A readable dataset may sit in a subdirectory of a mirrored
repository (``HABIT/full``, a UMI session's ``mcap/``): every directory down to :data:`MAX_DEPTH` levels is looked
at and each readable one listed, named ``<repository>/<path>``. LeRobot and Lance entries then read their
``meta/info.json`` for the version and episode count, as v1's catalog does; an mcap directory counts its files.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

LEROBOT, LANCE, MCAP, UMI_SESSION = "lerobot", "lance", "mcap", "umi_session"
#: how deep under a mirrored repository a readable dataset is looked for (0 = the repository itself)
MAX_DEPTH = 3
#: how often the manifest's ETag is checked again
RECHECK_S = 60.0
_THREADS = 8
_ID_RE = re.compile(r'"id"\s*:\s*"([^"]+)"')
_CACHE: dict = {"etag": None, "entries": [], "checked": 0.0}


def kind_of(keys: set[str]) -> str | None:
    """The format of a directory from the object keys under it (relative to it), or None when the platform
    cannot read it."""
    if "meta/info.json" in keys:
        return LANCE if any(k.startswith("frames.lance/") for k in keys) else LEROBOT
    if "dataset_plan.pkl" in keys and any(k.startswith("demos/") for k in keys):
        return UMI_SESSION
    if any("/" not in k and k.endswith(".mcap") for k in keys):
        return MCAP
    return None


def readable(name: str, files: list[str], prefix: str) -> list[tuple[str, str, set[str]]]:
    """``[(path, kind, keys)]`` of one manifest entry: the repository itself (path ``""``) and its directories down
    to :data:`MAX_DEPTH` levels that the platform can read, ``keys`` relative to that directory."""
    base = f"{prefix}{name}/"
    under: dict[str, set[str]] = {}
    for f in files:
        f = str(f)
        if not f.startswith(base):
            continue
        parts = f[len(base):].split("/")
        for d in range(min(len(parts) - 1, MAX_DEPTH) + 1):
            under.setdefault("/".join(parts[:d]), set()).add("/".join(parts[d:]))
    out = []
    for path in sorted(under):
        kind = kind_of(under[path])
        if kind is not None:
            out.append((path, kind, under[path]))
    return out


def _hf_id(st, c: dict, prefix: str, name: str, files: list[str]) -> str | None:
    """The mirrored repository's HuggingFace id (``.metadata.json``), if it says one."""
    key = f"{prefix}{name}/.metadata.json"
    if key not in files:
        return None
    try:
        m = _ID_RE.search(st.get_bytes(c["bucket"], key)[:2048].decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - the name stands in
        return None
    return m.group(1) if m else None


def _entry(st, c: dict, prefix: str, name: str, files: list[str], path: str, kind: str, keys: set[str]) -> dict:
    from .ingest import public_catalog

    full = f"{name}/{path}" if path else name
    base = f"{prefix}{full}/"
    hf = _hf_id(st, c, prefix, name, files)
    out = {"name": full, "id": f"{hf}/{path}" if hf and path else (hf or full), "kind": kind, "version": "",
           "episodes": None, "files": len(keys), "url": public_catalog.dataset_url(full)}
    if kind in (LEROBOT, LANCE):
        try:
            info = json.loads(st.get_bytes(c["bucket"], base + "meta/info.json"))
            out["version"] = str(info.get("codebase_version") or "")
            total = info.get("total_episodes")
            out["episodes"] = int(total) if isinstance(total, (int, float)) else None
        except Exception:  # noqa: BLE001 - one broken info.json does not hold the list back
            pass
    if kind == MCAP:
        out["episodes"] = sum(1 for k in keys if "/" not in k and k.endswith(".mcap"))
    if kind == LEROBOT:
        # a mirror that did not finish: zero-byte videos, or none (v1's catalog found them, 2026-08-21)
        mp4s = sorted(k for k in keys if k.endswith(".mp4"))
        if not mp4s:
            out["warning"] = "没有视频文件"
        else:
            try:
                size, _etag = st.head(c["bucket"], base + mp4s[0])
                if size == 0:
                    out["warning"] = "视频文件为空(镜像不完整)"
            except Exception:  # noqa: BLE001
                pass
    return out


def _load(st, c: dict) -> list[dict]:
    from .ingest.public_catalog import PublicCatalogError

    try:
        manifest = json.loads(st.get_bytes(c["bucket"], c["manifest"]))
    except Exception as e:  # noqa: BLE001 - one sentence for the page
        raise PublicCatalogError(f"读不到公共数据集清单 tos://{c['bucket']}/{c['manifest']}:"
                                 f"{type(e).__name__}: {str(e)[:160]}") from e
    if not isinstance(manifest, dict):
        raise PublicCatalogError(f"公共数据集清单 {c['manifest']} 不是 {{名字: [文件…]}} 形状")
    prefix = f"{c['prefix']}/" if c["prefix"] else ""
    jobs = []
    for name in sorted(manifest):
        files = [str(f) for f in manifest[name]] if isinstance(manifest[name], list) else []
        jobs += [(name, files, path, kind, keys) for path, kind, keys in readable(name, files, prefix)]
    if not jobs:
        return []
    with ThreadPoolExecutor(min(_THREADS, len(jobs))) as ex:
        return list(ex.map(lambda j: _entry(st, c, prefix, *j), jobs))


def catalog(*, store=None, force: bool = False, now=None) -> list[dict]:
    """The bucket's readable datasets ``[{name, id, kind, version, episodes, files, url[, warning]}]`` by name;
    [] when no bucket is configured, ``PublicCatalogError`` when the manifest cannot be read. Kept by the
    manifest's ETag, checked again after :data:`RECHECK_S`."""
    from .ingest import public_catalog

    c = public_catalog.configured()
    if not c:
        return []
    t = time.time() if now is None else now
    if not force and _CACHE["entries"] and t - _CACHE["checked"] < RECHECK_S:
        return list(_CACHE["entries"])
    if store is not None:
        st = store
    else:
        from . import tos_store

        st = tos_store.make_store_for(c["bucket"], c["region"])
    try:
        _size, etag = st.head(c["bucket"], c["manifest"])
    except Exception:  # noqa: BLE001 - unknown ETag: read it whole
        etag = None
    if not force and etag and _CACHE["entries"] and _CACHE["etag"] == etag:
        _CACHE["checked"] = t
        return list(_CACHE["entries"])
    entries = _load(st, c)
    _CACHE.update(etag=etag, entries=entries, checked=t)
    return list(entries)


def reset() -> None:
    """Forget the kept list (tests, a configuration change)."""
    _CACHE.update(etag=None, entries=[], checked=0.0)
