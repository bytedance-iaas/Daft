"""A seekable file over ranged reads, sized for how the mcap reader actually reads.

With a summary section (every file the reader accepts) the mcap library reads the
magic, seeks to the footer, reads the summary, then per chunk one ``seek`` plus about
seven reads of eight bytes or less, and finally one read of the whole chunk payload
(MiB-scale). Small reads want a cached block around them; the payload read wants one
ranged GET - stretched to ``readahead`` so the chunks right behind it are already in
cache when the reader gets to them (chunks are read in file order). A file without a
summary is read the same way by preflight's record scan: its chunk records are large
reads too, so it costs the same handful of GETs.

Blocks are evicted, so memory is bounded by ``budget`` no matter how large the object.

Deliberately not an ``io.RawIOBase``: the mcap reader wraps those in a
``BufferedReader`` per record stream, whose garbage collection closes the raw file
under the next one. As a plain object it is also never closed behind our back, so
``close()`` means what it says.

Not thread-safe by design contract - one handle serves one reader - but the block
table is still guarded so a stray concurrent read cannot corrupt it.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

#: Block size for the indexed shape. The small reads there are a chunk header's worth
#: (tens of bytes), while the payload right behind them is served by the direct path -
#: so the block only has to cover the header. A large block would re-fetch the payload
#: it overlaps and double the bytes read (measured: 1 MiB blocks cost 197% of the object,
#: 16 KiB blocks 102%).
BLOCK = 1 << 14

#: Cached bytes per handle. Concurrency multiplies this - see the orchestrator's budget.
BUDGET = 16 << 20

#: Reads at least this large go straight to one GET (a chunk payload is read once, so
#: caching it would only cost memory).
DIRECT_MIN = 1 << 19

#: A direct read fetches at least this much: the surplus is kept as a block, so the
#: next chunk's header and payload come out of it. Measured against TOS over the public
#: internet: 1 MiB ranges one at a time ran at half the speed of a single streamed GET
#: (each request pays a round trip and TCP slow start); 4 MiB ranges matched it.
READAHEAD = 4 << 20

#: The tail is fetched whole because the footer and the summary section live there.
TAIL = 1 << 16

#: Ranged reads are retried at least as hard as remote video decoding (adapters.decode).
ATTEMPTS = 4
BACKOFF_MAX_S = 4.0


@dataclass
class RangeStats:
    """What one dataset's reads cost, for the run log and for the tests."""

    gets: int = 0
    bytes: int = 0
    direct: int = 0
    retries: int = 0
    waits_s: float = 0.0

    def merge(self, other: "RangeStats") -> None:
        self.gets += other.gets
        self.bytes += other.bytes
        self.direct += other.direct
        self.retries += other.retries
        self.waits_s += other.waits_s

    def describe(self) -> str:
        return (f"{self.gets} GET, {self.bytes / 1e6:.1f} MB"
                + (f", {self.retries} retries" if self.retries else ""))


class ObjectTruncated(Exception):
    """The object has fewer bytes than the listing promised: it changed under us.

    Separate from a transport failure on purpose - retrying cannot fix it, and the
    caller turns it into ``cli.errors.SourceChanged`` rather than failing the read.
    """


