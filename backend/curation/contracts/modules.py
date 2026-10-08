"""C1 - module registry: the one place that says what the QA modules are.

The CLI, the Daemon and the frontend all read the module list, Chinese names, capability needs,
where a module runs, its finding codes and tunable parameters from here (the frontend through
``GET /api/v1/modules``). Nobody hard-codes a module list, a code table or an item name.

Registry 2.0 (design doc 17, D56-D58) describes a module by three things:

* **Where it runs.** Two blocks run side by side and never filter each other (D57): the CPU block
  (integrity -> numeric -> frame -> dedup) and the VLM block (autolabel -> vlm -> profile). Stages
  inside a block exist to share a decode and to size their own concurrency; every stage gets every
  selected episode. ``dedup`` and ``profile`` need the whole selection and start once the stages
  before them in their block are done (``FULL_SET_STAGES``). ``depends_on`` is data only: the
  captions ``autolabel`` writes for episodes without a task text.
* **What it can find.** Every module carries its catalogue of finding codes (``codes``). A code maps
  to one item of the taxonomy (C6, ``docs/contracts/taxonomy.json``, bound version
  ``TAXONOMY_VERSION``) and has a default severity and a default level under the default policy:
  ``blocking`` (today's hard gate failures), ``review`` (today's suspects, abstentions and
  questions; it names the review line a person answers on) or ``info`` (reported only, which is
  what the retired soft scores became, P18). A blocking code may be ``appealable`` (D42). Whether an
  episode is rejected is no longer the module's business: a policy decides in ``aggregate``.
* **What it covers.** ``covers`` - the items the module assesses on an episode it can read: the
  items of its codes plus ``also_covers`` (items it only reports a reading for). A record lists
  what it assessed and what it could not, with a reason from ``UNASSESSABLE_REASONS``, so "found
  nothing" and "did not look" are told apart.

``autolabel`` (captioning episodes without a task text) is not a module; it is the first stage of
the VLM block and a data dependency of ``task_success`` and ``skill_profile``.

``param_schema`` drives the second screen of the new-task form (D38), so every parameter carries
``title`` (the field label), ``description`` (help text) and ``default``; a choice lists its
options as ``oneOf`` of ``{const, title}``, and required parameters go into the object's
``required``. Parameters that stand in for one another carry the same ``x-choice-group``
(``{id, title, required}``): a form offers the group as one field. File parameters are
``format: upload`` (see ``UPLOAD_FORMAT``). Adding a module with parameters needs no frontend
change.

Human review is declared here too (D42, D43). ``REVIEW_LINES`` is the catalogue of the questions a
person can be asked - line id, the list its episodes are in, whether an open item counts as
pending, the decisions with their titles. A review-level code names its line; a new kind of review
is a new catalogue entry plus its apply rule in ``adjudicate-apply``.

Modules v2 runs itself, outside v1's check configuration, are ``native`` (the data integrity and
EEF modules); a ``rides_on`` module is answered inside the model requests of the module it names
(the camera defects ride on task_success): it is never selected on its own and runs whenever its
host runs. Neither is part of the JSON export's semantics beyond ``rides_on``.

History: 1.x described modules by their funnel gate (``gate``: hard veto, soft score, dedup removal
or none), their input (``input_scope``: the funnel's survivors or every selected episode) and
whether they voted (``affects_dataset_verdict``). 2.0 dropped all three with the funnel: the task's
policy grades a module's findings (design doc 17 §4) and the two blocks hand every episode on (§3).
2.2 bound taxonomy 1.3: the time-length outlier and the undersampled skill family became the platform's own
dataset-level readings (no item: the task and skill statistics item was dropped), which motion_quality covered too.
3.0 binds taxonomy 2.0, which numbers the items of every dimension again without gaps (C6 ``renumbered`` maps
1.3 ids to 2.0); results of tasks run before keep the ids they were run with.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

REGISTRY_VERSION = "3.0"
#: The taxonomy (C6) this registry binds: every finding code names one of its items (design doc 17 §1.3).
TAXONOMY_VERSION = "2.0"

Level = Literal["episode", "dataset"]
Block = Literal["cpu", "vlm"]
Stage = Literal["integrity", "numeric", "frame", "dedup", "autolabel", "vlm", "profile"]

#: The two blocks and their stages in order (design doc 17 §3.1).
BLOCKS: dict[str, tuple[str, ...]] = {"cpu": ("integrity", "numeric", "frame", "dedup"),
                                      "vlm": ("autolabel", "vlm", "profile")}
BLOCK_TITLES: dict[str, str] = {"cpu": "CPU 块", "vlm": "VLM 块"}
#: Stages that need the whole selection: they start once their block's earlier stages are done (§3.2).
FULL_SET_STAGES: tuple[str, ...] = ("dedup", "profile")
#: Every stage, the CPU block's first.
STAGES: tuple[str, ...] = BLOCKS["cpu"] + BLOCKS["vlm"]

#: Capabilities a dataset or task must provide (design doc 05, section 2).
NEEDS: frozenset[str] = frozenset({"timestamps", "action", "state", "video",
                                   "embodiment_profile", "vlm", "raw_bytes", "eef_input"})

#: What a module's input depends on, data only (2.0): the captions of episodes without a task text.
#: A change upstream (a relabel) makes the module's results stale.
DEPENDENCIES: frozenset[str] = frozenset({"autolabel"})

#: A finding's severity, the module's own measure (design doc 17 §1.2).
SEVERITIES: tuple[str, ...] = ("high", "medium", "low")
#: A finding's level under a policy (design doc 17 §4): its id and the title the console shows.
FINDING_LEVELS: tuple[tuple[str, str], ...] = (("blocking", "判废"), ("review", "待人工复核"),
                                               ("info", "仅报告"))
#: What a code's findings are about, which decides how the console groups them.
SCOPE_KINDS: tuple[str, ...] = ("episode", "camera", "channel", "dataset")

#: Why a module could not assess an item on an episode (design doc 17 §1.4).
UNASSESSABLE_REASONS: tuple[tuple[str, str], ...] = (
    ("embodiment_not_in_library", "本体不在规格库里，或没有给机器人型号"),
    ("no_state_columns", "数据集没有状态量"),
    ("no_video", "没有视频"),
    ("no_action", "没有动作"),
    ("format_unsupported_by_module", "本模块不支持这种数据格式"),
    ("model_no_answer", "模型没有回答这一项"),
    ("single_description", "只有一份描述，无从比较"),
    ("not_applicable", "对本数据集不适用"),
)

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
    # the EEF module's episodes it could not settle (design doc 12 D-E13): a person looks at
    # the marked frames and says whether the declared end effector matches the video
    ReviewLine("eef_check", "eef_consistency", "EEF 与画面核对", "passed", True,
               (("consistent", "一致，判过"), ("inconsistent", "不一致，判废"), ("unsure", "拿不准"))),
    # the data integrity module's suspects (design doc 14 §4.4): a person looks at the findings
    # and says whether the files are fine; like every line on passed episodes it counts as pending
    ReviewLine("integrity_check", "integrity_suspect", "完整性存疑", "passed", True,
               (("intact", "数据无误，保留"), ("broken", "确有问题，判废"), ("unsure", "拿不准"))),
)


@dataclass(frozen=True)
class FindingCode:
    """One kind of problem a module reports (design doc 17 §2.1). Unique within the module; the
    global key is (module, code)."""

    code: str
    item: str | None                     # taxonomy item; None only for an info-level item of the platform's own
    name_zh: str
    severity: str                        # default severity: high / medium / low (a module may grade by measure)
    level: str                           # level under the default policy: blocking / review / info (P18)
    review_line: str | None = None       # review level: the REVIEW_LINES id a person answers on
    appealable: bool = False             # blocking level: a reject it caused may be appealed (D42)
    scope_kind: str = "episode"          # episode / camera / channel / dataset

    def to_json(self) -> dict:
        out = {"code": self.code, "item": self.item, "name_zh": self.name_zh,
               "severity": self.severity, "level": self.level, "scope_kind": self.scope_kind,
               "appealable": self.appealable}
        if self.review_line:
            out["review_line"] = self.review_line
        return out


def _blocking(code: str, item: str, name_zh: str, severity: str = "high", **kw) -> FindingCode:
    return FindingCode(code, item, name_zh, severity, "blocking", **kw)


def _review(code: str, item: str, name_zh: str, line: str, severity: str = "medium", **kw) -> FindingCode:
    return FindingCode(code, item, name_zh, severity, "review", review_line=line, **kw)


def _info(code: str, item: str | None, name_zh: str, severity: str = "low", **kw) -> FindingCode:
    return FindingCode(code, item, name_zh, severity, "info", **kw)


@dataclass(frozen=True)
class ModuleSpec:
    id: str
    name_zh: str
    summary_zh: str
    level: Level
    needs: frozenset[str]
    block: Block
    stage: str                           # a stage of its block (BLOCKS)
    depends_on: tuple[str, ...]          # DEPENDENCIES
    codes: tuple[FindingCode, ...]       # the finding code catalogue (design doc 17 §2.2)
    param_schema: dict[str, Any]         # JSON Schema of task-level ``modules[].params``
    tables: tuple[TableSpec, ...] = ()
    #: items assessed although no code names them: the module reports a reading only (§2.3)
    also_covers: tuple[str, ...] = ()
    merge_units: Callable | None = field(default=None, compare=False)  # design doc 04 §4.2
    #: v2 runs it itself, outside v1's check configuration (1.11); not part of the JSON
    native: bool = False
    #: 1.14: a rider is answered inside the model requests of the module it names - it is never
    #: selected on its own, runs whenever its host runs, and makes no request of its own
    rides_on: str | None = None

    @property
    def covers(self) -> tuple[str, ...]:
        """The items the module assesses: its codes' items, then ``also_covers``, without repeats."""
        return tuple(dict.fromkeys([c.item for c in self.codes if c.item] + list(self.also_covers)))

    @property
    def review_lines(self) -> tuple[str, ...]:
        """The REVIEW_LINES its review-level codes raise, in code order."""
        return tuple(dict.fromkeys(c.review_line for c in self.codes
                                   if c.level == "review" and c.review_line))

    @property
    def appealable(self) -> bool:
        """Whether some reject it causes may be appealed (D42): one of its blocking codes is."""
        return any(c.appealable for c in self.codes)

    @property
    def produces_adjudication(self) -> bool:
        """It raises review items or its rejects may be appealed."""
        return bool(self.review_lines) or self.appealable

    def code(self, code: str) -> FindingCode:
        for c in self.codes:
            if c.code == code:
                return c
        raise KeyError(f"module {self.id!r} has no finding code {code!r}; known: "
                       f"{', '.join(c.code for c in self.codes)}")

    def to_json(self) -> dict:
        out = {"id": self.id, "name_zh": self.name_zh, "summary_zh": self.summary_zh,
               "level": self.level, "needs": sorted(self.needs), "block": self.block,
               "stage": self.stage, "depends_on": list(self.depends_on),
               "codes": [c.to_json() for c in self.codes], "covers": list(self.covers),
               "param_schema": self.param_schema,
               "tables": [t.to_json() for t in self.tables],
               "mergeable": self.merge_units is not None}
        if self.rides_on:
            out["rides_on"] = self.rides_on
        return out


