"""L1 配置层:YAML 声明式配置(DESIGN.md §11.1)。

客户勾选跑哪些检查/设阈值/定硬门软门;权重阈值全部来自配置,无魔法数字。
未知检查名/非法门类型 → 报错(配置错误不能静默跑偏)。
"""
from __future__ import annotations

import os

import yaml

KNOWN_CHECKS = (
    "timestamp_check", "kinematic_limits", "motion_quality",
    "visual_quality", "video_action_sync", "task_success",
)

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "default.yaml")


class ConfigError(ValueError):
    pass


def validate_config(cfg: dict, origin: str = "config") -> None:
    """结构校验(load 与 --set 覆盖后都要过):配置错误不能静默跑偏。"""
    checks = cfg.get("checks")
    if not checks:
        raise ConfigError(f"{origin}: 缺 checks 段")
    for name, c in checks.items():
        if name not in KNOWN_CHECKS:
            raise ConfigError(f"{origin}: 未知检查 {name!r};可用: {KNOWN_CHECKS}")
        gate = c.get("gate", "soft")
        if gate not in ("hard", "soft"):
            raise ConfigError(f"{origin}: {name}.gate 必须是 hard/soft,got {gate!r}")
        if gate == "soft" and c.get("enable", True) and c.get("weight", 1.0) < 0:
            raise ConfigError(f"{origin}: {name}.weight 不能为负")
    if "verdict" not in cfg or "soft_threshold" not in cfg["verdict"]:
        raise ConfigError(f"{origin}: 缺 verdict.soft_threshold")


def load_config(path: str | None = None) -> dict:
    path = path or DEFAULT_CONFIG_PATH
    with open(path) as f:
        cfg = yaml.safe_load(f)
    validate_config(cfg, path)
    cfg.setdefault("pipeline", {})
    return cfg


def apply_overrides(cfg: dict, sets: list[str]) -> dict:
    """CLI --set 路径=值(可重复)→ 点路径覆盖单个配置值(2026-07-15 用户定)。

    动机:为拨一个开关复制整份 yaml 不可扩展且副本会腐烂(不随 default 升级)。
    规则:中间路径必须存在(防拼写错静默生效);叶子键可新增但会打印提醒;
    值经 yaml 解析(数字/布尔/字符串/null 自动转型)。覆盖后调用方须重新校验。
    """
    for item in sets or []:
        if "=" not in item:
            raise ConfigError(f"--set 需要 '路径=值' 形式,got {item!r}")
        path, raw = item.split("=", 1)
        keys = [k.strip() for k in path.strip().split(".") if k.strip()]
        if not keys:
            raise ConfigError(f"--set 路径为空: {item!r}")
        node = cfg
        for i, k in enumerate(keys[:-1]):
            if not isinstance(node, dict) or k not in node:
                avail = sorted(node) if isinstance(node, dict) else "(非字典层)"
                raise ConfigError(f"--set 未知路径 '{'.'.join(keys[:i + 1])}';"
                                  f"该层可选: {avail}")
            node = node[k]
        if not isinstance(node, dict):
            raise ConfigError(f"--set '{path}' 的父层不是字典,无法设置")
        leaf = keys[-1]
        if leaf not in node:
            print(f"[curation] ⚠️ --set 新增键 '{path}'(原配置无此键,请确认非拼写错误)")
        node[leaf] = yaml.safe_load(raw)
    return cfg


def enabled(cfg: dict, name: str) -> bool:
    c = cfg["checks"].get(name)
    return bool(c and c.get("enable", True))


def apply_check_selection(cfg: dict, only: str | None = None,
                          skip: str | None = None) -> dict:
    """CLI 的 --only/--skip → enable 开关覆盖(单跑任意模块/组合)。

    only 与 skip 互斥;名字必须是已知模块(语义化名,如 visual_quality),
    错名报错并列出全部可选。M 编号不进 CLI(与"编号不进代码"同一纪律)。
    除漏斗检查外,数据集级模块 skill_profile(技能画像)同样可选:
    --only skill_profile = 跳过全部检查、只跑画像;--skip skill_profile = 反之。
    """
    if only and skip:
        raise ValueError("--only 与 --skip 互斥,只能用一个")

    cfg.setdefault("skill_profile", {})
    cfg.setdefault("dedup", {})
    extras = ("skill_profile", "dedup")    # 数据集级可选模块(非漏斗检查)

    def parse(s):
        names = []
        for tok in s.split(","):
            tok = tok.strip().lower()
            if not tok:
                continue
            if tok not in cfg["checks"] and tok not in extras:
                raise ValueError(f"未知检查名 {tok!r}。可选: "
                                 f"{sorted(cfg['checks']) + sorted(extras)}")
            names.append(tok)
        if not names:
            raise ValueError("检查名列表为空")
        return names

    if only:
        keep = set(parse(only))
        for name in cfg["checks"]:
            cfg["checks"][name]["enable"] = name in keep
        cfg["skill_profile"]["enable"] = "skill_profile" in keep
        cfg["dedup"]["enable"] = "dedup" in keep
    elif skip:
        names = parse(skip)
        for name in names:
            if name in cfg["checks"]:
                cfg["checks"][name]["enable"] = False
        if "skill_profile" in names:
            cfg["skill_profile"]["enable"] = False
        if "dedup" in names:
            cfg["dedup"]["enable"] = False
    return cfg
