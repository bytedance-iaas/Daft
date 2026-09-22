"""C1 - module registry: the one place that says what the QA modules are.

The CLI, the Daemon and the frontend all read the module list, Chinese names,
capability needs, stage, dependencies and tunable parameters from here (the
frontend through ``GET /api/v1/modules``). Nobody hard-codes a module list.

Two facts from v1 shape it (design doc 05, section 1):

* v1 has "six + two": the six funnel checks run per episode and vote on the
  verdict; ``dedup`` and ``skill_profile`` run afterwards on the kept set only.
  ``stage`` and ``depends_on`` carry that, so the planner can order the stages
  and knows which results go stale when an upstream verdict changes.
* ``autolabel`` (captioning episodes without a task text) is not a module; it
  is a shared prerequisite of ``task_success`` and ``skill_profile``.

``param_schema`` drives the second screen of the new-task form (D38), so every
parameter carries ``title`` (the field label), ``description`` (help text) and
``default``; a choice lists its options as ``oneOf`` of ``{const, title}``, and
required parameters go into the object's ``required``. Adding a module with
parameters needs no frontend change.

Human review is declared here too (D42, D43). ``REVIEW_LINES`` is the catalog of
the questions a person can be asked - line id, the list its episodes are in,
whether an open item counts as pending, the decisions with their titles - and
every module names the lines it raises (``review_lines``) and whether a reject
attributed to it may be appealed (``appealable``). A new kind of review is a new
catalog entry plus its apply rule in ``adjudicate-apply``; the REST and CLI
contracts carry lines and decisions as open strings, and the frontend renders a
line it has no dedicated view for from this catalog.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

REGISTRY_VERSION = "1.3"

Level = Literal["episode", "dataset"]
Gate = Literal["hard", "soft", "dedup", "none"]
Stage = Literal["numeric", "frame", "vlm", "post_verdict"]

#: Stages in execution order (design doc 04, section 2).
STAGE_ORDER: tuple[str, ...] = ("numeric", "frame", "vlm", "post_verdict")

#: Capabilities a dataset or task must provide (design doc 05, section 2).
NEEDS: frozenset[str] = frozenset({"timestamps", "action", "state", "video",
                                   "embodiment_profile", "vlm", "raw_bytes"})

#: What a module's input depends on. A change upstream makes the module stale.
#: numeric_gates / frame_gates: survivors of the hard gates of that stage;
#: funnel_verdict: the keep set after the funnel verdict.
DEPENDENCIES: frozenset[str] = frozenset({"numeric_gates", "frame_gates", "autolabel",
                                          "funnel_verdict", "dedup"})

#: v1's evidence modes (``pipeline.sync_plots`` / ``pipeline.evidence_frames``).
EVIDENCE_MODES = ("flagged", "all", "off")


@dataclass(frozen=True)
class TableSpec:
    """A detail table of the module's report section (``GET .../report/tables/{id}``)."""

    id: str
    title_zh: str
    sortable: tuple[str, ...]            # whitelist for ``?sort=``
    default_sort: str = "episode_index"

    def to_json(self) -> dict:
        return {"id": self.id, "title_zh": self.title_zh, "sortable": list(self.sortable),
                "default_sort": self.default_sort}


@dataclass(frozen=True)
class FollowUp:
    """A question a card gains once one of ``after`` is its answer on the owning line.

    v1's relabel card: after adopting a new label a person may also give the task
    verdict right away (the machine then takes it and does not re-judge); left open,
    the episode is judged again with the new label. Only on cards that do not ask
    ``line`` already; the answer lapses when the decision that opened it changes.
    """

    after: tuple[str, ...]               # decisions of the owning line that open it
    line: str                            # the REVIEW_LINES id it answers on
    decisions: tuple[str, ...]           # the subset of that line's decisions it offers
    optional: bool = True                # may stay open; never counts as pending

    def to_json(self) -> dict:
        return {"after": list(self.after), "line": self.line, "decisions": list(self.decisions),
                "optional": self.optional}


@dataclass(frozen=True)
class ReviewLine:
    """One kind of question a person answers on the adjudication page (design doc 06 §5)."""

    id: str                              # C4 ``line`` and decisions.json ``line``
    review_kind: str                     # the ``kind`` of review.json items (v1's queue names)
    title_zh: str
    applies_to: Literal["passed", "reject"]   # the list its episodes are in when asked
    counts_as_pending: bool              # an open item must be decided (vs. may be appealed)
    decisions: tuple[tuple[str, str], ...]    # (value, button title), in display order
    follow_ups: tuple[FollowUp, ...] = ()

    def to_json(self) -> dict:
        return {"id": self.id, "review_kind": self.review_kind, "title_zh": self.title_zh,
                "applies_to": self.applies_to, "counts_as_pending": self.counts_as_pending,
                "decisions": [{"const": c, "title": title} for c, title in self.decisions],
                "follow_ups": [f.to_json() for f in self.follow_ups]}