def _no_params() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _evidence_param(name: str, title: str, labels: dict[str, str], description: str) -> dict:
    options = [{"const": mode, "title": labels[mode]} for mode in EVIDENCE_MODES]
    return {"type": "object", "additionalProperties": False,
            "properties": {name: {"title": title, "description": description,
                                  "default": "flagged", "oneOf": options}}}


#: File parameters (1.5): ``format: upload`` plus the kind of file, the extensions the picker offers
#: and a size cap. In a task (C4) the value is an upload handle ``upload:<upload_id>`` returned by
#: ``POST /uploads``; on the command line it is a path. The Daemon resolves handles to paths.
UPLOAD_FORMAT = "upload"
UPLOAD_PREFIX = "upload:"


def _upload(kind: str, accept: list[str], max_mb: int, **fields) -> dict:
    return {"type": "string", "format": UPLOAD_FORMAT, "x-upload-kind": kind, "x-accept": accept,
            "x-max-mb": max_mb, **fields}


#: The EEF module's two ways to find the gripper in the picture (1.10): a form offers one of them.
#: Optional since 1.12 (D-E15): without either, the model gives its opinion only (design doc 12 §10.5).
GRIPPER_REFERENCE = {"id": "gripper_reference", "title": "夹爪参考", "required": False}


