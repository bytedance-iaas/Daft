"""LeRobot metadata without sample data: format sniffing, episode table, file keys.

Shared by ``preflight`` (seconds, metadata only - design doc 05, section 3) and
``snapshot`` (which files a task will read - doc 02, section 3.3). Everything
here works on a :class:`~curation.cli.storage.Storage` listing plus the small
files under ``meta/``; no data parquet or video is opened.

The per-episode task text follows v1 exactly (``ingest.lerobot_reader``): v2
takes the first entry of ``tasks`` in ``meta/episodes.jsonl``, v3 the first
entry of the ``tasks`` column of ``meta/episodes/*.parquet``. v1's other v3
fallback (``data.task_index`` -> ``meta/tasks.parquet``) reads data files, so
preflight does not take it and says so in a warning instead.
"""
from __future__ import annotations

import fnmatch
import io
import json
from dataclasses import dataclass, field
from typing import Any

from .storage import ObjectInfo, ObjectMissing, Storage

INFO_KEY = "meta/info.json"
V2_EPISODES_KEY = "meta/episodes.jsonl"
V3_EPISODES_GLOB = "meta/episodes/chunk-*/file-*.parquet"
#: v1 resolves a dataset's semantics from the data of its first this-many episodes,
#: whatever the selection (``ingest.lerobot_reader.SEMANTICS_VOTE_EPISODES``; a test
#: keeps the two equal, this module must not import the numeric reader)
SEMANTICS_SAMPLE = 100


def complete(ep: "Episode", listing) -> bool:
    """v1's ``_v2_missing`` in reverse: the data parquet and every camera's video are listed."""
    return all(k in listing for k in list(ep.data_keys) + list(ep.video_keys.values()))


def semantics_sample(meta: "DatasetMeta", max_episodes: int | None = None) -> list["Episode"]:
    """The episodes whose data v1 reads to resolve the dataset's semantics."""
    n = SEMANTICS_SAMPLE if not max_episodes else min(SEMANTICS_SAMPLE, int(max_episodes))
    return list(meta.episodes[:n])


class MetaError(Exception):
    """Metadata that cannot be used; the message goes to ``validation`` verbatim."""


@dataclass
class Format:
    kind: str                            # lerobot | mcap | lance | lancedb | rrd | unknown
    version: str | None = None           # v2 | v3 | None
    codebase_version: str | None = None
    note: str = ""                       # why an unsupported kind was chosen


@dataclass
class Episode:
    index: int
    length: int
    task: str                            # "" = no task text
    data_keys: tuple[str, ...] = ()
    video_keys: dict[str, str] = field(default_factory=dict)   # camera feature -> key


@dataclass
class DatasetMeta:
    info: dict
    fmt: Format
    cameras: list[str]                   # feature keys with dtype video, info.json order
    episodes: list[Episode]
    warnings: list[str] = field(default_factory=list)


def _count_suffix(keys, suffix: str) -> int:
    return sum(1 for k in keys if k.lower().endswith(suffix))


def detect_format(listing: dict[str, ObjectInfo]) -> Format:
    """Sniff the input layout from the listing alone, in v1's order (``run.py``, D44):

    * lerobot-lance-convert's tables (``frames.lance`` + ``videos.lance``) -> ``lance``,
      with or without ``meta/`` (then ``meta.lance`` holds it);
    * ``meta/info.json`` at the root -> LeRobot (version decided from info.json);
    * ``*.mcap`` files at the root -> ``mcap`` (v1 reads ``<dir>/*.mcap``);
    * ``.rrd`` files -> ``rrd``, other Lance tables -> ``lancedb`` (both unsupported:
      ``.rrd`` stays as in v1, D6 / D34; single-table Lance layouts are what
      lerobot-lance-convert 0.3.0 retired); anything else is unknown.
    """
    from . import containers

    keys = list(listing)
    if containers.is_lance_layout(listing):
        return Format("lance", note="lerobot-lance-convert tables (frames.lance, videos.lance)")
    if INFO_KEY in listing:
        return Format("lerobot")
    top = containers.mcap_keys(listing)
    if top:
        return Format("mcap", note=f"{len(top)} .mcap files")
    if _count_suffix(keys, ".rrd"):
        return Format("rrd", note=f"{_count_suffix(keys, '.rrd')} .rrd files")
    if any(seg.endswith(".lance") for k in keys for seg in k.split("/")[:-1]) \
            or any(k.endswith("_latest.manifest") or "/_versions/" in f"/{k}" for k in keys):
        return Format("lancedb", note=("Lance tables that are not lerobot-lance-convert's "
                                       "layout (frames.lance + videos.lance + meta)"))
    if _count_suffix(keys, ".mcap"):
        return Format("unknown", note=(f"{_count_suffix(keys, '.mcap')} .mcap files, all in "
                                       f"sub-directories; point --input at the directory that "
                                       f"holds them"))
    nested = sorted({k[:-len("/" + INFO_KEY)] for k in keys if k.endswith("/" + INFO_KEY)})
    if nested:
        shown = ", ".join(nested[:5]) + (", ..." if len(nested) > 5 else "")
        return Format("unknown", note=(f"a directory of {len(nested)} LeRobot datasets "
                                       f"({shown}); point --input at one of them"))
    return Format("unknown", note="no meta/info.json and no known data files")


