"""Translate the single user-facing thinking switch for known model families.

Model IDs may be Ark aliases or dated model IDs. Keep this list deliberately
small: sending an unsupported parameter is worse than asking for a policy.
"""
from __future__ import annotations

import re

from .config import ConfigError


def _model_family(model: str) -> str | None:
    name = re.sub(r"[._]", "-", (model or "").strip().lower())
    name = name.removeprefix("ark-")
    if re.fullmatch(r"glm-?5-3-flash(?:-\d{6})?", name):
        return "glm-5.3-flash"
    if re.fullmatch(r"glm-?5-2(?:-\d{6})?", name):
        return "glm-5.2"
    if name == "doubao-seed-2-0-pro-260215":
        return "doubao-seed-2.0-pro"
    return None


def thinking_request_fields(model: str, thinking: bool | None) -> dict:
    """Return only API fields supported by this known model for the switch."""
    if thinking is None:
        return {}
    family = _model_family(model)
    if family == "glm-5.3-flash":
        # This family always thinks. "Off" means its lightest supported effort.
        return ({"thinking": {"type": "enabled"}} if thinking else
                {"thinking": {"type": "enabled"}, "reasoning_effort": "low"})
    if family in ("glm-5.2", "doubao-seed-2.0-pro"):
        return {"thinking": {"type": "enabled" if thinking else "disabled"}}
    raise ConfigError(
        f"model {model!r} has no verified thinking policy; "
        "leave pipeline.thinking=null or add its model policy before setting --thinking/--no-thinking")


def thinking_notice(model: str, thinking: bool | None) -> str | None:
    if thinking is False and _model_family(model) == "glm-5.3-flash":
        return (f"[curation] {model}: --no-thinking 对应 reasoning_effort=low；"
                "该模型仍在思考，不能关闭思考")
    return None
