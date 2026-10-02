"""The verdict policy: which findings reject an episode, which ask a person, which are reported only
(design doc 17 §4, D58).

A policy is a function from a finding to a level - ``blocking``, ``review`` or ``info``. Modules never
see it: they report findings with the codes of their registry catalogue (C1 2.0), and ``aggregate``
grades every finding with the task's policy before deciding keep / drop / held. Changing the policy
re-runs ``aggregate`` only; the module records stay as they are.

A policy is a list of override rules on top of the registry's default levels. Rules are tried in
order and the first one that matches decides; a finding no rule matches keeps its code's default
level (P18: today's hard gates block, today's suspects / abstentions / questions are review, the
rest - the retired soft scores among them - is info). A rule matches on any of ``module``, ``code``,
``item``, ``severity`` and ``level`` (the default level; ``any`` matches every finding). A rule that
makes a finding review keeps the review line of its code; a code without one cannot be raised to
review (there is nobody to ask) and stays as it was.

Two presets ship (``PRESETS``): ``default`` (no rule) and ``report_only`` (everything info: nothing is
rejected, nobody is asked, every finding is reported). The task names one in ``params.policy`` (C4
``TaskParams.policy``); the Daemon freezes the full table into ``run.json`` at start (``policy``) and
every result revision keeps a copy (``policy.json``).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..contracts import modules as registry

POLICY_VERSION = "1"
LEVELS = tuple(lv for lv, _ in registry.FINDING_LEVELS)
MATCH_KEYS = ("module", "code", "item", "severity", "level")

#: The presets a task can name (C4 TaskParams.policy.preset).
PRESETS: dict[str, tuple[dict, ...]] = {
    "default": (),
    "report_only": ({"match": {"level": "any"}, "level": "info"},),
}
#: Chinese names for the report and the console.
PRESET_TITLES = {"default": "默认（今天的判废规则）", "report_only": "只报不拒"}
DEFAULT_PRESET = "default"
POLICY_NAME = "policy.json"


class PolicyError(ValueError):
    """A policy that cannot be used (an unknown preset, a malformed rule)."""


def _check_rule(rule: dict) -> dict:
    if not isinstance(rule, dict) or not isinstance(rule.get("match"), dict) \
            or rule.get("level") not in LEVELS:
        raise PolicyError(f"a policy rule needs a match object and a level of {', '.join(LEVELS)}: {rule!r}")
    unknown = set(rule["match"]) - set(MATCH_KEYS)
    if unknown:
        raise PolicyError(f"a policy rule matches on {', '.join(MATCH_KEYS)}, not {', '.join(sorted(unknown))}")
    return {"match": dict(rule["match"]), "level": rule["level"]}


@dataclass(frozen=True)
class Policy:
    preset: str = DEFAULT_PRESET
    rules: tuple[dict, ...] = field(default=())

    @classmethod
    def of(cls, preset: str | None = None, rules: list[dict] | tuple[dict, ...] | None = None) -> "Policy":
        name = preset or DEFAULT_PRESET
        if rules is None:
            if name not in PRESETS:
                raise PolicyError(f"unknown policy preset {name!r}; known: {', '.join(PRESETS)}")
            rules = PRESETS[name]
        return cls(name, tuple(_check_rule(r) for r in rules))

    def level(self, module: str, finding: dict) -> str:
        """The level of one finding (``code``, ``item``, ``severity``) reported by ``module``."""
        code = finding.get("code")
        try:
            spec = registry.finding_code(module, code)
        except KeyError:
            spec = None
        default = spec.level if spec is not None else "info"
        facts = {"module": module, "code": code, "item": finding.get("item"),
                 "severity": finding.get("severity"), "level": default}
        for rule in self.rules:
            if all(want == "any" or facts.get(key) == want for key, want in rule["match"].items()):
                lv = rule["level"]
                if lv == "review" and not (spec is not None and spec.review_line):
                    return default                   # nobody to ask: a code needs a review line
                return lv
        return default

    def review_line(self, module: str, finding: dict) -> str | None:
        """The review line a review-level finding is asked on (its code's)."""
        try:
            return registry.finding_code(module, finding.get("code")).review_line
        except KeyError:
            return None

    def appealable(self, module: str, finding: dict) -> bool:
        """Whether a reject this finding causes may be appealed (D42; the code's flag)."""
        try:
            return registry.finding_code(module, finding.get("code")).appealable
        except KeyError:
            return False

    def to_json(self) -> dict:
        return {"preset": self.preset, "version": POLICY_VERSION, "rules": [dict(r) for r in self.rules]}

    @classmethod
    def from_json(cls, doc: dict | None) -> "Policy":
        if not isinstance(doc, dict):
            return cls.of(DEFAULT_PRESET)
        return cls.of(doc.get("preset"), doc.get("rules"))


def from_task_params(params: dict | None) -> Policy:
    """The policy a task names (``params.policy``; default when it names none)."""
    chosen = (params or {}).get("policy") or {}
    return Policy.of(chosen.get("preset") if isinstance(chosen, dict) else None)


def load(run_dir: str) -> Policy:
    """The policy frozen into the run directory (``run.json``'s ``policy``), else the default."""
    path = os.path.join(run_dir, "run.json")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return Policy.of(DEFAULT_PRESET)
    return Policy.from_json(doc.get("policy") if isinstance(doc, dict) else None)
