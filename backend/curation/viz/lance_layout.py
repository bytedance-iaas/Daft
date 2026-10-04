"""Lance datasets for the visualizer (design doc 19 §4): the lerobot-lancedb layouts and their tables.

Three layouts, one per lerobot-lancedb generation (checked against the upstream sources, 2026-10-04):

* ``lance-0.3`` - ``lerobot-lance-convert`` 0.3.x: ``frames.lance`` (a row per frame in ``index`` order:
  row N is frame N), ``videos.lance`` (a row per source mp4: ``video_key`` / ``chunk_index`` /
  ``file_index``, byte-index columns, the bytes in the blob v2 column ``video_bytes``), ``meta.lance``
  (``path`` / ``data`` of every ``meta/`` file, for roots that hold only the tables) and ``meta/``;
* ``lance-0.2-video`` - ``lerobot-convert-to-lance-video`` 0.1-0.2: ``<name>.lance`` (the numeric
  features) and ``<name>_videos.lance`` (the same three keys, a blob v1 ``video_bytes``) next to ``meta/``;
* ``lance-0.2-frames`` - ``lerobot-convert-to-lance`` 0.1-0.2: ``<name>.lance`` with a JPEG per frame in
  a ``binary`` column per camera, next to ``meta/``.

``meta/`` is the source LeRobot v3.0 dataset's, so everything but an episode's frame columns and the
camera bytes is read as LeRobot. Column names are the feature keys with dots turned into underscores,
unless the frames table's schema metadata maps them (``source-column-name-map``, the check reader's
convention). The check reader (``ingest/lance_reader``, A-class) is not used here: it reads only 0.3 and
only local paths, and writes whole merged mp4s to a temporary directory.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from .series import column_values

LANCE_03 = "lance-0.3"
LANCE_02_VIDEO = "lance-0.2-video"
LANCE_02_FRAMES = "lance-0.2-frames"
BLOB_COLUMN = "video_bytes"
META_TABLE = "meta.lance"


@dataclass(frozen=True)
class Layout:
    name: str                      # lance-0.3 | lance-0.2-video | lance-0.2-frames
    frames: str                    # the frames table's directory (``frames.lance``, ``<name>.lance``)
    videos: str | None             # the videos table's, when the cameras are mp4 blobs
    meta: str | None               # ``meta.lance``, when there is one


def tables_of(keys: Iterable[str]) -> set[str]:
    """The Lance tables at a dataset's root, from the keys (or names) under it."""
    out = set()
    for k in keys:
        top = str(k).strip("/").split("/", 1)[0]
        if top.endswith(".lance"):
            out.add(top)
    return out


def detect(tables: Iterable[str]) -> Layout | None:
    """The layout of the root holding ``tables`` (``frames.lance`` ...), or None when it is none of the
    three. A lone ``<name>.lance`` is taken for the frames layout; the reader confirms it has pictures."""
    names = set(tables)
    meta = META_TABLE if META_TABLE in names else None
    if "frames.lance" in names and "videos.lance" in names:
        return Layout(LANCE_03, "frames.lance", "videos.lance", meta)
    for n in sorted(names):
        if n.endswith("_videos.lance"):
            frames = n[: -len("_videos.lance")] + ".lance"
            if frames in names:
                return Layout(LANCE_02_VIDEO, frames, n, meta)
    rest = sorted(n for n in names if n != META_TABLE)
    if len(rest) == 1:
        return Layout(LANCE_02_FRAMES, rest[0], None, meta)
    return None


def column_map(schema) -> dict[str, str]:
    """``{feature: column}`` from the frames table's ``source-column-name-map`` metadata (column ->
    feature, JSON); empty without one."""
    meta = schema.metadata or {}
    raw = meta.get(b"source-column-name-map") or meta.get("source-column-name-map")
    if not raw:
        return {}
    try:
        doc = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return {str(feature): str(col) for col, feature in doc.items()} if isinstance(doc, dict) else {}


def column_of(feature: str, names: set[str], mapped: dict[str, str]) -> str | None:
    """The frames table's column of a LeRobot feature key: the metadata's, the key with dots turned
    into underscores, the key itself; None when the table has none of them."""
    for cand in (mapped.get(feature), feature.replace(".", "_"), feature):
        if cand and cand in names:
            return cand
    return None


