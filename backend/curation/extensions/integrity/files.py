"""One file's structure (L1) and whole-file read (L2) - design doc 14 §3.2, §3.3.

Reading and judging are kept apart: :class:`Blob` does every read and turns a failure of
the storage (a timeout, a 5xx, an object gone) into :class:`ReadFailure`, which the judge
records as an execution error (D33) and never as a finding; everything else here parses
bytes already in hand, so an exception from a parser is a property of the file.
"""
from __future__ import annotations

import io
import os
import struct
from dataclasses import dataclass, field
from typing import Callable

from . import mp4
from .findings import Finding

#: design doc 14 §3.3: a 64 KiB block of zeros, aligned, inside compressed media
ZERO_BLOCK = 64 * 1024
CHUNK = 8 * 1024 * 1024
PARQUET_MAGIC = b"PAR1"


class ReadFailure(Exception):
    """The storage could not give the bytes (not a property of the file)."""


@dataclass
class Blob:
    """Bytes of one object: a local path, or ranged reads of a remote one."""

    key: str
    size: int
    path: str | None = None
    ranged: Callable[[int, int], bytes] | None = None

    def read(self, start: int, n: int) -> bytes:
        n = max(0, min(n, self.size - start))
        if n == 0:
            return b""
        try:
            if self.path is not None:
                with open(self.path, "rb") as fh:
                    fh.seek(start)
                    return fh.read(n)
            return self.ranged(start, n)
        except Exception as e:  # noqa: BLE001 - any storage failure is the same thing here
            raise ReadFailure(f"{type(e).__name__}: {e}"[:300]) from e

    def chunks(self, size: int = CHUNK):
        pos = 0
        while pos < self.size:
            data = self.read(pos, size)
            if not data:
                raise ReadFailure(f"{self.key}: nothing read at {pos} of {self.size}")
            yield pos, data
            pos += len(data)


@dataclass
class FileReport:
    """What one file shows. ``findings`` are the file's own (spans on its time axis)."""

    key: str
    kind: str                               # parquet | mp4 | mcap
    size: int
    camera: str | None = None
    findings: list[Finding] = field(default_factory=list)
    tiers: list[str] = field(default_factory=list)
    samples: mp4.Samples | None = None
    num_rows: int | None = None
    crc: str | None = None                  # what checked the content: mcap_chunk
    cut_off: bool = False                   # mcap without its end marker
    read_error: str | None = None           # ReadFailure: an execution error, not a finding
    stats: dict = field(default_factory=dict)

    def add(self, code: str, message: str, tier: str, **kw) -> None:
        self.findings.append(Finding(code, message, tier, file=self.key, camera=self.camera, **kw))

    def summary(self) -> dict:
        out = {"file": self.key, "size": self.size, "tiers": list(self.tiers), "crc": self.crc}
        if self.samples is not None:
            out["frames"] = len(self.samples)
        if self.num_rows is not None:
            out["rows"] = self.num_rows
        if self.cut_off:
            out["cut_off"] = True
        out.update(self.stats)
        return out


def _mb(n: int) -> str:
    if n >= 1e6:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e3:.1f} KB" if n >= 1e3 else f"{n} 字节"


def _at(n: int) -> str:
    """"1.2 MB 处" / "512 字节处": a position in the file, spaced the Chinese way."""
    size = _mb(n)
    return f"{size}处" if size.endswith("字节") else f"{size} 处"


def _zero(data: bytes) -> bool:
    return bool(data) and not data.strip(b"\0")


def _empty(rep: FileReport, minimum: int) -> bool:
    if rep.size < minimum:
        rep.add("file_empty", f"文件只有 {rep.size} 字节，放不下该格式最少的内容", "L1")
        return True
    return False


# ---------------------------------------------------------------- parquet