#: The review lines of v1 (design doc 06 §5.1). ``discard`` drops the whole
#: episode and wins over any task verdict; ``unsure`` is recorded and changes nothing.
REVIEW_LINES: tuple[ReviewLine, ...] = (
    ReviewLine("label", "label_conflict", "标注分歧", "passed", True,
               (("adopt_suggestion", "采纳新标注"), ("custom_label", "自行改写标注"),
                ("keep_label", "维持原标注"), ("unsure", "拿不准"),
                ("discard", "其它原因，整条弃用")),
               follow_ups=(FollowUp(("adopt_suggestion", "custom_label"), "task_verdict",
                                    ("success", "failure", "unsure")),)),
    ReviewLine("task_verdict", "task_verdict", "任务成败弃权", "passed", True,
               (("success", "判成功"), ("failure", "判失败"), ("unsure", "拿不准"),
                ("discard", "其它原因，整条弃用"))),
    ReviewLine("reject_appeal", "reject_appeal", "被拒复议", "reject", False,
               (("restore", "恢复为可用"), ("keep_rejected", "维持拒绝"), ("unsure", "拿不准"))),
)


@dataclass(frozen=True)
class ModuleSpec:
    id: str
    name_zh: str
    summary_zh: str
    level: Level
    gate: Gate                           # hard veto / soft score / dedup removal / output only
    needs: frozenset[str]
    stage: Stage
    depends_on: tuple[str, ...]
    produces_adjudication: bool          # contributes to the human adjudication queue
    param_schema: dict[str, Any]         # JSON Schema of task-level ``modules[].params``
    tables: tuple[TableSpec, ...] = ()
    merge_units: Callable | None = field(default=None, compare=False)  # design doc 04 §4.2
    review_lines: tuple[str, ...] = ()   # REVIEW_LINES ids this module raises
    appealable: bool = False             # a reject attributed to it may be appealed (D42)

    def to_json(self) -> dict:
        return {"id": self.id, "name_zh": self.name_zh, "summary_zh": self.summary_zh,
                "level": self.level, "gate": self.gate, "needs": sorted(self.needs),
                "stage": self.stage, "depends_on": list(self.depends_on),
                "produces_adjudication": self.produces_adjudication,
                "review_lines": list(self.review_lines), "appealable": self.appealable,
                "param_schema": self.param_schema,
                "tables": [t.to_json() for t in self.tables],
                "mergeable": self.merge_units is not None}


def _no_params() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _evidence_param(name: str, title: str, labels: dict[str, str], description: str) -> dict:
    options = [{"const": mode, "title": labels[mode]} for mode in EVIDENCE_MODES]
    return {"type": "object", "additionalProperties": False,
            "properties": {name: {"title": title, "description": description,
                                  "default": "flagged", "oneOf": options}}}


