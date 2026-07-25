"""--vlm-backend 预设切换测试(2026-07-23)。

背景:切 VLM 后端(方舟 ↔ 自托管 H20)要记三条 --set,且拼错会**静默跑偏**
——实测踩过:--set 掉在 sh -c 引号外,用了默认值还以为切了。
预设收进 default.yaml 的 vlm_backends 段(端点/模型仍只在 YAML 一处,红线),
CLI 一个参数整组切换;错名必须报错列出可选,绝不静默回退。
"""
from __future__ import annotations

import pytest

from curation.pipeline.config import (ConfigError, apply_overrides,
                                      apply_vlm_backend, load_config)


def _cfg():
    return load_config(None)          # default.yaml,自带 vlm_backends 预设


def test_known_backend_swaps_all_three_keys():
    """选中预设 → endpoint/model/api_key_env 三元组整组换。"""
    cfg = apply_vlm_backend(_cfg(), "ark")
    v = cfg["checks"]["task_success"]["vlm"]
    assert v["endpoint"].startswith("https://ark.cn-beijing")
    assert v["model"] == "doubao-seed-2-0-pro-260215"
    assert v["api_key_env"] == "ARK_API_KEY"


def test_h20_backend_clears_stale_api_key_env():
    """自托管预设必须**置空** api_key_env——先切 ark 再切 h20-8b,
    不能残留 ARK_API_KEY(否则给自托管端点发无意义鉴权头,还可能泄密钥)。"""
    cfg = apply_vlm_backend(_cfg(), "ark")
    cfg = apply_vlm_backend(cfg, "h20-8b")
    v = cfg["checks"]["task_success"]["vlm"]
    assert v["model"] == "nvidia/Cosmos-Reason2-8B"
    assert "vllm-cosmos-8b" in v["endpoint"]
    assert not v["api_key_env"], "切回自托管后 api_key_env 必须清掉"


def test_unknown_backend_raises_with_choices():
    """错名报错并列出可选——绝不静默回退默认(那正是要消灭的事故形态)。"""
    with pytest.raises(ConfigError) as e:
        apply_vlm_backend(_cfg(), "h20_8b")      # 下划线手滑
    assert "h20-8b" in str(e.value) and "ark" in str(e.value)


def test_none_is_noop():
    """不传 --vlm-backend → 配置原样(默认行为零变化)。"""
    before = _cfg()["checks"]["task_success"]["vlm"].copy()
    after = apply_vlm_backend(_cfg(), None)["checks"]["task_success"]["vlm"]
    assert after == before


def test_set_override_wins_after_backend():
    """应用顺序契约:backend 先、--set 后 ⇒ --set 可在预设之上微调单项。"""
    cfg = apply_vlm_backend(_cfg(), "h20-8b")
    cfg = apply_overrides(cfg, ["checks.task_success.vlm.model=nvidia/Cosmos-Reason2-32B"])
    v = cfg["checks"]["task_success"]["vlm"]
    assert v["model"] == "nvidia/Cosmos-Reason2-32B"     # --set 赢
    assert "vllm-cosmos-8b" in v["endpoint"]             # 预设其余保留


def test_h20_32b_preset_available():
    """32B 上线后(2026-07-23)三预设齐备;32B 端点/模型/无鉴权正确。"""
    cfg = apply_vlm_backend(_cfg(), "h20-32b")
    v = cfg["checks"]["task_success"]["vlm"]
    assert v["model"] == "nvidia/Cosmos-Reason2-32B"
    assert "vllm-cosmos-32b" in v["endpoint"]
    assert not v["api_key_env"]
