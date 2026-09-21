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


def _git(repo: str, *args: str, text: bool = True):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                          text=text).stdout


def _tools_path_at(repo: str, ref: str) -> str:
    """Where the parity tools live at ``ref`` (they moved in F1.2)."""
    for path in ("tools/parity", "robot-curation/tools/parity"):
        if _git(repo, "ls-tree", ref, "--", path).strip():
            return path
    raise FileNotFoundError(f"no parity tools at {ref}")


def _copy_members(src_blob: bytes, dst, rename=None) -> int:
    import io
    import tarfile

    n = 0
    with tarfile.open(fileobj=io.BytesIO(src_blob), mode="r:") as src:
        for member in src.getmembers():
            data = src.extractfile(member) if member.isfile() else None
            if rename:
                member.name = rename(member.name)
            dst.addfile(member, data)
            n += 1
    return n


def pack(repo: str, commit: str, out: str) -> dict:
    """One tarball for the pod: v1 at the freeze commit + these tools at HEAD.

    Layout inside (independent of where the tools live in the repo today):
    ``robot-curation/curation`` and ``robot-curation/requirements.txt`` from the
    freeze commit, ``robot-curation/tools/parity`` from HEAD. Run it with
    ``cd robot-curation && PYTHONPATH=tools python -m parity dump-v1 ...``.
    """
    import tarfile

    tools = _tools_path_at(repo, "HEAD")
    dirty = _git(repo, "status", "--porcelain", "--", tools).strip()
    head = _git(repo, "rev-parse", "HEAD").strip()
    v1 = _git(repo, "archive", "--format=tar", commit, "robot-curation/curation",
              "robot-curation/requirements.txt", text=False)
    tl = _git(repo, "archive", "--format=tar", "HEAD", tools, text=False)
    with tarfile.open(out, "w:gz") as dst:
        n = _copy_members(v1, dst)
        n += _copy_members(tl, dst, rename=lambda name: "robot-curation/tools/parity"
                           + name[len(tools):])
    return {"out": out, "members": n, "v1_commit": commit, "tools_commit": head,
            "tools_uncommitted_changes": bool(dirty)}


def extract_v1(repo: str, commit: str, out_dir: str) -> str:
    """Write the v1 package at ``commit`` under ``out_dir``; return the ``--v1-src`` path.

    After F1.2 the working tree no longer holds v1 as it was at the freeze
    point, so local runs of ``dump-v1`` take their v1 from git instead.
    """
    import io
    import tarfile

    blob = _git(repo, "archive", "--format=tar", commit, "robot-curation/curation",
                "robot-curation/requirements.txt", text=False)
    os.makedirs(out_dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as tar:
        if hasattr(tarfile, "data_filter"):      # Python >= 3.10.12 / 3.11.4 / 3.12
            tar.extractall(out_dir, filter="data")
        else:
            tar.extractall(out_dir)
    return os.path.join(out_dir, "robot-curation")


def pack_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m parity pack")
    p.add_argument("--repo", default=os.getcwd())
    p.add_argument("--commit", default=DEFAULT_COMMIT)
    p.add_argument("--out", required=True, help="e.g. parity-pod.tar.gz")
    args = p.parse_args(argv)
    info = pack(args.repo, args.commit, args.out)
    print(json.dumps(info, indent=1))
    if info["tools_uncommitted_changes"]:
        print("warning: the parity tools have uncommitted changes; the pack "
              "contains HEAD, not the working tree")
    return 0


def v1_src_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m parity v1-src",
                                description="Extract v1 at the freeze commit for --v1-src.")
    p.add_argument("--repo", default=os.getcwd())
    p.add_argument("--commit", default=DEFAULT_COMMIT)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    print(extract_v1(args.repo, args.commit, args.out))
    return 0
