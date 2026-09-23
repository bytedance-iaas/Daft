"""``python -m curation.extensions.eef_consistency <command>`` - offline entry points of the module.

validate   check a trajectory.json (container, Schemas, semantics, media, self-consistency) and print
           the validation report plus the per-episode capability table
run        assess episodes offline: one detail line per episode (JSON Lines) plus observations, curves and
           evidence under --out
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


def _run(a: argparse.Namespace) -> int:
    import pathlib

    from . import profile, runner

    r = load.load_bundle(a.trajectory, lerobot_root=a.lerobot_root, episodes=a.episodes)
    if not r.ok:
        json.dump({"report": r.report}, sys.stdout, ensure_ascii=False, indent=1, default=str)
        return 1
    cfg = runner.RunConfig(lerobot_root=a.lerobot_root, seed_root=a.seeds, profile=profile.load(a.profile),
                           out_dir=a.out, evidence_mode=a.evidence_mode)
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "details.jsonl", "w", encoding="utf-8") as fh:
        for ep, s in sorted(r.samples.items()):
            detail, _ = runner.run_episode(s, cfg)
            fh.write(json.dumps({"episode_index": ep, "detail": detail}, ensure_ascii=False) + "\n")
            summary = {k: v["status"] for k, v in detail["summary"].items()}
            print(json.dumps({"episode_index": ep, "overall": detail["overall"], "summary": summary}), flush=True)
    return 0


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
    r = sub.add_parser("run", help="assess episodes offline")
    r.add_argument("--trajectory", required=True)
    r.add_argument("--lerobot-root", required=True)
    r.add_argument("--seeds", default=None, help="observation seed directory (P-A provider)")
    r.add_argument("--profile", default="demo", help="threshold profile name or YAML path; 'none' = curves only")
    r.add_argument("--episodes", type=int, nargs="*", default=None)
    r.add_argument("--evidence-mode", choices=["flagged", "all", "off"], default="flagged")
    r.add_argument("--out", required=True)
    r.set_defaults(fn=_run)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
