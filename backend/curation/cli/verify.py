"""``curation verify`` - read the delivery back before calling it complete (doc 02 §3.10).

Writing is not the same as being readable: v1 once reported a 10853-byte
all-zero ``passed.json`` as delivered (``pipeline/run.py``
``_verify_delivery_visible``, ``export/safe_write.py``). This command reads
every key file back from ``--output`` (the task's ``<run_id>/`` directory in
the delivery) and checks it against the local ``--run-dir``:

* it exists (``missing``); it is listed but cannot be read yet after the
  visibility window (``not_visible_in_time``);
* its size equals the local file (``size_mismatch``) - artifacts that exist
  only remotely (listed in ``export/manifest.json``) are not size-checked;
* its head is not all zero bytes (``zero_filled``) and it parses
  (``unparseable``): JSON / JSON Lines as a whole, parquet by its magic bytes
  and footer, mp4 by finding the ``moov`` box, JPEG and PNG by their magic.

Key files are every file of the run directory except work-in-progress ones
(``logs/``, ``inflight.json``, hidden and temporary files) plus every artifact
of ``export/manifest.json`` under ``export/lerobot_curated/``. Only when all of
them pass is ``_COMPLETE`` written, last; a stale ``_COMPLETE`` is removed when
they do not. Output: ``docs/contracts/cli/verify.schema.json``. Failed files
do not change the exit code (0) - the Daemon reads ``failed``.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import time
from dataclasses import dataclass

from .errors import UsageError
from .framework import Context, Result, now_ms
from .storage import ObjectInfo, ObjectMissing, Storage, open_storage

SCHEMA_VERSION = "1.0"
STAGE = "verify"
COMPLETE = "_COMPLETE"
EXPORT_MANIFEST = "export/manifest.json"
EXPORT_ROOT = "export/lerobot_curated"
_HEAD_BYTES = 4096
_POLL_S = 2.0
_TEXT_EXT = {"md", "txt", "csv", "tsv", "yaml", "yml", "html", "log"}


def add_parser(sub, parents) -> None:
    p = sub.add_parser(
        "verify", parents=parents,
        help="read the delivery back and write _COMPLETE when every key file is good",
        description="Read every key file of the delivery back from --output and compare it "
                    "with the run directory: present, same size, not zero-filled, parseable. "
                    "_COMPLETE is written last, and only if all pass.")
    p.add_argument("--run-dir", required=True, metavar="DIR",
                   help="the task's local work directory (what was delivered)")
    p.add_argument("--output", required=True, metavar="URI",
                   help="the task's directory in the delivery: tos://bucket/deliveries/<name>/"
                        "<run_id> or a local path")
    p.add_argument("--visibility-timeout", type=float, default=60.0, metavar="S",
                   help="how long to wait for files that are not readable yet (default 60)")
    p.set_defaults(func=run)


@dataclass
class Expected:
    key: str
    size: int | None                     # None: no local copy to compare with


def is_delivered(rel: str) -> bool:
    """Whether a run-directory file is part of the delivery (not work in progress)."""
    parts = rel.split("/")
    name = parts[-1]
    if any(p.startswith(".") for p in parts) or parts[0] == "logs":
        return False
    if name in (COMPLETE, "latest", "inflight.json"):
        return False
    return not (name.endswith((".tmp", ".partial", ".lock")) or ".tmp-" in name)


def collect_expected(run_dir: str, output: Storage) -> dict[str, Expected]:
    out: dict[str, Expected] = {}
    for dirpath, dirnames, filenames in os.walk(run_dir):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, run_dir).replace(os.sep, "/")
            if is_delivered(rel):
                out[rel] = Expected(rel, os.path.getsize(full))
    if not out:                      # no reference at all: nothing may be declared complete
        raise UsageError(f"--run-dir {run_dir} holds no delivered file to verify")
    manifest = _export_manifest(run_dir, output)
    sizes = _manifest_sizes(manifest)
    for rel in _manifest_artifacts(manifest) + sorted(sizes):
        key = f"{EXPORT_ROOT}/{rel}"
        if key in out and out[key].size is not None:
            continue                      # a local copy: its size is the reference
        out[key] = Expected(key, sizes.get(rel))
    return out


def _manifest_sizes(manifest) -> dict[str, int]:
    """``files`` of export/manifest.json (C2 1.1): every delivered file with its size."""
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict):
        return {}
    return {str(rel).strip("/"): int(rec["size"]) for rel, rec in files.items()
            if isinstance(rec, dict) and isinstance(rec.get("size"), int)}


def _export_manifest(run_dir: str, output: Storage) -> dict | None:
    local = os.path.join(run_dir, *EXPORT_MANIFEST.split("/"))
    try:
        if os.path.isfile(local):
            with open(local, encoding="utf-8") as fh:
                return json.load(fh)
        return json.loads(output.read_bytes(EXPORT_MANIFEST).decode("utf-8"))
    except (ObjectMissing, OSError, ValueError):
        return None                      # a broken manifest is reported by its own check


def _manifest_artifacts(manifest) -> list[str]:
    if not isinstance(manifest, dict):
        return []
    rels: list[str] = []
    for ep in manifest.get("episodes") or []:
        art = ep.get("artifacts") if isinstance(ep, dict) else None
        if not isinstance(art, dict):
            continue
        if isinstance(art.get("parquet"), str):
            rels.append(art["parquet"])
        rels += [v for v in (art.get("videos") or {}).values() if isinstance(v, str)]
    rels += [m for m in manifest.get("meta_files") or [] if isinstance(m, str)]
    return [r.strip("/") for r in rels if r.strip("/")]


# ---------------------------------------------------------------- content checks


def _ext(key: str) -> str:
    name = key.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _all_zero(data: bytes) -> bool:
    return bool(data) and not data.strip(b"\x00")


def content_problem(storage: Storage, key: str, size: int) -> str | None:
    """``zero_filled`` / ``unparseable`` / None; raises ObjectMissing if unreadable."""
    ext = _ext(key)
    if ext in ("json", "jsonl") or ext in _TEXT_EXT:
        data = storage.read_bytes(key)
        if _all_zero(data):
            return "zero_filled"
        if b"\x00" in data:
            return "unparseable"
        try:
            text = data.decode("utf-8")
            if ext == "json":
                json.loads(text)
            elif ext == "jsonl":
                for line in text.splitlines():
                    if line.strip():
                        json.loads(line)
        except ValueError:
            return "unparseable"
        return None
    if ext not in ("parquet", "mp4", "m4v", "mov", "jpg", "jpeg", "png"):
        return None                              # existence and size only
    head = storage.read_range(key, 0, min(size, _HEAD_BYTES))
    if _all_zero(head):
        return "zero_filled"
    if ext == "parquet":
        return _parquet_problem(storage, key, size, head)
    if ext in ("mp4", "m4v", "mov"):
        return _mp4_problem(storage, key, size)
    if ext in ("jpg", "jpeg"):
        return None if head[:3] == b"\xff\xd8\xff" else "unparseable"
    return None if head[:8] == b"\x89PNG\r\n\x1a\n" else "unparseable"


def _parquet_problem(storage: Storage, key: str, size: int, head: bytes) -> str | None:
    if size < 12 or head[:4] != b"PAR1":
        return "unparseable"
    tail = storage.read_range(key, size - 8, 8)
    if len(tail) != 8 or tail[4:] != b"PAR1":
        return "unparseable"
    footer_len = int.from_bytes(tail[:4], "little")
    if footer_len <= 0 or footer_len + 12 > size:
        return "unparseable"
    footer = storage.read_range(key, size - 8 - footer_len, footer_len)
    try:
        import pyarrow.parquet as pq

        # The footer alone parses: column offsets are only followed when reading data.
        _ = pq.ParquetFile(io.BytesIO(b"PAR1" + footer + tail)).metadata
    except Exception:  # noqa: BLE001 - any pyarrow failure means unreadable
        return "unparseable"
    return None


def _mp4_problem(storage: Storage, key: str, size: int) -> str | None:
    """Walk the top-level boxes until ``moov``; without it no player can open the file."""
    pos, boxes = 0, 0
    while pos + 8 <= size and boxes < 4096:
        hdr = storage.read_range(key, pos, 16)
        if len(hdr) < 8:
            return "unparseable"
        box_size = int.from_bytes(hdr[:4], "big")
        box_type = hdr[4:8]
        if not all(0x20 <= b < 0x7f for b in box_type):
            return "unparseable"
        if box_type == b"moov":
            return None
        if box_size == 1:
            if len(hdr) < 16:
                return "unparseable"
            box_size = int.from_bytes(hdr[8:16], "big")
        elif box_size == 0:
            box_size = size - pos                  # the box runs to the end of the file
        if box_size < 8:
            return "unparseable"
        pos += box_size
        boxes += 1
    return "unparseable"


# ---------------------------------------------------------------- command


def check_file(storage: Storage, listing: dict[str, ObjectInfo], exp: Expected) -> str | None:
    """None when good; else a reason. ``missing`` / ``not_visible`` may still resolve."""
    have = listing.get(exp.key)
    if have is None:
        return "missing"
    if exp.size is not None and have.size != exp.size:
        return "not_visible" if have.size == 0 else "size_mismatch"
    if exp.size is None and have.size == 0:
        return "not_visible"             # a dataset artifact is never empty; v1 saw 0-byte reads
    try:
        return content_problem(storage, exp.key, have.size)
    except ObjectMissing:
        return "not_visible"


def run(ctx: Context, args: argparse.Namespace) -> Result:
    run_dir = os.path.abspath(os.path.expanduser(args.run_dir))
    if not os.path.isdir(run_dir):
        raise UsageError(f"--run-dir {run_dir} is not a directory")
    if args.visibility_timeout < 0:
        raise UsageError("--visibility-timeout must not be negative")
    output = open_storage(args.output, role="output", region=ctx.output_region)
    expected = collect_expected(run_dir, output)
    listing = output.list()
    total = len(expected)
    ctx.log("info", f"verifying {total} files under {output.uri}")
    results: dict[str, str | None] = {}
    for n, key in enumerate(sorted(expected), 1):
        ctx.check_stop("while reading the delivery back")
        results[key] = check_file(output, listing, expected[key])
        ctx.progress(STAGE, n, total)

    deadline = time.monotonic() + float(args.visibility_timeout)
    waiting = [k for k, r in results.items() if r in ("missing", "not_visible")]
    if waiting and args.visibility_timeout > 0:
        ctx.log("info", f"{len(waiting)} files are not readable yet; waiting up to "
                        f"{args.visibility_timeout:g}s for object storage to show them")
    while waiting and time.monotonic() < deadline:
        ctx.sleep(min(_POLL_S, max(0.0, deadline - time.monotonic())))
        for key in list(waiting):
            info = output.stat(key)
            if info is None:
                continue
            results[key] = check_file(output, {key: info}, expected[key])
            if results[key] not in ("missing", "not_visible"):
                waiting.remove(key)
    failed = []
    for key in sorted(results):
        reason = results[key]
        if reason is None:
            continue
        if reason == "not_visible":
            reason = "not_visible_in_time"
        failed.append({"path": key, "reason": reason})

    ctx.check_stop("before writing _COMPLETE")
    complete = not failed
    if complete:
        marker = {"schema_version": SCHEMA_VERSION, "verified_at": now_ms(), "checked": total}
        data = (json.dumps(marker) + "\n").encode("utf-8")
        output.put_bytes(COMPLETE, data)
        seen = output.stat(COMPLETE)
        if seen is None or seen.size != len(data):
            ctx.log("warn", f"wrote {COMPLETE} but cannot read it back yet; readers may see "
                            f"it late")
        ctx.log("info", f"all {total} files verified; wrote {COMPLETE}")
    elif COMPLETE in listing:
        output.delete(COMPLETE)
        ctx.log("warn", f"removed a stale {COMPLETE}: the delivery no longer verifies")
    doc = {"schema_version": SCHEMA_VERSION, "output": output.uri, "checked": total,
           "failed": failed, "complete_marker": complete}
    return Result(doc, human=render(doc))


def render(doc: dict) -> str:
    if not doc["failed"]:
        return (f"verified {doc['checked']} files under {doc['output']}: all readable; "
                f"wrote {COMPLETE}")
    lines = [f"{len(doc['failed'])} of {doc['checked']} files under {doc['output']} failed; "
             f"{COMPLETE} not written:"]
    lines += [f"  {f['path']}: {f['reason']}" for f in doc["failed"]]
    return "\n".join(lines)
