"""``python -m curation.extensions.eef_consistency <command>`` - offline entry points of the module.

validate   check a trajectory.json (container, Schemas, semantics, media, self-consistency) and print
           the validation report plus the per-episode capability table
"""
from __future__ import annotations

import argparse
import json
import sys

from . import capability as CAP
from . import load


def _validate(a: argparse.Namespace) -> int:
    r = load.load_bundle(a.trajectory, lerobot_root=a.lerobot_root, check_media=a.lerobot_root is not None)
    out = {"report": r.report}
    if r.ok:
        observable = None
        if a.all_points_observable:
            observable = {ep: {cid: set(load.declared_point_ids(s, cid)) for cid in s.cameras}
                          for ep, s in r.samples.items()}
        episodes = sorted(r.samples) if a.episodes is None else a.episodes
        out["capability"] = CAP.dataset_capability(r, episodes, observable=observable)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1, default=str)
    print()
    return 0 if r.ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m curation.extensions.eef_consistency", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="validate trajectory.json and print the capability table")
    v.add_argument("--trajectory", required=True)
    v.add_argument("--lerobot-root", default=None, help="LeRobot dataset root (directory holding meta/)")
    v.add_argument("--episodes", type=int, nargs="*", default=None)
    v.add_argument("--all-points-observable", action="store_true",
                   help="assume an observation route for every declared point (form check without seeds)")
    v.set_defaults(fn=_validate)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