def version_of(codebase_version: str) -> str | None:
    if codebase_version.startswith("v2"):
        return "v2"
    if codebase_version.startswith("v3"):
        return "v3"
    return None


def load_info(storage: Storage) -> dict:
    try:
        raw = storage.read_bytes(INFO_KEY)
    except ObjectMissing:
        raise MetaError("meta/info.json disappeared while reading the dataset") from None
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise MetaError(f"meta/info.json is not valid JSON: {e}") from None
    if not isinstance(info, dict):
        raise MetaError("meta/info.json must hold a JSON object")
    return info


def cameras_of(info: dict) -> list[str]:
    feats = info.get("features") or {}
    if not isinstance(feats, dict):
        return []
    return [k for k, v in feats.items() if isinstance(v, dict) and v.get("dtype") == "video"]


def short_camera(feature_key: str) -> str:
    """``observation.images.wrist`` -> ``wrist`` (the name people see)."""
    for prefix in ("observation.images.", "observation.image."):
        if feature_key.startswith(prefix):
            return feature_key[len(prefix):]
    return feature_key


def check_layout(listing: dict[str, ObjectInfo], codebase_version: str) -> list[str]:
    """v1's ``_verify_layout`` rule, plus: the episode table must exist at all."""
    has_v3 = any(fnmatch.fnmatchcase(k, V3_EPISODES_GLOB) for k in listing)
    has_v2 = V2_EPISODES_KEY in listing
    version = version_of(codebase_version)
    if version == "v3" and not has_v3:
        if has_v2:
            return [f"info.json declares codebase_version={codebase_version} but meta/ has the "
                    f"v2.x layout (episodes.jsonl, no episodes/ directory); the declared "
                    f"version is probably wrong"]
        return ["meta/episodes/chunk-*/file-*.parquet is missing (LeRobot v3 episode table)"]
    if version == "v2" and not has_v2:
        if has_v3:
            return [f"info.json declares codebase_version={codebase_version} but meta/ has the "
                    f"v3.x layout (episodes/ directory, no episodes.jsonl); the declared "
                    f"version is probably wrong"]
        return ["meta/episodes.jsonl is missing (LeRobot v2 episode table)"]
    return []


def _template(info: dict, name: str, **kw: Any) -> str:
    tpl = info.get(name)
    if not isinstance(tpl, str) or not tpl:
        raise MetaError(f"info.json has no {name}")
    try:
        return tpl.format(**kw)
    except (KeyError, IndexError, ValueError) as e:
        raise MetaError(f"info.json {name} {tpl!r} does not fit codebase_version "
                        f"{info.get('codebase_version')!r}: {e!r}") from None


def _v2_task(ep: dict) -> str:
    """v1's ``read_lerobot_meta`` rule, verbatim: the first entry of ``tasks``."""
    try:
        tasks = ep.get("tasks") or []
        return str(tasks[0]) if tasks else ""
    except (KeyError, IndexError, TypeError):
        return ""


def _v2_episodes(storage: Storage, info: dict, cameras: list[str]) -> list[Episode]:
    try:
        text = storage.read_bytes(V2_EPISODES_KEY).decode("utf-8")
    except ObjectMissing:
        raise MetaError("meta/episodes.jsonl disappeared while reading the dataset") from None
    except UnicodeDecodeError as e:
        raise MetaError(f"meta/episodes.jsonl is not UTF-8: {e}") from None
    try:
        chunks_size = int(info["chunks_size"])
    except (KeyError, TypeError, ValueError):
        raise MetaError("info.json chunks_size is missing or not an integer") from None
    if chunks_size <= 0:
        raise MetaError(f"info.json chunks_size must be positive, got {chunks_size}")
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            ep = json.loads(line)
            idx = int(ep["episode_index"])
        except (ValueError, KeyError, TypeError) as e:
            raise MetaError(f"meta/episodes.jsonl line {n} is not a valid episode "
                            f"record: {e!r}") from None
        try:
            length = int(ep["length"])                      # what v1 reads
        except (KeyError, TypeError, ValueError):
            raise MetaError(f"meta/episodes.jsonl line {n} (episode {idx}) has no valid "
                            f"length") from None
        chunk = idx // chunks_size
        out.append(Episode(
            index=idx,
            length=length,
            task=_v2_task(ep),
            data_keys=(_template(info, "data_path", episode_chunk=chunk, episode_index=idx),),
            video_keys={vk: _template(info, "video_path", episode_chunk=chunk, video_key=vk,
                                      episode_index=idx) for vk in cameras}))
    return out


