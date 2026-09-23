"""F5.2 on the DEMO data: the P-A tracker keeps its coverage and error floor (synthetic_fixture seeds).

The dense truth under ``<dataset>/evaluation/`` is read here only to measure the tracker; the module
itself never sees it.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from curation.extensions.eef_consistency import load
from curation.extensions.eef_consistency import observations as O
from curation.extensions.eef_consistency import tracking as T
from curation.extensions.eef_consistency import video as V

from . import demo_data


def _truth(name, sample_id, cam, n):
    path = demo_data.path(name) / "evaluation" / "truth_pixels" / sample_id / f"{cam}.jsonl"
    if not path.is_file():
        pytest.skip("dense evaluation truth not present")
    out = {}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        for pid, p in row["points"].items():
            out.setdefault(pid, np.full((n, 2), np.nan))[row["frame_index"]] = p["uv_px"] or [np.nan, np.nan]
    return out


@pytest.mark.parametrize("name,cam", [("dataset2", "27432424_left"), ("dataset2", "28221883_left"),
                                      ("dataset1", "27432424_left")])
def test_baseline_coverage_and_error_floor(name, cam):
    root = demo_data.require(name)
    r = load.load_bundle(root / "trajectory.json", lerobot_root=demo_data.lerobot_root(name), episodes=[0])
    s = r.samples[0]
    seeds = O.find_seeds(root / "observations_seed", s.sample_id, cam)
    assert seeds is not None and seeds.method == "synthetic_fixture"
    ctx, targets = O.provider_inputs(s, cam, media_root=demo_data.lerobot_root(name), seeds=seeds)
    batch = T.SeededLKProvider().locate(
        V.iter_clip(ctx.media_path, clip_start_s=ctx.clip_start_s, clip_end_s=ctx.clip_end_s, fps=ctx.fps,
                    frame_count=ctx.media_frame_count), targets, ctx)
    assert batch.stats["seed_hash_matches"] == batch.stats["seed_hash_checked"] == len(seeds.by_media_frame)
    truth = _truth(name, s.sample_id, cam, s.n_frames)
    for pid in targets.point_ids:
        uv, vis, _ = O.on_sample_frames(batch, s.cameras[cam], pid)
        assert (vis == O.VISIBLE).mean() > 0.55, pid
        err = np.linalg.norm(uv - truth[pid], axis=1)
        assert np.nanpercentile(err, 95) < 5.0 and np.nanmax(err) < 10.0, (pid, np.nanmax(err))
