"""mcap and lance datasets in the v2 commands (D44; v1's PR #155).

v1 reads both formats with two A-class readers, which take local directories only:
``ingest/mcap_reader.py`` (one ``episode_<N>.mcap`` per episode, ROS2 cdr / protobuf /
json messages) and ``ingest/lance_reader.py`` (lerobot-lance-convert >= 0.3.0: ``meta/``
- LeRobot v3.0 with ``storage_format: "lance"`` - plus ``frames.lance``, ``videos.lance``
and ``meta.lance``). What the v2 commands add around them lives here:

* **recognising** them from a listing (:func:`mcap_keys`, :func:`lance_tables`);
* **their episodes** and the objects a command reads (:func:`mcap_episodes` numbers the
  files by v1's own rule, ``mcap_reader._episode_files``; a lance read needs the tables
  whole, :func:`lance_keys`);
* **metadata without samples** for preflight and the Daemon's episode grid: an mcap
  file's summary section - channels, message counts, metadata records - through a few
  ranged reads (:func:`mcap_summary`); a lance dataset's ``meta/`` (:func:`lance_meta`);
* **the source cache** (:class:`SourceCache`): a ``tos://`` dataset is read from a local
  copy of the objects the command needs - mcap one file per episode as the episodes are
  read, lance the tables whole - kept under ``$CURATION_SOURCE_CACHE`` (the Daemon points
  it at the task's cache on the data volume and removes it when the run ends) or, without
  it, in a temporary directory removed when the command ends. Objects are only read from
  the source, never written back, and every copy is checked against the size and ETag the
  listing gave.

Nothing here decides a verdict: the rows come from v1's readers unchanged
(``pipeline.rows.ContainerRowSource``).
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import io
import json
import os
import shutil
import struct
import tempfile
import threading
import zlib
from dataclasses import dataclass, field

from .errors import SourceChanged
from .storage import ObjectInfo, Storage, normalize_etag

MCAP = "mcap"
LANCE = "lance"
FORMATS = (MCAP, LANCE)
#: lerobot-lance-convert's tables; frames + videos are the layout's identity (v1's has_lance_files)
LANCE_TABLES = ("frames.lance", "videos.lance")
LANCE_META_TABLE = "meta.lance"
LANCE_PREFIXES = ("meta/", "frames.lance/", "videos.lance/", "meta.lance/")
#: where the Daemon keeps a task's source cache (one sub-directory per dataset)
CACHE_ENV = "CURATION_SOURCE_CACHE"


# ---------------------------------------------------------------- recognising


def mcap_keys(listing) -> list[str]:
    """The top-level ``*.mcap`` objects - what v1's ``glob(<dir>/*.mcap)`` sees."""
    return sorted(k for k in listing
                  if "/" not in k and k.endswith(".mcap") and not k.startswith("."))


def lance_tables(listing) -> set[str]:
    """Top-level Lance tables (``<name>.lance/_versions/...`` present, v1's ``_is_table_dir``)."""
    out = set()
    for k in listing:
        parts = k.split("/")
        if len(parts) >= 3 and parts[0].endswith(".lance") and parts[1] == "_versions":
            out.add(parts[0])
    return out


def is_lance_layout(listing) -> bool:
    """lerobot-lance-convert's layout: frames.lance and videos.lance (v1's has_lance_files)."""
    return set(LANCE_TABLES) <= lance_tables(listing)


def lance_keys(listing) -> list[str]:
    """Every object a lance read touches: ``meta/`` and the three tables."""
    return sorted(k for k in listing if k.startswith(LANCE_PREFIXES))


