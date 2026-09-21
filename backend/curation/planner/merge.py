"""VLM request merge framework: what goes into one request (design doc 04, section 4.2).

The smallest mergeable thing is a :class:`MergeUnit`: one question one module asks
about one episode. Units of the same episode whose :class:`FramePolicy` is identical
can share one request, because they would send the same frames. A
:class:`MergeStrategy` turns units into :class:`MergeGroup` s under hard
:class:`MergeLimits`; the executor (``curation.planner.executor``) sends them.

Merge discipline (04 §4.2), enforced here:

1. every task's prompt text is the module's own text, verbatim; only the numbering
   and the output-format declaration around it are added;
2. the answer is split back by key and each part goes to the module's own parser;
3. a part that fails to parse falls back to a single request for that unit only;
4. a module may be merged only after the merge-vs-single consistency check
   (doc 10, section 3.4; ``curation.planner.consistency``);
5. merging can be switched off (``vlm.merge.enabled = false``).

Modules that are evidence chains (``task_success``, where one answer decides the
next question) do not declare units; the framework never touches them (D23).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from ..vlm_call_kinds import CALL_KIND_ORDER

#: Call kinds a single request can carry: v1's five latency tags, a data contract
#: (``vlm_call_kinds.py``); C3 adds only ``merged`` for merged requests.
SINGLE_CALL_KINDS: tuple[str, ...] = tuple(CALL_KIND_ORDER)
MERGED_CALL_KIND = "merged"

_MODULE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_PICK = re.compile(r"^linspace:([1-9][0-9]*)$")


@dataclass(frozen=True)
class FramePolicy:
    """How a unit's frames are decoded and picked; identical policies share frames."""

    decode: str                  # "interval" | "full_rate"
    interval_s: float | None
    max_side: int
    max_cams: int
    pick: str                    # e.g. "linspace:8" (per camera)

    def __post_init__(self) -> None:
        if self.decode not in ("interval", "full_rate"):
            raise ValueError(f"decode must be interval or full_rate, got {self.decode!r}")
        if self.decode == "interval":
            if isinstance(self.interval_s, bool) or not isinstance(self.interval_s, (int, float)) \
                    or self.interval_s <= 0:
                raise ValueError(f"interval decoding needs interval_s > 0, got {self.interval_s!r}")
            object.__setattr__(self, "interval_s", float(self.interval_s))   # 1 and 1.0 are one policy
        elif self.interval_s is not None:
            raise ValueError("full_rate decoding takes no interval_s")
        for name in ("max_side", "max_cams"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")
        if not isinstance(self.pick, str) or not self.pick.strip() or "/" in self.pick:
            raise ValueError("pick must be a non-empty string without '/', such as 'linspace:8'")

    @property
    def key(self) -> str:
        """Canonical text form; ``plan.json`` ``merge.groups[].frame_policy`` carries it.

        Exact: two policies have the same key only if they are equal (the interval is
        written with ``repr``, which round-trips, and ``pick`` cannot contain ``/``).
        """
        decode = "full_rate" if self.decode == "full_rate" else f"interval:{self.interval_s!r}"
        return f"{decode}/max_side:{self.max_side}/max_cams:{self.max_cams}/{self.pick}"

    def frames_per_camera(self) -> int | None:
        m = _PICK.match(self.pick)
        return int(m.group(1)) if m else None

    def max_images(self) -> int | None:
        """Upper bound of the images one request carries under this policy."""
        per_cam = self.frames_per_camera()
        return None if per_cam is None else per_cam * self.max_cams


@dataclass(frozen=True)
class MergeUnit:
    """One question of one module about one episode."""

    episode_index: int
    module_id: str
    frame_policy: FramePolicy
    prompt_part: str                             # the module's prompt text, verbatim
    parser: Callable[[str], Any] = field(compare=False, repr=False)
    call_kind: str                               # its latency tag when sent alone
    question: str = ""                           # tells apart several units of one module
    max_tokens: int | None = None                # output budget of its single request

    def __post_init__(self) -> None:
        if isinstance(self.episode_index, bool) or not isinstance(self.episode_index, int) \
                or self.episode_index < 0:
            raise ValueError(f"episode_index must be a non-negative integer, got {self.episode_index!r}")
        if not isinstance(self.module_id, str) or not _MODULE_ID.match(self.module_id):
            raise ValueError(f"bad module id {self.module_id!r}")
        if not isinstance(self.frame_policy, FramePolicy):
            raise ValueError("frame_policy must be a FramePolicy")
        if not isinstance(self.prompt_part, str) or not self.prompt_part.strip():
            raise ValueError(f"{self.module_id}: prompt_part must be non-empty text")
        if not callable(self.parser):
            raise ValueError(f"{self.module_id}: parser must be callable")
        if self.call_kind not in SINGLE_CALL_KINDS:
            raise ValueError(f"{self.module_id}: call_kind must be one of {SINGLE_CALL_KINDS} "
                             f"(the latency tags are a data contract), got {self.call_kind!r}")
        if self.max_tokens is not None and (isinstance(self.max_tokens, bool)
                                            or not isinstance(self.max_tokens, int)
                                            or self.max_tokens < 1):
            raise ValueError(f"{self.module_id}: max_tokens must be a positive integer")

    @property
    def key(self) -> tuple[int, str, str]:
        return (self.episode_index, self.module_id, self.question)


def estimate_tokens(text: str) -> int:
    """A deliberately high token count for ``text``: UTF-8 bytes / 3.

    ASCII runs about 4 characters per token and CJK about 1 character per token
    (3 bytes), so this over-counts both; over-counting only splits a group early.
    """
    return (len(text.encode("utf-8")) + 2) // 3


@dataclass(frozen=True)
class MergeLimits:
    """Hard limits of one request; a group that breaks one is split (04 §4.2).

    The defaults are conservative placeholders; the tuning task sets real values
    per model through the site config (``vlm.merge.*``).
    """

    max_units: int = 8               # tasks in one merged request
    max_images: int = 32             # v1's largest request (caption) sends <= 32 images
    max_prompt_tokens: int = 6000    # text part, estimated with estimate_tokens()
    context_window: int = 128000     # prompt text + images + reserved output
    image_tokens: int = 1024         # estimated tokens per image
    output_tokens: int = 512         # output reserved per unit without its own max_tokens

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"MergeLimits.{name} must be a positive integer, got {value!r}")

    def output_budget(self, units: Sequence[MergeUnit]) -> int:
        return sum(u.max_tokens or self.output_tokens for u in units)

    def check(self, units: Sequence[MergeUnit], n_images: int) -> str | None:
        """``None`` when one request carrying ``units`` fits, else the first limit broken."""
        if len(units) > self.max_units:
            return "max_units"
        if n_images > self.max_images:
            return "max_images"
        text = units[0].prompt_part if len(units) == 1 else compose_merged_prompt(units, n_images)
        prompt = estimate_tokens(text)
        if prompt > self.max_prompt_tokens:
            return "max_prompt_tokens"
        if prompt + n_images * self.image_tokens + self.output_budget(units) > self.context_window:
            return "context_window"
        return None


