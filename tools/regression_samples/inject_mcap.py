#!/usr/bin/env python
"""Structure faults (FILE-3) and their legal look-alikes, injected into copies of mcap episodes.

An mcap file is the magic, a sequence of records (opcode u8, length u64, content), a data section that ends with a
DataEnd record, an optional summary section, the Footer and the magic again (https://mcap.dev/spec). The edits work on
records, never on message content:

    no_summary   legal: the summary section and its offsets are left out (Footer summary_start = 0, summary CRC 0)
    zero_crcs    legal: every CRC is 0, which the spec reads as "not computed"
    cut_off      the file stops in the middle of the data section (no DataEnd, Footer or closing magic)
    chunk_crc    the first chunk's stored CRC does not match its records
    summary_crc  the Footer's summary CRC does not match the summary section

    python tools/regression_samples/inject_mcap.py --out <dir> --plan no_summary,zero_crcs,cut_off,chunk_crc,summary_crc \\
        --bases a.mcap,b.mcap,... --controls 2

The bases must be clean recordings; each is used once. Writes episode_<n>.mcap, injection.json and README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import time
import zlib

MAGIC = b"\x89MCAP0\r\n"
OP_FOOTER, OP_CHUNK, OP_DATA_END = 0x02, 0x06, 0x0F
ITEM = "FILE-3"


def records(buf: bytes):
    """(offset, opcode, content start, content length) of every record, in file order, through the Footer"""
    if buf[:8] != MAGIC:
        raise ValueError("not an mcap file")
    pos = 8
    while pos + 9 <= len(buf):
        op = buf[pos]
        n = struct.unpack_from("<Q", buf, pos + 1)[0]
        yield pos, op, pos + 9, n
        if op == OP_FOOTER:
            return
        pos += 9 + n


def find(buf: bytes, opcode: int):
    return [r for r in records(buf) if r[1] == opcode]


def footer_record(summary_start=0, summary_offset_start=0, summary_crc=0) -> bytes:
    return bytes([OP_FOOTER]) + struct.pack("<Q", 20) + struct.pack("<QQI", summary_start, summary_offset_start, summary_crc)


def no_summary(buf: bytes) -> tuple[bytes, dict]:
    end = find(buf, OP_DATA_END)[0]
    out = buf[:end[2] + end[3]] + footer_record() + MAGIC
    return out, {"dropped_bytes": len(buf) - len(out) + 29}


def zero_crcs(buf: bytes) -> tuple[bytes, dict]:
    b = bytearray(buf)
    chunks = find(buf, OP_CHUNK)
    for _, _, c, _ in chunks:
        struct.pack_into("<I", b, c + 24, 0)        # start u64, end u64, uncompressed_size u64, then the CRC
    for _, _, c, _ in find(buf, OP_DATA_END):
        struct.pack_into("<I", b, c, 0)
    for _, _, c, _ in find(buf, OP_FOOTER):
        struct.pack_into("<I", b, c + 16, 0)
    return bytes(b), {"chunks": len(chunks)}


def cut_off(buf: bytes, fraction: float = 0.6) -> tuple[bytes, dict]:
    end = find(buf, OP_DATA_END)[0][0]
    at = int(8 + (end - 8) * fraction)
    return buf[:at], {"kept_bytes": at, "of_bytes": len(buf), "cut_at_fraction_of_data": fraction}


def chunk_crc(buf: bytes) -> tuple[bytes, dict]:
    b = bytearray(buf)
    _, _, c, _ = find(buf, OP_CHUNK)[0]
    was = struct.unpack_from("<I", b, c + 24)[0]
    now = (was ^ 0xA5A5A5A5) or 0x12345678
    struct.pack_into("<I", b, c + 24, now)
    return bytes(b), {"chunk": 0, "crc_was": was, "crc_now": now}


def summary_crc(buf: bytes) -> tuple[bytes, dict]:
    b = bytearray(buf)
    _, _, c, _ = find(buf, OP_FOOTER)[0]
    start, _, was = struct.unpack_from("<QQI", b, c)
    if not start:
        raise ValueError("the base has no summary section")
    now = (was ^ 0x5A5A5A5A) or 0x87654321
    struct.pack_into("<I", b, c + 16, now)
    return bytes(b), {"crc_was": was, "crc_now": now}


#: fault -> (edit, item it carries or None for a legal variant)
FAULTS = {"no_summary": (no_summary, None), "zero_crcs": (zero_crcs, None), "cut_off": (cut_off, ITEM),
          "chunk_crc": (chunk_crc, ITEM), "summary_crc": (summary_crc, ITEM)}


def source_of(path: str) -> str:
    """the base's source line from its subset README (| file | source |), else the path"""
    readme = os.path.join(os.path.dirname(path), "README.md")
    name = os.path.basename(path)
    if os.path.exists(readme):
        for line in open(readme, encoding="utf-8"):
            m = re.match(r"\|\s*" + re.escape(name) + r"\s*\|\s*(.+?)\s*\|$", line.strip())
            if m:
                return m.group(1)
    return path


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--bases", required=True, help="comma list of clean .mcap files, one per output episode")
    ap.add_argument("--plan", default="no_summary,zero_crcs,cut_off,chunk_crc,summary_crc")
    ap.add_argument("--controls", type=int, default=2)
    a = ap.parse_args(argv)
    bases = a.bases.split(",")
    plan = a.plan.split(",")
    if len(bases) < len(plan) + a.controls:
        raise SystemExit(f"{len(plan) + a.controls} bases needed, {len(bases)} given")
    if os.path.exists(a.out):
        shutil.rmtree(a.out)
    os.makedirs(a.out)
    recs, rows = [], []
    for n, base in enumerate(bases[:len(plan) + a.controls]):
        name = plan[n] if n < len(plan) else None
        buf = open(base, "rb").read()
        if name:
            edit, item = FAULTS[name]
            out, params = edit(buf)
        else:
            out, params, item = buf, {}, None
        dst = os.path.join(a.out, f"episode_{n}.mcap")
        open(dst, "wb").write(out)
        base_ep = re.search(r"episode_(\d+)\.mcap$", base)
        lineage = f"{os.path.basename(os.path.dirname(base))}:{base_ep.group(1) if base_ep else os.path.basename(base)}"
        rec = {"episode_index": n, "base_episode": int(base_ep.group(1)) if base_ep else None, "base_file": base, "lineage": lineage,
               "fault": name, "item": item, "severity": "obvious" if item else None, "params": params,
               "bytes": len(out), "crc32_of_file": zlib.crc32(out) & 0xFFFFFFFF}
        if name and not item:
            rec["note"] = "a legal way to write the file (mcap spec); the platform must not report FILE-3"
        elif not name:
            rec["note"] = "pristine copy of the base episode: a control"
        recs.append(rec)
        rows.append((f"| episode_{n}.mcap | {source_of(base)} |", f"- episode_{n}.mcap: {name or 'control'}"))
        print(f"ep {n}: {name or 'control':12s} {json.dumps(params)[:100]} <- {base}", flush=True)
    json.dump({"schema_version": "0.1", "note": f"mcap structure faults ({ITEM}) and legal variants; plan {a.plan}",
               "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "episodes": recs},
              open(os.path.join(a.out, "injection.json"), "w"), ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("# mcap structure faults (FILE-3) and legal variants\n\nBuilt by tools/regression_samples/inject_mcap.py from clean GenRobot "
                 "recordings (genrobot2025/10Kh-RealOmin-OpenData, CC BY-SA 4.0). injection.json says what each file carries.\n\n"
                 "| file | source |\n|---|---|\n" + "\n".join(r[0] for r in rows) + "\n\nVariants:\n\n" + "\n".join(r[1] for r in rows) + "\n")
    print("INJECT_MCAP_DONE", len(recs), "episodes ->", a.out)


if __name__ == "__main__":
    main()