def mcap_episodes(listing) -> dict[int, str]:
    """``{episode: key}`` by v1's rule: ``episode_<N>.mcap`` numbered by name (other files
    ignored); without any such name, the sorted files numbered 0, 1, ... by position.

    The rule is v1's own function (``mcap_reader._episode_files``), run over empty stand-ins
    named like the objects, so a remote dataset is numbered exactly as the local copy the
    readers see."""
    from ..ingest import mcap_reader

    keys = mcap_keys(listing)
    if not keys:
        return {}
    with tempfile.TemporaryDirectory(prefix="curation-mcap-names-") as tmp:
        for k in keys:
            open(os.path.join(tmp, k), "wb").close()
        pairs = mcap_reader._episode_files(tmp)
    return {int(i): os.path.basename(p) for i, p in pairs}


def enabled(fmt: str, cfg: dict | None) -> tuple[bool, str]:
    """(is the format switched on, v1's message when it is not): ``ingest.mcap_enabled`` /
    ``ingest.lance_enabled`` (default on) and ``CURATION_*_ENABLED``, as v1 reads them."""
    if fmt == MCAP:
        from ..ingest import mcap_reader as mod

        mod.apply_config(cfg)
        return mod.mcap_enabled(), mod.MCAP_DISABLED_MSG
    from ..ingest import lance_reader as mod

    mod.apply_config(cfg)
    return mod.lance_enabled(), mod.LANCE_DISABLED_MSG


def mapping_of(cfg: dict | None) -> dict | None:
    """``ingest.mcap_mapping``: the site's topic mapping for mcap (None = v1's defaults)."""
    return ((cfg or {}).get("ingest") or {}).get("mcap_mapping") or None


# ---------------------------------------------------------------- mcap summaries


class RangeFile:
    """A read-only, seekable file over ``read_range(start, length)``: the mcap reader reads
    the magic at the start, then seeks to the footer and the summary section at the end,
    so a summary costs two or three small ranged GETs instead of the whole file. Reads
    are cached as segments; a read near the end fetches the whole tail (``tail`` bytes)
    at once, where the summary section lives.

    Deliberately not an ``io.RawIOBase``: the mcap reader wraps those in a
    ``BufferedReader`` per record stream, whose garbage collection closes the raw file
    under the next one."""

    def __init__(self, read_range, size: int, *, readahead: int = 1 << 14,
                 tail: int = 1 << 16):
        self._read_range, self._size = read_range, int(size)
        self._readahead, self._tail = int(readahead), int(tail)
        self._pos = 0
        self._segments: list[tuple[int, bytes]] = []

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + int(offset))
        return self._pos

    def _segment(self, pos: int) -> tuple[int, bytes] | None:
        for start, data in self._segments:
            if start <= pos < start + len(data):
                return start, data
        return None

    def _fetch(self, pos: int, n: int) -> tuple[int, bytes]:
        if pos >= self._size - self._tail:            # the tail, whole (a small file: all of it)
            start = max(0, self._size - self._tail)
            length = self._size - start
        else:
            start = pos
            length = min(self._size - start, max(n, self._readahead))
        data = self._read_range(start, length)
        self._segments.append((start, data))
        return start, data

    def read(self, size: int = -1) -> bytes:
        want = max(0, self._size - self._pos)
        if size is not None and size >= 0:
            want = min(want, size)
        out = bytearray()
        while len(out) < want:
            seg = self._segment(self._pos) or self._fetch(self._pos, want - len(out))
            start, data = seg
            chunk = data[self._pos - start:self._pos - start + (want - len(out))]
            if not chunk:
                break
            out += chunk
            self._pos += len(chunk)
        return bytes(out)

    def readinto(self, buf) -> int:
        data = self.read(len(buf))
        buf[:len(data)] = data
        return len(data)


@dataclass
class McapSummary:
    """What an mcap file's summary section says (no message is read)."""

    key: str
    indexed: bool                                      # False: the file has no summary
    topics: dict[str, int | None] = field(default_factory=dict)   # topic -> messages
    properties: dict[str, str] = field(default_factory=dict)      # "record:key" -> value
    start_ns: int | None = None
    end_ns: int | None = None
    error: str = ""
    #: D52: the file does not end with the mcap magic - a recording that was cut off
    truncated: bool = False
    #: D52: the footer's ``summary_crc`` against the bytes it covers; None when not written
    summary_crc_ok: bool | None = None


