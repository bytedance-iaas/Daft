# Due to dependency issues we ended up creating two distinct environments for
# HuggingFace/LeRobot (curator) & cosmos/emded1 (curator_embed1_test).
# Firstly, using the 1st environment, we extract few episodes from the LeRobot dataset
# (in .pt format). These episodes (.pt files) are fed to the subsequent embed1 environment
# for determining similarity across them.

from __future__ import annotations

import os

import torch

os.environ["LEROBOT_VIDEO_BACKEND"] = "pyav"

from lerobot.datasets.lerobot_dataset import LeRobotDataset

DATASET_ID = "lerobot/aloha_static_coffee"
CAMERA_KEY = "observation.images.cam_high"
OUTPUT_DIR = "episodes_full"

MAX_EPISODES = 5  # start small; full video takes space
STORE_VIDEO_FLOAT16 = True


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    dataset = LeRobotDataset(
        DATASET_ID,
        video_backend="pyav",
    )

    current_episode = None
    buffer = None
    saved = 0

    for idx in range(len(dataset)):
        sample = dataset[idx]
        episode_index = int(sample["episode_index"].item())

        if current_episode is None:
            current_episode = episode_index
            buffer = new_buffer()

        if episode_index != current_episode:
            save_episode(current_episode, buffer)
            saved += 1

            if saved >= MAX_EPISODES:
                break

            current_episode = episode_index
            buffer = new_buffer()

        buffer["images"].append(sample[CAMERA_KEY].detach().cpu())
        buffer["states"].append(sample["observation.state"].detach().cpu())
        buffer["actions"].append(sample["action"].detach().cpu())
        buffer["timestamps"].append(float(sample["timestamp"].item()))
        buffer["frame_indices"].append(int(sample["frame_index"].item()))
        buffer["dataset_indices"].append(int(sample["index"].item()))

    if buffer is not None and saved < MAX_EPISODES:
        save_episode(current_episode, buffer)


def new_buffer():
    return {
        "images": [],
        "states": [],
        "actions": [],
        "timestamps": [],
        "frame_indices": [],
        "dataset_indices": [],
    }


def sample_for_embed1(video, num_frames=8):
    T = video.shape[0]
    indices = torch.linspace(0, T - 1, steps=num_frames).long()
    return video[indices], indices.tolist()


def save_episode(episode_index, buffer):
    full_video = torch.stack(buffer["images"], dim=0)  # [T,C,H,W]

    if STORE_VIDEO_FLOAT16:
        full_video = full_video.to(torch.float16)

    state = torch.stack(buffer["states"], dim=0)
    action = torch.stack(buffer["actions"], dim=0)

    # Keep this too for backward compatibility with your old scripts.
    embed1_video, embed1_indices = sample_for_embed1(full_video, 8)

    out = {
        "video": full_video,  # full episode video [T,C,H,W]
        "embed1_video": embed1_video,  # 8-frame summary [8,C,H,W]
        "state": state,  # full state trajectory [T,state_dim]
        "action": action,  # full action trajectory [T,action_dim]
        "metadata": {
            "dataset_id": DATASET_ID,
            "camera_key": CAMERA_KEY,
            "episode_index": episode_index,
            "num_frames": len(buffer["images"]),
            "frame_indices": buffer["frame_indices"],
            "dataset_indices": buffer["dataset_indices"],
            "timestamps": buffer["timestamps"],
            "video_dtype": str(full_video.dtype),
            "embed1_sample_indices": embed1_indices,
        },
    }

    path = os.path.join(
        OUTPUT_DIR,
        f"episode_{episode_index:06d}.pt",
    )

    torch.save(out, path)

    size_gb = os.path.getsize(path) / (1024**3)

    print(
        f"saved {path} "
        f"size={size_gb:.2f}GB "
        f"video={tuple(out['video'].shape)} "
        f"video_dtype={out['video'].dtype} "
        f"state={tuple(out['state'].shape)} "
        f"action={tuple(out['action'].shape)}"
    )


if __name__ == "__main__":
    main()
