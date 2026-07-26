"""配置叠加(site.yaml overlay)+ 直连参数测试(2026-07-25,manager 反馈落地)。

需求背景:产品最终跑在什么硬件上事先未知,客户要能用**自己的配置文件**设 IP/模型。
方案:出厂 default.yaml 为底,--config 的站点文件**深合并**其上——客户只写想改的
几行,没写的一切随出厂默认升级(消灭"复制整份 yaml → 副本腐烂")。
应用顺序契约:default ⊕ site → --vlm-backend 预设 → 直连参数 → --set(后到者赢)。
"""
from __future__ import annotations

import os

import pytest
import yaml

from curation.pipeline.config import (ConfigError, apply_overrides,
                                      apply_vlm_backend, apply_vlm_direct,
                                      load_config)


def _write(tmp_path, content: dict) -> str:
    p = tmp_path / "site.yaml"
    p.write_text(yaml.safe_dump(content, allow_unicode=True))
    return str(p)


# ───────── overlay 深合并语义 ─────────

def test_site_adds_custom_preset_keeping_defaults(tmp_path):
    """客户 5 行 site.yaml:加自己的预设 → 出厂预设仍在、其余配置原封不动。
    这就是"星辰机器人"场景:自己的 IP + 自己的模型,名字自己起。"""
    site = _write(tmp_path, {"vlm_backends": {"my-gpu-server": {
        "endpoint": "http://10.88.0.7:8000/v1", "model": "Qwen/Qwen2.5-VL-32B"}}})
    cfg = load_config(site)
    assert "my-gpu-server" in cfg["vlm_backends"]          # 客户的进来了
    assert "ark" in cfg["vlm_backends"]                    # 出厂的还在
    assert cfg["checks"]["task_success"]["enable"] is True  # 没写的照用出厂
    # 且立刻能用 --vlm-backend 选中它
    cfg = apply_vlm_backend(cfg, "my-gpu-server")
    assert cfg["checks"]["task_success"]["vlm"]["endpoint"] == "http://10.88.0.7:8000/v1"


def test_nested_partial_override(tmp_path):
    """嵌套局部覆盖:只改 vlm.endpoint 一个叶子,同层其他键(model 等)不受伤。"""
    site = _write(tmp_path, {"checks": {"task_success": {"vlm": {
        "endpoint": "http://10.0.0.9:8000/v1"}}}})
    cfg = load_config(site)
    v = cfg["checks"]["task_success"]["vlm"]
    assert v["endpoint"] == "http://10.0.0.9:8000/v1"      # 改的生效
    assert v["model"]                                       # 没改的保留出厂值
    assert cfg["checks"]["task_success"]["params"]["n_probe"] == 8


def test_scalar_and_threshold_override(tmp_path):
    site = _write(tmp_path, {"verdict": {"soft_threshold": 0.7},
                             "pipeline": {"vlm_episode_concurrency": 2}})
    cfg = load_config(site)
    assert cfg["verdict"]["soft_threshold"] == 0.7
    assert cfg["pipeline"]["vlm_episode_concurrency"] == 2
    assert cfg["pipeline"]["frame_max_side"] == 448        # 同段其他键仍在


def test_merged_config_still_validated(tmp_path):
    """叠加后仍走结构校验:站点文件写坏(非法 gate)必须报错,不能静默跑偏。"""
    site = _write(tmp_path, {"checks": {"visual_quality": {"gate": "banana"}}})
    with pytest.raises(ConfigError):
        load_config(site)


def test_site_must_be_mapping(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("- 这是个列表\n- 不是映射\n")
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_env_var_config_path(tmp_path, monkeypatch):
    """CURATION_CONFIG 环境变量 = --config 的部署态等价物(K8s ConfigMap 场景:
    Deployment 设一次 env,日常命令零改动)。显式 --config 优先于 env。"""
    site = _write(tmp_path, {"vlm_backends": {"from-env": {
        "endpoint": "http://env:8000/v1", "model": "m"}}})
    monkeypatch.setenv("CURATION_CONFIG", site)
    cfg = load_config(None)                                # 没传 --config,走 env
    assert "from-env" in cfg["vlm_backends"]
    # 显式路径压过 env
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(yaml.safe_dump({"vlm_backends": {"from-flag": {
        "endpoint": "http://flag:8000/v1", "model": "m"}}}))
    cfg2 = load_config(str(explicit))
    assert "from-flag" in cfg2["vlm_backends"] and "from-env" not in cfg2["vlm_backends"]


# ───────── 直连参数(免别名的正门)─────────

def test_direct_flags_standalone():
    """manager 场景:客户不懂也不想懂别名——两个直连参数即完成接线。"""
    cfg = apply_vlm_direct(load_config(None),
                           endpoint="http://10.1.2.3:8000/v1", model="my/model")
    v = cfg["checks"]["task_success"]["vlm"]
    assert v["endpoint"] == "http://10.1.2.3:8000/v1" and v["model"] == "my/model"


def test_direct_overrides_preset_single_field():
    """顺序契约:backend 打底,直连单项覆盖(换模型不换端点)。"""
    cfg = apply_vlm_backend(load_config(None), "ark")
    cfg = apply_vlm_direct(cfg, model="doubao-experimental")
    v = cfg["checks"]["task_success"]["vlm"]
    assert v["model"] == "doubao-experimental"
    assert "ark.cn-beijing" in v["endpoint"]               # 端点仍是预设的


def test_set_still_wins_last():
    """--set 仍是最后手(万能逃生门):直连之后还能被 --set 压。"""
    cfg = apply_vlm_direct(load_config(None), model="a")
    cfg = apply_overrides(cfg, ["checks.task_success.vlm.model=b"])
    assert cfg["checks"]["task_success"]["vlm"]["model"] == "b"


def test_direct_noop_when_all_none():
    before = load_config(None)["checks"]["task_success"]["vlm"].copy()
    after = apply_vlm_direct(load_config(None))["checks"]["task_success"]["vlm"]
    assert after == before