def episode_columns(ds, features: list[str], *, from_index: int | None = None, to_index: int | None = None,
                    episode_index: int | None = None) -> dict[str, Any]:
    """``{feature: values}`` of one episode's rows of a frames table (as
    :func:`curation.viz.series.read_episode_columns` gives them for parquet): the global frame window
    ``[from_index, to_index)`` on ``index`` when the episode table has one, else ``episode_index``.
    Features the table does not have are left out; rows come back in ``index`` order."""
    import pyarrow.compute as pc

    schema = ds.schema
    names = set(schema.names)
    mapped = column_map(schema)
    cols = {f: column_of(f, names, mapped) for f in dict.fromkeys(features)}
    cols = {f: c for f, c in cols.items() if c}
    if not cols:
        return {}
    if from_index is not None and to_index is not None and "index" in names:
        flt = f"index >= {int(from_index)} AND index < {int(to_index)}"
    elif episode_index is not None and "episode_index" in names:
        flt = f"episode_index = {int(episode_index)}"
    else:
        flt = None
    read = list(dict.fromkeys(list(cols.values()) + (["index"] if "index" in names else [])))
    table = ds.to_table(columns=read, filter=flt)
    if "index" in names and table.num_rows > 1:
        table = table.take(pc.sort_indices(table.column("index")))
    return {f: column_values(table.column(c)) for f, c in cols.items()}


def episode_rows(ds, *, from_index: int | None = None, to_index: int | None = None,
                 episode_index: int | None = None) -> list[int]:
    """The row offsets of one episode in a frames table, in ``index`` order (for blobs and pictures)."""
    names = set(ds.schema.names)
    if from_index is not None and to_index is not None and "index" in names:
        flt = f"index >= {int(from_index)} AND index < {int(to_index)}"
    elif episode_index is not None and "episode_index" in names:
        flt = f"episode_index = {int(episode_index)}"
    else:
        return []
    cols = ["index"] if "index" in names else ["episode_index"]
    table = ds.to_table(columns=cols, filter=flt, with_row_id=True)
    rows = list(zip(table.column(cols[0]).to_pylist(), table.column("_rowid").to_pylist()))
    rows.sort()
    return [int(r) for _, r in rows]


def picture_columns(ds, features: Iterable[str]) -> dict[str, str]:
    """``{feature: column}`` of the camera features a frames table keeps as binary pictures (0.1-0.2)."""
    import pyarrow as pa

    schema = ds.schema
    names = set(schema.names)
    mapped = column_map(schema)
    out = {}
    for f in features:
        c = column_of(f, names, mapped)
        if c and (pa.types.is_binary(schema.field(c).type) or pa.types.is_large_binary(schema.field(c).type)):
            out[f] = c
    return out


def meta_files(ds) -> dict[str, bytes]:
    """``{"meta/<path>": bytes}`` of a ``meta.lance`` table (paths that would leave ``meta/`` are dropped)."""
    out = {}
    table = ds.to_table(columns=["path", "data"])
    for path, data in zip(table.column("path").to_pylist(), table.column("data").to_pylist()):
        p = str(path or "").replace("\\", "/").strip("/")
        if not p or p.startswith("../") or "/../" in f"/{p}/" or data is None:
            continue
        out[f"meta/{p}"] = bytes(data)
    return out


def video_rows(ds) -> dict[tuple[str, int, int], int]:
    """``{(video_key, chunk_index, file_index): row id}`` of a videos table."""
    table = ds.to_table(columns=["video_key", "chunk_index", "file_index"], with_row_id=True)
    keys = table.column("video_key").to_pylist()
    chunks = table.column("chunk_index").to_pylist()
    files = table.column("file_index").to_pylist()
    ids = table.column("_rowid").to_pylist()
    return {(str(k), int(c), int(f)): int(r) for k, c, f, r in zip(keys, chunks, files, ids)}


# ---------------------------------------------------------------- where a table lives

def s3_options(endpoint: str, region: str, *, key_id: str | None = None, secret: str | None = None,
               token: str | None = None, virtual_hosted: bool = True) -> dict[str, str]:
    """Lance's object-store options for an S3-compatible endpoint (TOS's ``tos-s3-<region>``); no key
    is an anonymous (unsigned) read."""
    opts = {"aws_endpoint": endpoint.rstrip("/"), "aws_region": region,
            "aws_virtual_hosted_style_request": "true" if virtual_hosted else "false"}
    if endpoint.startswith("http://"):
        opts["aws_allow_http"] = "true"
    if key_id and secret:
        opts["aws_access_key_id"] = key_id
        opts["aws_secret_access_key"] = secret
        if token:
            opts["aws_session_token"] = token
    else:
        opts["aws_skip_signature"] = "true"
    return opts


def s3_endpoint_of(native: str) -> str:
    """TOS's S3-compatible endpoint for one of its native ones (``https://tos-cn-beijing.ivolces.com`` ->
    ``https://tos-s3-cn-beijing.ivolces.com``); anything else as it is."""
    scheme, _, rest = native.partition("://")
    host, slash, path = rest.partition("/")
    if host.startswith("tos-") and not host.startswith("tos-s3-") and host.endswith((".volces.com", ".ivolces.com")):
        host = "tos-s3-" + host[len("tos-"):]
    return f"{scheme}://{host}{slash}{path}".rstrip("/")
