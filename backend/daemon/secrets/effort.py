"""Reasoning effort: Ark's ``reasoning_effort`` and the per-model levels (design doc 08, §4.1).

What is stored is the API's own value or NULL (01 §2.2). NULL means the request carries no
``reasoning_effort`` field at all and the model uses its own default - the factory setting
and the parity setting, because v1 never sent a thinking parameter.

Ark accepts all seven values for every model that supports the field and maps them onto the
model's effective levels. The table below (08 §4.1, from Ark's Chat API and Deep Thinking
docs, read 2026-09-20) says which levels are effective for which model family, matched by
model-name prefix. It only decides which levels are offered and accepted for a model; it is
not a model catalogue and does not decide which models can be used. Models it does not know
get all seven levels ("the server maps them").

Site override: ``CURATOR_REASONING_EFFORT_TABLE`` - a JSON file path, or the JSON itself -
a list of ``{"prefix": "glm-4.5", "levels": ["low", "medium", "high"], "default": "medium"}``
entries (``prefix`` may be a list). They take precedence over the built-in rows.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Iterable, Mapping

log = logging.getLogger("daemon.secrets")

#: Ark's native values, in increasing effort.
ALL_LEVELS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

TABLE_ENV = "CURATOR_REASONING_EFFORT_TABLE"

_DOUBAO_LEVELS = ("minimal", "low", "medium", "high")
#: Values outside the effective levels that Ark maps (so the error can say what they become).
_DOUBAO_MAPPED = {"none": "minimal", "xhigh": "high", "max": "high"}


@dataclass(frozen=True)
class EffortRule:
    prefixes: tuple[str, ...]
    levels: tuple[str, ...]
    default: str | None = None
    mapped: Mapping[str, str] = field(default_factory=dict)


BUILTIN_RULES: tuple[EffortRule, ...] = (
    # doubao-seed-2-1-pro / turbo, doubao-seed-evolving: default high; minimal turns thinking off
    EffortRule(("doubao-seed-2-1-pro", "doubao-seed-2-1-turbo", "doubao-seed-evolving"),
               _DOUBAO_LEVELS, "high", _DOUBAO_MAPPED),
    # doubao-seed-2-0 pro / lite / mini (every version), doubao-seed-1-8, doubao-seed-1-6-251015
    EffortRule(("doubao-seed-2-0-pro", "doubao-seed-2-0-lite", "doubao-seed-2-0-mini",
                "doubao-seed-1-8", "doubao-seed-1-6-251015"),
               _DOUBAO_LEVELS, "medium", _DOUBAO_MAPPED),
)


@dataclass(frozen=True)
class Levels:
    levels: tuple[str, ...]
    known: bool                      # False = not in the table, all seven offered
    default: str | None = None
    mapped: Mapping[str, str] = field(default_factory=dict)


class EffortNotAllowed(ValueError):
    """The value is not an effective level of the model; ``message_zh`` says which are."""

    def __init__(self, model: str, value: str, levels: Levels):
        self.model, self.value, self.levels = model, value, levels
        allowed = "、".join(levels.levels)
        hint = ""
        if value in levels.mapped:
            hint = f"（{value} 在这个模型上等同于 {levels.mapped[value]}）"
        self.message_zh = (f"模型 {model} 的思考强度只能是 {allowed}，或者留空用模型默认；"
                           f"不能设为 {value}{hint}")
        super().__init__(f"reasoning_effort {value!r} is not an effective level of {model!r}")


def _by_prefix(rules: Iterable[EffortRule]) -> list[tuple[str, EffortRule]]:
    # within one tier the longest matching prefix wins, whatever the order of the rules
    return sorted(((p.lower(), r) for r in rules for p in r.prefixes),
                  key=lambda pr: len(pr[0]), reverse=True)


class EffortTable:
    """``overrides`` (the site's rows) are searched before ``rules`` (the built-in ones)."""

    def __init__(self, rules: Iterable[EffortRule] = BUILTIN_RULES,
                 overrides: Iterable[EffortRule] = ()):
        self.rules = tuple(rules)
        self.overrides = tuple(overrides)
        self._tiers = (_by_prefix(self.overrides), _by_prefix(self.rules))

    def lookup(self, model_name: str | None) -> Levels:
        name = str(model_name or "").strip().lower()
        # a "provider/model" name (vLLM, gateways) is matched on its last segment too
        candidates = [name] + ([name.rsplit("/", 1)[1]] if "/" in name else [])
        for tier in self._tiers:
            for cand in candidates:
                for prefix, rule in tier:
                    if cand.startswith(prefix):
                        return Levels(rule.levels, True, rule.default, dict(rule.mapped))
        return Levels(ALL_LEVELS, False)

    def levels_for(self, model_name: str | None) -> tuple[str, ...]:
        return self.lookup(model_name).levels

    def check(self, model_name: str, value: str | None) -> None:
        """Raise :class:`EffortNotAllowed` unless ``value`` is None or an effective level."""
        if value is None:
            return
        if value not in ALL_LEVELS:
            raise EffortNotAllowed(model_name, value, Levels(ALL_LEVELS, False))
        levels = self.lookup(model_name)
        if value not in levels.levels:
            raise EffortNotAllowed(model_name, value, levels)


def _rules_from_json(value) -> list[EffortRule]:
    if not isinstance(value, list):
        raise ValueError("the table must be a JSON list of {prefix, levels, default} objects")
    rules = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"entry {i} is not an object")
        prefixes = item.get("prefix")
        prefixes = [prefixes] if isinstance(prefixes, str) else prefixes
        if not prefixes or not all(isinstance(p, str) and p.strip() for p in prefixes):
            raise ValueError(f"entry {i}: prefix must be a non-empty string or list of strings")
        levels = item.get("levels")
        if not isinstance(levels, list) or not levels or any(lv not in ALL_LEVELS for lv in levels):
            raise ValueError(f"entry {i}: levels must be a non-empty subset of {list(ALL_LEVELS)}")
        default = item.get("default")
        if default is not None and default not in ALL_LEVELS:
            raise ValueError(f"entry {i}: default must be one of {list(ALL_LEVELS)}")
        mapped = item.get("mapped") or {}
        if not isinstance(mapped, dict) or any(k not in ALL_LEVELS or v not in levels
                                               for k, v in mapped.items()):
            raise ValueError(f"entry {i}: mapped must map levels onto the entry's levels")
        ordered = tuple(lv for lv in ALL_LEVELS if lv in levels)
        rules.append(EffortRule(tuple(p.strip() for p in prefixes), ordered, default, mapped))
    return rules


def load_table(environ: Mapping[str, str] | None = None) -> EffortTable:
    """The built-in table with the site's override on top; a broken override is logged and
    ignored (the table only shapes a dropdown, it must not take the Daemon down)."""
    env = os.environ if environ is None else environ
    raw = str(env.get(TABLE_ENV, "") or "").strip()
    if not raw:
        return EffortTable()
    try:
        text = raw
        if not raw.startswith("["):
            with open(raw, encoding="utf-8") as fh:
                text = fh.read()
        extra = _rules_from_json(json.loads(text))
    except (OSError, ValueError) as err:
        log.error("%s ignored, using the built-in reasoning-effort table: %s", TABLE_ENV, err)
        return EffortTable()
    return EffortTable(BUILTIN_RULES, overrides=extra)