# ---------------------------------------------------------------- composing and splitting

MERGED_HEADER = (
    "You are given {frames} from one robot demonstration episode.\n"
    "Complete ALL of the following tasks. Return STRICT JSON with exactly\n"
    "these top-level keys: {keys}.\n"
    "The value of each key is your answer to that task, in the form the task asks for."
)


def task_key(position: int) -> str:
    """Key of the ``position``-th task (1-based) in a merged answer."""
    return f"task_{position}"


def compose_merged_prompt(units: Sequence[MergeUnit], n_images: int | None) -> str:
    """The text of a merged request: numbering and output format around verbatim prompts."""
    if len(units) < 2:
        raise ValueError("a merged request carries at least two units")
    frames = f"{n_images} frames" if n_images else "frames"
    keys = ", ".join(f'"{task_key(i)}"' for i in range(1, len(units) + 1))
    tasks = "\n\n".join(f"Task {i} ({u.module_id}): {u.prompt_part}"
                        for i, u in enumerate(units, 1))
    return MERGED_HEADER.format(frames=frames, keys=keys) + "\n\n" + tasks


def strip_reasoning(text: str) -> str:
    """Drop a reasoning model's ``<think>...</think>`` (same rule as v1's vlm_client)."""
    return text.rsplit("</think>", 1)[1] if "</think>" in text else text


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _json_object(text: str) -> dict | None:
    body = _FENCE.sub("", strip_reasoning(str(text)).strip()).strip()
    try:
        obj = json.loads(body)
    except ValueError:
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(body[start:end + 1])
        except ValueError:
            return None
    return obj if isinstance(obj, dict) else None