def parquet_l1(blob: Blob, rep: FileReport) -> None:
    rep.tiers.append("L1")
    if _empty(rep, 12):
        return
    head = blob.read(0, min(blob.size, 4096))
    if _zero(head):
        rep.add("zero_filled", "文件开头全是零（写入没有完成或被零填充）", "L1")
        return
    if head[:4] != PARQUET_MAGIC:
        rep.add("structure_invalid", "开头不是 parquet 标识", "L1")
        return
    tail = blob.read(blob.size - 8, 8)
    if tail[4:] != PARQUET_MAGIC:
        rep.add("file_truncated", "结尾没有 parquet 标识：文件被截断或没写完", "L1")
        return
    footer_len = int.from_bytes(tail[:4], "little")
    if footer_len <= 0 or footer_len + 12 > blob.size:
        rep.add("structure_invalid", f"尾部元数据长度 {footer_len} 不合理", "L1")
        return
    footer = blob.read(blob.size - 8 - footer_len, footer_len)
    try:
        import pyarrow.parquet as pq

        rep.num_rows = int(pq.ParquetFile(io.BytesIO(PARQUET_MAGIC + footer + tail)).metadata.num_rows)
    except Exception as e:  # noqa: BLE001 - pyarrow raises several kinds
        rep.add("structure_invalid", f"尾部元数据读不出：{type(e).__name__}", "L1",
                args={"error": str(e)[:200]})


