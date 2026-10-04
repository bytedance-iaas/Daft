"""Curve groups of a LeRobot dataset (design doc 18 §5.3).

1. Only float features are curves (integers are indices and labels, booleans are flags); the
   bookkeeping columns never are.
2. A state feature and the action feature of the same name are one group: ``observation.state``
   with ``action``, ``observation.state.left_arm`` with ``action.left_arm`` (Galaxea's split
   columns), ``robot0.observation.state.X`` with ``robot0.action.X``. Their dimensions pair by
   position when both have as many (the names often spell the same joint two ways), else by name;
   state lines are drawn solid, action dashed.
3. Gripper dimensions (a name containing ``gripper``) get a group of their own.
4. At most 8 dimensions per group: more are split by the names' common prefix
   (``left_* / right_*``, ``arm_left_* / arm_right_*``, ``kLeft* / kRight*``), and what is still
   too long into groups of 7.
5. Dimensions without names are ``dim_0 … dim_n``.
6. Every other float feature (``observation.force``, HABIT's ``robot0.*``) becomes groups too, but
   only the paired groups (and a lone ``observation.state`` / ``action``) are ``smart``: the smart
   layout shows those, the rest are offered in the + / 更换 menus.
7. Names are the dataset's own (requester, 2026-10-04: nothing translated): ``observation.state /
   action``, a part after `` · `` (``left_joint``, ``gripper``), a feature key.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .lerobot_info import BOOKKEEPING, flat_names, width_of

FLOAT_DTYPES = frozenset({"float16", "float32", "float64"})
MAX_DIMS = 8
CHUNK = 7
_STATE = "observation.state"
_ACTION = "action"
_SLUG = re.compile(r"[^0-9A-Za-z_-]+")
_TOKENS = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


@dataclass
class Line:
    name: str
    role: str                     # state | action | other
    source: str                   # feature key
    dim: int

    def as_dict(self) -> dict:
        return {"name": self.name, "role": self.role, "source": self.source, "dim": self.dim,
                "unit": None}


@dataclass
class Group:
    key: str
    name: str
    lines: list[Line]
    smart: bool
    sources: list[str] = field(default_factory=list)

    def as_stream(self) -> dict:
        """C4 ``VizStream`` (kind series)."""
        return {"key": self.key, "kind": "series", "name": self.name, "unit": None,
                "lines": [ln.as_dict() for ln in self.lines], "smart": self.smart,
                "available": True, "reason": None, "sources": list(self.sources), "rate_hz": None}


def slug(text: str) -> str:
    return _SLUG.sub("_", text).strip("_") or "x"


def _role_of(key: str) -> tuple[str, str, str] | None:
    """(role, prefix, suffix) of a state / action feature key, else None."""
    for marker, role in ((_STATE, "state"), (_ACTION, "action")):
        if key == marker:
            return role, "", ""
        if key.startswith(marker + "."):
            return role, "", key[len(marker) + 1:]
        hit = f".{marker}"
        if hit + "." in key or key.endswith(hit):
            pre, _, rest = key.partition(hit)
            return role, pre, rest.lstrip(".")
    return None


def _names(feat: dict) -> list[str]:
    n = width_of(feat)
    names = flat_names(feat.get("names"), n)
    if not names or len(names) != n:
        return [f"dim_{i}" for i in range(n)]
    return names


def _slots(state: tuple[str, dict] | None, action: tuple[str, dict] | None) -> list:
    """[(label, [Line])] pairing a state and an action feature by name or by position."""
    sk, ak = (state or (None, None))[0], (action or (None, None))[0]

    def slot(label: str, sd: int | None, ad: int | None):
        lines = []
        if sd is not None:
            lines.append(Line(label, "state", sk, sd))
        if ad is not None:
            lines.append(Line(label, "action", ak, ad))
        return (label, lines)

    if state and action:
        sn, an = _names(state[1]), _names(action[1])
        if len(sn) == len(an):
            # the same dimensions under two spellings (/hdas/feedback_arm_left.position[0] and
            # /motion_target/target_joint_state_arm_left.position[0]): pair them by position
            labels = sn if _named(sn) or not _named(an) else an
            return [slot(labels[i], i, i) for i in range(len(sn))]
        if set(sn) & set(an) and _named(sn) and _named(an):
            where = {a: i for i, a in enumerate(an)}
            out = [slot(s, i, where.get(s)) for i, s in enumerate(sn)]
            return out + [slot(a, None, i) for i, a in enumerate(an) if a not in set(sn)]
        return [slot(s, i, None) for i, s in enumerate(sn)] + [slot(a, None, i) for i, a in enumerate(an)]
    names = _names((state or action)[1])
    if state:
        return [slot(n, i, None) for i, n in enumerate(names)]
    return [slot(n, None, i) for i, n in enumerate(names)]


def _other_slots(key: str, feat: dict) -> list:
    return [(n, [Line(n, "other", key, i)]) for i, n in enumerate(_names(feat))]


def _named(names: list[str]) -> bool:
    return not all(re.fullmatch(r"(dim|position)_\d+", n) for n in names)


def _tokens(label: str) -> list[str]:
    parts = []
    for chunk in re.split(r"[._/\[\]\s-]+", label):
        parts += _TOKENS.findall(chunk) or ([chunk] if chunk else [])
    return parts


def _common_prefix(labels: list[str]) -> str:
    if not labels:
        return ""
    lo, hi = min(labels), max(labels)
    n = 0
    while n < min(len(lo), len(hi)) and lo[n] == hi[n]:
        n += 1
    return lo[:n].rstrip("_.-/ [")


def _families(slots: list, depth: int) -> dict[str, list] | None:
    fams: dict[str, list] = {}
    for s in slots:
        toks = _tokens(s[0])
        if len(toks) <= depth:
            return None
        fams.setdefault("_".join(toks[:depth]), []).append(s)
    return fams


def _split(slots: list, depth: int = 1, *, nested: bool = False) -> list[tuple[str, list]]:
    """[(part name, slots)] with at most MAX_DIMS slots each (rule 4): split by the first name
    tokens that give 2-6 families, a family still too big once more by the next token, and what is
    left too long into groups of CHUNK."""
    if len(slots) <= MAX_DIMS:
        return [("", slots)]
    for d in range(depth, depth + 3):
        fams = _families(slots, d)
        if fams is None:
            break
        limit = 3 if nested else 6            # a family split once more: arm / leg, never six crumbs
        if 2 <= len(fams) <= limit and all(len(v) < len(slots) for v in fams.values()) and (
                not nested or min(len(v) for v in fams.values()) >= 2):
            out = []
            for members in fams.values():
                name = _common_prefix([m[0] for m in members]) or "_".join(_tokens(members[0][0])[:d])
                if len(members) <= MAX_DIMS:
                    out.append((name, members))
                elif not nested:
                    out += [((p if p.startswith(name) else f"{name} {p}") if p else name, m)
                            for p, m in _split(members, d + 1, nested=True)]
                else:
                    out += [(f"{name} {i + 1}–{min(i + CHUNK, len(members))}", members[i:i + CHUNK])
                            for i in range(0, len(members), CHUNK)]
            return out
    return [(f"{i + 1}–{min(i + CHUNK, len(slots))}", slots[i:i + CHUNK])
            for i in range(0, len(slots), CHUNK)]


def curve_groups(info: dict, *, exclude: set[str] | frozenset = frozenset()) -> list[Group]:
    """Every curve group of the dataset, the smart ones first (feature order within each)."""
    feats = {k: f for k, f in (info.get("features") or {}).items()
             if isinstance(f, dict) and str(f.get("dtype")) in FLOAT_DTYPES
             and k not in BOOKKEEPING and k not in exclude and not k.startswith("meta.")}
    order = {k: i for i, k in enumerate(feats)}
    pairs: dict[tuple[str, str], dict[str, str]] = {}
    for k in feats:
        r = _role_of(k)
        if r is not None:
            role, pre, suf = r
            pairs.setdefault((pre, suf), {})[role] = k
    groups: list[Group] = []
    seen_keys: set[str] = set()

    def add(base_key: str, base_name: str, slots: list, smart: bool, sources: list[str]) -> None:
        grippers = [s for s in slots if "gripper" in s[0].lower()]
        rest = [s for s in slots if "gripper" not in s[0].lower()]
        parts = _split(rest) if rest else []
        if grippers and rest:
            parts.append(("gripper", grippers))
        elif grippers:
            parts.append(("", grippers))
        for part, members in parts:
            key = base_key if not part else f"{base_key}.{slug(part)}"
            n = 2
            while key in seen_keys:
                key, n = f"{base_key}.{slug(part) or 'x'}_{n}", n + 1
            seen_keys.add(key)
            label = base_name if not part else f"{base_name} · {part}"
            lines = [ln for _, ls in members for ln in ls]
            if lines:
                groups.append(Group(key, label, lines, smart, list(sources)))

    used: set[str] = set()
    ordered = sorted(pairs.items(), key=lambda kv: min(order[v] for v in kv[1].values()))
    any_pair = any(len(v) == 2 for _, v in ordered)
    for (pre, suf), members in ordered:
        sk, ak = members.get("state"), members.get("action")
        paired = sk is not None and ak is not None
        root = pre == "" and suf == ""
        if root:
            name = f"{sk} / {ak}" if paired else (sk or ak)
        else:
            name = ".".join(x for x in (pre, suf) if x)
        base_key = slug(f"{pre}_{_STATE}_{suf}".strip("_").replace(".", "_")) if paired else slug(
            (sk or ak).replace(".", "_"))
        slots = _slots((sk, feats[sk]) if sk else None, (ak, feats[ak]) if ak else None)
        if paired and not any(len(ls) == 2 for _, ls in slots):
            # nothing pairs up (15 state numbers, 8 action numbers, no names): two groups, not one
            for role, key in (("state", sk), ("action", ak)):
                mine = [(lbl, ls) for lbl, ls in slots if ls and ls[0].role == role]
                add(f"{base_key}.{role}", key, mine, True, [key])
        else:
            add(base_key, name, slots, paired or (root and not any_pair), [x for x in (sk, ak) if x])
        used.update(x for x in (sk, ak) if x)
    for k, f in feats.items():
        if k not in used:
            add(slug(k.replace(".", "_")), k, _other_slots(k, f), False, [k])
    root = [g for g in groups if g.smart and g.sources and set(g.sources) <= {_STATE, _ACTION}]
    return root + [g for g in groups if g.smart and g not in root] + [g for g in groups if not g.smart]
