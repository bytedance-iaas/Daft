"""A-class guard: algorithm files must stay identical to the freeze commit.

    python -m parity a-class-check [--tree backend/curation] [--pr-body-env PR_BODY]
                                   [--declared a_class_declared.json]

A-class code (design doc 10, section 2) is copied verbatim from v1; only import
paths may change. This compares every A-class file under ``--tree`` with its
git blob hash in ``v1_manifest.json``. Any difference fails the check unless
the pull request description contains a ``parity-change:`` line explaining it,
or ``a_class_declared.json`` pins the file's current hash with the reason it was
adopted (design 13, 2026-09-24): a branch push has no pull request description,
and a declared file edited once more is caught again.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .dump_v1 import DEFAULT_MANIFEST, git_blob_sha1

#: Paths relative to the ``curation`` package (design doc 10, section 2).
A_CLASS = ("core/", "registry/", "ingest/", "dataset_level/",
           "export/lerobot_writer.py", "export/safe_write.py",
           "pipeline/verdict.py", "episode_select.py", "vlm_call_kinds.py")
MARKER = "parity-change:"
#: A-class files adopted with a known difference: {rel: {"blob", "since", "why"}}.
DEFAULT_DECLARED = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "a_class_declared.json")


def is_a_class(rel: str) -> bool:
    return any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in A_CLASS)


def load_declared(path: str = DEFAULT_DECLARED) -> dict[str, dict]:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return dict(json.load(fh).get("files") or {})


def check(tree: str, manifest_path: str = DEFAULT_MANIFEST,
          declared_path: str = DEFAULT_DECLARED) -> dict:
    with open(manifest_path, encoding="utf-8") as fh:
        expected = {rel: sha for rel, sha in json.load(fh)["files"].items() if is_a_class(rel)}
    adopted = load_declared(declared_path)

    def pinned(rel: str, blob: str) -> bool:
        return blob == (adopted.get(rel) or {}).get("blob")

    changed, missing, declared = [], [], []
    for rel, sha in sorted(expected.items()):
        path = os.path.join(tree, rel)
        if not os.path.isfile(path):
            missing.append(rel)
            continue
        blob = git_blob_sha1(path)
        if blob != sha:
            (declared if pinned(rel, blob) else changed).append(rel)
    added = []
    for dirpath, dirnames, filenames in os.walk(tree):
        dirnames[:] = [d for d in dirnames if d != "__pycache__" and not d.startswith(".")]
        for name in filenames:
            if name.endswith((".pyc", ".pyo")) or name.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), tree).replace(os.sep, "/")
            if is_a_class(rel) and rel not in expected:
                added.append(rel)
    new = []
    for rel in sorted(added):
        (declared if pinned(rel, git_blob_sha1(os.path.join(tree, rel))) else new).append(rel)
    return {"a_class_files": len(expected), "changed": changed, "missing": missing,
            "added": new, "declared": sorted(declared)}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m parity a-class-check")
    p.add_argument("--tree", default="backend/curation")
    p.add_argument("--manifest", default=DEFAULT_MANIFEST)
    p.add_argument("--declared", default=DEFAULT_DECLARED,
                   help="A-class files adopted with a known difference (pinned hashes)")
    p.add_argument("--pr-body-env", metavar="VAR",
                   help="environment variable holding the pull request description")
    args = p.parse_args(argv)
    res = check(args.tree, args.manifest, args.declared)
    drift = res["changed"] + res["missing"] + res["added"]
    print(f"A-class files: {res['a_class_files']}, changed {len(res['changed'])}, "
          f"missing {len(res['missing'])}, added {len(res['added'])}, "
          f"declared {len(res['declared'])}")
    for key in ("changed", "missing", "added", "declared"):
        for rel in res[key]:
            print(f"  {key}: {rel}")
    if not drift:
        return 0
    body = os.environ.get(args.pr_body_env, "") if args.pr_body_env else ""
    if MARKER in body:
        print(f"declared in the pull request description ({MARKER}), allowed")
        return 0
    print(f"A-class code differs from the freeze commit. If intended, add a line starting "
          f"with `{MARKER}` to the pull request description saying why, and show the "
          f"parity result; a change adopted for good is pinned in "
          f"{os.path.basename(args.declared)} with its reason.", file=sys.stderr)
    return 1
