"""A LeRobot v2.1 dataset with many H.264 cameras, for the player's grid and sync checks (design doc 19 §2).

Every frame burns in its camera, episode, frame number and time, so a screenshot shows whether the cells
agree; state and action are two arms of six joints and a gripper each. The F13.4 drift set was the same
with three 1280x720 cameras.

    ../.venv/bin/python scripts/make_cams_dataset.py $L/inputs/cams_10 --cameras 10 --fps 10 --frames 200
    ../.venv/bin/python scripts/make_cams_dataset.py $L/inputs/cams_16 --cameras 16 --fps 30 --frames 300
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np

NAMES = ([f"left_joint_{i}" for i in range(6)] + ["left_gripper"]
         + [f"right_joint_{i}" for i in range(6)] + ["right_gripper"])
TASK = "move both arms in sync"


def video(path: str, label: str, cam: int, ep: int, frames: int, fps: int, w: int, h: int) -> None:
    import av
    import cv2

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with av.open(path, "w") as out:
        st = out.add_stream("libx264", rate=fps)
        st.width, st.height, st.pix_fmt = w, h, "yuv420p"
        st.options = {"preset": "ultrafast", "crf": "28", "g": str(fps), "bf": "0"}
        scale = h / 300
        for f in range(frames):
            img = np.full((h, w, 3), ((30 + cam * 37) % 200, 40 + (cam * 23) % 120, 60), np.uint8)
            x = int(f / frames * w)
            img[:, max(0, x - 3):x + 3] = (255, 200, 0)
            cv2.putText(img, f"{label}  ep {ep}  frame {f}", (20, h // 3), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2)
            cv2.putText(img, f"t = {f / fps:6.3f} s", (20, 2 * h // 3), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2)
            for p in st.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                out.mux(p)
        for p in st.encode():
            out.mux(p)


def main() -> None:
    import pandas as pd

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root")
    ap.add_argument("--cameras", type=int, default=10)
    ap.add_argument("--episodes", type=int, default=2)
    ap.add_argument("--frames", type=int, default=200)
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=360)
    a = ap.parse_args()
    cams = [f"cam_{i:02d}" for i in range(a.cameras)]
    feats = {
        "observation.state": {"dtype": "float32", "shape": [14], "names": {"motors": NAMES}},
        "action": {"dtype": "float32", "shape": [14], "names": {"motors": NAMES}},
        "timestamp": {"dtype": "float32", "shape": [1], "names": None},
        "frame_index": {"dtype": "int64", "shape": [1], "names": None},
        "episode_index": {"dtype": "int64", "shape": [1], "names": None},
        "index": {"dtype": "int64", "shape": [1], "names": None},
        "task_index": {"dtype": "int64", "shape": [1], "names": None},
    }
    for c in cams:
        feats[f"observation.images.{c}"] = {
            "dtype": "video", "shape": [a.height, a.width, 3], "names": ["height", "width", "channel"],
            "info": {"video.codec": "h264", "video.width": a.width, "video.height": a.height, "video.fps": a.fps,
                     "video.pix_fmt": "yuv420p"}}
    info = {"codebase_version": "v2.1", "robot_type": "dual_arm", "total_episodes": a.episodes,
            "total_frames": a.episodes * a.frames, "total_tasks": 1, "chunks_size": 1000, "fps": a.fps,
            "splits": {"train": f"0:{a.episodes}"},
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
            "features": feats}
    os.makedirs(os.path.join(a.root, "meta"), exist_ok=True)
    with open(os.path.join(a.root, "meta", "info.json"), "w") as fh:
        json.dump(info, fh, indent=1)
    with open(os.path.join(a.root, "meta", "tasks.jsonl"), "w") as fh:
        fh.write(json.dumps({"task_index": 0, "task": TASK}) + "\n")
    with open(os.path.join(a.root, "meta", "episodes.jsonl"), "w") as fh:
        for ep in range(a.episodes):
            fh.write(json.dumps({"episode_index": ep, "tasks": [TASK], "length": a.frames}) + "\n")
    os.makedirs(os.path.join(a.root, "data", "chunk-000"), exist_ok=True)
    for ep in range(a.episodes):
        t = np.arange(a.frames) / a.fps
        state = np.stack([np.sin(t * (0.5 + 0.1 * j) + ep) for j in range(14)], axis=1).astype(np.float32)
        df = pd.DataFrame({"observation.state": list(state), "action": list((state + 0.02).astype(np.float32)),
                           "timestamp": t.astype(np.float32), "frame_index": np.arange(a.frames), "episode_index": ep,
                           "index": np.arange(a.frames) + ep * a.frames, "task_index": 0})
        df.to_parquet(os.path.join(a.root, "data", "chunk-000", f"episode_{ep:06d}.parquet"))
        for i, c in enumerate(cams):
            video(os.path.join(a.root, "videos", "chunk-000", f"observation.images.{c}", f"episode_{ep:06d}.mp4"),
                  c, i, ep, a.frames, a.fps, a.width, a.height)
    print(a.root)


if __name__ == "__main__":
    main()