MCAP_MAGIC = b"\x89MCAP0\r\n"
#: the footer record: opcode, record length, summary_start, summary_offset_start, summary_crc
_FOOTER_LEN = 1 + 8 + 8 + 8 + 4
_FOOTER_OPCODE = 0x02


@dataclass
class McapFooter:
    """The end of an mcap file, read without the mcap library (it does not check the CRC)."""

    size: int
    ends_with_magic: bool
    summary_start: int | None = None                   # None: no footer record
    summary_crc: int = 0                               # 0: not written
    summary_crc_ok: bool | None = None


def read_footer(stream) -> McapFooter:
    """The footer of a seekable mcap stream and whether its summary CRC holds (D52).

    The CRC covers the summary section and the footer record up to its
    ``summary_offset_start`` field, i.e. ``[summary_start or footer start, size - 12)``;
    a writer that skips CRCs writes 0. Only bytes a summary read fetches anyway are read."""
    size = stream.seek(0, io.SEEK_END)
    footer_at = size - len(MCAP_MAGIC) - _FOOTER_LEN
    if size < len(MCAP_MAGIC) * 2 + _FOOTER_LEN:
        return McapFooter(size, False)
    stream.seek(size - len(MCAP_MAGIC))
    if stream.read(len(MCAP_MAGIC)) != MCAP_MAGIC:
        return McapFooter(size, False)
    stream.seek(footer_at)
    rec = stream.read(_FOOTER_LEN)
    if len(rec) != _FOOTER_LEN or rec[0] != _FOOTER_OPCODE:
        return McapFooter(size, True)
    summary_start, _offsets = struct.unpack_from("<QQ", rec, 9)
    crc = struct.unpack_from("<I", rec, 25)[0]
    out = McapFooter(size, True, summary_start, crc)
    start = summary_start or footer_at
    if crc and len(MCAP_MAGIC) <= start <= footer_at:
        stream.seek(start)
        out.summary_crc_ok = zlib.crc32(stream.read(size - len(MCAP_MAGIC) - 4 - start)) == crc
    return out


def scan_summary(stream, key: str) -> McapSummary:
    """What a summary would say, by reading the whole file once (a file without a summary
    section, e.g. a recording that was cut off). Preflight does this for one file at most."""
    from mcap.records import Channel, Message, Metadata
    from mcap.stream_reader import StreamReader

    topic_of: dict[int, str] = {}
    counts: dict[str, int] = {}
    props: dict[str, str] = {}
    start = end = None
    try:
        stream.seek(0)
        for rec in StreamReader(stream).records:
            if isinstance(rec, Channel):
                topic_of[rec.id] = rec.topic
                counts.setdefault(rec.topic, 0)
            elif isinstance(rec, Message):
                t = topic_of.get(rec.channel_id)
                if t is not None:
                    counts[t] = counts.get(t, 0) + 1
                start = rec.log_time if start is None else min(start, rec.log_time)
                end = rec.log_time if end is None else max(end, rec.log_time)
            elif isinstance(rec, Metadata):
                for k, v in (rec.metadata or {}).items():
                    props[f"{rec.name}:{k}"] = str(v)
    except Exception as e:  # noqa: BLE001 - truncated: what was read so far counts
        if not topic_of:
            return McapSummary(key, False, error=f"{type(e).__name__}: {e}"[:300])
    return McapSummary(key, True, dict(counts), props, start, end)