def parquet_l2(blob: Blob, rep: FileReport) -> None:
    if rep.findings:
        return
    rep.tiers.append("L2")
    data = b"".join(chunk for _, chunk in blob.chunks())
    try:
        import pyarrow.parquet as pq

        pq.read_table(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        rep.add("structure_invalid", f"数据页读不出：{type(e).__name__}", "L2",
                args={"error": str(e)[:200]})


# ---------------------------------------------------------------- mp4


def mp4_l1(blob: Blob, rep: FileReport) -> None:
    rep.tiers.append("L1")
    if _empty(rep, mp4.MIN_BYTES):
        return
    if _zero(blob.read(0, min(blob.size, 4096))):
        rep.add("zero_filled", "文件开头全是零（写入没有完成或被零填充）", "L1")
        return
    layout = mp4.walk(blob.read, blob.size)
    moov = layout.first(b"moov")
    if moov is None or moov.end > blob.size:
        if layout.truncated_at is not None:
            rep.add("file_truncated", f"文件在 {_at(blob.size)}结束，找不到完整的 moov", "L1",
                    args={"size": blob.size})
        else:
            why = layout.problem or "顶层没有 moov"
            rep.add("structure_invalid", f"不是可读的 mp4：{why}", "L1")
        return
    try:
        rep.samples = mp4.samples(mp4.read_moov(blob.read, layout))
    except (mp4.Mp4Error, struct.error, IndexError, ValueError) as e:
        rep.add("structure_invalid", f"视频轨的样本表读不出：{e}", "L1")
        return
    missing = rep.samples.first_missing(blob.size)
    if missing is not None:
        t = rep.samples.times[missing]
        rep.add("file_truncated", f"文件在 {_at(blob.size)}被截断，第 {missing} 帧（{t:.2f} 秒）起的数据缺失",
                "L1", span=(t, float("inf")), args={"size": blob.size, "first_missing_frame": missing})


def mp4_l2(blob: Blob, rep: FileReport) -> None:
    """Aligned 64 KiB blocks of zeros inside ``mdat`` (compressed video never has them)."""
    if any(f.span is None for f in rep.findings):
        return                              # the whole file is already broken
    rep.tiers.append("L2")
    layout = mp4.walk(blob.read, blob.size)
    ranges = [(b.payload, min(b.end, blob.size)) for b in layout.mdat()]
    zero = bytes(ZERO_BLOCK)
    hits: list[int] = []
    for pos, data in blob.chunks():
        first = -(-pos // ZERO_BLOCK) * ZERO_BLOCK
        for b in range(first, pos + len(data) - ZERO_BLOCK + 1, ZERO_BLOCK):
            if any(s <= b and b + ZERO_BLOCK <= e for s, e in ranges) \
                    and data[b - pos:b - pos + ZERO_BLOCK] == zero:
                hits.append(b)
    if not hits:
        return
    runs: list[list[int]] = []
    for b in hits:
        if runs and b == runs[-1][1]:
            runs[-1][1] = b + ZERO_BLOCK
        else:
            runs.append([b, b + ZERO_BLOCK])
    s = rep.samples
    for start, end in runs:
        span = None
        if s is not None and len(s):
            i, j = s.at_offset(start), s.at_offset(end - 1)
            if i is not None and j is not None:
                lo, hi = min(s.times[i], s.times[j]), max(s.times[i], s.times[j])
                span = (lo, hi + (s.duration_s / len(s) if len(s) else 0.0))
        where = f"，约 {span[0]:.2f}–{span[1]:.2f} 秒" if span else ""
        rep.add("zero_filled", f"从 {_at(start)}起有 {_mb(end - start)} 全是零{where}", "L2",
                span=span, args={"offset": start, "bytes": end - start})


# ---------------------------------------------------------------- mcap


def mcap_l1(path: str, rep: FileReport) -> None:
    from ...cli import containers

    rep.tiers.append("L1")
    if _empty(rep, 45):
        return
    with open(path, "rb") as fh:
        head = fh.read(4096)
        if _zero(head):
            rep.add("zero_filled", "文件开头全是零（写入没有完成或被零填充）", "L1")
            return
        if head[:8] != containers.MCAP_MAGIC:
            rep.add("structure_invalid", "开头不是 mcap 标识", "L1")
            return
        footer = containers.read_footer(fh)
        rep.cut_off = not footer.ends_with_magic
        if footer.summary_crc_ok is False:
            rep.add("structure_invalid", "摘要区的 CRC 校验不符", "L1")
            return
        if rep.cut_off or not footer.summary_start:
            return
        try:
            from mcap.reader import make_reader

            fh.seek(0)
            summary = make_reader(fh).get_summary()
        except Exception as e:  # noqa: BLE001 - a summary that does not parse
            rep.add("structure_invalid", f"摘要区读不出：{type(e).__name__}", "L1",
                    args={"error": str(e)[:200]})
            return
        for ci in (summary.chunk_indexes if summary else []):
            if ci.chunk_start_offset + ci.chunk_length > footer.summary_start:
                rep.add("structure_invalid", f"数据块索引指到了 {ci.chunk_start_offset} 字节，"
                        f"超出数据区", "L1", args={"chunk_start_offset": ci.chunk_start_offset})
                return


def mcap_l2(path: str, rep: FileReport) -> None:
    """Every chunk's CRC and the data section's, when the writer wrote them."""
    from mcap.exceptions import McapError
    from mcap.records import Chunk
    from mcap.stream_reader import CRCValidationError, StreamReader, get_chunk_data_stream

    if rep.findings:
        return
    rep.tiers.append("L2")
    zero = bytes(ZERO_BLOCK)
    checked = unchecked = 0
    with open(path, "rb") as fh:
        try:
            for i, rec in enumerate(StreamReader(fh, emit_chunks=True, validate_crcs=True).records):
                if not isinstance(rec, Chunk):
                    continue
                if rec.uncompressed_crc:
                    get_chunk_data_stream(rec, validate_crc=True)
                    checked += 1
                    continue
                unchecked += 1
                if rec.compression:
                    data = rec.data
                    if any(data[k:k + ZERO_BLOCK] == zero
                           for k in range(0, len(data) - ZERO_BLOCK + 1, ZERO_BLOCK)):
                        rep.add("zero_filled", f"第 {i} 条记录（压缩数据块）里有 64 KiB 全是零", "L2")
                        return
        except CRCValidationError as e:
            what = "数据区" if type(e.record).__name__ == "DataEnd" else "一个数据块"
            rep.add("crc_mismatch", f"{what}的 CRC 校验不符", "L2",
                    args={"expected": e.expected, "actual": e.actual})
            return
        except OSError as e:                # the local copy could not be read: not the file's
            raise ReadFailure(f"{type(e).__name__}: {e}"[:300]) from e
        except Exception as e:  # noqa: BLE001 - McapError, struct, zstd / lz4 errors: the bytes
            if rep.cut_off and isinstance(e, (McapError, EOFError, struct.error)):
                return                      # a cut-off recording ends mid-record: expected
            rep.add("structure_invalid", f"读到 {_at(fh.tell())}出错：{type(e).__name__}", "L2",
                    args={"error": str(e)[:200]})
            return
    if checked:
        rep.crc = "mcap_chunk"
    rep.stats = {"crc_chunks": checked, "unchecked_chunks": unchecked}


def local_path(root: str, key: str) -> str:
    return os.path.join(root, *key.split("/"))