MODULES: tuple[ModuleSpec, ...] = (
    ModuleSpec(
        id="timestamp_check", name_zh="时间戳检查",
        summary_zh="时间戳是否单调、有没有丢帧跳变、是不是短于残段阈值的碎片",
        level="episode", gate="hard", needs=frozenset({"timestamps"}), stage="numeric",
        depends_on=(), produces_adjudication=False, param_schema=_no_params(),
        tables=(TableSpec("timestamp_check", "时间戳异常",
                          ("episode_index", "duration_s", "max_dt")),)),
    ModuleSpec(
        id="kinematic_limits", name_zh="运动学极限",
        summary_zh="对照机器人规格库检查关节位置与速度是否越限",
        level="episode", gate="hard", needs=frozenset({"action", "embodiment_profile"}),
        stage="numeric", depends_on=(), produces_adjudication=False,
        param_schema=_no_params(),
        tables=(TableSpec("kinematic_violations", "越限明细",
                          ("episode_index", "joint", "value")),)),
    ModuleSpec(
        id="motion_quality", name_zh="运动质量",
        summary_zh="动作是否平滑、有无尖刺与执行器卡死、操作是否流畅（打分项）",
        level="episode", gate="soft", needs=frozenset({"action", "state"}), stage="numeric",
        depends_on=(), produces_adjudication=False, param_schema=_no_params(),
        tables=(TableSpec("motion_quality", "运动质量明细", ("episode_index", "score")),)),
    ModuleSpec(
        id="visual_quality", name_zh="视觉质量",
        summary_zh="逐机位检查清晰度、曝光与画面冻结（打分项）",
        level="episode", gate="soft", needs=frozenset({"video"}), stage="frame",
        depends_on=("numeric_gates",), produces_adjudication=False,
        param_schema=_no_params(),
        tables=(TableSpec("visual_quality", "逐机位打分", ("episode_index", "score", "camera")),)),
    ModuleSpec(
        id="video_action_sync", name_zh="视频-动作同步",
        summary_zh="逐机位比对画面运动与关节速度，找出画面与动作的时间错位",
        level="episode", gate="hard", needs=frozenset({"video", "action"}), stage="frame",
        depends_on=("numeric_gates",), produces_adjudication=False,
        param_schema=_evidence_param(
            "sync_plots", "同步曲线证据图",
            {"flagged": "有标注或未对齐的", "all": "全部", "off": "不画"},
            "为哪些条目画画面运动与关节速度的对照曲线"),
        tables=(TableSpec("video_action_sync", "逐机位同步读数",
                          ("episode_index", "lag_s", "corr_peak")),)),
    ModuleSpec(
        id="task_success", name_zh="任务成败判定",
        summary_zh="由多模态模型看画面判断任务是否完成，拿不准的交给人工裁决",
        level="episode", gate="hard", needs=frozenset({"video", "vlm"}), stage="vlm",
        depends_on=("frame_gates", "autolabel"), produces_adjudication=True,
        param_schema=_evidence_param(
            "evidence_frames", "证据帧",
            {"flagged": "拒绝与待裁决的", "all": "全部", "off": "不存"},
            "为哪些条目保存判定时看过的画面"),
        tables=(TableSpec("task_success", "判定明细", ("episode_index", "verdict")),),
        review_lines=("task_verdict", "label"), appealable=True),
    ModuleSpec(
        id="dedup", name_zh="精确去重",
        summary_zh="找出动作与视频字节级完全相同的条目，只留遍历顺序里的第一条",
        level="dataset", gate="dedup", needs=frozenset({"raw_bytes"}), stage="post_verdict",
        depends_on=("funnel_verdict",), produces_adjudication=True,
        param_schema=_no_params(),
        tables=(TableSpec("dedup_groups", "重复组", ("episode_index", "duplicate_of")),),
        appealable=True),
    ModuleSpec(
        id="skill_profile", name_zh="技能画像",
        summary_zh="归纳两级技能体系并统计分布，检出标注与画面不一致的条目",
        level="dataset", gate="none", needs=frozenset({"video", "vlm"}), stage="post_verdict",
        depends_on=("funnel_verdict", "dedup", "autolabel"), produces_adjudication=True,
        param_schema=_no_params(),
        tables=(TableSpec("skill_assignment", "技能归属", ("episode_index", "family", "subskill")),),
        review_lines=("label",)),
)

_BY_ID = {m.id: m for m in MODULES}
_LINES = {line.id: line for line in REVIEW_LINES}


def get(module_id: str) -> ModuleSpec:
    try:
        return _BY_ID[module_id]
    except KeyError:
        raise KeyError(f"unknown module {module_id!r}; known: {', '.join(_BY_ID)}") from None


def ids() -> tuple[str, ...]:
    return tuple(_BY_ID)


def by_stage(stage: str) -> tuple[ModuleSpec, ...]:
    return tuple(m for m in MODULES if m.stage == stage)


def validate_params(module_id: str, params: dict | None) -> None:
    """Raise ``jsonschema.ValidationError`` if ``params`` do not fit the module."""
    import jsonschema

    jsonschema.validate(params or {}, get(module_id).param_schema)


def review_line(line_id: str) -> ReviewLine:
    try:
        return _LINES[line_id]
    except KeyError:
        raise KeyError(f"unknown review line {line_id!r}; known: {', '.join(_LINES)}") from None


def review_line_of_kind(review_kind: str) -> ReviewLine:
    """The line of a review.json item ``kind``."""
    for line in REVIEW_LINES:
        if line.review_kind == review_kind:
            return line
    raise KeyError(f"unknown review kind {review_kind!r}")


def follow_up(line_id: str, decision: str, target: str) -> FollowUp | None:
    """The follow-up ``decision`` on ``line_id`` opens for ``target``, if any."""
    for f in review_line(line_id).follow_ups:
        if f.line == target and decision in f.after:
            return f
    return None


def appealable(module_id: str) -> bool:
    """Whether a reject attributed to ``module_id`` may be appealed (D42)."""
    return get(module_id).appealable


def export() -> dict:
    """The registry as JSON (``GET /api/v1/modules`` and ``docs/contracts/modules.json``)."""
    return {"registry_version": REGISTRY_VERSION, "stages": list(STAGE_ORDER),
            "review_lines": [line.to_json() for line in REVIEW_LINES],
            "modules": [m.to_json() for m in MODULES]}
