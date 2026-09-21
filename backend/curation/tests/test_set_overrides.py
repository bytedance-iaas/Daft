"""--set 路径=值 验收(2026-07-15 用户定):免为一个开关复制整份 yaml。"""
from __future__ import annotations

import json
import os

import pytest

from curation.pipeline.config import ConfigError, apply_overrides, load_config

PUSHT = "/data03/hao/data/pusht"


def test_set_basic_types():
    cfg = load_config()
    apply_overrides(cfg, ["pipeline.sync_plots=all",
                          "checks.visual_quality.params.blur_ref_var=80",
                          "checks.task_success.enable=false"])
    assert cfg["pipeline"]["sync_plots"] == "all"                      # 字符串
    assert cfg["checks"]["visual_quality"]["params"]["blur_ref_var"] == 80   # 数字
    assert cfg["checks"]["task_success"]["enable"] is False            # 布尔


def test_set_unknown_intermediate_path_errors():
    cfg = load_config()
    with pytest.raises(ConfigError, match="未知路径"):
        apply_overrides(cfg, ["checks.visula_quality.params.blur_ref_var=80"])  # 拼写错


def test_set_bad_format_errors():
    cfg = load_config()
    with pytest.raises(ConfigError, match="路径=值"):
        apply_overrides(cfg, ["pipeline.sync_plots"])                  # 缺 =


def test_set_new_leaf_allowed_with_notice(capsys):
    cfg = load_config()
    apply_overrides(cfg, ["pipeline.brand_new_knob=1"])
    assert cfg["pipeline"]["brand_new_knob"] == 1
    assert "新增键" in capsys.readouterr().out


def test_set_breaking_invariant_caught():
    """覆盖出非法配置(负权重)→ 重新校验拦住,不静默跑偏。"""
    from curation.pipeline.config import validate_config
    cfg = load_config()
    apply_overrides(cfg, ["checks.visual_quality.weight=-1"])
    with pytest.raises(ConfigError, match="不能为负"):
        validate_config(cfg, "--set 覆盖后")


@pytest.mark.skipif(not os.path.exists(os.path.join(PUSHT, "meta")), reason="无 pusht 数据")
def test_cli_set_e2e_sync_plots_all(tmp_path):
    """端到端:--set pipeline.sync_plots=all → 每条一张图(等价于改 yaml)。"""
    from curation.cli import main
    out = tmp_path / "out"
    rc = main(["run", "--input", PUSHT, "--output", str(out),
               "--embodiment-id", "pusht", "--max-episodes", "3",
               "--only", "video_action_sync", "--report-only",
               "--set", "pipeline.sync_plots=all"])
    assert rc == 0
    assert len(list((out / "details" / "plots").glob("ep*_sync.png"))) == 3


@pytest.mark.skipif(not os.path.exists(os.path.join(PUSHT, "meta")), reason="无 pusht 数据")
def test_cli_set_bad_path_friendly_exit(tmp_path):
    from curation.cli import main
    rc = main(["run", "--input", PUSHT, "--output", str(tmp_path / "o"),
               "--embodiment-id", "pusht", "--max-episodes", "2",
               "--only", "motion_quality", "--report-only",
               "--set", "checkz.motion.enable=false"])
    assert rc == 2
