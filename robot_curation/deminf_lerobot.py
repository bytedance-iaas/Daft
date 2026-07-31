"""DemInf PoC on lerobot/aloha_static_coffee.

arXiv:2502.08623 — "Robot Data Curation with Mutual Information Estimators"

No lerobot import needed — reads the parquet files HuggingFace already cached
on disk, or downloads them on first run via the `datasets` library.
"""

from __future__ import annotations

import glob
import os
import sys

import numpy as np
from scipy.special import digamma
from sklearn.decomposition import PCA

DATASET_ID = "lerobot/aloha_static_coffee"
LATENT_DIM = 6  # PCA dims — stand-in for the paper's VAE latent
KNN_K_RANGE = (2, 7)  # Fix 2: average over k=2..6 to reduce score variance
KAPPA_PCT = 40.0  # keep demos above this percentile threshold κ
SUBSAMPLE = 8  # use every Nth frame (keeps compute fast)
RELATIVE_ACTIONS = True  # Fix 1: paper recommends relative (delta) actions

# ── 1. Load ───────────────────────────────────────────────────────────────────


def find_local_parquet(dataset_id: str) -> list[str]:
    """Locate the parquet files HuggingFace has already cached for a dataset.

    HuggingFace caches datasets under ~/.cache/huggingface/hub/
    as parquet files. Try to find them without any extra imports.
    """
    repo_slug = "datasets--" + dataset_id.replace("/", "--")
    hf_cache = os.path.expanduser(os.environ.get("HF_HOME", "~/.cache/huggingface"))
    pattern = os.path.join(hf_cache, "hub", repo_slug, "**", "*.parquet")
    files = sorted(glob.glob(pattern, recursive=True))
    return files


def load_via_datasets_lib(dataset_id: str):
    """Fallback: use the `datasets` library (pip install datasets)."""
    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: couldn't find parquet files locally and `datasets` is not installed.")
        print("  Fix option A — install datasets library:")
        print("    pip install datasets")
        print("  Fix option B — set HF_HOME to wherever you downloaded the dataset:")
        print("    export HF_HOME=/path/to/your/hf/cache")
        sys.exit(1)

    print("  (loading via datasets library — may download if not cached)")
    ds = load_dataset(dataset_id, split="train")
    return ds.to_pandas()


def load_dataframe(dataset_id: str):
    import pandas as pd

    files = find_local_parquet(dataset_id)
    if files:
        print(f"  Found {len(files)} local parquet file(s):")
        for f in files[:3]:
            print(f"    {f}")
        if len(files) > 3:
            print(f"    … and {len(files) - 3} more")
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    else:
        print("  No local parquet files found — trying datasets library …")
        df = load_via_datasets_lib(dataset_id)

    return df


def load_episodes(dataset_id: str) -> list[dict]:
    print(f"Loading  {dataset_id} …")
    df = load_dataframe(dataset_id)

    print(f"  Columns : {list(df.columns)}")
    print(f"  Rows    : {len(df)}")

    # Identify state column (skip image columns)
    state_col = next((c for c in df.columns if "state" in c and "image" not in c), None)
    if state_col is None:
        raise ValueError(f"No state column found. Available: {list(df.columns)}")

    print(f"  Using   : state='{state_col}'  action='action'")

    # Each cell may be a list/array — stack into proper arrays
    def to_matrix(series):
        return np.stack(series.values).astype(np.float32)

    states_all = to_matrix(df[state_col])  # (N, state_dim)
    actions_all = to_matrix(df["action"])  # (N, action_dim)
    ep_idxs = df["episode_index"].values  # (N,)

    print(f"  State dim={states_all.shape[1]}, Action dim={actions_all.shape[1]}\n")

    episodes = []
    for ep_id in np.unique(ep_idxs):
        mask = ep_idxs == ep_id
        states = states_all[mask][::SUBSAMPLE]
        actions = actions_all[mask][::SUBSAMPLE]

        # Fix 1 — relative actions (paper's own recommendation)
        # Converts absolute joint targets → deltas from current state.
        # Removes the positional confound so MI captures behavioural intent,
        # not just where the arm happens to be.
        if RELATIVE_ACTIONS:
            actions = actions - states

        episodes.append({"ep_idx": int(ep_id), "states": states, "actions": actions})

    n_pairs = sum(len(e["states"]) for e in episodes)
    print(f"  {len(episodes)} episodes, {n_pairs} pairs after {SUBSAMPLE}x subsample\n")
    return episodes


# ── 2. Embed (PCA as VAE stand-in) ───────────────────────────────────────────