def split_answer(content: str, keys: Sequence[str]) -> dict[str, str] | None:
    """Split a merged answer into the text each task's parser gets.

    ``None`` when the answer is not a JSON object at all; otherwise only the keys
    that are present and not null. A string value is passed on as is; any other
    JSON value as its JSON text (``73`` -> ``"73"``).
    """
    obj = _json_object(content)
    if obj is None:
        return None
    out = {}
    for key in keys:
        value = obj.get(key)
        if value is None:
            continue
        out[key] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return out


# ---------------------------------------------------------------- groups and strategies

@dataclass(frozen=True)
class MergeGroup:
    """Units that go out in one request.

    They share one frame policy. The two strategies here keep a group inside one
    episode; a later cross-episode strategy (04 §4.4) may span several, and then
    supplies its own ``compose()`` to tell the episodes apart in the prompt.
    """

    units: tuple[MergeUnit, ...]
    split: bool = False              # a piece of a group that broke a limit
    reason: str | None = None        # the limit it broke

    def __post_init__(self) -> None:
        if not self.units:
            raise ValueError("a group needs at least one unit")
        if any(u.frame_policy != self.units[0].frame_policy for u in self.units[1:]):
            raise ValueError("a group's units share one frame policy")

    @property
    def episodes(self) -> tuple[int, ...]:
        """Episodes whose frames the request carries, in first-seen order."""
        return tuple(dict.fromkeys(u.episode_index for u in self.units))

    @property
    def receipt(self) -> str:
        """How its units were sent: split, merged or single (fallback is decided later)."""
        if self.split:
            return "split"
        return "merged" if len(self.units) > 1 else "single"

    @property
    def modules(self) -> tuple[str, ...]:
        return tuple(u.module_id for u in self.units)


class MergeStrategy(Protocol):
    """Decides which units share a request. A strategy may also define
    ``compose(units, n_images) -> str`` for its prompt; without one the executor uses
    :func:`compose_merged_prompt`. A new strategy needs no change to the executor."""

    name: str

    def group(self, units: Sequence[MergeUnit], limits: MergeLimits) -> list[MergeGroup]: ...


class NoMerge:
    """``none``: every unit goes out alone."""

    name = "none"

    def group(self, units: Sequence[MergeUnit], limits: MergeLimits) -> list[MergeGroup]:
        return [MergeGroup((u,)) for u in units]


def pack(units: Sequence[MergeUnit], limits: MergeLimits) -> list[MergeGroup]:
    """One group if it fits the limits, else greedy pieces in order (all marked split)."""
    n_images = units[0].frame_policy.max_images() or 0
    if len(units) == 1:
        return [MergeGroup(tuple(units))]
    reason = limits.check(units, n_images)
    if reason is None:
        return [MergeGroup(tuple(units))]
    pieces: list[list[MergeUnit]] = [[]]
    for unit in units:
        if pieces[-1] and limits.check(pieces[-1] + [unit], n_images) is not None:
            pieces.append([])
        pieces[-1].append(unit)
    return [MergeGroup(tuple(p), split=True, reason=reason) for p in pieces]