def upload_params(module_id: str) -> dict[str, str]:
    """param key -> upload kind of the module's file parameters."""
    props = get(module_id).param_schema.get("properties") or {}
    return {k: p["x-upload-kind"] for k, p in props.items() if p.get("format") == UPLOAD_FORMAT}


def _line(title: str, description: str, default: float, minimum: float, maximum: float) -> dict:
    """A judgement line of a finding (registry 2.1, design doc 17 §1.3): a number with a default. ``x-advanced``:
    the new-task form leaves it at its default (an API client may set it)."""
    return {"type": "number", "title": title, "description": description, "default": default,
            "minimum": minimum, "maximum": maximum, "x-advanced": True}


def _visual_params() -> dict:
    """The lines visual_quality's findings are drawn with (design doc 17 §2.2); the scores themselves are
    v1's and unchanged."""
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "frozen_ratio_min": _line("冻结判定线", "一路相机相邻采样帧（约隔 0.5 秒）几乎不变的比例达到这个值，"
                                                     "报「画面冻结」（IMG-1）", 0.95, 0.5, 1.0),
            "exposure_min": _line("曝光判定线", "一路相机的曝光分低于这个值，报「曝光不良」（IMG-2）", 0.6, 0.0, 1.0),
            "sharpness_min": _line("清晰度判定线", "一路相机的清晰度分低于这个值，报「画面模糊」（IMG-4）", 0.6, 0.0, 1.0),
            "dead_share_min": _line("信息死亡判定线", "一路相机里灰度几乎没有变化（标准差小于 3）的帧占比达到这个值，"
                                                       "报「信息死亡帧偏多」（IMG-3）", 0.6, 0.0, 1.0),
        }}


