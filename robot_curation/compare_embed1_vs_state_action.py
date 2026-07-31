# Episodes extracted (as .pt files) earlier are fed to the embed1 environment
#  for determining similarity across them.

from __future__ import annotations

import os

import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoProcessor

MODEL_ID = "nvidia/Cosmos-Embed1-224p"
EPISODE_DIR = "episodes_full"

WINDOW_FRAMES = 8
NUM_WINDOWS_PER_EPISODE = 8


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


def sample_windows_from_episode_video(video, num_windows=8, window_frames=8):
    """Sample evenly spaced windows spanning the full episode.

    video: [T, C, H, W]
    returns: [num_windows, window_frames, C, H, W]
    """
    T = video.shape[0]

    windows = []

    if T <= window_frames:
        idx = torch.linspace(0, T - 1, steps=window_frames).long()
        clip = video[idx]
        for _ in range(num_windows):
            windows.append(clip)
        return torch.stack(windows, dim=0)

    start_positions = torch.linspace(
        0,
        T - window_frames,
        steps=num_windows,
    ).long()

    for start in start_positions:
        end = start + window_frames
        windows.append(video[start:end])

    return torch.stack(windows, dim=0)


def embed_windows(windows, processor, model, device):
    """Embed each window and average-pool into a single episode embedding.

    windows: [N, 8, C, H, W]
    returns pooled episode embedding: [256]
    """
    inputs = processor(
        videos=windows,
        return_tensors="pt",
    )

    inputs = {k: v.to(device) for k, v in inputs.items() if torch.is_tensor(v)}

    with torch.no_grad():
        outputs = model.get_video_embeddings(**inputs)
        emb = outputs.visual_proj  # [N, 256]
        emb = emb / emb.norm(dim=-1, keepdim=True)

        # Average-pool window embeddings into one episode embedding
        pooled = emb.mean(dim=0)
        pooled = pooled / pooled.norm()

    return pooled.cpu()


def embed1_episode_embedding(obj, processor, model, device):
    # IMPORTANT:
    # This uses full video if present.
    # If not present, it falls back to embed1_video.
    if "video" in obj:
        video = obj["video"]
    else:
        video = obj["embed1_video"]

    windows = sample_windows_from_episode_video(
        video,
        num_windows=NUM_WINDOWS_PER_EPISODE,
        window_frames=WINDOW_FRAMES,
    )

    return embed_windows(windows, processor, model, device)


def downsample_sequence(x, n=128):
    T = x.shape[0]
    idx = torch.linspace(0, T - 1, steps=n).long()
    return x[idx]


def state_action_feature(obj):
    state = downsample_sequence(obj["state"], 128)
    action = downsample_sequence(obj["action"], 128)

    feat = torch.cat(
        [
            state.flatten(),
            action.flatten(),
        ],
        dim=0,
    )

    feat = feat.float()
    feat = feat / (feat.norm() + 1e-12)
    return feat


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

        items.append(
            {
                "path": path,
                "episode": obj["metadata"]["episode_index"],
                "embed1": embed1_episode_embedding(
                    obj,
                    processor,
                    model,
                    device,
                ),
                "state_action": state_action_feature(obj),
            }
        )

    print("\nPairwise comparison:")
    print("episode_a, episode_b, embed1_window_pooled_similarity, state_action_similarity, gap")

    candidates = []

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            e_sim = cosine(items[i]["embed1"], items[j]["embed1"])
            sa_sim = cosine(items[i]["state_action"], items[j]["state_action"])
            gap = e_sim - sa_sim

            print(
                items[i]["episode"],
                items[j]["episode"],
                round(e_sim, 6),
                round(sa_sim, 6),
                round(gap, 6),
            )

            candidates.append(
                {
                    "episode_a": items[i]["episode"],
                    "episode_b": items[j]["episode"],
                    "embed1_similarity": e_sim,
                    "state_action_similarity": sa_sim,
                    "gap": gap,
                }
            )

    candidates = sorted(
        candidates,
        key=lambda x: x["gap"],
        reverse=True,
    )

    print("\nTop candidates: visually similar but state/action different")
    for c in candidates[:10]:
        print(
            f"episodes {c['episode_a']} vs {c['episode_b']} | "
            f"embed1={c['embed1_similarity']:.6f} | "
            f"state_action={c['state_action_similarity']:.6f} | "
            f"gap={c['gap']:.6f}"
        )


if __name__ == "__main__":
    main()