class PerEpisodeMultiModule:
    """``per_episode_multi_module``: one request per (episode, frame policy).

    ``allowed`` restricts merging to the module groups a plan proposed
    (``[(modules, frame_policy_key), ...]``); ``None`` merges any units whose
    frame policies are identical.
    """

    name = "per_episode_multi_module"

    def __init__(self, allowed: Iterable[tuple[Iterable[str], str]] | None = None):
        self.allowed = None if allowed is None else [(frozenset(mods), str(key))
                                                     for mods, key in allowed]

    def _slot(self, unit: MergeUnit) -> int | None:
        if self.allowed is None:
            return 0
        for i, (mods, key) in enumerate(self.allowed):
            if unit.module_id in mods and unit.frame_policy.key == key:
                return i
        return None

    def group(self, units: Sequence[MergeUnit], limits: MergeLimits) -> list[MergeGroup]:
        order: list[tuple[str, Any]] = []
        buckets: dict[tuple, list[MergeUnit]] = {}
        for unit in units:
            slot = self._slot(unit)
            if slot is None:
                order.append(("alone", unit))
                continue
            key = (unit.episode_index, unit.frame_policy, slot)
            if key not in buckets:
                buckets[key] = []
                order.append(("bucket", key))
            buckets[key].append(unit)
        out: list[MergeGroup] = []
        for kind, item in order:
            out.extend([MergeGroup((item,))] if kind == "alone" else pack(buckets[item], limits))
        return out


STRATEGIES = ("none", "per_episode_multi_module")


def strategy_named(name: str) -> MergeStrategy:
    if name == "none":
        return NoMerge()
    if name == "per_episode_multi_module":
        return PerEpisodeMultiModule()
    raise ValueError(f"unknown merge strategy {name!r}; known: {STRATEGIES}")


def strategy_for_stage(stage: Mapping[str, Any] | None, *, enabled: bool = True) -> MergeStrategy:
    """The strategy ``check --plan-stage`` runs: the plan's proposal, or ``none``.

    ``enabled=False`` is the kill switch on the executing side; the planner already
    proposes ``none`` when the site switched merging off.
    """
    merge = dict((stage or {}).get("merge") or {})
    name = merge.get("strategy", "none")
    if name not in STRATEGIES:
        raise ValueError(f"unknown merge strategy {name!r}; known: {STRATEGIES}")
    if not enabled or name == "none":
        return NoMerge()
    return PerEpisodeMultiModule(allowed=[(g["modules"], g["frame_policy"])
                                          for g in merge.get("groups") or []])


def merge_enabled(config: Mapping[str, Any] | None) -> bool:
    """``vlm.merge.enabled`` of a nested config mapping (default true)."""
    value = (((config or {}).get("vlm") or {}).get("merge") or {}).get("enabled", True)
    if not isinstance(value, bool):
        raise ValueError(f"vlm.merge.enabled must be true or false, got {value!r}")
    return value


# ---------------------------------------------------------------- module declarations

@dataclass(frozen=True)
class DeclaredMergeUnits:
    """What a "sample N frames, ask once" VLM module puts in ``ModuleSpec.merge_units``.

    The planner reads ``frame_policy`` to propose groups; the check process calls
    the object per episode to get the units (doc 05, section 7, step 4).
    """

    frame_policy: FramePolicy
    call_kind: str
    build: Callable[[int, Mapping[str, Any]], Sequence[MergeUnit]] = field(compare=False,
                                                                          repr=False)
    units_per_episode: int = 1

    def __post_init__(self) -> None:
        if self.call_kind not in SINGLE_CALL_KINDS:
            raise ValueError(f"call_kind must be one of {SINGLE_CALL_KINDS}, got {self.call_kind!r}")
        if isinstance(self.units_per_episode, bool) or not isinstance(self.units_per_episode, int) \
                or self.units_per_episode < 1:
            raise ValueError("units_per_episode must be a positive integer")

    def __call__(self, episode_index: int,
                 context: Mapping[str, Any] | None = None) -> list[MergeUnit]:
        units = list(self.build(episode_index, dict(context or {})))
        for unit in units:
            if unit.episode_index != episode_index:
                raise ValueError(f"{unit.module_id}: unit for episode {unit.episode_index} "
                                 f"returned for episode {episode_index}")
            if unit.frame_policy != self.frame_policy or unit.call_kind != self.call_kind:
                raise ValueError(f"{unit.module_id}: units must use the declared frame policy "
                                 "and call kind")
        return units


def declared_frame_policy(merge_units: Any) -> FramePolicy | None:
    """The frame policy a registry entry's ``merge_units`` declares, if any."""
    policy = getattr(merge_units, "frame_policy", None)
    return policy if isinstance(policy, FramePolicy) else None