def _motion_params() -> dict:
    """The lines motion_quality's sub-items are reported with: the composite score is retired as a verdict
    (P18), each sub-item below its line is a finding of its own."""
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "smoothness_min": _line("平滑度判定线", "平滑度分低于这个值，报「动作不平滑」（ACT-1）", 0.5, 0.0, 1.0),
            "spike_min": _line("尖刺判定线", "尖刺分低于这个值，报「动作尖刺」（ACT-2）", 0.5, 0.0, 1.0),
            "gripper_jitter_min": _line("夹爪抖动判定线", "夹爪平稳分低于这个值，报「夹爪抖动」（ACT-5）", 0.5, 0.0, 1.0),
            "saturation_min": _line("执行器饱和判定线", "执行响应分低于这个值，报「执行器饱和」（ACT-4）", 0.5, 0.0, 1.0),
            "fluency_min": _line("流畅度判定线", "流畅度分低于这个值，报「操作不流畅」（TASK-7）", 0.5, 0.0, 1.0),
            "severe_below": _line("严重判定线", "平滑度、尖刺分低于这个值时，发现的严重度记为高", 0.2, 0.0, 1.0),
            "idle_edge_min_s": _line("起止空转判定线（秒）", "开头或结尾空转不短于这么多秒，报「开头空转」「结尾空转」（TASK-1）",
                                     1.0, 0.0, 60.0),
        }}


def _timestamp_params() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "duration_outlier_iqr": _line(
                "时长离群判定线（四分位距倍数）", "一条的时长比全体的四分位区间再往外超出这么多倍四分位距，"
                "在报告里报「时长离群」（数据集级的读数）", 3.0, 1.0, 10.0),
        }}


def _sync_params() -> dict:
    out = _evidence_param(
        "sync_plots", "同步曲线证据图",
        {"flagged": "有标注或未对齐的", "all": "全部", "off": "不画"},
        "为哪些条目画画面运动与关节速度的对照曲线")
    out["properties"]["spread_tol_s"] = _line(
        "相机滞后允许极差（秒）", "可信相机之间测得的滞后相差超过这个值，报「各相机的滞后不一致」（MV-3）；"
                                 "判废与否不受它影响", 0.3, 0.05, 2.0)
    return out


def _integrity_params() -> dict:
    """design doc 14 §5.3: L3 is the one choice a task makes; the thresholds are the site's."""
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "decode_test": {
                "type": "boolean", "title": "逐帧解码测试", "default": False,
                "description": "把每路相机从头到尾严格解码一遍，能发现文件结构完好、但画面数据已经损坏的条目，"
                               "包括解码器自己掩盖掉的错误。耗时相当于把全部视频完整解码一次"},
        }}


