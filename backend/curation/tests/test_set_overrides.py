"""--set 路径=值 验收(2026-07-15 用户定):免为一个开关复制整份 yaml。"""
from __future__ import annotations

import pytest

from curation.pipeline.config import ConfigError, apply_overrides, load_config



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
    """覆盖出非法配置(视频抽帧率越界)→ 重新校验拦住,不静默跑偏。"""
    from curation.pipeline.config import validate_config
    cfg = load_config()
    apply_overrides(cfg, ["checks.task_success.vlm.video.fps=30"])
    with pytest.raises(ConfigError, match="fps"):
        validate_config(cfg, "--set 覆盖后")
