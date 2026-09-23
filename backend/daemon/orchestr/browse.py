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

mcap and lance (D44): a folder of ``episode_N.mcap`` is hinted ``mcap`` with its episode
count, lerobot-lance-convert's layout ``lance``; their episode grids have no camera URLs
(the videos are inside the files / ``videos.lance``). An mcap page reads the summary
section of each of its episodes (a few ranged reads) for the length and a metadata task
text; a task that only lives in a ``/task`` topic is ``task_unread``.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import io
import json
import logging
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
    #: mcap (D44): the episode's file; its summary gives the length and a metadata task text
    key: str | None = None
    size: int = 0


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

    def top(self) -> tuple[dict[str, int], list[str]]:
        """The top level of the dataset: ({file name: size}, [directory names])."""
        if self.root is not None:
            files, dirs = {}, []
            for entry in sorted(self.root.iterdir(), key=lambda p: p.name):
                if entry.is_dir():
                    dirs.append(entry.name)
                elif entry.is_file():
                    files[entry.name] = entry.stat().st_size
            return files, dirs
        start = self.prefix.strip("/") + "/" if self.prefix.strip("/") else ""
        files, dirs, token = {}, [], None
        while True:
            page = self.client.list_objects_type2(self.bucket, prefix=start, delimiter="/",
                                                  continuation_token=token, max_keys=1000)
            for o in getattr(page, "contents", None) or []:
                files[o.key[len(start):]] = int(getattr(o, "size", 0) or 0)
            for cp in getattr(page, "common_prefixes", None) or []:
                dirs.append(str(getattr(cp, "prefix", cp))[len(start):].strip("/"))
            if not getattr(page, "is_truncated", False):
                return files, sorted(dirs)
            token = page.next_continuation_token

    def read_range(self, rel: str, start: int, length: int) -> bytes:
        if self.root is not None:
            with open(self.root / rel, "rb") as fh:
                fh.seek(start)
                return fh.read(length)
        out = self.client.get_object(self.bucket, self._key(rel), range_start=start,
                                     range_end=start + length - 1)
        return out.read()

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
    """The episodes of a dataset from its metadata; (rows, fps).

    LeRobot v2 / v3 from ``meta/``; lance (lerobot-lance-convert, D44) from its ``meta/``
    too, without cameras - its videos are inside ``videos.lance``, no URL can play them;
    mcap (D44) from the file names, numbered by v1's rule - :meth:`Browser.episodes` reads
    the summaries of the episodes on a page for their length and metadata task text."""
    raw = st.read(_INFO)
    if raw is None:
        rows = _mcap_rows(st)
        if rows:
            return rows, None
        raise ApiError("validation_failed", f"{st.uri} 下没有 meta/info.json，也没有 episode_N.mcap："
                                            f"地址不对，或者不是可质检的数据集",
                       details={"errors": [{"field": "uri", "problem": "no meta/info.json"}]})
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError("validation_failed", "meta/info.json 不是合法的 JSON") from None
    feats = info.get("features") or {}
    cams = [k for k, v in feats.items() if isinstance(v, dict) and v.get("dtype") == "video"]
    fps = info.get("fps") if isinstance(info.get("fps"), (int, float)) else None
    version = str(info.get("codebase_version") or "")
    if str(info.get("storage_format") or "") == "lance" and version.startswith("v3"):
        rows = _v3_rows(st, info, cams)
        for r in rows:                         # the mp4s are blobs of videos.lance
            r.videos, r.windows = {}, {}
        return rows, fps
    if version.startswith("v2"):
        return _v2_rows(st, info, cams), fps
    if version.startswith("v3"):
        return _v3_rows(st, info, cams), fps
    raise ApiError("validation_failed", f"不支持的 LeRobot 版本 {version or '（未写）'}：本期只支持 v2 / v3")


def _mcap_rows(st: _MetaStorage) -> list[EpisodeRow]:
    from curation.cli.containers import mcap_episodes

    files, _dirs = st.top()
    numbering = mcap_episodes(files)
    return [EpisodeRow(i, 0, "", {}, {}, key=k, size=int(files[k]))
            for i, k in sorted(numbering.items())]


