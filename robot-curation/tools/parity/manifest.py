"""Build ``v1_manifest.json``: git blob hashes of the v1 package at a commit."""
from __future__ import annotations

import argparse
import json
import os
import subprocess

from .dump_v1 import DEFAULT_MANIFEST

DEFAULT_COMMIT = "45bdf929222e7aa08b6ec1827876af3571515202"   # D34 freeze point
DEFAULT_PREFIX = "robot-curation/curation"


def build_manifest(repo: str, commit: str, prefix: str) -> dict:
    out = subprocess.run(["git", "-C", repo, "ls-tree", "-r", commit, "--", prefix],
                         check=True, capture_output=True, text=True).stdout
    files = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        _mode, kind, sha = meta.split()
        if kind == "blob":
            files[path[len(prefix) + 1:]] = sha
    full = subprocess.run(["git", "-C", repo, "rev-parse", commit], check=True,
                          capture_output=True, text=True).stdout.strip()
    return {"commit": full, "prefix": prefix, "files": dict(sorted(files.items()))}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m parity v1-manifest")
    p.add_argument("--repo", default=os.getcwd())
    p.add_argument("--commit", default=DEFAULT_COMMIT)
    p.add_argument("--prefix", default=DEFAULT_PREFIX)
    p.add_argument("--out", default=DEFAULT_MANIFEST)
    args = p.parse_args(argv)
    manifest = build_manifest(args.repo, args.commit, args.prefix)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"{len(manifest['files'])} files at {manifest['commit'][:10]} -> {args.out}")
    return 0


def pack(repo: str, commit: str, out: str) -> dict:
    """One tarball for the pod: v1 at the freeze commit + these tools at HEAD.

    Layout inside: ``robot-curation/curation`` (freeze commit),
    ``robot-curation/requirements.txt`` (freeze commit) and
    ``robot-curation/tools/parity`` (HEAD). Run it with
    ``PYTHONPATH=robot-curation/tools python -m parity dump-v1 ...``.
    """
    import io
    import tarfile

    def archive(ref: str, *paths: str) -> bytes:
        return subprocess.run(["git", "-C", repo, "archive", "--format=tar", ref, *paths],
                              check=True, capture_output=True).stdout

    dirty = subprocess.run(["git", "-C", repo, "status", "--porcelain", "--",
                            "robot-curation/tools/parity"], check=True,
                           capture_output=True, text=True).stdout.strip()
    head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    parts = [archive(commit, "robot-curation/curation", "robot-curation/requirements.txt"),
             archive("HEAD", "robot-curation/tools/parity")]
    n = 0
    with tarfile.open(out, "w:gz") as dst:
        for blob in parts:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as src:
                for member in src.getmembers():
                    data = src.extractfile(member) if member.isfile() else None
                    dst.addfile(member, data)
                    n += 1
    return {"out": out, "members": n, "v1_commit": commit, "tools_commit": head,
            "tools_uncommitted_changes": bool(dirty)}


def pack_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m parity pack")
    p.add_argument("--repo", default=os.getcwd())
    p.add_argument("--commit", default=DEFAULT_COMMIT)
    p.add_argument("--out", required=True, help="e.g. parity-pod.tar.gz")
    args = p.parse_args(argv)
    info = pack(args.repo, args.commit, args.out)
    print(json.dumps(info, indent=1))
    if info["tools_uncommitted_changes"]:
        print("warning: robot-curation/tools/parity has uncommitted changes; the pack "
              "contains HEAD, not the working tree")
    return 0
