"""One episode's remote source bytes, read once and shared by the modules of the vlm stage
(design doc 23 §3.2, step S1).

task_success and the EEF module read the same camera videos of an episode, one after the
other in the same thread (``check_stage._vlm``). Before this, task_success opened each video
by a presigned URL and decoded its window - ranged reads of only what it needed - while the
EEF module copied every video file whole into a scratch directory first (a LeRobot v3 file
holds many episodes). Now both open the remote object through :func:`open_remote`, a seekable
file over 1 MiB blocks kept on local disk for the episode: whichever module reads a block
first fetches it with a ranged GET, the other reads it from disk, and nothing outside the
episode's window is fetched. The blocks go when the episode is done (:func:`episode`).

Outside an :func:`episode` scope (or for a path that is not under the scope's dataset)
:func:`open_remote` returns None and the caller reads as before.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

#: Block size kept on disk.
BLOCK = 1 << 20
#: A missing block is fetched together with the blocks right behind it, up to this many bytes:
#: decoding reads forward, and one 4 MiB range runs at the speed of a streamed GET where 1 MiB
#: ranges one at a time ran at half of it (streams.rangefile.READAHEAD).
READAHEAD = 4 << 20
ATTEMPTS = 4

_scope = threading.local()


@dataclass
class Stats:
    gets: int = 0
    bytes: int = 0
    hits: int = 0


@dataclass
class _Object:
    key: str
    size: int
    etag: str
    have: set[int] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)


class EpisodeBlocks:
    """The blocks of the objects one episode reads, under a temporary directory."""

    def __init__(self, storage, listing: dict | None = None, *, parent: str | None = None):
        self.storage, self.listing = storage, listing or {}
        self.prefix = str(storage.uri).rstrip("/") + "/"
        self.root = tempfile.mkdtemp(prefix="vlm-blocks-", dir=parent)
        self.stats = Stats()
        self._objects: dict[str, _Object] = {}
        self._guard = threading.Lock()

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def key_of(self, uri: str) -> str | None:
        uri = str(uri)
        return uri[len(self.prefix):] if uri.startswith(self.prefix) else None

    def _object(self, key: str) -> _Object:
        with self._guard:
            obj = self._objects.get(key)
            if obj is None:
                from ..cli.storage import normalize_etag

                info = self.listing.get(key) or self.storage.stat(key)
                if info is None:
                    raise FileNotFoundError(self.prefix + key)
                obj = _Object(key, int(info.size), normalize_etag(getattr(info, "etag", None)))
                self._objects[key] = obj
            return obj

    def _path(self, obj: _Object, index: int) -> str:
        return os.path.join(self.root, f"{hashlib.sha1(obj.key.encode()).hexdigest()[:16]}-{index}")

    def _fetch(self, obj: _Object, index: int) -> None:
        """Block ``index`` and the missing ones right behind it, in one ranged GET."""
        from ..cli.errors import SourceChanged
        from ..cli.storage import normalize_etag

        last = min((obj.size - 1) // BLOCK, index + READAHEAD // BLOCK - 1)
        while last > index and last in obj.have:
            last -= 1
        start, end = index * BLOCK, min(obj.size, (last + 1) * BLOCK)
        data, etag = b"", None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                data, etag = self.storage.read_range_etag(obj.key, start, end - start)
                break
            except Exception:  # noqa: BLE001 - the storage retries are not ours; a few more here
                if attempt == ATTEMPTS:
                    raise
        if obj.etag and etag is not None and normalize_etag(etag) != obj.etag:
            raise SourceChanged(f"{self.prefix}{obj.key} 在读取过程中变了(ETag {obj.etag} → "
                                f"{normalize_etag(etag)});请重新预检后再跑")
        if len(data) != end - start:
            raise SourceChanged(f"{self.prefix}{obj.key}: 读到 {len(data)} 字节，应为 {end - start}")
        self.stats.gets += 1
        self.stats.bytes += len(data)
        for i in range(index, last + 1):
            piece = data[(i - index) * BLOCK:(i - index + 1) * BLOCK]
            tmp = self._path(obj, i) + ".part"
            with open(tmp, "wb") as fh:
                fh.write(piece)
            os.replace(tmp, self._path(obj, i))
            obj.have.add(i)

    def block(self, obj: _Object, index: int) -> bytes:
        with obj.lock:
            if index in obj.have:
                self.stats.hits += 1
            else:
                self._fetch(obj, index)
        with open(self._path(obj, index), "rb") as fh:
            return fh.read()

    def open(self, uri: str) -> "BlockFile | None":
        key = self.key_of(uri)
        if key is None:
            return None
        return BlockFile(self, self._object(key), str(uri))


class BlockFile:
    """A read-only seekable file over an episode's blocks (what PyAV is given instead of a URL)."""

    def __init__(self, blocks: EpisodeBlocks, obj: _Object, name: str):
        self._blocks, self._obj, self.name = blocks, obj, name
        self._pos = 0
        self._cur: tuple[int, bytes] | None = None
        self.closed = False

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = 0) -> int:
        base = {0: 0, 1: self._pos, 2: self._obj.size}[whence]
        self._pos = max(0, base + int(offset))
        return self._pos

    def read(self, n: int = -1) -> bytes:
        size = self._obj.size
        if n is None or n < 0:
            n = size - self._pos
        out = bytearray()
        while n > 0 and self._pos < size:
            index = self._pos // BLOCK
            if self._cur is None or self._cur[0] != index:
                self._cur = (index, self._blocks.block(self._obj, index))
            data = self._cur[1]
            at = self._pos - index * BLOCK
            piece = data[at:at + n]
            if not piece:
                break
            out += piece
            self._pos += len(piece)
            n -= len(piece)
        return bytes(out)

    def close(self) -> None:
        self._cur = None
        self.closed = True

    def __enter__(self) -> "BlockFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@contextmanager
def episode(storage, listing: dict | None = None) -> Iterator[EpisodeBlocks | None]:
    """The scope of one episode in this thread: remote reads under ``storage`` share its blocks,
    which are deleted on the way out. A local dataset gets no scope."""
    if not getattr(storage, "remote", False):
        yield None
        return
    blocks = EpisodeBlocks(storage, listing)
    previous = getattr(_scope, "blocks", None)
    _scope.blocks = blocks
    try:
        yield blocks
    finally:
        _scope.blocks = previous
        blocks.close()


def open_remote(uri: str) -> BlockFile | None:
    """``uri`` through the current episode's blocks, or None (no scope, or not under its dataset)."""
    blocks = getattr(_scope, "blocks", None)
    if blocks is None or "://" not in str(uri):
        return None
    return blocks.open(uri)
