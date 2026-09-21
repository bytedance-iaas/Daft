"""``python -m parity <command> [args]``."""
from __future__ import annotations

import sys

USAGE = """usage: python -m parity <command> [args]

commands:
  dump-v1       run v1 once with taps and write a normalized dump
  compare       compare a candidate dump with the golden dump
  make-fixture  write the synthetic 8-episode LeRobot v2 dataset
  v1-manifest   regenerate v1_manifest.json from git (freeze commit)
  v1-src        extract v1 at the freeze commit (the --v1-src for local runs)
  a-class-check fail when algorithm (A-class) files differ from the freeze commit
  tape-summary  counts and failures of a VLM tape
  pack          tarball for the pod: v1 at the freeze commit + these tools
  archive       write MANIFEST.json into a dump and upload it (tos:// or a directory)
  fetch         download an archived dump and verify it against its manifest
"""


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "dump-v1":
        from .dump_v1 import main as run
    elif cmd == "compare":
        from .compare import main as run
    elif cmd == "make-fixture":
        from .fixtures import main as run
    elif cmd == "v1-manifest":
        from .manifest import main as run
    elif cmd == "v1-src":
        from .manifest import v1_src_main as run
    elif cmd == "a-class-check":
        from .aclass import main as run
    elif cmd == "tape-summary":
        from .vlm_tape import main as run
    elif cmd == "pack":
        from .manifest import pack_main as run
    elif cmd == "archive":
        from .archive import main as run
    elif cmd == "fetch":
        from .archive import fetch_main as run
    else:
        print(f"unknown command {cmd!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    return run(rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