def _eef_params() -> dict:
    """design doc 12 §11.1 / §12, C.9 (D49: the review's two parameters joined). ``trajectory_json`` is required;
    ``observation_seeds`` (a person's anchors)
    or ``gripper_template`` (automatic anchors, F5.8) give the tracker its anchors - one of the two per camera,
    seeds win where both exist; without either (1.12, D-E15) the CPU measures nothing and the model gives an
    advisory opinion on each camera's whole clip (design doc 12 §10.5)."""
    return {
        "type": "object", "additionalProperties": False, "required": ["trajectory_json"],
        "properties": {
            "trajectory_json": _upload(
                "eef_trajectory", [".json"], 64, minLength=1, title="trajectory.json",
                description="约定格式 eef-video/1.0.0 的单文件包（每条 episode 的点与轴定义、标定、逐帧位姿与投影），"
                            "上传即校验，错误会定位到样本、帧和字段", default=""),
            "observation_seeds": _upload(
                "eef_observation_seeds", [".jsonl", ".json"], 64, title="观测种子",
                description="P-A 跟踪的种子：observation 格式的行（JSONL，或这些行的 JSON 数组），"
                            "每行是某个样本、某路相机、某一帧里人点出的点。夹爪参考可以不给：不给时只请模型看整段视频给出意见"
                            "（哪些片段不匹配、置信度多少），不参与判决",
                default="", **{"x-choice-group": GRIPPER_REFERENCE}),
            "gripper_template": _upload(
                "eef_gripper_template", [".json"], 64, title="夹爪外观模板",
                description="gripper-template/1.0：同一夹爪在各路相机里的若干小图与标好的物理点，跟踪器用它自动找锚点，"
                            "不用逐条 episode 点种子",
                default="", **{"x-choice-group": GRIPPER_REFERENCE}),
            "record_mapping": _upload(
                "eef_record_mapping", [".json"], 8, title="数据集记录映射",
                description="eef-mapping/1.1 的 record 块：数据集自己的末端位姿列（或 mcap topic）、关节角列与机器人型号"
                            "（DEMO 内置 Franka）、单位与坐标系。给了就把上传轨迹和数据集的记录逐帧比对，只报告、不参与判决",
                default=""),
            "threshold_profile": {
                "title": "阈值", "description": "demo 由基准噪声底定、未校准；模块参与判决（D49），没有阈值就判不了，"
                                                "所以不再提供「不判定」",
                "default": "demo",
                "oneOf": [{"const": "demo", "title": "demo（未校准）"}]},
            "camera_mounts": {
                "title": "参与的相机", "description": "腕部相机只做位置与方向（D-E9）；移动相机不支持",
                "default": "fixed_external_and_wrist",
                "oneOf": [{"const": "fixed_external_and_wrist", "title": "外部固定与腕部"},
                          {"const": "fixed_external", "title": "只看外部固定相机"}]},
            "lag_search_s": {
                "type": "number", "title": "时间错位搜索范围（秒）",
                "description": "在 ±这么多秒内找画面与记录的时间错位", "default": 1.0,
                "minimum": 0.2, "maximum": 3.0},
            "interpolation_gap_factor": {
                "type": "number", "title": "插值缺口倍数",
                "description": "相邻采样间隔超过局部中位间隔的这么多倍就不跨缺口插值", "default": 2.0,
                "minimum": 1.0, "maximum": 10.0},
            "evidence_mode": {
                "title": "证据图", "description": "为哪些条目保存声明投影与独立观测的叠加图",
                "default": "flagged",
                "oneOf": [{"const": "flagged", "title": "有可疑分项的"}, {"const": "all", "title": "全部"},
                          {"const": "off", "title": "不存"}]},
            "review_windows_per_camera": {
                "type": "integer", "title": "每路相机的复核窗口数",
                "description": "模型复核时，候选窗口与均匀抽查窗口各取至多这么多个", "default": 3, "minimum": 1,
                "maximum": 6},
            "review_frames_per_window": {
                "type": "integer", "title": "每个窗口的帧数", "description": "每个复核窗口最多送给模型的帧数",
                "default": 6, "minimum": 1, "maximum": 12},
        }}