def read_summary(stream, key: str, *, scan: bool = False) -> McapSummary:
    """The summary of one mcap file (``stream`` seekable). Never reads the messages:
    without a summary section the file is reported as not indexed - unless ``scan``,
    which reads it through once instead (:func:`scan_summary`)."""
    from mcap.reader import make_reader

    try:
        footer = read_footer(stream)
    except Exception:  # noqa: BLE001 - a stream that cannot seek to its end: the reader says why
        footer = McapFooter(0, True)
    marks = {"truncated": not footer.ends_with_magic, "summary_crc_ok": footer.summary_crc_ok}
    try:
        stream.seek(0)
        reader = make_reader(stream)
        summary = reader.get_summary()
    except Exception as e:  # noqa: BLE001 - a truncated or foreign file
        if scan:
            return _marked(scan_summary(stream, key), marks)
        return McapSummary(key, False, error=f"{type(e).__name__}: {e}"[:300], **marks)
    if summary is None:
        if scan:
            return _marked(scan_summary(stream, key), marks)
        return McapSummary(key, False, error="the file has no summary section", **marks)
    st = summary.statistics
    counts = dict(st.channel_message_counts or {}) if st else None
    topics: dict[str, int | None] = {}
    for cid, ch in (summary.channels or {}).items():
        if counts is None:                     # no statistics record: counts unknown
            topics[ch.topic] = None
        else:
            topics[ch.topic] = int(topics.get(ch.topic) or 0) + int(counts.get(cid, 0))
    props: dict[str, str] = {}
    try:
        for rec in reader.iter_metadata():      # through the summary's metadata index
            for k, v in (rec.metadata or {}).items():
                props[f"{rec.name}:{k}"] = str(v)
    except Exception:  # noqa: BLE001 - metadata records are optional, as in v1
        pass
    return McapSummary(key, True, topics, props,
                       int(st.message_start_time) if st else None,
                       int(st.message_end_time) if st else None, **marks)


def _marked(summary: McapSummary, marks: dict) -> McapSummary:
    summary.truncated, summary.summary_crc_ok = marks["truncated"], marks["summary_crc_ok"]
    return summary


def mcap_summary(storage: Storage, key: str, size: int, *, scan: bool = False) -> McapSummary:
    if not storage.remote:
        with open(os.path.join(storage.root, key), "rb") as fh:
            return read_summary(fh, key, scan=scan)
    return read_summary(RangeFile(lambda s, n: storage.read_range(key, s, n), size), key,
                        scan=scan)


def mcap_summaries(storage: Storage, listing, keys, *, workers: int = 16) -> dict[str, McapSummary]:
    """Summaries of many files, read in parallel (preflight reads every episode's)."""
    keys = list(keys)
    if not keys:
        return {}
    with cf.ThreadPoolExecutor(min(workers, len(keys))) as ex:
        got = list(ex.map(lambda k: mcap_summary(storage, k, int(listing[k].size)), keys))
    return {s.key: s for s in got}


@dataclass
class McapEpisodeFacts:
    """One episode as v1's reader will see it, from the summary alone."""

    mapping: dict
    action_topics: list[str]
    missing_action: list[str]                          # sources without a topic / messages
    state_topics: list[str]
    state_present: bool
    cameras: list[str]                                 # v1's camera keys
    task: str                                          # metadata-record task text
    has_task: bool                                     # a task topic with messages, or task text
    robot_type: str                                    # metadata-record robot type
    frames: int | None                                 # messages of the first action source
    profile: str                                       # "umi_das" when v1 recognises UMI


