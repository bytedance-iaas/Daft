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


def _deep_merge(base: dict, over: dict) -> dict:
    """递归合并:dict 递归、其余类型(标量/列表)整值覆盖。over 赢。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None = None) -> dict:
    """出厂 default.yaml 为底,--config 的文件**叠加**其上(2026-07-25 manager 反馈)。

    此前 --config 是整文件替换 → 客户想改一个 endpoint 就得复制整份 default.yaml,
    副本会腐烂(default 升级不跟随)。改为深合并:客户站点文件(site.yaml)只写想改的
    几行,没写的一切照用出厂默认、且随产品升级自动更新。
    传完整文件的老用法不受影响(全量覆盖=结果相同)。合并后统一校验。
    路径缺省可用环境变量 CURATION_CONFIG 提供(K8s 场景:ConfigMap 挂载成文件,
    Deployment 里设一次 env,日常命令零改动)。
    """
    with open(DEFAULT_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    path = path or os.environ.get("CURATION_CONFIG") or None
    if path:
        with open(path) as f:
            site = yaml.safe_load(f) or {}
        if not isinstance(site, dict):
            raise ConfigError(f"{path}: 站点配置必须是 YAML 映射(键值),got {type(site).__name__}")
        cfg = _deep_merge(cfg, site)
    validate_config(cfg, path or DEFAULT_CONFIG_PATH)
    cfg.setdefault("pipeline", {})
    return cfg


def apply_vlm_direct(cfg: dict, endpoint: str | None = None, model: str | None = None,
                     api_key_env: str | None = None) -> dict:
    """CLI 直连参数 --vlm-endpoint/--vlm-model/--vlm-api-key-env → 单项覆盖。

    业界通行的"正门"(OpenAI 系工具惯例):不用预先起别名、不用记 --set 点路径。
    与预设可混用:--vlm-backend 打底,本函数在其后单项覆盖(应用顺序:
    site/default 合并 → backend 预设 → 本函数 → --set,后到者赢)。
    环境变量 CURATION_VLM_ENDPOINT/MODEL/API_KEY_ENV 由 CLI 层作为参数缺省值接入。
    """
    vlm = cfg.setdefault("checks", {}).setdefault("task_success", {}).setdefault("vlm", {})
    shown = []
    if endpoint:
        vlm["endpoint"] = endpoint; shown.append(f"endpoint={endpoint}")
        # 换了端点 = 换了机器:预设带来的 hardware/service_type 立刻作废(2026-07-30)。
        # 留着会让性能剖析页拿旧机器型号解释新端点的延时。
        for _k in ("hardware", "service_type"):
            vlm.pop(_k, None)
        if not model:
            # 只给端点不给模型(2026-07-28 同事反馈:单模型服务报模型名是冗余)→
            # 清掉沿袭自默认配置的旧模型名,交给运行期从 GET /models 自动发现;
            # 不清会拿着出厂 doubao 模型名去打客户端点,必错。
            vlm["model"] = ""; shown.append("model=(待自动发现)")
    if model:
        vlm["model"] = model; shown.append(f"model={model}")
    if api_key_env is not None and api_key_env != "":
        vlm["api_key_env"] = api_key_env; shown.append(f"api_key_env={api_key_env}")
    if shown:
        print(f"[curation] VLM 直连覆盖: {', '.join(shown)}")
    return cfg


def probe_concurrency_hint(cfg: dict) -> str | None:
    """n_probe > 帧问询并发时给一行提醒(2026-07-27 用户定,选'提示'弃'绑定')。

    两键语义不同(帧数=看多细;并发=压服务端多重),绑定会让调帧数静默加压;
    但不对齐会让问询分波、单条耗时上升且**不报错**——用提示消灭这个失误模式。
    纯函数:返回提示文本或 None,不打印(打印归调用方,可单测)。
    """
    ts = cfg.get("checks", {}).get("task_success", {})
    if not ts.get("enable", False):
        return None
    n_probe = ts.get("params", {}).get("n_probe", 8)
    mc = ts.get("vlm", {}).get("max_concurrency", 8)
    if n_probe <= mc:
        return None
    waves = -(-n_probe // mc)                  # ceil
    return (f"[curation] ⚠️ n_probe={n_probe} > 帧问询并发 {mc}:问询将分 {waves} 波,"
            f"单条耗时约 ×{waves}。建议 --set checks.task_success.vlm.max_concurrency="
            f"{n_probe} 对齐(一波齐发)")


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


def apply_vlm_backend(cfg: dict, name: str | None) -> dict:
    """CLI --vlm-backend <名> → 按预设(default.yaml 的 vlm_backends)整组切换
    checks.task_success.vlm 的 endpoint/model/api_key_env(2026-07-23 用户定)。

    动机:切后端要记三条 --set 且拼错静默跑偏(曾把 --set 掉在 sh -c 引号外,
    用了默认值还以为切了)。预设仍集中在 YAML 一处(端点/模型不进代码,红线);
    本函数只做查表覆盖。错名报错并列出可选——绝不静默回退默认。
    调用顺序:backend 先、--set 后 ⇒ --set 仍可在预设之上微调单项。
    """
    if not name:
        return cfg
    presets = cfg.get("vlm_backends") or {}
    if name not in presets:
        raise ConfigError(f"--vlm-backend 未知预设 {name!r};可用: {sorted(presets)}"
                          "(预设定义在 default.yaml 的 vlm_backends 段)")
    p = presets[name] or {}
    vlm = cfg.setdefault("checks", {}).setdefault("task_success", {}).setdefault("vlm", {})
    for k in ("endpoint", "model", "api_key_env"):
        vlm[k] = p.get(k)          # 三元组整组覆盖(含置空 api_key_env,防止残留旧密钥头)
    # 描述性字段(2026-07-30):hardware/service_type 只用于交付记录与界面展示,
    # 不参与任何请求。有则带上、无则**清掉**(整组覆盖语义:换后端不能残留上一台
    # 机器的型号,那比不显示更糟——会拿 A 机的型号解释 B 机的延时)。
    for k in ("hardware", "service_type"):
        vlm.pop(k, None)
        if p.get(k):
            vlm[k] = p[k]
    print(f"[curation] VLM 后端 → {name}: {p.get('model')} @ {p.get('endpoint')}")
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