MODULES: tuple[ModuleSpec, ...] = (
    ModuleSpec(
        id="data_integrity", name_zh="数据完整性",
        summary_zh="检查每条 episode 的文件是否完整、可读：文件结构、零填充、mcap 的 CRC、逐条数据的结构校验，"
                   "可选逐帧解码",
        level="episode", needs=frozenset({"raw_bytes"}), block="cpu", stage="integrity", depends_on=(),
        # design doc 14 §4.2's codes; v1's row_invalid is split by its cause (design doc 17 §2.2)
        codes=(_blocking("file_empty", "FILE-1", "文件为空或过小"),
               _blocking("video_missing", "FILE-1", "视频文件不存在"),
               _blocking("file_truncated", "FILE-2", "文件被截断"),
               _blocking("zero_filled", "FILE-2", "文件里有成块的零填充"),
               _blocking("structure_invalid", "FILE-3", "文件结构损坏"),
               _blocking("crc_mismatch", "FILE-3", "CRC 校验不符"),
               _blocking("cut_unreadable", "FILE-3", "录制中断，读不出数据"),
               _review("cut_off", "FILE-3", "录制中断", "integrity_check", "low"),
               _blocking("decode_failed", "FILE-4", "视频解码失败"),
               _review("decode_concealed", "FILE-4", "解码器掩盖了错误", "integrity_check", "low"),
               _review("count_mismatch", "FILE-5", "帧数与记录的长度对不上", "integrity_check"),
               _blocking("length_mismatch", "FILE-5", "数据长度与帧数对不上"),
               _blocking("values_invalid", "FILE-6", "数值不合规（NaN、Inf、维度或类型）"),
               _blocking("metadata_invalid", "FILE-8", "元数据不合规（帧率、时间边界）"),
               _review("table_inconsistent", "FILE-10", "episode 表前后不一致", "integrity_check"),
               _blocking("timestamps_invalid", "STRM-4", "时间戳不合规"),
               _blocking("action_missing", "STRM-1", "缺少动作数据"),
               _review("stream_missing", "STRM-1", "缺一路其他条都有的流", "integrity_check"),
               _review("rate_outlier", "STRM-3", "某路流的频率明显偏低", "integrity_check"),
               _review("duplicate_content", "SET-1", "与另一条的文件完全相同", "integrity_check"),
               _info("orphan_files", None, "不属于任何 episode 的文件", scope_kind="dataset"),
               _info("dark_camera", "STRM-1", "近乎全黑的相机", scope_kind="dataset"),
               _info("table_overlap", "FILE-10", "episode 表的帧区间重叠或不连续", "medium",
                     scope_kind="dataset")),
        param_schema=_integrity_params(),
        tables=(TableSpec("integrity_findings", "完整性发现",
                          ("episode_index", "level", "code", "file")),),
        native=True),
    ModuleSpec(
        id="timestamp_check", name_zh="时间戳检查",
        summary_zh="时间戳是否单调、有没有丢帧跳变、是不是短于残段阈值的碎片",
        level="episode", needs=frozenset({"timestamps"}), block="cpu", stage="numeric", depends_on=(),
        codes=(_blocking("gap", "STRM-3", "丢帧跳变"),
               _blocking("jitter", "STRM-3", "采样间隔抖动", "medium"),
               _blocking("out_of_order", "STRM-4", "时间戳倒序或重复"),
               _blocking("fragment", "STRM-5", "残段：短于最短时长"),
               _blocking("single_stamp", "STRM-5", "只有一个时间戳"),
               _info("duration_outlier", None, "时长离群", scope_kind="dataset")),
        param_schema=_timestamp_params(),
        tables=(TableSpec("timestamp_check", "时间戳异常",
                          ("episode_index", "duration_s", "max_dt")),)),
    ModuleSpec(
        id="kinematic_limits", name_zh="运动学极限",
        summary_zh="对照机器人规格库检查关节位置与速度是否越限",
        level="episode", needs=frozenset({"action", "embodiment_profile"}), block="cpu", stage="numeric",
        depends_on=(),
        # the injected saw-tooth and jump samples are caught by the end-effector velocity limit today;
        # they stay ACT-4 until motion_quality's spike detection improves (design doc 17 §2.2)
        codes=(_blocking("joint_limit", "ACT-4", "关节位置越限", scope_kind="channel"),
               _blocking("velocity_limit", "ACT-4", "关节速度越限", scope_kind="channel"),
               _blocking("ee_reach", "ACT-4", "末端超出工作空间"),
               _blocking("ee_translation_velocity", "ACT-4", "末端平移速度越限"),
               _blocking("ee_rotation_velocity", "ACT-4", "末端转动速度越限"),
               # 2.1: a malformed action (wrong number of joints, NaN) fails the check without a violation
               _blocking("data_invalid", "FILE-6", "动作数据无法与规格表对照（维度对不上或有无效值）")),
        param_schema=_no_params(),
        tables=(TableSpec("kinematic_violations", "越限明细",
                          ("episode_index", "joint", "value")),)),
    ModuleSpec(
        id="motion_quality", name_zh="运动质量",
        summary_zh="动作是否平滑、有无尖刺与执行器卡死、操作是否流畅（打分项）",
        level="episode", needs=frozenset({"action", "state"}), block="cpu", stage="numeric", depends_on=(),
        # the composite score is retired (P18): every sub-item reports on its own, info by default
        codes=(_info("smoothness_low", "ACT-1", "动作不平滑", "medium"),
               _info("spike", "ACT-2", "动作尖刺", "medium"),
               _info("actuator_saturation", "ACT-4", "执行器饱和", "medium", scope_kind="channel"),
               _info("gripper_jitter", "ACT-5", "夹爪抖动", "medium"),
               _info("stuck", "ACT-8", "执行器卡死", "medium", scope_kind="channel"),
               _info("fluency_low", "TASK-7", "操作不流畅"),
               _info("idle_opening", "TASK-1", "开头空转"),
               _info("idle_closing", "TASK-1", "结尾空转"),
               _info("action_semantics_undetermined", "ACT-6", "判断不了动作的语义", "medium",
                     scope_kind="dataset")),
        param_schema=_motion_params(),
        tables=(TableSpec("motion_quality", "运动质量明细", ("episode_index", "score")),)),
    ModuleSpec(
        id="visual_quality", name_zh="视觉质量",
        summary_zh="逐机位检查清晰度、曝光与画面冻结（打分项）",
        level="episode", needs=frozenset({"video"}), block="cpu", stage="frame", depends_on=(),
        codes=(_info("frozen", "IMG-1", "画面冻结", "high", scope_kind="camera"),
               _info("exposure_low", "IMG-2", "曝光不良", "medium", scope_kind="camera"),
               _info("information_death", "IMG-3", "信息死亡帧偏多", "medium", scope_kind="camera"),
               _info("sharpness_low", "IMG-4", "画面模糊", "medium", scope_kind="camera"),
               _info("dead_or_padded", "STRM-1", "相机没有信号或被填充", "medium", scope_kind="camera")),
        param_schema=_visual_params(),
        tables=(TableSpec("visual_quality", "逐机位打分", ("episode_index", "score", "camera")),)),
    ModuleSpec(
        id="video_action_sync", name_zh="视频-动作同步",
        summary_zh="逐机位比对画面运动与关节速度，找出画面与动作的时间错位",
        level="episode", needs=frozenset({"video", "action"}), block="cpu", stage="frame", depends_on=(),
        codes=(_blocking("misaligned_all", "AV-1", "画面与动作错位（全部可信相机）"),
               _info("camera_misaligned", "AV-1", "单路相机与动作错位", "medium", scope_kind="camera"),
               _info("suspect", "AV-1", "疑似错位"),
               _info("undecidable", "AV-3", "测不准：有动作但画面运动对不上"),
               _info("lag_inconsistent", "MV-3", "各相机的滞后不一致", "medium")),
        param_schema=_sync_params(),
        tables=(TableSpec("video_action_sync", "逐机位同步读数",
                          ("episode_index", "lag_s", "corr_peak")),)),
    ModuleSpec(
        id="eef_video_consistency", name_zh="EEF–视频一致性",
        summary_zh="比较数据集中声明的末端执行器投影与画面里独立定位的夹爪轨迹和方向是否匹配",
        level="episode", needs=frozenset({"video", "vlm", "eef_input"}), block="vlm", stage="vlm",
        depends_on=(),
        # a mixed module: its CPU measuring takes CPU-pool slots, its model review the VLM gates (§3.1)
        codes=(_blocking("inconsistent", "MV-4", "末端投影与画面不符", appealable=True),
               _review("unsettled", "MV-4", "末端投影与画面是否相符待人工核对", "eef_check"),
               _info("opinion_mismatch", "MV-4", "模型意见：末端投影与画面不符"),
               _info("record_mismatch", "MV-4", "上传轨迹与数据集的记录不符")),
        param_schema=_eef_params(),
        tables=(TableSpec("eef_camera_metrics", "逐相机分项",
                          ("episode_index", "camera", "position_median_px", "orientation_median_deg",
                           "lag_s", "coverage")),
                TableSpec("eef_segments", "候选段", ("episode_index", "camera", "subitem", "start_s",
                                                   "duration_s")),
                TableSpec("eef_diagnosis", "诊断假设", ("episode_index", "camera", "hypothesis")),
                TableSpec("eef_review_windows", "复核窗口", ("episode_index", "camera", "kind", "status",
                                                          "review_status", "conflict")),
                TableSpec("eef_record", "轨迹与数据集记录", ("episode_index", "source", "status", "position_p95_mm",
                                                          "rotation_p95_deg", "lag_frames"))),
        native=True),
    ModuleSpec(
        id="task_success", name_zh="任务成败判定",
        summary_zh="由多模态模型看画面判断任务是否完成，拿不准的交给人工裁决",
        level="episode", needs=frozenset({"video", "vlm"}), block="vlm", stage="vlm",
        depends_on=("autolabel",),
        codes=(_blocking("failure", "TASK-4", "任务失败", appealable=True),
               _review("uncertain", "TASK-4", "任务成败拿不准", "task_verdict"),
               _info("recovery", "TASK-10", "中途失误后完成"),
               _review("label_conflict_suspect", "LABEL-4", "标注与画面疑似不符", "label"),
               _info("task_text_missing", "LABEL-2", "没有任务标注，用的是自产描述")),
        param_schema=_evidence_param(
            "evidence_frames", "证据帧",
            {"flagged": "拒绝与待裁决的", "all": "全部", "off": "不存"},
            "为哪些条目保存判定时看过的画面"),
        tables=(TableSpec("task_success", "判定明细", ("episode_index", "verdict")),)),
    ModuleSpec(
        id="camera_defects", name_zh="镜头画面缺陷",
        summary_zh="借任务成败判定的逐机位复核请求，由模型顺带报告花屏、抖动与镜头污染；只出结果，不影响判决",
        level="episode", needs=frozenset({"video", "vlm"}), block="vlm", stage="vlm",
        depends_on=("autolabel",),
        # minor -> low, severe -> medium
        codes=(_info("glitch", "IMG-5", "花屏", scope_kind="camera"),
               _info("shake", "IMG-6", "画面抖动", scope_kind="camera"),
               _info("contamination", "IMG-7", "镜头污染或遮挡", scope_kind="camera")),
        param_schema=_no_params(),
        tables=(TableSpec("camera_defects", "逐机位画面缺陷",
                          ("episode_index", "camera", "glitch", "shake", "contamination")),),
        rides_on="task_success"),
    ModuleSpec(
        id="dedup", name_zh="精确去重",
        summary_zh="找出动作与视频字节级完全相同的条目，只留遍历顺序里的第一条",
        level="dataset", needs=frozenset({"raw_bytes"}), block="cpu", stage="dedup", depends_on=(),
        codes=(_blocking("duplicate", "SET-1", "与另一条完全重复", appealable=True),),
        param_schema=_no_params(),
        tables=(TableSpec("dedup_groups", "重复组", ("episode_index", "duplicate_of")),)),
    ModuleSpec(
        id="skill_profile", name_zh="技能画像",
        summary_zh="归纳两级技能体系并统计分布，检出标注与画面不一致的条目",
        level="dataset", needs=frozenset({"video", "vlm"}), block="vlm", stage="profile",
        depends_on=("autolabel",),
        codes=(_review("label_disagreement", "LABEL-4", "标注与画面不符", "label"),
               _info("descriptions_conflict", "LABEL-1", "多份描述彼此不一致", "medium"),
               _info("undersampled_family", None, "样本偏少的技能族", scope_kind="dataset")),
        param_schema=_no_params(),
        tables=(TableSpec("skill_assignment", "技能归属", ("episode_index", "family", "subskill")),)),
)