def mcap_facts(summary: McapSummary, mapping: dict | None) -> McapEpisodeFacts:
    """Apply v1's topic rules (``mcap_reader._effective_mapping``, ``_norm_sources``,
    ``_umi_mapping``, ``_prop_leaf``) to a summary: an explicit mapping wins, then the
    default topics, then the built-in UMI recognition."""
    from ..ingest import mcap_reader as M

    topics = sorted(summary.topics)
    if mapping:
        mp = dict(M.DEFAULT_MAPPING, **mapping)
    else:
        mp = dict(M.DEFAULT_MAPPING)
        if mp["action"] not in topics:
            mp = M._umi_mapping(topics) or mp
    def has(t):
        return t in summary.topics and summary.topics[t] != 0

    act = [s["topic"] for s in (M._norm_sources(mp["action"]) or [])]
    st = [s["topic"] for s in (M._norm_sources(mp.get("state")) or [])]
    video = set(mp.get("video_topics") or [])
    cams = sorted(t.lstrip("/").replace("/", "_") for t in topics
                  if (t in video or t.startswith(mp["video_prefix"])) and has(t))
    task = M._prop_leaf(summary.properties, M._TASK_KEYS)
    return McapEpisodeFacts(
        mapping=mp, action_topics=act, missing_action=[t for t in act if not has(t)],
        state_topics=st, state_present=bool(st) and all(has(t) for t in st), cameras=cams,
        task=task, has_task=bool(task) or has(mp.get("task") or ""),
        robot_type=M._prop_leaf(summary.properties, M._ROBOT_KEYS),
        frames=summary.topics.get(act[0]) if act else None,
        profile=str(mp.get("profile") or ""))


# ---------------------------------------------------------------- lance metadata


@dataclass
class LanceMeta:
    info: dict
    episodes: list[dict]                               # rows of meta/episodes/*.parquet
    source: str                                        # "meta/" or "meta.lance"


def _episodes_from_parquet(blobs: list[bytes]) -> list[dict]:
    import pandas as pd

    if not blobs:
        return []
    table = pd.concat([pd.read_parquet(io.BytesIO(b)) for b in blobs], ignore_index=True)
    return [row for _, row in table.iterrows()]


def lance_meta(storage: Storage, listing) -> LanceMeta:
    """``meta/info.json`` and the episode table of a lance dataset, without its tables.

    ``meta/`` is read directly; a root that only has the three tables gets it from the
    ``meta.lance`` mirror (v1's ``lance_reader._meta_root``: a small table, copied to a
    temporary directory when remote). Raises ``ValueError`` / ``NotADatasetError``."""
    import fnmatch

    from .lerobot_meta import INFO_KEY, V3_EPISODES_GLOB

    if INFO_KEY in listing:
        info = json.loads(storage.read_bytes(INFO_KEY).decode("utf-8"))
        eps = _episodes_from_parquet([storage.read_bytes(k) for k in sorted(listing)
                                      if fnmatch.fnmatchcase(k, V3_EPISODES_GLOB)])
        return LanceMeta(info, eps, "meta/")
    from ..ingest import lance_reader

    with tempfile.TemporaryDirectory(prefix="curation-lance-meta-") as tmp:
        if storage.remote:
            for k in listing:
                if k.startswith(LANCE_META_TABLE + "/"):
                    dst = os.path.join(tmp, *k.split("/"))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    storage.download(k, dst)
            root_dir = tmp
        else:
            root_dir = storage.root
        try:
            meta_root = lance_reader._meta_root(root_dir)
            with open(os.path.join(meta_root, "meta", "info.json"), encoding="utf-8") as fh:
                info = json.load(fh)
            found = []
            for dirpath, _dirs, names in os.walk(os.path.join(meta_root, "meta", "episodes")):
                for n in sorted(names):
                    if n.endswith(".parquet"):
                        with open(os.path.join(dirpath, n), "rb") as fh:
                            found.append(fh.read())
            return LanceMeta(info, _episodes_from_parquet(found), LANCE_META_TABLE)
        finally:
            lance_reader.cleanup_video_cache(root_dir)


# ---------------------------------------------------------------- the report's facts

SOURCE_INFO = "source_info.json"
#: the info.json-shaped facts v1's report reads about a container (export/report.py)
_INFO_KEYS = ("codebase_version", "storage_format", "fps", "robot_type", "robot_type_source",
              "robot_type_file", "time_source", "has_task_text", "task_source")