def _v3_table(storage: Storage, listing: dict[str, ObjectInfo]):
    import pandas as pd

    keys = sorted(k for k in listing if fnmatch.fnmatchcase(k, V3_EPISODES_GLOB))
    frames = []
    for key in keys:
        try:
            frames.append(pd.read_parquet(io.BytesIO(storage.read_bytes(key))))
        except ObjectMissing:
            raise MetaError(f"{key} disappeared while reading the dataset") from None
        except Exception as e:  # noqa: BLE001 - pyarrow raises several kinds
            raise MetaError(f"{key} is not a readable parquet file: {e}") from None
    if not frames:
        raise MetaError("meta/episodes/chunk-*/file-*.parquet is missing")
    return pd.concat(frames, ignore_index=True)


def _v3_episodes(storage: Storage, listing: dict[str, ObjectInfo], info: dict,
                 cameras: list[str], warnings: list[str]) -> list[Episode]:
    from ..ingest.lerobot_reader import _v3_instruction

    table = _v3_table(storage, listing)
    needed = ["episode_index", "length", "data/chunk_index", "data/file_index"]
    for vk in cameras:
        needed += [f"videos/{vk}/chunk_index", f"videos/{vk}/file_index"]
    missing = [c for c in needed if c not in table.columns]
    if missing:
        raise MetaError(f"meta/episodes parquet lacks columns {missing}")
    if "tasks" not in table.columns:
        warnings.append("meta/episodes has no tasks column; task texts are read from the data "
                        "files at run time, so the labelled count here is a lower bound")
    out = []
    for _, ep in table.iterrows():
        try:
            idx = int(ep["episode_index"])
            data_key = _template(info, "data_path", chunk_index=int(ep["data/chunk_index"]),
                                 file_index=int(ep["data/file_index"]))
            videos = {vk: _template(info, "video_path", video_key=vk,
                                    chunk_index=int(ep[f"videos/{vk}/chunk_index"]),
                                    file_index=int(ep[f"videos/{vk}/file_index"]))
                      for vk in cameras}
        except (TypeError, ValueError) as e:
            raise MetaError(f"meta/episodes parquet has a malformed row: {e!r}") from None
        try:
            length = int(ep["length"])
        except (TypeError, ValueError):
            raise MetaError(f"meta/episodes parquet: episode {idx} has no valid length") from None
        out.append(Episode(index=idx, length=length, task=_v3_instruction(ep),
                           data_keys=(data_key,), video_keys=videos))
    return out


def read_dataset(storage: Storage, listing: dict[str, ObjectInfo], info: dict,
                 fmt: Format) -> DatasetMeta:
    """Episode table and file keys of a LeRobot v2 / v3 dataset (``fmt.version`` set)."""
    cameras = cameras_of(info)
    warnings: list[str] = []
    if fmt.version == "v2":
        episodes = _v2_episodes(storage, info, cameras)
    elif fmt.version == "v3":
        episodes = _v3_episodes(storage, listing, info, cameras, warnings)
    else:
        raise MetaError(f"unsupported LeRobot version {fmt.codebase_version!r}")
    if not episodes:
        raise MetaError("the episode table lists no episode")
    episodes.sort(key=lambda e: e.index)
    neighbours = zip(episodes, episodes[1:], strict=False)          # sorted: dups are adjacent
    dup = sorted({a.index for a, b in neighbours if a.index == b.index})
    if dup:
        raise MetaError(f"episode_index repeats in the episode table: {dup[:8]}")
    return DatasetMeta(info=info, fmt=fmt, cameras=cameras, episodes=episodes,
                       warnings=warnings)


def meta_keys(listing: dict[str, ObjectInfo]) -> list[str]:
    return sorted(k for k in listing if k.startswith("meta/"))


def fingerprint_keys(listing: dict[str, ObjectInfo], kind: str) -> list[str]:
    """The objects a dataset's ``meta_fingerprint`` covers - the same rule as the Daemon's
    ``rules.meta_fingerprint`` over a snapshot: ``meta/``; a lance root without it, its
    ``meta.lance`` mirror; an mcap dataset, its episode files (it has no other metadata)."""
    from . import containers

    if kind == "mcap":
        return containers.mcap_keys(listing)
    keys = meta_keys(listing)
    if not keys and kind == "lance":
        keys = sorted(k for k in listing if k.startswith(containers.LANCE_META_TABLE + "/"))
    return keys


def fingerprint(objects: list[ObjectInfo]) -> str:
    """``sha256:`` over one ``key<TAB>size<TAB>identity`` line per object, sorted by key."""
    import hashlib

    h = hashlib.sha256()
    for obj in sorted(objects, key=lambda o: o.key):
        h.update(f"{obj.key}\t{obj.size}\t{obj.identity()}\n".encode())
    return "sha256:" + h.hexdigest()