def native_ids() -> tuple[str, ...]:
    """Modules v2 runs itself, outside v1's check configuration (the EEF module since 1.8, the data
    integrity module since 1.11): v1's ``apply_check_selection`` never sees them."""
    return tuple(m.id for m in MODULES if m.native)


def is_rider(module_id: str) -> bool:
    """Answered inside another module's requests (``rides_on``): its findings are reported, it never holds an
    episode of its own and a model failure of its host is the host's. False for an unknown id."""
    try:
        return bool(get(module_id).rides_on)
    except KeyError:
        return False


def riders_of(host: str) -> tuple[str, ...]:
    """The modules answered inside ``host``'s own requests (``rides_on``, 1.14)."""
    return tuple(m.id for m in MODULES if m.rides_on == host)


def with_riders(module_ids) -> list[str]:
    """``module_ids`` plus every rider whose host is among them, in registry order.

    Every list of modules a run works from goes through here - the check call, the plan,
    the aggregate / report calls, the report itself - so a rider is never selected and
    never forgotten: whenever its host runs, it is there.
    """
    chosen = set(module_ids)
    chosen |= {m.id for m in MODULES if m.rides_on and m.rides_on in chosen}
    return [m.id for m in MODULES if m.id in chosen]


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


def by_block(block: str) -> tuple[ModuleSpec, ...]:
    return tuple(m for m in MODULES if m.block == block)


