"""``python -m parity <command> [args]``."""
from __future__ import annotations

import sys

USAGE = """usage: python -m parity <command> [args]

commands:
  run-v2        run the v2 command chain on a dataset (taped model calls)
  compare       compare a candidate v2 run directory with the golden one
  make-fixture  write the synthetic 8-episode dataset (LeRobot, mcap or lance)
  a-class-check fail when algorithm (A-class) files differ from the freeze commit
  tape-summary  counts and failures of a VLM tape
"""


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "run-v2":
        from .v2run import main as run
    elif cmd == "compare":
        from .compare import main as run
    elif cmd == "make-fixture":
        from .fixtures import main as run
    elif cmd == "a-class-check":
        from .aclass import main as run
    elif cmd == "tape-summary":
        from .vlm_tape import main as run
    else:
        print(f"unknown command {cmd!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    return run(rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