def embed_with_pca(episodes: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """Embed states and actions into a latent where kNN distances are meaningful.

    Paper uses separate VAEs for states and action chunks to get a latent
    where kNN distances are meaningful. PCA after z-scoring achieves the same
    goal for proprioceptive data without any training.
    """
    all_s = np.vstack([e["states"] for e in episodes])
    all_a = np.vstack([e["actions"] for e in episodes])

    def zscore(X):
        return (X - X.mean(0)) / (X.std(0) + 1e-8)

    all_s, all_a = zscore(all_s), zscore(all_a)

    nc = min(LATENT_DIM, all_s.shape[1], all_a.shape[1])
    pca_s = PCA(n_components=nc).fit(all_s)
    pca_a = PCA(n_components=nc).fit(all_a)

    print(f"Embedding  (PCA → {nc}d)")
    print(f"  State  variance explained : {pca_s.explained_variance_ratio_.sum():.1%}")
    print(f"  Action variance explained : {pca_a.explained_variance_ratio_.sum():.1%}\n")

    return pca_s.transform(all_s), pca_a.transform(all_a)


# ── 3. KSG mutual information estimator ──────────────────────────────────────


def ksg_contributions(states: np.ndarray, actions: np.ndarray, k: int) -> np.ndarray:
    """Per-sample KSG MI contribution (Kraskov et al., 2004).

    For point i:
      ρ_i  = k-NN distance in joint (s,a) space under Chebyshev metric
      n_s  = # points with state-dist  < ρ_i
      n_a  = # points with action-dist < ρ_i
      contrib = −ψ(n_s+1) − ψ(n_a+1)

    High MI (good demo): state→action is predictable, joint space is tight,
    ρ is small, n_s and n_a stay small → less negative → higher score.

    Low MI (bad demo): actions ignore or are noisy w.r.t. state, ρ must grow
    to find k neighbours → large n_s, n_a → very negative → lower score.
    """
    N = len(states)
    contribs = np.empty(N)
    for i in range(N):
        ds = np.linalg.norm(states - states[i], axis=1)
        da = np.linalg.norm(actions - actions[i], axis=1)
        d_joint = np.maximum(ds, da)  # Chebyshev
        d_joint[i] = np.inf  # exclude self
        rho = np.partition(d_joint, k - 1)[k - 1]
        n_s = int(np.sum(ds < rho))
        n_a = int(np.sum(da < rho))
        contribs[i] = -digamma(n_s + 1) - digamma(n_a + 1)
    return contribs


def score_episodes(episodes, states_emb, actions_emb) -> np.ndarray:
    """Average KSG scores over a range of k values.

    A single k can be noisy (especially with small datasets). Averaging over
    k=2..6 smooths the estimates without changing which episodes rank high vs low.
    The paper found results are robust to k choice — this just reduces variance.
    """
    total = len(states_emb)
    k_min, k_max = KNN_K_RANGE
    k_values = list(range(k_min, k_max))

    print(f"Running KSG  (k={k_min}..{k_max - 1}, averaging {len(k_values)} estimates, N={total} pairs) …")

    all_contribs = []
    for k in k_values:
        print(f"  k={k} … ", end="", flush=True)
        all_contribs.append(ksg_contributions(states_emb, actions_emb, k=k))
        print("done")

    # Average contributions across all k values
    contribs = np.mean(all_contribs, axis=0)
    print()

    scores, ptr = [], 0
    for ep in episodes:
        T = len(ep["states"])
        scores.append(float(contribs[ptr : ptr + T].mean()))
        ptr += T
    return np.array(scores)


# ── 4. Report & filter ────────────────────────────────────────────────────────


def report(episodes, scores):
    ranked = np.argsort(scores)[::-1]
    mn, mx = scores.min(), scores.max()

    print(f"{'Rank':>5}  {'Episode':>8}  {'MI Score':>10}  {'Frames':>7}  bar")
    print("─" * 55)
    for rank, idx in enumerate(ranked):
        ep = episodes[idx]
        frac = (scores[idx] - mn) / (mx - mn + 1e-9)
        bar = "▓" * round(frac * 12)
        print(f"{rank + 1:>5}  {ep['ep_idx']:>8}  {scores[idx]:>10.4f}  {len(ep['states']):>7}  {bar}")

    threshold = np.percentile(scores, KAPPA_PCT)
    kept = [ep["ep_idx"] for ep, m in zip(episodes, scores >= threshold) if m]
    drop = [ep["ep_idx"] for ep, m in zip(episodes, scores >= threshold) if not m]

    print(f"\nFilter κ  : top {100 - KAPPA_PCT:.0f}%  (threshold = {threshold:.4f})")
    print(f"Kept      : {len(kept)} / {len(episodes)} episodes")
    print(f"Top    5  : episode IDs {[episodes[i]['ep_idx'] for i in ranked[:5]]}")
    print(f"Bottom 5  : episode IDs {[episodes[i]['ep_idx'] for i in ranked[-5:]]}")
    print(f"\nKept    : {kept}")
    print(f"Dropped : {drop}")


def main():
    print("=" * 60)
    print("DemInf PoC  ·  lerobot/aloha_static_coffee")
    print("=" * 60, "\n")

    episodes = load_episodes(DATASET_ID)
    action_label = "relative" if RELATIVE_ACTIONS else "absolute"
    print(f"Actions      : {action_label}  ·  k range: {KNN_K_RANGE[0]}..{KNN_K_RANGE[1] - 1}\n")
    states_emb, act_emb = embed_with_pca(episodes)
    scores = score_episodes(episodes, states_emb, act_emb)

    report(episodes, scores)
    print("=" * 60)


if __name__ == "__main__":
    main()
