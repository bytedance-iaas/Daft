# In this experiment we add a third similarity space: a fused embedding made by
# concatenating normalized Embed1 video embeddings with normalized state-action features.

from __future__ import annotations

import os

import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoProcessor

MODEL_ID = "nvidia/Cosmos-Embed1-224p"
EPISODE_DIR = "episodes_full"

WINDOW_FRAMES = 8
NUM_WINDOWS_PER_EPISODE = 8

# Tune these
VIDEO_WEIGHT = 1.0
STATE_ACTION_WEIGHT = 1.0


def load_model():
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print("Device:", device)

    processor = AutoProcessor.from_pretrained(
        MODEL_ID,
        trust_remote_code=True,
    )

    model = AutoModel.from_pretrained(
        MODEL_ID,
        trust_remote_code=True,
    ).to(device)

    model.eval()
    return processor, model, device


def sample_windows(video, num_windows=8, window_frames=8):
    """Sample evenly spaced windows spanning the full episode.

    video: [T, C, H, W]
    returns: [num_windows, window_frames, C, H, W]
    """
    T = video.shape[0]

    if T <= window_frames:
        idx = torch.linspace(0, T - 1, steps=window_frames).long()
        clip = video[idx]
        return clip.unsqueeze(0).repeat(num_windows, 1, 1, 1, 1)

    starts = torch.linspace(0, T - window_frames, steps=num_windows).long()
    windows = []

    for s in starts:
        windows.append(video[s : s + window_frames])

    return torch.stack(windows, dim=0)


def embed1_episode(obj, processor, model, device):
    video = obj["video"] if "video" in obj else obj["embed1_video"]

    # Convert float16 back to float32 for processor/model safety
    video = video.float()

    windows = sample_windows(
        video,
        num_windows=NUM_WINDOWS_PER_EPISODE,
        window_frames=WINDOW_FRAMES,
    )

    inputs = processor(
        videos=windows,
        return_tensors="pt",
    )

    inputs = {k: v.to(device) for k, v in inputs.items() if torch.is_tensor(v)}

    with torch.no_grad():
        outputs = model.get_video_embeddings(**inputs)
        emb = outputs.visual_proj  # [num_windows, 256]
        emb = emb / emb.norm(dim=-1, keepdim=True)

        pooled = emb.mean(dim=0)
        pooled = pooled / pooled.norm()

    return pooled.cpu()


def downsample_sequence(x, n=128):
    T = x.shape[0]
    idx = torch.linspace(0, T - 1, steps=n).long()
    return x[idx]


def state_action_feature(obj):
    state = downsample_sequence(obj["state"].float(), 128)
    action = downsample_sequence(obj["action"].float(), 128)

    # Add both absolute values and temporal deltas
    state_delta = state[1:] - state[:-1]
    action_delta = action[1:] - action[:-1]

    feat = torch.cat(
        [
            state.flatten(),
            action.flatten(),
            state_delta.flatten(),
            action_delta.flatten(),
        ],
        dim=0,
    )

    feat = feat.float()
    feat = feat / (feat.norm() + 1e-12)
    return feat


def fused_feature(video_emb, state_action_emb):
    v = video_emb / (video_emb.norm() + 1e-12)
    sa = state_action_emb / (state_action_emb.norm() + 1e-12)

    fused = torch.cat(
        [
            VIDEO_WEIGHT * v,
            STATE_ACTION_WEIGHT * sa,
        ],
        dim=0,
    )

    fused = fused / (fused.norm() + 1e-12)
    return fused


def cosine(a, b):
    return F.cosine_similarity(
        a.unsqueeze(0),
        b.unsqueeze(0),
    ).item()


def main():
    processor, model, device = load_model()

    paths = sorted(
        os.path.join(EPISODE_DIR, x) for x in os.listdir(EPISODE_DIR) if x.endswith(".pt") and x.startswith("episode_")
    )

    print("Found episodes:", len(paths))
    print("VIDEO_WEIGHT:", VIDEO_WEIGHT)
    print("STATE_ACTION_WEIGHT:", STATE_ACTION_WEIGHT)

    items = []

    for path in paths:
        obj = torch.load(path, map_location="cpu")

        print(
            "Processing",
            path,
            "episode=",
            obj["metadata"]["episode_index"],
            "frames=",
            obj["metadata"]["num_frames"],
        )

        video_emb = embed1_episode(obj, processor, model, device)
        sa_emb = state_action_feature(obj)
        fused_emb = fused_feature(video_emb, sa_emb)

        items.append(
            {
                "path": path,
                "episode": obj["metadata"]["episode_index"],
                "video_emb": video_emb,
                "state_action_emb": sa_emb,
                "fused_emb": fused_emb,
            }
        )

    print("\nPairwise comparison:")
    print(
        "episode_a, episode_b, "
        "embed1_video_similarity, "
        "state_action_similarity, "
        "fused_similarity, "
        "video_minus_state_action_gap"
    )

    rows = []

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            video_sim = cosine(items[i]["video_emb"], items[j]["video_emb"])
            sa_sim = cosine(items[i]["state_action_emb"], items[j]["state_action_emb"])
            fused_sim = cosine(items[i]["fused_emb"], items[j]["fused_emb"])
            gap = video_sim - sa_sim

            row = {
                "episode_a": items[i]["episode"],
                "episode_b": items[j]["episode"],
                "video_sim": video_sim,
                "state_action_sim": sa_sim,
                "fused_sim": fused_sim,
                "gap": gap,
            }
            rows.append(row)

            print(
                row["episode_a"],
                row["episode_b"],
                round(video_sim, 6),
                round(sa_sim, 6),
                round(fused_sim, 6),
                round(gap, 6),
            )

    rows = sorted(rows, key=lambda r: r["gap"], reverse=True)

    print("\nTop disagreement candidates:")
    for r in rows[:10]:
        print(
            f"episodes {r['episode_a']} vs {r['episode_b']} | "
            f"video={r['video_sim']:.6f} | "
            f"state_action={r['state_action_sim']:.6f} | "
            f"fused={r['fused_sim']:.6f} | "
            f"gap={r['gap']:.6f}"
        )


if __name__ == "__main__":
    main()
