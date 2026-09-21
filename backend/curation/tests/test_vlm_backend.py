"""--vlm-backend 预设切换测试(2026-07-23;2026-07-25 预设去机房化后改造)。

出厂 default.yaml 只带通用预设(ark / self-hosted-example)——具体机房预设
(h20-8b 等)属于站点配置,经 --config 叠加注入,见 test_config_overlay.py。

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


def test_selfhosted_backend_clears_stale_api_key_env():
    """自托管预设必须**置空** api_key_env——先切 ark 再切自托管,
    不能残留 ARK_API_KEY(否则给自托管端点发无意义鉴权头,还可能泄密钥)。"""
    cfg = apply_vlm_backend(_cfg(), "ark")
    cfg = apply_vlm_backend(cfg, "self-hosted-example")
    v = cfg["checks"]["task_success"]["vlm"]
    assert not v["api_key_env"], "切回自托管后 api_key_env 必须清掉"


def test_unknown_backend_raises_with_choices():
    """错名报错并列出可选——绝不静默回退默认(那正是要消灭的事故形态)。"""
    with pytest.raises(ConfigError) as e:
        apply_vlm_backend(_cfg(), "arkk")        # 手滑
    assert "ark" in str(e.value) and "self-hosted-example" in str(e.value)


def test_none_is_noop():
    """不传 --vlm-backend → 配置原样(默认行为零变化)。"""
    before = _cfg()["checks"]["task_success"]["vlm"].copy()
    after = apply_vlm_backend(_cfg(), None)["checks"]["task_success"]["vlm"]
    assert after == before


def test_set_override_wins_after_backend():
    """应用顺序契约:backend 先、--set 后 ⇒ --set 可在预设之上微调单项。"""
    cfg = apply_vlm_backend(_cfg(), "ark")
    cfg = apply_overrides(cfg, ["checks.task_success.vlm.model=another-model"])
    v = cfg["checks"]["task_success"]["vlm"]
    assert v["model"] == "another-model"                 # --set 赢
    assert "ark.cn-beijing" in v["endpoint"]             # 预设其余保留



# ───────── 硬件描述字段(2026-07-30,性能剖析页签的数据通路)─────────
#
# hardware / service_type 只描述、不参与请求;它们必须**随预设整组切换**并进入
# 生效配置,run.collect_runtime 才能把型号写进交付,UI 才不用硬编码映射表。


def test_backend_carries_hardware_and_service_type():
    """预设声明的 hardware/service_type 跟着 endpoint/model 一起进生效配置。"""
    cfg = apply_vlm_backend(_cfg(), "ark")
    v = cfg["checks"]["task_success"]["vlm"]
    assert "硬件不可见" in v["hardware"]           # 托管服务如实标注
    assert "MaaS" in v["service_type"]


def test_switching_backend_clears_stale_hardware():
    """★ 换后端必须清掉上一台机器的型号——残留比不显示更糟(拿 A 机规格解释 B 机延时)。"""
    site = {"vlm_backends": {"gpu-a": {"endpoint": "http://a:8000/v1",
                                       "model": "m", "hardware": "NVIDIA A30",
                                       "service_type": "自托管 vLLM"},
                             "gpu-b": {"endpoint": "http://b:8000/v1",
                                       "model": "m"}}}       # b 没声明硬件
    cfg = _cfg()
    cfg["vlm_backends"].update(site["vlm_backends"])
    cfg = apply_vlm_backend(cfg, "gpu-a")
    assert cfg["checks"]["task_success"]["vlm"]["hardware"] == "NVIDIA A30"
    cfg = apply_vlm_backend(cfg, "gpu-b")
    v = cfg["checks"]["task_success"]["vlm"]
    assert "hardware" not in v and "service_type" not in v


def test_direct_endpoint_override_clears_hardware():
    """--vlm-endpoint 直连也换了机器 → 预设带来的型号立即作废。"""
    from curation.pipeline.config import apply_vlm_direct
    cfg = apply_vlm_backend(_cfg(), "ark")
    assert cfg["checks"]["task_success"]["vlm"].get("hardware")
    cfg = apply_vlm_direct(cfg, endpoint="http://elsewhere:8000/v1")
    v = cfg["checks"]["task_success"]["vlm"]
    assert "hardware" not in v and "service_type" not in v


# ───────── runtime 信息块(run.collect_runtime / container_limits)─────────


def test_container_limits_parses_cgroup_v2(tmp_path, monkeypatch):
    """cgroup v2 的 cpu.max='1600000 100000' → 16 核;memory.max 字节原样。"""
    from curation.pipeline import run as run_mod
    files = {"/sys/fs/cgroup/cpu.max": "1600000 100000",
             "/sys/fs/cgroup/memory.max": "34359738368"}
    monkeypatch.setattr(run_mod, "_read_first",
                        lambda *ps: next((files[p] for p in ps if p in files), None))
    lim = run_mod.container_limits()
    assert lim["cpu_limit_cores"] == 16.0
    assert lim["memory_limit_bytes"] == 34359738368


def test_container_limits_none_when_unlimited_or_absent(monkeypatch):
    """无配额('max')/读不到 → 一律 None(界面写"未记录",绝不编数字)。"""
    from curation.pipeline import run as run_mod
    monkeypatch.setattr(run_mod, "_read_first",
                        lambda *ps: {"/sys/fs/cgroup/cpu.max": "max"}.get(ps[0]))
    assert run_mod.container_limits() == {"cpu_limit_cores": None,
                                          "memory_limit_bytes": None}
    monkeypatch.setattr(run_mod, "_read_first", lambda *ps: None)
    assert run_mod.container_limits() == {"cpu_limit_cores": None,
                                          "memory_limit_bytes": None}


def test_collect_runtime_takes_hardware_from_config(monkeypatch):
    """runtime 块的硬件来自**生效配置**(站点文件声明),不是代码里的映射表。"""
    from curation.pipeline import run as run_mod
    cfg = apply_vlm_backend(_cfg(), "ark")
    cfg["checks"]["task_success"]["vlm"]["hardware"] = "NVIDIA H20"
    monkeypatch.setenv("NODE_NAME", "node-7")
    rt = run_mod.collect_runtime(cfg)
    b = rt["vlm_backend"]
    assert b["hardware"] == "NVIDIA H20" and b["model"]
    assert b["episode_concurrency"] == cfg["pipeline"]["vlm_episode_concurrency"]
    assert b["frame_concurrency"] == cfg["checks"]["task_success"]["vlm"]["max_concurrency"]
    assert rt["environment"]["node"] == "node-7"
    assert rt["environment"]["node_source"] == "NODE_NAME"


def test_collect_runtime_falls_back_to_hostname(monkeypatch):
    """没注入 NODE_NAME → 退回 hostname,并**标明**取的是 hostname(不冒充节点名)。"""
    import socket

    from curation.pipeline import run as run_mod
    monkeypatch.delenv("NODE_NAME", raising=False)
    rt = run_mod.collect_runtime(_cfg())
    assert rt["environment"]["node"] == socket.gethostname()
    assert rt["environment"]["node_source"] == "hostname"
