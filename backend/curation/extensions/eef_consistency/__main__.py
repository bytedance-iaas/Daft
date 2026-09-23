"""``python -m curation.extensions.eef_consistency <command>`` - offline entry points of the module.

validate   check a trajectory.json (container, Schemas, semantics, media, self-consistency) and print
           the validation report plus the per-episode capability table
run        assess episodes offline: one detail line per episode (JSON Lines) plus observations, curves and
           evidence under --out
export     write trajectory.json from a LeRobot dataset's columns by an explicit eef-mapping/1.0 file
           (design 12 §3.3; a convenience, the platform itself only reads the uploaded file)
template-build   a gripper template (gripper-template/1.0, F5.8) from a few seed / clicked rows
template-check   the re-detector alone over the clips: detection rate, and errors against seed rows
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np

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
    template = None
    if a.template:
        from . import template as TP

        try:
            template = TP.load_template(a.template)
        except TP.TemplateError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    cfg = runner.RunConfig(lerobot_root=a.lerobot_root, seed_root=a.seeds, profile=profile.load(a.profile),
                           out_dir=a.out, evidence_mode=a.evidence_mode, template=template)
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "details.jsonl", "w", encoding="utf-8") as fh:
        for ep, s in sorted(r.samples.items()):
            detail, _ = runner.run_episode(s, cfg)
            fh.write(json.dumps({"episode_index": ep, "detail": detail}, ensure_ascii=False) + "\n")
            summary = {k: v["status"] for k, v in detail["summary"].items()}
            print(json.dumps({"episode_index": ep, "overall": detail["overall"], "summary": summary}), flush=True)
    return 0


def _export(a: argparse.Namespace) -> int:
    from .adapters import lerobot_mapping as M

    try:
        bundle = M.export(M.load_mapping(a.mapping), a.lerobot_root, episodes=a.episodes)
    except M.MappingError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    data = json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode()
    r = load.load_bundle(data, lerobot_root=a.lerobot_root)
    if not r.ok:                                    # never write a file the platform would refuse
        json.dump({"report": r.report}, sys.stdout, ensure_ascii=False, indent=1, default=str)
        return 1
    with open(a.out, "wb") as fh:
        fh.write(data)
    print(json.dumps({"out": a.out, "sha256": r.sha256, "samples": len(r.samples),
                      "frames": r.report["total_frames"]}))
    return 0


MASK_MAX_GAP = 60                                    # frames the builder keeps between two anchors for the mask


def _template_build(a: argparse.Namespace) -> int:
    """A gripper template from seed / clicked rows: every ``--every`` frames one entry per camera."""
    from . import observations as O
    from . import template as TP
    from . import video as V

    r = load.load_bundle(a.trajectory, lerobot_root=a.lerobot_root, episodes=a.episodes)
    if not r.ok:
        json.dump({"report": r.report}, sys.stdout, ensure_ascii=False, indent=1, default=str)
        return 1
    entries = []
    masked = skipped_static = 0
    for ep, s in sorted(r.samples.items()):
        for cid in s.cameras:
            if a.cameras and cid not in a.cameras:
                continue
            seeds = O.find_seeds(a.seeds, s.sample_id, cid)
            if seeds is None:
                continue
            chosen, last = {}, None
            for f in sorted(seeds.by_media_frame):
                pts = {pid: sp.uv for pid, sp in seeds.by_media_frame[f].items()
                       if sp.visibility == O.VISIBLE and sp.uv is not None}
                if len(pts) < a.min_points or (last is not None and f - last < a.every):
                    continue
                chosen[f], last = pts, f
            if not chosen:
                continue
            method = TP.PROVENANCE_OF_ROW_METHOD.get(seeds.method.split("+")[0], "tool_export")
            ctx, _ = O.provider_inputs(s, cid, media_root=a.lerobot_root, seeds=None, point_ids=[])
            grabbed, segments, buf, prev_f = {}, {}, None, None
            for fr in V.iter_clip(ctx.media_path, clip_start_s=ctx.clip_start_s, clip_end_s=ctx.clip_end_s, fps=ctx.fps,
                                  frame_count=ctx.media_frame_count):
                if fr.index in chosen:
                    grabbed[fr.index] = (fr.gray, fr.sha256())
                    if prev_f is not None and buf is not None:
                        segments[(prev_f, fr.index)] = buf + [fr.gray]
                    buf, prev_f = [fr.gray], fr.index
                elif buf is not None:
                    buf.append(fr.gray)
                    if len(buf) > MASK_MAX_GAP:                 # anchors too far apart for the tracker
                        buf = None
                if fr.index >= max(chosen):
                    break
            order = sorted(grabbed)
            for k, f in enumerate(order):
                gray, sha = grabbed[f]
                # the gripper's features: those the tracker carries rigidly with the points to a neighbouring anchor
                mask = None
                if k + 1 < len(order) and (f, order[k + 1]) in segments:
                    mask = TP.rigid_member_mask(segments[(f, order[k + 1])], chosen[f], chosen[order[k + 1]])
                if mask is None and k > 0 and (order[k - 1], f) in segments:
                    mask = TP.rigid_member_mask(segments[(order[k - 1], f)][::-1], chosen[f], chosen[order[k - 1]])
                if mask is None and not a.keep_unmasked:
                    skipped_static += 1                     # no neighbouring anchor with enough motion for a mask
                    continue
                masked += mask is not None
                entries.append(TP.make_entry(
                    gray, chosen[f], entry_id=f"{s.sample_id}-{cid}-{f:06d}", camera_id=cid,
                    source={"dataset": a.dataset, "sample_id": s.sample_id, "camera_id": cid, "frame_index": f,
                            "image_sha256": sha},
                    method=method, margin_px=a.margin, mask=mask, note=f"from seed rows ({seeds.method})"))
    if not entries:
        print("error: no seed rows with enough visible points; nothing to build", file=sys.stderr)
        return 2
    point_ids = sorted({pid for e in entries for pid in e["points_patch_px"]})
    # the inlier floor follows the entries' feature counts: 1280x720 grippers give hundreds of features
    # (floor 20, 5 %), small or low-texture patches fewer (floor 8, 2 %)
    probe = TP.load_template(TP.make_template(entries, tool_name=a.tool_name, point_ids=point_ids,
                                              matching={"min_inliers": 4, "min_inlier_ratio": 0.0}))
    median_features = float(np.median([len(e.descriptors) for e in probe.entries]))
    matching = {"every_frames": a.redetect_every,
                "min_inliers": int(min(20, max(8, round(0.06 * median_features)))),
                "min_inlier_ratio": 0.05 if median_features >= 300 else 0.02}
    tmpl = TP.make_template(entries, tool_name=a.tool_name, point_ids=point_ids, matching=matching,
                            notes=["built by python -m curation.extensions.eef_consistency template-build; entries from "
                                   "seed rows, so their provenance is the rows' (synthetic_fixture entries are DEMO only)"])
    loaded = TP.load_template(tmpl)                 # never write a file the platform would refuse
    data = json.dumps(tmpl, ensure_ascii=False, allow_nan=False).encode()
    with open(a.out, "wb") as fh:
        fh.write(data)
    usable = sum(len(e.descriptors) >= loaded.matching.min_inliers for e in loaded.entries)
    print(json.dumps({"out": a.out, "entries": len(entries), "usable_entries": usable, "masked_entries": masked, "skipped_static": skipped_static,
                      "matching": matching, "median_features": median_features, "point_ids": point_ids,
                      "cameras": sorted({e["camera_id"] for e in entries}), "methods": list(loaded.methods),
                      "size_bytes": len(data)}))
    return 0


def _template_check(a: argparse.Namespace) -> int:
    """The re-detector alone over the episodes' clips; with --seeds, errors against the seed rows."""
    from . import observations as O
    from . import template as TP
    from . import video as V

    try:
        tmpl = TP.load_template(a.template)
    except TP.TemplateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    r = load.load_bundle(a.trajectory, lerobot_root=a.lerobot_root, episodes=a.episodes)
    if not r.ok:
        json.dump({"report": r.report}, sys.stdout, ensure_ascii=False, indent=1, default=str)
        return 1
    for ep, s in sorted(r.samples.items()):
        for cid in s.cameras:
            ref = None
            if a.seeds:
                seeds = O.find_seeds(a.seeds, s.sample_id, cid)
                if seeds is not None:
                    ref = {f: {pid: sp.uv for pid, sp in pts.items() if sp.uv is not None}
                           for f, pts in seeds.by_media_frame.items()}
            ctx, _ = O.provider_inputs(s, cid, media_root=a.lerobot_root, seeds=None, point_ids=[])
            frames = V.iter_clip(ctx.media_path, clip_start_s=ctx.clip_start_s, clip_end_s=ctx.clip_end_s, fps=ctx.fps,
                                 frame_count=ctx.media_frame_count)
            out = TP.check(tmpl, cid, frames, every=a.every, reference=ref)
            print(json.dumps({"episode_index": ep, **out}), flush=True)
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
    r.add_argument("--template", default=None,
                   help="gripper template (gripper-template/1.0): automatic anchors for cameras without seeds")
    r.add_argument("--profile", default="demo", help="threshold profile name or YAML path; 'none' = curves only")
    r.add_argument("--episodes", type=int, nargs="*", default=None)
    r.add_argument("--evidence-mode", choices=["flagged", "all", "off"], default="flagged")
    r.add_argument("--out", required=True)
    r.set_defaults(fn=_run)
    x = sub.add_parser("export", help="trajectory.json from LeRobot columns by a mapping file")
    x.add_argument("--mapping", required=True, help="eef-mapping/1.0 file (YAML or JSON)")
    x.add_argument("--lerobot-root", required=True)
    x.add_argument("--episodes", type=int, nargs="*", default=None)
    x.add_argument("--out", required=True)
    x.set_defaults(fn=_export)
    b = sub.add_parser("template-build", help="gripper template from seed / clicked observation rows")
    b.add_argument("--trajectory", required=True)
    b.add_argument("--lerobot-root", required=True)
    b.add_argument("--seeds", required=True, help="observation rows directory (<sample_id>/<camera_id>.jsonl)")
    b.add_argument("--episodes", type=int, nargs="*", default=None)
    b.add_argument("--cameras", nargs="*", default=None)
    b.add_argument("--every", type=int, default=30, help="minimum frames between two entries of one camera")
    b.add_argument("--min-points", type=int, default=2, help="visible points a row needs to become an entry")
    b.add_argument("--margin", type=int, default=70, help="patch margin around the points, pixels")
    b.add_argument("--redetect-every", type=int, default=15, help="matching.every_frames written into the template")
    b.add_argument("--tool-name", default="gripper")
    b.add_argument("--keep-unmasked", action="store_true",
                   help="keep entries without a gripper mask (no neighbouring anchor with enough motion); off by default")
    b.add_argument("--dataset", default=None, help="source.dataset written into the entries")
    b.add_argument("--out", required=True)
    b.set_defaults(fn=_template_build)
    c = sub.add_parser("template-check", help="re-detection rate (and error against seeds) over the clips")
    c.add_argument("--template", required=True)
    c.add_argument("--trajectory", required=True)
    c.add_argument("--lerobot-root", required=True)
    c.add_argument("--seeds", default=None, help="observation rows directory to compare against")
    c.add_argument("--episodes", type=int, nargs="*", default=None)
    c.add_argument("--every", type=int, default=None, help="try every N frames (default: the template's every_frames)")
    c.set_defaults(fn=_template_check)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