def write_source_info(run_dir: str, src, embodiment_id: str | None) -> None:
    """``<run-dir>/source_info.json``, once per run directory: what v1's run knows about an
    mcap / lance dataset when it writes its report - the dataset info of its reader
    (``mcap_dataset_info``: the first episode's robot type and where it came from, the
    time source, a task text or not; ``lance_dataset_info``: meta/info.json) and the robot
    it used (``run.py``'s identity line). ``report`` turns it into v1's container findings
    (``export/report.container_findings``)."""
    path = os.path.join(run_dir, SOURCE_INFO)
    if not getattr(src, "container", False) or os.path.isfile(path):
        return
    from ..pipeline.records import write_json_atomic
    from ..pipeline.rows import _INGEST
    from ..registry.registry import EmbodimentRegistry

    if src.kind == MCAP:
        from ..ingest.mcap_reader import mcap_dataset_info

        num = src.numbering()
        src.fetch([min(num)])
        info = mcap_dataset_info(src.input_dir, mapping=_INGEST["mcap_mapping"],
                                 embodiment_id=embodiment_id)
    else:
        from ..ingest.lance_reader import lance_dataset_info

        src.fetch([])
        info = lance_dataset_info(src.input_dir)
    rt = str(info.get("robot_type") or "unknown")
    emb = embodiment_id or rt
    try:
        prof = EmbodimentRegistry().get(emb)
        robot = {"robot_type": rt, "embodiment_id": emb,
                 "registry_profile": prof.embodiment_id, "quality": prof.quality}
    except Exception:  # noqa: BLE001 - not in the registry: v1's "(未注册)"
        robot = {"robot_type": rt, "embodiment_id": emb, "registry_profile": "(未注册)",
                 "quality": None}
    write_json_atomic(path, {"format": src.kind,
                             "info": {k: info[k] for k in _INFO_KEYS if k in info},
                             "robot": robot})


# ---------------------------------------------------------------- the source cache


