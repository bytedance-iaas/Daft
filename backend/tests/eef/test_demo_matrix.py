"""F5.3 acceptance on the DEMO data, sampled: the §13.3 cells of four telling episodes (baseline, the
three lag signs, the one-camera extrinsic error). The full 18-episode matrix is
``python -m eef_eval.matrix`` (about two minutes); the truth is read by the evaluator only."""
from __future__ import annotations

import pytest

from curation.extensions.eef_consistency import load, profile, runner

from . import demo_data

CASES = [("dataset1", 0), ("dataset1", 9), ("dataset2", 5), ("dataset2", 6)]


@pytest.mark.parametrize("name,ep", CASES)
def test_matrix_cells(name, ep, tmp_path):
    root = demo_data.require(name)
    from eef_eval import matrix, truth

    r = load.load_bundle(root / "trajectory.json", lerobot_root=demo_data.lerobot_root(name), episodes=[ep])
    cfg = runner.RunConfig(lerobot_root=str(demo_data.lerobot_root(name)), seed_root=str(root / "observations_seed"),
                           profile=profile.load("demo"), out_dir=str(tmp_path), evidence_mode="off")
    detail, _ = runner.run_episode(r.samples[ep], cfg)
    fault = truth.faults(name)[ep]                                     # evaluator side only
    bad = [c for c in matrix.check(name, ep, fault, detail) if not c["ok"]]
    assert not bad, bad