def finding_code(module_id: str, code: str) -> FindingCode:
    return get(module_id).code(code)


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
    """Whether a reject attributed to ``module_id`` may be appealed (D42): one of its codes is."""
    return get(module_id).appealable


@functools.lru_cache(maxsize=1)
def taxonomy() -> dict:
    """The taxonomy (C6) this registry binds, ``docs/contracts/taxonomy.json``."""
    from . import schemas

    doc = schemas.load(schemas.TAXONOMY)
    if doc.get("taxonomy_version") != TAXONOMY_VERSION:
        raise RuntimeError(f"{schemas.TAXONOMY} is taxonomy {doc.get('taxonomy_version')}, "
                           f"the registry binds {TAXONOMY_VERSION}")
    return doc


def export() -> dict:
    """The registry as JSON (``GET /api/v1/modules`` and ``docs/contracts/modules.json``)."""
    tax = taxonomy()
    return {"registry_version": REGISTRY_VERSION, "taxonomy_version": TAXONOMY_VERSION,
            "blocks": [{"id": b, "title_zh": BLOCK_TITLES[b], "stages": list(stages)}
                       for b, stages in BLOCKS.items()],
            "stages": list(STAGES), "full_set_stages": list(FULL_SET_STAGES),
            "finding_levels": [{"id": lv, "title_zh": title} for lv, title in FINDING_LEVELS],
            "unassessable_reasons": [{"id": r, "title_zh": title} for r, title in UNASSESSABLE_REASONS],
            "review_lines": [line.to_json() for line in REVIEW_LINES],
            "taxonomy": {"dimensions": tax["dimensions"],
                         "items": [{k: it[k] for k in ("id", "dimension", "name_zh", "explain_zh", "kind",
                                                       "level")} for it in tax["items"]]},
            "modules": [m.to_json() for m in MODULES]}