class SourceCache:
    """A local copy of the parts of a remote mcap / lance dataset that commands read.

    ``data`` is the directory v1's readers are given. An mcap cache holds a zero-length
    stand-in for every episode file of the listing (v1 numbers the files by what the
    directory holds; a stand-in is never read) and the real file once an episode is
    fetched; a lance cache holds ``meta/`` and the tables once fetched. ``index.json``
    records the size and ETag of every copy: a later command of the same task reuses a
    copy whose object is unchanged and fetches it again otherwise.
    """

    def __init__(self, storage: Storage, listing: dict[str, ObjectInfo], fmt: str, *,
                 root: str | None = None, log=None):
        base = root if root is not None else (os.environ.get(CACHE_ENV) or "").strip()
        self.storage, self.listing, self.fmt = storage, listing, fmt
        self.owned = not base
        if base:
            tag = hashlib.sha256(storage.uri.encode("utf-8")).hexdigest()[:16]
            self.dir = os.path.join(os.path.abspath(base), f"{fmt}-{tag}")
            os.makedirs(self.dir, exist_ok=True)
        else:
            self.dir = tempfile.mkdtemp(prefix=f"curation-source-{fmt}-")
        # named like the source: v1 resolves a dataset's semantics profile by its directory
        # name too (lance_reader: resolve_dataset_semantics(info, rows, basename(dir)))
        name = storage.uri.rstrip("/").rsplit("/", 1)[-1]
        if not name or name.startswith(".") or name.startswith("tos:") or "\\" in name:
            name = "dataset"
        self.data = os.path.join(self.dir, name)
        os.makedirs(self.data, exist_ok=True)
        self._index_path = os.path.join(self.dir, "index.json")
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._log = log or (lambda level, msg: None)
        self.fetched = 0
        self.fetched_bytes = 0
        try:
            with open(self._index_path, encoding="utf-8") as fh:
                self._index: dict[str, list] = json.load(fh).get("objects") or {}
        except (OSError, ValueError):
            self._index = {}
        if fmt == MCAP:
            self._stand_ins()
        else:
            self._prune()

    # -- bookkeeping ----------------------------------------------------------------
    def _path(self, key: str) -> str:
        return os.path.join(self.data, *key.split("/"))

    def _identity(self, key: str) -> list:
        obj = self.listing[key]
        return [int(obj.size), obj.identity()]

    def _save_index(self) -> None:
        tmp = f"{self._index_path}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"uri": self.storage.uri, "objects": self._index}, fh)
        os.replace(tmp, self._index_path)

    def _stand_ins(self) -> None:
        """Every episode file present by name; the ones not fetched are empty."""
        wanted = set(mcap_keys(self.listing))
        for name in os.listdir(self.data):
            if name.endswith(".mcap") and name not in wanted:
                os.unlink(os.path.join(self.data, name))     # gone from the source
                self._index.pop(name, None)
        for key in wanted:
            path = self._path(key)
            if not os.path.exists(path):
                open(path, "wb").close()

    def _prune(self) -> None:
        """Local files whose object is gone from the source leave the copy (a table
        version must not survive its deletion in the source)."""
        wanted = set(lance_keys(self.listing))
        for dirpath, _dirs, names in os.walk(self.data):
            for name in names:
                full = os.path.join(dirpath, name)
                key = os.path.relpath(full, self.data).replace(os.sep, "/")
                if key not in wanted:
                    os.unlink(full)
                    self._index.pop(key, None)

    def fresh(self, key: str) -> bool:
        return self._index.get(key) == self._identity(key) and os.path.isfile(self._path(key))

    # -- fetching -------------------------------------------------------------------
    def keys_for(self, episodes, numbering: dict[int, str] | None = None) -> list[str]:
        if self.fmt == LANCE:
            return lance_keys(self.listing)
        numbering = numbering if numbering is not None else mcap_episodes(self.listing)
        return sorted({numbering[int(e)] for e in episodes if int(e) in numbering})

    def fetch(self, keys, *, workers: int = 8) -> int:
        """Make sure the local copies of ``keys`` are there and current; returns how many
        objects were downloaded now."""
        todo = [k for k in dict.fromkeys(keys) if k in self.listing and not self.fresh(k)]
        if not todo:
            return 0
        if len(todo) == 1 or workers <= 1:
            for k in todo:
                self._fetch_one(k)
        else:
            with cf.ThreadPoolExecutor(min(workers, len(todo))) as ex:
                list(ex.map(self._fetch_one, todo))
        with self._lock:
            self._save_index()
        return len(todo)

    def _fetch_one(self, key: str) -> None:
        with self._lock:
            lock = self._key_locks.setdefault(key, threading.Lock())
        with lock:
            if self.fresh(key):
                return
            want = self.listing[key]
            path = self._path(key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            part = f"{path}.part-{os.getpid()}-{threading.get_ident()}"
            try:
                got = self.storage.download(key, part)
                if int(got.size) != int(want.size) or (
                        want.etag is not None and got.etag is not None
                        and normalize_etag(got.etag) != normalize_etag(want.etag)):
                    raise SourceChanged(
                        f"source object {key} changed while it was being read "
                        f"({got.size} bytes, ETag {got.etag}; the listing said {want.size} "
                        f"bytes, ETag {want.etag})",
                        {"key": key, "change": "etag" if got.size == want.size else "size"})
                os.replace(part, path)
            finally:
                if os.path.exists(part):
                    os.unlink(part)
            with self._lock:
                self._index[key] = self._identity(key)
                self.fetched += 1
                self.fetched_bytes += int(want.size)

    def close(self) -> None:
        """A cache of this command alone is removed; a task's cache stays for the next
        command (the Daemon removes it when the run ends)."""
        if self.owned:
            shutil.rmtree(self.dir, ignore_errors=True)

    def describe(self) -> str:
        return (f"{self.fetched} object(s), {self.fetched_bytes / 1e6:.1f} MB copied to the "
                f"source cache {self.dir}")
