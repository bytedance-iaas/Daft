"""Archive a dump to TOS (or a local directory) and fetch it back, with checksums.

    python -m parity archive --dump DIR --to tos://bucket/prefix [--region R]
    python -m parity fetch --from tos://bucket/prefix --to DIR [--region R]

``archive`` writes ``MANIFEST.json`` (every file with size and sha256, plus the
dump status) into the dump, then uploads the whole directory. The manifest is
also printed so it can be committed to the repo; ``fetch`` refuses a download
whose files do not match it. TOS transfers go through v1's ``tos_store`` and
use the same credentials as a v1 run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys

MANIFEST = "MANIFEST.json"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def build_manifest(dump_dir: str) -> dict:
    files = {}
    for cur, dirs, names in os.walk(dump_dir):
        dirs.sort()
        for name in sorted(names):
            path = os.path.join(cur, name)
            rel = os.path.relpath(path, dump_dir).replace(os.sep, "/")
            if rel == MANIFEST:
                continue
            files[rel] = {"bytes": os.path.getsize(path), "sha256": sha256_file(path)}
    meta = {}
    if os.path.isfile(os.path.join(dump_dir, "dump.json")):
        with open(os.path.join(dump_dir, "dump.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    return {"label": meta.get("label"), "status": meta.get("status"),
            "v1_commit": (meta.get("v1_source") or {}).get("commit"),
            "dataset": meta.get("dataset"), "v1_args": meta.get("v1_args"),
            "config_sha256": meta.get("config_sha256"), "files": files}


def verify(dump_dir: str) -> list[str]:
    """Files that are missing or differ from the dump's manifest."""
    with open(os.path.join(dump_dir, MANIFEST), encoding="utf-8") as fh:
        manifest = json.load(fh)
    bad = []
    for rel, info in manifest["files"].items():
        path = os.path.join(dump_dir, rel)
        if not os.path.isfile(path) or sha256_file(path) != info["sha256"]:
            bad.append(rel)
    return bad


def _tos_store(v1_src: str | None = None):
    from .dump_v1 import DEFAULT_V1_SRC

    src = os.path.abspath(v1_src or DEFAULT_V1_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    from curation import tos_store  # noqa: E402 - v1's TOS helper, same credentials as a run

    return tos_store


def archive(dump_dir: str, dest: str, region: str | None, v1_src: str | None = None) -> dict:
    manifest = build_manifest(dump_dir)
    with open(os.path.join(dump_dir, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    if dest.startswith("tos://"):
        n = _tos_store(v1_src).stage_out(dump_dir, dest, region)
    else:
        shutil.copytree(dump_dir, dest, dirs_exist_ok=False)
        n = len(manifest["files"]) + 1
    return {"dest": dest, "uploaded": n, "manifest": manifest}


def fetch(src: str, dest: str, region: str | None, v1_src: str | None = None) -> list[str]:
    if src.startswith("tos://"):
        local = _tos_store(v1_src).stage_in(src, region)
        shutil.copytree(local, dest, dirs_exist_ok=False)
    else:
        shutil.copytree(src, dest, dirs_exist_ok=False)
    return verify(dest)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m parity archive")
    p.add_argument("--dump", required=True)
    p.add_argument("--to", required=True, help="tos://bucket/prefix or a local directory")
    p.add_argument("--region")
    p.add_argument("--manifest-out", help="also write the manifest here (to commit it)")
    p.add_argument("--v1-src", help="v1 tree whose tos_store to use (default: next to the tools)")
    args = p.parse_args(argv)
    info = archive(args.dump, args.to, args.region, args.v1_src)
    if args.manifest_out:
        with open(args.manifest_out, "w", encoding="utf-8") as fh:
            json.dump(info["manifest"], fh, indent=1, sort_keys=True)
    print(json.dumps({k: v for k, v in info.items() if k != "manifest"}
                     | {"files": len(info["manifest"]["files"]),
                        "status": info["manifest"]["status"]}, indent=1))
    return 0


def fetch_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m parity fetch")
    p.add_argument("--from", dest="src", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--region")
    p.add_argument("--v1-src", help="v1 tree whose tos_store to use (default: next to the tools)")
    args = p.parse_args(argv)
    bad = fetch(args.src, args.to, args.region, args.v1_src)
    if bad:
        print(f"fetch: {len(bad)} files do not match MANIFEST.json: {bad[:10]}", file=sys.stderr)
        return 1
    print(f"fetched and verified: {args.to}")
    return 0
