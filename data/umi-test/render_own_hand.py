"""Render an exported UMI episode with the production CPU overlay, without calling a VLM.

Run from the repository root with PYTHONPATH=backend; see the EEF extension README.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import time

from curation.adapters.video_input import encode_rendered_video
from curation.extensions.eef_consistency import load, opinion, review, umi


def main() -> None:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, default=root / "lerobot",
                        help="LeRobot directory produced by export-umi")
    parser.add_argument("--trajectory", type=Path, help="Default: <dataset-root>/trajectory.json")
    parser.add_argument("--episode", type=int, default=0, help="Episode index in trajectory.json")
    parser.add_argument("--out", type=Path, default=root / "history-hand")
    parser.add_argument("--max-side", type=int, default=720, help="Longest rendered image side")
    parser.add_argument("--max-mb", type=int, default=256, help="Maximum encoded bytes per camera, in MiB")
    args = parser.parse_args()
    if args.max_side < 2 or args.max_mb < 1:
        parser.error("--max-side must be >= 2 and --max-mb must be >= 1")
    loaded = load.load_bundle(args.trajectory or args.dataset_root / "trajectory.json",
                              lerobot_root=args.dataset_root)
    if not loaded.ok:
        parser.error(f"Invalid trajectory bundle: {loaded.report}")
    sample = loaded.samples.get(args.episode)
    if sample is None or not sample.hand_poses:
        parser.error(f"Episode {args.episode} is missing or has no UMI hand poses")
    args.out.mkdir(parents=True, exist_ok=True)
    items = []
    for index, (camera_id, cam) in enumerate(sorted(sample.cameras.items())):
        start = time.monotonic()
        owner = sample.sample["umi"]["camera_hands"][camera_id]
        mapping = {int(v): i for i, v in enumerate(cam.video_frame_index) if v >= 0}
        if not mapping:
            parser.error(f"Camera {camera_id} has no mapped video frames")
        # The shared renderer takes ordinary EEF marks, but its UMI branch draws
        # directly from hand_poses and camera_hands; no ordinary point/axis is used.
        marks = review.Marks(declared=None, observed=None, axis=None)
        lo, hi = min(mapping), max(mapping)
        rendered = opinion._render(sample, camera_id, marks, None, str(args.dataset_root),
                                   lo, hi, mapping, args.max_side)
        clip = encode_rendered_video(f"{camera_id} MARKED", rendered, fps=cam.media["fps"],
                                     end_s=(hi + 1) / cam.media["fps"], max_bytes=args.max_mb * 1024 * 1024)
        path = args.out / f"ep_{args.episode:06d}_camera{index}_marked.mp4"
        path.write_bytes(base64.b64decode(clip.url.split(",", 1)[1]))
        items.append({**clip.metadata(), "camera": camera_id, "hand": owner, "file": path.name})
        print(f"{camera_id} ({owner}): {path}, {time.monotonic() - start:.2f}s", flush=True)
    manifest = {"episode_index": args.episode, "prompt_version": umi.PROMPT_VERSION,
                "horizon_s": sample.sample["umi"]["horizon_s"], "videos": items}
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