def _mcap_facts(st: _MetaStorage, row: EpisodeRow) -> tuple[float | None, str, bool]:
    """(length in seconds, metadata task text, has a task topic) of one mcap episode, from
    its summary section (a few ranged reads, never the messages)."""
    from curation.cli.containers import RangeFile, mcap_facts, read_summary

    summary = read_summary(RangeFile(lambda s, n: st.read_range(row.key, s, n), row.size),
                           row.key)
    if not summary.indexed:
        return None, "", False
    facts = mcap_facts(summary, None)
    length = None
    if summary.start_ns is not None and summary.end_ns is not None:
        length = round((summary.end_ns - summary.start_ns) / 1e9, 3)
    return length, facts.task, facts.has_task


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

    def _mcap_page(self, rows: list[EpisodeRow], source: str, uri: str, region: str | None,
                   cred_id: str | None, owner: str) -> dict[int, tuple]:
        """mcap (D44): length and metadata task of a page's episodes, from their summaries
        (read in parallel; a file that cannot be read keeps its row without them)."""
        todo = [r for r in rows if r.key]
        if not todo:
            return {}

        def one(st: _MetaStorage, r: EpisodeRow):
            try:
                return r.index, _mcap_facts(st, r)
            except Exception:  # noqa: BLE001 - one bad file does not spoil the page
                return r.index, (None, "", False)

        workers = min(8, len(todo))
        if source == "local":
            st = _MetaStorage(uri, root=self._local_root(uri))
            with concurrent.futures.ThreadPoolExecutor(workers) as ex:
                return dict(ex.map(lambda r: one(st, r), todo))
        key = self._key(source, cred_id, owner)
        try:
            with self.orch.svc.tos(key, region) as (client, _):
                st = _MetaStorage(uri, client=client)
                with concurrent.futures.ThreadPoolExecutor(workers) as ex:
                    return dict(ex.map(lambda r: one(st, r), todo))
        except Exception as err:  # noqa: BLE001 - SDK errors, network
            raise self._tos_error(err, uri) from None

    def episodes(self, *, source: str, uri: str, region: str | None, cred_id: str | None,
                 owner: str, cursor: str | None, limit: int, ttl_s: int = 1800) -> dict:
        rows, fps = self.episode_rows(source, uri, region, cred_id, owner)
        page = keyset_page(rows, key=lambda r: r.index, kind="episodes",
                           scope={"source": source, "uri": uri, "region": region},
                           cursor=cursor, limit=limit)
        key = self._key(source, cred_id, owner) if source == "tos" else None
        mcap = self._mcap_page(page.items, source, uri, region, cred_id, owner)
        items = []
        for r in page.items:
            if r.index in mcap:                          # an mcap episode (D44)
                length_s, task, has_task = mcap[r.index]
                item = {"index": r.index, "length_s": length_s, "task": task,
                        "task_source": "原始标注" if task else "无", "cameras": []}
                if has_task and not task:
                    item["task_unread"] = True           # in a /task topic, read by the checks
                items.append(item)
                continue
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
        def guess(st: _MetaStorage, out: dict) -> None:
            raw = st.read(_INFO)
            if raw is None:
                # D44: mcap episode files, lerobot-lance-convert's tables without meta/, .rrd
                from curation.cli.containers import mcap_episodes

                files, dirs = st.top()
                numbering = mcap_episodes(files)
                if numbering:
                    out["format_hint"], out["episodes"] = "mcap", len(numbering)
                elif {"frames.lance", "videos.lance"} <= set(dirs):
                    out["format_hint"] = "lance"
                elif any(name.endswith(".rrd") for name in files):
                    out["format_hint"] = "rrd"
                return
            info = json.loads(raw.decode("utf-8"))
            version = str(info.get("codebase_version") or "")
            out["format_hint"] = "lerobot_v3" if version.startswith("v3") else (
                "lerobot_v2" if version.startswith("v2") else "unknown")
            if str(info.get("storage_format") or "") == "lance":
                out["format_hint"] = "lance"
            te = info.get("total_episodes")
            out["episodes"] = int(te) if isinstance(te, (int, float)) else None

        def hint(entry: dict) -> dict:
            out = {"name": entry["name"], "uri": entry["uri"], "format_hint": "unknown",
                   "episodes": None}
            try:
                if source == "local":
                    guess(_MetaStorage(entry["uri"], root=pathlib.Path(entry["uri"])), out)
                else:
                    key = self._key(source, cred_id, owner)
                    with self.orch.svc.tos(key, region) as (client, _):
                        guess(_MetaStorage(entry["uri"], client=client), out)
            except Exception:  # noqa: BLE001 - one bad entry does not spoil the listing
                pass
            return out

        if not items:
            return []
        with concurrent.futures.ThreadPoolExecutor(min(8, len(items))) as ex:
            return list(ex.map(hint, items))
