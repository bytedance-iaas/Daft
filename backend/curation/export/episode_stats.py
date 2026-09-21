"""Per-episode statistics for ``meta/episodes_stats.jsonl`` (LeRobot v2.1).

The official v2.1 loader (lerobot 0.3.x) *requires* that file: without it
``LeRobotDatasetMetadata`` falls back to downloading the dataset from the Hub and
fails.  v1's exporter only copies it when the source has it
(``lerobot_writer._copy_v2_stats``), so a v2.1 source without it gave a delivery the
official loader cannot open.  When the source lacks it, the exporter computes it
here the way lerobot does (``lerobot.datasets.compute_stats.compute_episode_stats``):

* numeric features: min / max / mean / std over the frames, ``count`` = frames;
  per-frame scalars keep a length-1 axis;
* video features: sampled frames (lerobot's sample count and down-sampling), stats
  per channel over the sampled pixels, scaled to [0, 1], shape (3, 1, 1).

Video frames come from the delivered mp4 (the same bytes as the source in v2), not
from the raw frames lerobot would have used before encoding; the values differ by
the codec's loss, which is irrelevant for normalisation.
"""
from __future__ import annotations

import numpy as np

_SKIP_DTYPES = ("video", "image", "string")


def _stack(col) -> np.ndarray:
    values = col.to_numpy() if hasattr(col, "to_numpy") else np.asarray(col)
    if values.dtype == object:
        return np.stack([np.asarray(v) for v in values])
    return values


def _feature_stats(arr: np.ndarray, axis, keepdims: bool) -> dict:
    return {"min": np.min(arr, axis=axis, keepdims=keepdims),
            "max": np.max(arr, axis=axis, keepdims=keepdims),
            "mean": np.mean(arr, axis=axis, keepdims=keepdims),
            "std": np.std(arr, axis=axis, keepdims=keepdims),
            "count": np.array([len(arr)])}


def numeric_stats(df, features: dict) -> dict:
    """Stats of every non-visual feature present as a column of the frame table."""
    out = {}
    for key, ft in features.items():
        if ft.get("dtype") in _SKIP_DTYPES or key not in df.columns:
            continue
        arr = _stack(df[key])
        out[key] = _feature_stats(arr, axis=0, keepdims=arr.ndim == 1)
    return out


def estimate_num_samples(n: int, min_num_samples: int = 100, max_num_samples: int = 10_000,
                         power: float = 0.75) -> int:
    if n < min_num_samples:
        min_num_samples = n
    return max(min_num_samples, min(int(n ** power), max_num_samples))


def sample_indices(n: int) -> list[int]:
    k = estimate_num_samples(n)
    return np.round(np.linspace(0, n - 1, k)).astype(int).tolist()


def _downsample(img: np.ndarray, target_size: int = 150, max_size_threshold: int = 300) -> np.ndarray:
    _, h, w = img.shape
    if max(w, h) < max_size_threshold:
        return img
    f = int(w / target_size) if w > h else int(h / target_size)
    return img[:, ::f, ::f]


def video_stats(media: str, n_frames: int) -> dict | None:
    """Stats of one camera's episode video (``media``: a local path or a URL PyAV can open)."""
    import av

    if n_frames <= 0:
        return None
    wanted = set(sample_indices(n_frames))
    frames = []
    with av.open(media) as container:
        stream = container.streams.video[0]
        for i, frame in enumerate(container.decode(stream)):
            if i in wanted:
                img = frame.to_ndarray(format="rgb24").transpose(2, 0, 1)   # channel first
                frames.append(_downsample(img))
            if i >= n_frames - 1:
                break
    if not frames:
        return None
    arr = np.stack(frames).astype(np.uint8)
    st = _feature_stats(arr, axis=(0, 2, 3), keepdims=True)
    return {k: v if k == "count" else np.squeeze(v / 255.0, axis=0) for k, v in st.items()}


def to_json(stats: dict) -> dict:
    return {key: {k: np.asarray(v).tolist() for k, v in ft.items()} for key, ft in stats.items()}
