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

Two presets ship (``PRESETS``): ``default`` (no rule) and ``report_only`` (nobody is asked and every
finding is reported; only data integrity's blocking findings still reject - an empty, cut or unreadable
file leaves nothing for the modules after it, requester 2026-10-05, policy version 2; version 1 made
everything info). The task names one in ``params.policy`` (C4 ``TaskParams.policy``); the Daemon freezes
the full table into ``run.json`` at start (``policy``) and every result revision keeps a copy
(``policy.json``), so a task keeps the rules it started with.

From registry 5.0 on the frozen table carries the default levels too (``defaults``: module -> code -> level, for
the task's modules): a default that changes later (5.0 made the EEF module's ``inconsistent`` info, design doc 25
§7.3) does not change a task that started before. A run frozen without them reads the defaults its
``registry_version`` had (``LEGACY_DEFAULTS``), so an older task's next revision keeps its EEF rejects.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..contracts import modules as registry

#: 2 (2026-10-05): report_only keeps data integrity's blocking findings; 1 made everything info.
POLICY_VERSION = "2"
LEVELS = tuple(lv for lv, _ in registry.FINDING_LEVELS)
MATCH_KEYS = ("module", "code", "item", "severity", "level")

#: default levels that changed, by the registry version that changed them: a run from before that version and
#: without frozen defaults keeps the old level (design doc 25 §7.3)
LEGACY_DEFAULTS: dict[tuple[int, int], dict[tuple[str, str], str]] = {
    (5, 0): {("eef_video_consistency", "inconsistent"): "blocking"},
}
#: likewise the codes whose reject could be appealed (D42) before that version
LEGACY_APPEALABLE: dict[tuple[int, int], set[tuple[str, str]]] = {
    (5, 0): {("eef_video_consistency", "inconsistent")},
}

#: Modules whose blocking findings reject even under report_only: the modules after them cannot use
#: an episode whose files are empty, cut short or unreadable (requester, 2026-10-05).
REPORT_ONLY_GATES = ("data_integrity",)

#: The presets a task can name (C4 TaskParams.policy.preset).
PRESETS: dict[str, tuple[dict, ...]] = {
    "default": (),
    "report_only": tuple({"match": {"module": m, "level": "blocking"}, "level": "blocking"} for m in REPORT_ONLY_GATES)
    + ({"match": {"level": "any"}, "level": "info"},),
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


def _version(v) -> tuple[int, int] | None:
    try:
        major, minor = str(v).split(".")[:2]
        return int(major), int(minor)
    except (ValueError, TypeError):
        return None


def legacy_appealable(registry_version) -> tuple[tuple[str, str], ...] | None:
    """The appealable codes a run of ``registry_version`` had, where they differ from today's (None: today's)."""
    ver = _version(registry_version)
    extra = {key for changed_in, keys in LEGACY_APPEALABLE.items() if ver is not None and ver < changed_in
             for key in keys}
    if not extra:
        return None
    now = {(m.id, c.code) for m in registry.MODULES for c in m.codes if c.appealable}
    return tuple(sorted(now | extra))


def legacy_defaults(registry_version) -> tuple[tuple[str, str, str], ...]:
    """The default levels a run of ``registry_version`` had where they differ from today's."""
    ver = _version(registry_version)
    if ver is None:
        return ()
    out: dict[tuple[str, str], str] = {}
    for changed_in, levels in sorted(LEGACY_DEFAULTS.items()):
        if ver < changed_in:
            for key, level in levels.items():
                out.setdefault(key, level)
    return tuple((m, c, lv) for (m, c), lv in sorted(out.items()))


@dataclass(frozen=True)
class Policy:
    preset: str = DEFAULT_PRESET
    rules: tuple[dict, ...] = field(default=())
    #: frozen default levels, (module, code, level): they win over the registry's (5.0, design doc 25 §7.3)
    defaults: tuple[tuple[str, str, str], ...] = field(default=())
    #: frozen appealable codes, (module, code); None: the registry's
    appeals: tuple[tuple[str, str], ...] | None = None

    @classmethod
    def of(cls, preset: str | None = None, rules: list[dict] | tuple[dict, ...] | None = None) -> "Policy":
        name = preset or DEFAULT_PRESET
        if rules is None:
            if name not in PRESETS:
                raise PolicyError(f"unknown policy preset {name!r}; known: {', '.join(PRESETS)}")
            rules = PRESETS[name]
        return cls(name, tuple(_check_rule(r) for r in rules))

    def with_defaults(self, modules) -> "Policy":
        """This policy with the registry's default levels of ``modules`` frozen in (a task's start)."""
        frozen = []
        for mid in modules:
            try:
                spec = registry.get(mid)
            except KeyError:
                continue
            frozen += [(mid, c.code, c.level) for c in spec.codes]
        appeals = {(m, c) for m in modules for c in _appealable_codes(m)}
        return Policy(self.preset, self.rules, tuple(sorted(set(frozen))), tuple(sorted(appeals)))

    def default_level(self, module: str, code, spec=None) -> str:
        for m, c, lv in self.defaults:
            if m == module and c == code:
                return lv
        return spec.level if spec is not None else "info"

    def level(self, module: str, finding: dict) -> str:
        """The level of one finding (``code``, ``item``, ``severity``) reported by ``module``."""
        code = finding.get("code")
        try:
            spec = registry.finding_code(module, code)
        except KeyError:
            spec = None
        default = self.default_level(module, code, spec)
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
        """Whether a reject this finding causes may be appealed (D42; the code's flag, as frozen)."""
        if self.appeals is not None:
            return (module, finding.get("code")) in self.appeals
        try:
            return registry.finding_code(module, finding.get("code")).appealable
        except KeyError:
            return False

    def to_json(self) -> dict:
        out = {"preset": self.preset, "version": POLICY_VERSION, "rules": [dict(r) for r in self.rules]}
        if self.defaults:
            table: dict[str, dict[str, str]] = {}
            for m, c, lv in self.defaults:
                table.setdefault(m, {})[c] = lv
            out["defaults"] = table
        if self.appeals is not None:
            out["appealable"] = [list(x) for x in self.appeals]
        return out

    @classmethod
    def from_json(cls, doc: dict | None, registry_version=None) -> "Policy":
        """The frozen policy; without frozen defaults, a run of ``registry_version`` keeps the defaults it had."""
        if not isinstance(doc, dict):
            base = cls.of(DEFAULT_PRESET)
        else:
            base = cls.of(doc.get("preset"), doc.get("rules"))
        table = doc.get("defaults") if isinstance(doc, dict) else None
        listed = doc.get("appealable") if isinstance(doc, dict) else None
        if isinstance(table, dict) and table:
            frozen = tuple(sorted((str(m), str(c), str(lv)) for m, codes in table.items() if isinstance(codes, dict)
                                  for c, lv in codes.items() if lv in LEVELS))
            appeals = tuple(sorted((str(x[0]), str(x[1])) for x in listed if isinstance(x, (list, tuple)) and len(x) == 2)) \
                if isinstance(listed, list) else None
        else:
            frozen, appeals = legacy_defaults(registry_version), legacy_appealable(registry_version)
        return cls(base.preset, base.rules, frozen, appeals)


def _appealable_codes(module: str) -> list[str]:
    try:
        return [c.code for c in registry.get(module).codes if c.appealable]
    except KeyError:
        return []


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
    if not isinstance(doc, dict):
        return Policy.of(DEFAULT_PRESET)
    return Policy.from_json(doc.get("policy"), doc.get("registry_version"))
