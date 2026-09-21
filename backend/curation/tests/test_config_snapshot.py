# 交付快照瘦身(2026-08-30 用户审计后定):config_effective 只记「本次生效」,
# 不外泄站点拓扑与方法资产。背景:实测交付里发现快照整表带出了
# vlm_backends(全部后端端点/模型清单)、tos_buckets、public_datasets,
# 以及 skill_profile.taxonomy_guideline 数千字判据 prompt —— 全是客户
# 不该看到的站点内部信息。
from curation.pipeline.run import _SNAPSHOT_SITE_KEYS, _sanitize_config_snapshot


def _cfg():
    return {
        "vlm_backends": {"backend-a": {"endpoint": "https://maas.example", "model": "m1"},
                         "backend-b": {"endpoint": "http://10.0.0.1:8000"}},
        "vlm_presets": {"32b": "backend-a"},
        "tos_buckets": [{"bucket": "my-bucket", "region": "cn-beijing"}],
        "public_datasets": {"bucket": "hf-mirror", "region": "cn-beijing"},
        "checks": {"task_success": {"vlm": {"endpoint": "https://ark",
                                            "model": "doubao",
                                            "api_key_env": "ARK_API_KEY"}}},
        "pipeline": {"vlm_episode_concurrency": 32, "evidence_frames": 4},
        "skill_profile": {"taxonomy_guideline": "第一条:看物体不看手臂……" * 50,
                          "caption_concurrency": 8},
    }


def test_site_topology_tables_are_stripped():
    """站点拓扑整键剔除:后端菜单/桶清单/公共镜像配置一个都不许进交付。"""
    ce = _sanitize_config_snapshot(_cfg())
    for k in _SNAPSHOT_SITE_KEYS:
        assert k not in ce, f"{k} 不该出现在交付快照里"
    # 四个点名键必须都在剔除清单上(防手滑改短)
    for k in ("vlm_backends", "vlm_presets", "tos_buckets", "public_datasets"):
        assert k in _SNAPSHOT_SITE_KEYS


def test_taxonomy_guideline_becomes_fingerprint():
    """判据 prompt 全文换 sha256 指纹:复现性靠指纹对版本,原文不外发。"""
    ce = _sanitize_config_snapshot(_cfg())
    tg = ce["skill_profile"]["taxonomy_guideline"]
    assert tg.startswith("sha256:") and len(tg) == len("sha256:") + 12
    # 同文同指纹,改文换指纹(指纹要真能对版本)
    assert tg == _sanitize_config_snapshot(_cfg())["skill_profile"]["taxonomy_guideline"]
    other = _cfg()
    other["skill_profile"]["taxonomy_guideline"] = "别的判据"
    assert _sanitize_config_snapshot(other)["skill_profile"]["taxonomy_guideline"] != tg


def test_effective_settings_survive():
    """「本次生效」的信息必须原样保留:报告页性能剖析/证据帧数还要靠它们。

    api_key_env 存的是环境变量名(不是密钥本身),属于生效配置,保留。
    """
    ce = _sanitize_config_snapshot(_cfg())
    vlm = ce["checks"]["task_success"]["vlm"]
    assert vlm == {"endpoint": "https://ark", "model": "doubao",
                   "api_key_env": "ARK_API_KEY"}
    assert ce["pipeline"] == {"vlm_episode_concurrency": 32, "evidence_frames": 4}
    assert ce["skill_profile"]["caption_concurrency"] == 8


def test_running_config_is_untouched():
    """深拷贝后操作:运行中的 cfg 本体一个字都不许动(管道后面还要用它)。"""
    cfg = _cfg()
    before = repr(cfg)
    _sanitize_config_snapshot(cfg)
    assert repr(cfg) == before


def test_absent_keys_and_empty_guideline_are_fine():
    """键不在/guideline 为空:不炸、不硬造指纹(空文没有版本可对)。"""
    ce = _sanitize_config_snapshot({"skill_profile": {"taxonomy_guideline": "  "}})
    assert ce == {"skill_profile": {"taxonomy_guideline": "  "}}
    assert _sanitize_config_snapshot({}) == {}
