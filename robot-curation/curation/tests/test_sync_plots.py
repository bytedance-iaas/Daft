"""同步曲线证据图验收(2026-07-15 用户定):flagged 默认只画非'过';all 全画;off 不画。"""
from __future__ import annotations

import json
import os

import pytest

PUSHT = "/data03/hao/data/pusht"
需要 = pytest.mark.skipif(not os.path.exists(os.path.join(PUSHT, "meta")),
                          reason="无 pusht 数据")


def test_renderer_pure():
    """渲染器纯函数:合法曲线出 PNG;坏 JSON 跳过不炸;短曲线跳过。"""
    import numpy as np

    from curation.export.sync_plots import render_sync_plots
    t = np.linspace(0, 10, 100)
    good = json.dumps({"t": t.tolist(), "flow": np.sin(t).tolist(),
                       "speed": np.sin(t - 0.2).tolist(), "verdict": "abstain",
                       "detail": {"reason": "corr_peak 0.2 < 0.3"}})
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = render_sync_plots([("ep000001", good), ("ep000002", "不是json"),
                                 ("ep000003", json.dumps({"t": [1], "flow": [1],
                                                          "speed": [1]}))], d)
        assert out == ["ep000001_sync.png"]
        assert os.path.exists(os.path.join(d, "ep000001_sync.png"))


@需要
def test_flagged_mode_no_plots_when_all_pass(tmp_path):
    """pusht 同步全过 → flagged 默认不出图(不给没人看的图占磁盘)。"""
    from curation.cli import main
    out = tmp_path / "out"
    rc = main(["run", "--input", PUSHT, "--output", str(out),
               "--embodiment-id", "pusht", "--max-episodes", "3",
               "--only", "video_action_sync", "--report-only"])
    assert rc == 0
    plots = out / "details" / "plots"
    assert not plots.exists() or not list(plots.glob("*.png"))


@需要
def test_all_mode_plots_every_episode(tmp_path):
    """sync_plots: all → 每条一张,报告注明目录。"""
    import yaml

    from curation.cli import main
    from curation.pipeline.config import DEFAULT_CONFIG_PATH
    cfg = yaml.safe_load(open(DEFAULT_CONFIG_PATH))
    cfg["pipeline"]["sync_plots"] = "all"
    cfgp = tmp_path / "all.yaml"
    yaml.safe_dump(cfg, open(cfgp, "w"), allow_unicode=True)
    out = tmp_path / "out"
    rc = main(["run", "--config", str(cfgp), "--input", PUSHT, "--output", str(out),
               "--embodiment-id", "pusht", "--max-episodes", "3",
               "--only", "video_action_sync", "--report-only"])
    assert rc == 0
    pngs = list((out / "details" / "plots").glob("ep*_sync.png"))
    assert len(pngs) == 3
    rep = json.load(open(out / "passed.json"))
    assert rep["dataset"]["sync_plots"]["生成"] == 3
