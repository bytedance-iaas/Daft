"""Picking a dataset and previewing its episodes - metadata only (design doc 03 §2, §10).

* ``GET /datasets/browse``: the sub-directories of a private ``tos://`` prefix (or of
  a directory under the local data root), each with a format hint and, for LeRobot,
  its episode count from ``meta/info.json``; or the HuggingFace cache bucket's
  catalog (v1's ``public_catalog``, anonymous). Cursor paging over the names.
* ``GET /datasets/episodes``: one page of a dataset's episodes for the preview grid -
  index, length, task text, and per camera a browser URL (presigned on the public
  endpoint; the public bucket's plain URL; v3 files come with ``from_ts`` / ``to_ts``).
  The server decodes nothing: ``meta/info.json`` plus the episode table
  (``meta/episodes.jsonl`` or ``meta/episodes/*.parquet``) are all it reads, and the
  table is cached for a few minutes per dataset, so paging does not re-read it.

Both read in the Daemon, not through a CLI process: they are a couple of small
metadata reads, like the pre-start input check, and the CLI has no command for them.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import io
import json
import logging
import os
import pathlib
import threading
import time
from typing import Any

from ..errors import ApiError
from ..pagination import keyset_page
from ..secrets import BadPath, Unavailable, browser_url
from ..secrets import tos as T

log = logging.getLogger("daemon.orchestr")

CACHE_TTL_S = 300.0
_CACHE_MAX = 32
_INFO = "meta/info.json"


@dataclasses.dataclass
class EpisodeRow:
    index: int
    length: int
    task: str
    videos: dict[str, str]                    # short camera name -> key relative to the dataset
    windows: dict[str, tuple[float, float]]    # v3: camera -> (from_ts, to_ts)


class _MetaStorage:
    """The few reads the episode table needs, on TOS (a W8 client) or a local directory."""

    def __init__(self, uri: str, *, client=None, root: pathlib.Path | None = None):
        self.uri = uri
        self.client = client
        self.root = root
        if client is not None:
            self.bucket, self.prefix = T.split_uri(uri)

    def _key(self, rel: str) -> str:
        return T.join_key(self.prefix, rel)

    def read(self, rel: str) -> bytes | None:
        if self.root is not None:
            try:
                return (self.root / rel).read_bytes()
            except FileNotFoundError:
                return None
        try:
            return self.client.get_object(self.bucket, self._key(rel)).read()
        except Exception as err:  # noqa: BLE001
            code = str(getattr(err, "code", "") or "")
            if code in ("NoSuchKey", "NotFound") or getattr(err, "status_code", None) == 404:
                return None
            raise

    def list(self, rel_prefix: str) -> list[str]:
        if self.root is not None:
            base = self.root / rel_prefix
            if not base.is_dir():
                return []
            return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*")
                          if p.is_file())
        out, token = [], None
        start = self._key(rel_prefix).rstrip("/") + "/"
        cut = len(self.prefix.strip("/")) + 1 if self.prefix.strip("/") else 0
        while True:
            page = self.client.list_objects_type2(self.bucket, prefix=start,
                                                  continuation_token=token, max_keys=1000)
            out += [o.key[cut:] for o in getattr(page, "contents", None) or []]
            if not getattr(page, "is_truncated", False):
                return sorted(out)
            token = page.next_continuation_token


def _short(feature: str) -> str:
    for prefix in ("observation.images.", "observation.image."):
        if feature.startswith(prefix):
            return feature[len(prefix):]
    return feature


def read_episode_table(st: _MetaStorage) -> tuple[list[EpisodeRow], float | None]:
    """The episodes of a LeRobot v2 / v3 dataset from its metadata; (rows, fps)."""
    raw = st.read(_INFO)
    if raw is None:
        raise ApiError("validation_failed", f"{st.uri} 下没有 meta/info.json：地址不对，或者不是 LeRobot 数据集",
                       details={"errors": [{"field": "uri", "problem": "no meta/info.json"}]})
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError("validation_failed", "meta/info.json 不是合法的 JSON") from None
    feats = info.get("features") or {}
    cams = [k for k, v in feats.items() if isinstance(v, dict) and v.get("dtype") == "video"]
    fps = info.get("fps") if isinstance(info.get("fps"), (int, float)) else None
    version = str(info.get("codebase_version") or "")
    if version.startswith("v2"):
        return _v2_rows(st, info, cams), fps
    if version.startswith("v3"):
        return _v3_rows(st, info, cams), fps
    raise ApiError("validation_failed", f"不支持的 LeRobot 版本 {version or '（未写）'}：本期只支持 v2 / v3")


def _v2_rows(st: _MetaStorage, info: dict, cams: list[str]) -> list[EpisodeRow]:
    raw = st.read("meta/episodes.jsonl")
    if raw is None:
        raise ApiError("validation_failed", "meta/episodes.jsonl 不存在（LeRobot v2 的 episode 表）")
    chunks = int(info.get("chunks_size") or 1000) or 1000
    tpl = str(info.get("video_path") or "")
    rows = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            ep = json.loads(line)
            idx = int(ep["episode_index"])
        except (ValueError, KeyError, TypeError):
            continue
        tasks = ep.get("tasks") or []
        task = str(tasks[0]) if isinstance(tasks, list) and tasks else ""
        videos = {}
        for vk in cams:
            try:
                videos[_short(vk)] = tpl.format(episode_chunk=idx // chunks, video_key=vk,
                                                episode_index=idx)
            except (KeyError, IndexError, ValueError):
                continue
        rows.append(EpisodeRow(idx, int(ep.get("length") or 0), task, videos, {}))
    return sorted(rows, key=lambda r: r.index)


def _v3_rows(st: _MetaStorage, info: dict, cams: list[str]) -> list[EpisodeRow]:
    import pandas as pd

    keys = [k for k in st.list("meta/episodes") if k.endswith(".parquet")]
    if not keys:
        raise ApiError("validation_failed", "meta/episodes/ 下没有 parquet（LeRobot v3 的 episode 表）")
    frames = []
    for key in keys:
        raw = st.read(key)
        if raw is not None:
            frames.append(pd.read_parquet(io.BytesIO(raw)))
    table = pd.concat(frames, ignore_index=True)
    tpl = str(info.get("video_path") or "")
    rows = []
    for _, ep in table.iterrows():
        idx = int(ep["episode_index"])
        tasks = ep.get("tasks") if "tasks" in table.columns else None
        task = ""
        if tasks is not None and len(tasks) > 0:
            task = str(list(tasks)[0])
        videos, windows = {}, {}
        for vk in cams:
            try:
                videos[_short(vk)] = tpl.format(video_key=vk,
                                                chunk_index=int(ep[f"videos/{vk}/chunk_index"]),
                                                file_index=int(ep[f"videos/{vk}/file_index"]))
                windows[_short(vk)] = (float(ep[f"videos/{vk}/from_timestamp"]),
                                       float(ep[f"videos/{vk}/to_timestamp"]))
            except (KeyError, IndexError, ValueError, TypeError):
                continue
        rows.append(EpisodeRow(idx, int(ep.get("length") or 0), task, videos, windows))
    return sorted(rows, key=lambda r: r.index)


class Browser:
    def __init__(self, orch):
        self.orch = orch
        self._cache: dict[tuple, tuple[float, list[EpisodeRow], float | None]] = {}
        self._lock = threading.Lock()

    # -- where a dataset is --------------------------------------------------------------
    def _key(self, source: str, cred_id: str | None, owner: str):
        if source != "tos":
            return None
        try:
            return self.orch.svc.tos_key(cred_id, owner=owner, role="input")
        except Unavailable as err:
            raise ApiError("validation_failed", err.message_zh) from None

    def _local_root(self, uri: str) -> pathlib.Path:
        from ..taskspec import local_path

        return pathlib.Path(local_path(self.orch.settings, uri, "uri"))

    def _tos_error(self, err: Exception, uri: str) -> ApiError:
        from curation.cli.storage import describe_tos_error

        return ApiError("validation_failed", f"读不到 {uri}：{describe_tos_error(err)}",
                        details={"errors": [{"field": "uri", "problem": type(err).__name__}]})

    # -- episodes -----------------------------------------------------------------------
    def episode_rows(self, source: str, uri: str, region: str | None, cred_id: str | None,
                     owner: str) -> tuple[list[EpisodeRow], float | None]:
        cache_key = (source, uri, region, cred_id, owner)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(cache_key)
            if hit is not None and now - hit[0] < CACHE_TTL_S:
                return hit[1], hit[2]
        if source == "local":
            rows, fps = read_episode_table(_MetaStorage(uri, root=self._local_root(uri)))
        else:
            key = self._key(source, cred_id, owner)
            try:
                with self.orch.svc.tos(key, region) as (client, _):
                    rows, fps = read_episode_table(_MetaStorage(uri, client=client))
            except ApiError:
                raise
            except Exception as err:  # noqa: BLE001 - SDK errors, network
                raise self._tos_error(err, uri) from None
        with self._lock:
            if len(self._cache) >= _CACHE_MAX:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
            self._cache[cache_key] = (now, rows, fps)
        return rows, fps

    def episodes(self, *, source: str, uri: str, region: str | None, cred_id: str | None,
                 owner: str, cursor: str | None, limit: int, ttl_s: int = 1800) -> dict:
        rows, fps = self.episode_rows(source, uri, region, cred_id, owner)
        page = keyset_page(rows, key=lambda r: r.index, kind="episodes",
                           scope={"source": source, "uri": uri, "region": region},
                           cursor=cursor, limit=limit)
        key = self._key(source, cred_id, owner) if source == "tos" else None
        items = []
        for r in page.items:
            cams = []
            if source != "local":
                for name, rel in sorted(r.videos.items()):
                    try:
                        url = browser_url(self.orch.svc, uri, rel, ttl_s=ttl_s, key=key,
                                          region=region)
                    except BadPath:
                        continue
                    cam: dict[str, Any] = {"name": name, "url": url}
                    if name in r.windows:
                        cam["from_ts"], cam["to_ts"] = r.windows[name]
                    cams.append(cam)
            length_s = round(r.length / float(fps), 3) if fps else None
            items.append({"index": r.index, "length_s": length_s, "task": r.task,
                          "task_source": "原始标注" if r.task else "无", "cameras": cams})
        return {"items": items, "next_cursor": page.next_cursor, "has_more": page.has_more}

    # -- browse ------------------------------------------------------------------------
    def browse(self, *, source: str, uri: str | None, region: str | None, cred_id: str | None,
               owner: str, cursor: str | None, limit: int) -> dict:
        if source == "public":
            entries = self._public()
        elif source == "local":
            entries = self._local(uri)
        else:
            if not uri:
                raise ApiError("validation_failed", "source=tos 要给 uri（tos://存储桶/前缀）",
                               details={"errors": [{"field": "uri", "problem": "missing"}]})
            entries = self._tos(uri, region, cred_id, owner)
        page = keyset_page(entries, key=lambda e: e["name"], kind="browse",
                           scope={"source": source, "uri": uri, "region": region},
                           cursor=cursor, limit=limit)
        items = page.items
        if source != "public":
            items = self._hints(items, source, region, cred_id, owner)
        return {"items": items, "next_cursor": page.next_cursor, "has_more": page.has_more}

    def _public(self) -> list[dict]:
        from curation.ingest import public_catalog

        try:
            catalog = public_catalog.catalog()
        except public_catalog.PublicCatalogError as err:
            raise ApiError("validation_failed", str(err)) from None
        out = []
        for e in catalog:
            version = str(e.get("version") or "")
            hint = "lerobot_v3" if version.startswith("v3") else (
                "lerobot_v2" if version.startswith("v2") else "unknown")
            out.append({"name": str(e.get("id") or e["name"]), "uri": str(e["url"]),
                        "format_hint": hint, "episodes": e.get("episodes")})
        return sorted(out, key=lambda e: e["name"])

    def _local(self, uri: str | None) -> list[dict]:
        root = self.orch.settings.local_data_root
        if root is None:
            raise ApiError("validation_failed", "「本地挂载路径」这个数据来源没有开启（站点配置 localDataRoot）")
        base = self._local_root(uri) if uri else pathlib.Path(root)
        if not base.is_dir():
            raise ApiError("validation_failed", f"{base} 不是一个目录")
        return sorted(({"name": p.name, "uri": str(p)} for p in base.iterdir()
                       if p.is_dir() and not p.name.startswith(".")), key=lambda e: e["name"])

    def _tos(self, uri: str, region: str | None, cred_id: str | None, owner: str) -> list[dict]:
        try:
            bucket, prefix = T.split_uri(uri)
        except ValueError as err:
            raise ApiError("validation_failed", f"uri 的写法不对：{err}") from None
        key = self._key("tos", cred_id, owner)
        start = (prefix.strip("/") + "/") if prefix.strip("/") else ""
        names: list[str] = []
        try:
            with self.orch.svc.tos(key, region) as (client, _):
                token = None
                while True:
                    page = client.list_objects_type2(bucket, prefix=start, delimiter="/",
                                                     continuation_token=token, max_keys=1000)
                    for cp in getattr(page, "common_prefixes", None) or []:
                        names.append(str(getattr(cp, "prefix", cp))[len(start):].strip("/"))
                    if not getattr(page, "is_truncated", False):
                        break
                    token = page.next_continuation_token
        except Exception as err:  # noqa: BLE001
            raise self._tos_error(err, uri) from None
        base = uri.rstrip("/")
        return sorted(({"name": n, "uri": f"{base}/{n}"} for n in names if n),
                      key=lambda e: e["name"])

    def _hints(self, items: list[dict], source: str, region: str | None, cred_id: str | None,
               owner: str) -> list[dict]:
        """Format hint and episode count per entry, from its ``meta/info.json`` (in parallel)."""
        def hint(entry: dict) -> dict:
            out = {"name": entry["name"], "uri": entry["uri"], "format_hint": "unknown",
                   "episodes": None}
            try:
                if source == "local":
                    raw = _MetaStorage(entry["uri"], root=pathlib.Path(entry["uri"])).read(_INFO)
                else:
                    key = self._key(source, cred_id, owner)
                    with self.orch.svc.tos(key, region) as (client, _):
                        raw = _MetaStorage(entry["uri"], client=client).read(_INFO)
                if raw is None:
                    return out
                info = json.loads(raw.decode("utf-8"))
                version = str(info.get("codebase_version") or "")
                out["format_hint"] = "lerobot_v3" if version.startswith("v3") else (
                    "lerobot_v2" if version.startswith("v2") else "unknown")
                te = info.get("total_episodes")
                out["episodes"] = int(te) if isinstance(te, (int, float)) else None
            except Exception:  # noqa: BLE001 - one bad entry does not spoil the listing
                pass
            return out

        if not items:
            return []
        with concurrent.futures.ThreadPoolExecutor(min(8, len(items))) as ex:
            return list(ex.map(hint, items))


def env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")