@dataclass
class RangeFile:
    """Read-only seekable file over ``read_range(start, length) -> bytes``.

    ``read_range`` may return fewer bytes than asked; the gap is filled by asking
    again. An empty result before ``size`` is reached means the object shrank
    (:class:`ObjectTruncated`) - the previous implementation silently truncated the
    stream here, which surfaces much later as an mcap ``EndOfFile``.
    """

    read_range: Callable[[int, int], bytes]
    size: int
    block: int = BLOCK
    budget: int = BUDGET
    direct_min: int = DIRECT_MIN
    readahead: int = READAHEAD
    tail: int = TAIL
    attempts: int = ATTEMPTS
    stats: RangeStats = field(default_factory=RangeStats)
    name: str = ""
    _blocks: "OrderedDict[int, bytes]" = field(default_factory=OrderedDict, init=False)
    _held: int = field(default=0, init=False)
    _pos: int = field(default=0, init=False)
    _closed: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self.size = int(self.size)
        self.budget = max(self.budget, 2 * self.readahead)

    # ---------------------------------------------------------------- io surface
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    @property
    def closed(self) -> bool:
        return self._closed

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = 0) -> int:
        base = {0: 0, 1: self._pos, 2: self.size}[whence]
        self._pos = max(0, base + int(offset))
        return self._pos

    def close(self) -> None:
        with self._lock:
            self._blocks.clear()
            self._held = 0
            self._closed = True

    def __enter__(self) -> "RangeFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- fetching
    def _get(self, start: int, length: int) -> bytes:
        """One logical ranged read, retried, short reads filled in."""
        out = bytearray()
        while len(out) < length:
            want = length - len(out)
            at = start + len(out)
            chunk = b""
            for attempt in range(1, self.attempts + 1):
                try:
                    chunk = self.read_range(at, want)
                    break
                except ObjectTruncated:
                    raise
                except Exception:
                    if attempt == self.attempts:
                        raise
                    wait = min(0.5 * (2 ** (attempt - 1)), BACKOFF_MAX_S)
                    self.stats.retries += 1
                    self.stats.waits_s += wait
                    time.sleep(wait)
            self.stats.gets += 1
            self.stats.bytes += len(chunk)
            if not chunk:
                if at < self.size:
                    raise ObjectTruncated(
                        f"{self.name or 'object'}: {at} bytes in, {self.size} expected")
                break
            out += chunk
        return bytes(out)

    def _span(self, pos: int, want: int) -> tuple[int, int]:
        """Which byte range to fetch to serve ``want`` bytes at ``pos``."""
        if pos >= self.size - self.tail:          # the tail, whole (summary lives there)
            start = max(0, self.size - self.tail)
            return start, self.size - start
        # Block-aligned so lookups and eviction stay simple, but stretched to cover the
        # whole request: a read straddling a boundary still costs one GET.
        start = (pos // self.block) * self.block
        return start, min(self.size - start, max(self.block, pos + want - start))

    def _cached(self, pos: int) -> tuple[int, bytes] | None:
        """The cached block holding ``pos``, marked as just used."""
        for start, data in reversed(self._blocks.items()):
            if start <= pos < start + len(data):
                self._blocks.move_to_end(start)
                return start, data
        return None

    def _store(self, start: int, data: bytes) -> None:
        if start in self._blocks or not data:
            return
        self._blocks[start] = data
        self._held += len(data)
        while self._held > self.budget and len(self._blocks) > 1:
            _, evicted = self._blocks.popitem(last=False)
            self._held -= len(evicted)

    def _block_for(self, pos: int, want: int) -> tuple[int, bytes]:
        with self._lock:
            if self._closed:
                raise ValueError("read on a closed RangeFile")
            hit = self._cached(pos)
            if hit is not None:
                return hit
        start, length = self._span(pos, want)
        data = self._get(start, length)
        with self._lock:
            self._store(start, data)
        return start, data

    def _direct(self, pos: int, want: int) -> bytes:
        """A large read: whatever a cached block already holds of it, then one GET for
        the rest - stretched to ``readahead`` so the bytes right behind (the next chunk)
        are in cache before they are asked for."""
        out = bytearray()
        with self._lock:
            hit = self._cached(pos)
            if hit is not None:
                start, data = hit
                out += data[pos - start:pos - start + want]
        if len(out) < want:
            at = pos + len(out)
            fetch = min(self.size - at, max(want - len(out), self.readahead))
            data = self._get(at, fetch)
            self.stats.direct += 1
            need = want - len(out)
            out += data[:need]
            if len(data) > need:
                with self._lock:
                    self._store(at + need, data[need:])
        return bytes(out)

    # ---------------------------------------------------------------- reading
    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("read on a closed RangeFile")
        want = max(0, self.size - self._pos)
        if size is not None and size >= 0:
            want = min(want, size)
        if want == 0:
            return b""
        if want >= self.direct_min:
            data = self._direct(self._pos, want)
            self._pos += len(data)
            return data
        out = bytearray()
        while len(out) < want:
            start, data = self._block_for(self._pos, want - len(out))
            at = self._pos - start
            piece = data[at:at + (want - len(out))]
            if not piece:
                break
            out += piece
            self._pos += len(piece)
        return bytes(out)

    def readinto(self, buf) -> int:
        data = self.read(len(buf))
        buf[:len(data)] = data
        return len(data)
