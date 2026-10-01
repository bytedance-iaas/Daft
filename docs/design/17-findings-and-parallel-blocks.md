# 17 发现、策略判决与并行两块

> 状态：**定稿**（2026-10-01，需求方逐条确认了七个问题与三件待定事项）；F12.1–F12.6 尚未开工。
> 决策见 `00-overview.md` §7 的 D56–D59 与 §7.1 的 P18–P20。
> 来源：需求方 2026-10-01 提出——漏斗的「一票否决」让模块之间耦合、误判不可挽回；用回归样本集（设计 16）打分时，
> 被前面的档拦下的条目在后面的检测项上全是「没评估」，平台效果量不准，客户也看不到完整信息。
> 要改成：CPU 块与 VLM 块并行、互不依赖；每个模块按分类表的检测项编号（ACT-1、STRM-5、MV-1 这类）报出发现；
> 任务详情按模块展示检出统计。本篇把这个方向落成可编码的设计。

## 给实施 agent 的开工指引（先读这一节）

1. **分支与纪律**：直接在 `feat/curator-v2` 上做。A 类目录（`core/`、`registry/`、`ingest/`、`dataset_level/` 与 `export/` 里的 A 类文件，
   清单见设计 10 §2）一行不改：算法只调用，发现在编排壳里生成。新代码落在 `backend/curation/contracts/`（注册表与分类表）、
   `backend/curation/pipeline/`（壳、aggregate、report）、`backend/curation/extensions/`、`backend/curation/planner/`、`backend/daemon/`、
   `frontend/`、`tools/regression_samples/`。每个 feature 一个 commit；提交前在 `backend/` 下跑
   `../.venv/bin/python -m pytest -q curation/tests --ignore=curation/tests/test_environment.py` 与 `../.venv/bin/python -m pytest -q tests`，
   在仓库根跑 `PYTHONPATH=tools .venv/bin/python -m pytest -q tools/parity/tests` 与 `PYTHONPATH=tools .venv/bin/python -m pytest -q tools/regression_samples/tests`，
   前端跑 `npm run check:api && npm run lint && npm run typecheck && npm test && npm run build`。提交信息英文，不加模型 co-author。
   台账 `feature_list.md`（F12.x）与 `claude-progress.txt` 只在本地更新，不入库。
2. **读什么**：本篇 → `05-modules-and-preflight.md` §1、§7（注册表与加模块的清单）→ `02-cli-contract.md` §3.5、§3.6（`check`、`aggregate`）→
   `04-concurrency-and-vlm-merge.md` §2（分档与并发）→ `06-delivery-and-report.md` §3、§6（判决、报告）→ `07-frontend.md` §4.2、§5 →
   `16-regression-samples.md` §3、§8.4（分类表、打分）→ 设计 14 §4（完整性模块的原因码，发现码的样板）→
   `13-video-native-vlm.md` 的「逐机位画面缺陷」（带档位、时间段与 unknown 的记录，最接近目标格式）。
3. **顺序**：F12.1 契约与注册表 → F12.2 发现产出 → F12.3 策略判决（到这里评估就能直接对上）→ F12.4 两块并行 → F12.5 报告与前端 →
   F12.6 评估对接与全量实跑。每一步都能单独上线。
4. **不做**：新的检测算法（设计 16 §3.10 的「没有」那些归 F11.7 另立项）；区间定位算法（只填现成的区间）；自定义策略的编辑界面（本期只有两个预设）；
   旧任务迁移（D59）；跨任务的策略。

## 0. 摘要

### 0.1 一句话

模块只产出「发现」：平台细码加分类表的检测项编号，带严重度、范围、读数，能定位的带区间；两个块并行、互不过滤，每个模块对每条 episode
都给结论；拒不拒由一张策略表在 `aggregate` 时算，三份清单与裁决线照旧；报告和任务详情按检测项统计；评估直接拿发现比期望。

### 0.2 决策（需求方 2026-10-01 确认）

| # | 决策 |
|---|---|
| D56 | **发现与覆盖**：每个模块对一条 episode 的结果是一组发现，每条发现 = 平台细码 + 分类表检测项编号 + 严重度 + 范围 + 读数（可选帧 / 秒区间与证据）；分类表（设计 16 §3，1.1 起）升格为契约，注册表写明绑定的版本与每个细码对应的项；记录另带「评估了哪些项」与「哪些项评估不了、为什么」，分得清「没发现」和「没查」；判定线一律是模块参数，不再写在评估工具里；综合质量分退役，各子项各自出发现 |
| D57 | **两块并行、不过滤**：CPU 块（完整性 → 数值 → 帧 → 去重）与 VLM 块（补描述 → 模型判定 → 画像）并行执行，块内分段只为共享解码与各段的并发宽度，段之间传全部条目，不传幸存者；某模块出错不影响别的模块跑这一条。取代 D18 的「分档与顺序照搬 v1 漏斗」、P10 的「出错的 episode 不进后面的档」，以及 D49、D50 里「只跑前面没被判废的条目」「排在漏斗最前」的执行语义。去重与技能画像依赖全集，是各自块的「全量步骤」，等本块前面的段跑完全集才启动 |
| D58 | **策略判决**：拒不拒由策略表在 `aggregate` 时按发现算，模块不知道策略。默认策略复刻今天的硬门（P18）；拒绝理由列出全部 blocking 发现，不只第一个；held 的口径同 D35：有 blocking 发现直接拒，否则任一已勾选模块出错才待补跑；裁决线（D43）绑定在细码上；复议按发现：一条被拒时全部 blocking 发现都可复议才能恢复；去重组里留「未因别的原因被拒的第一条」，由 aggregate 选；画像同时给全集与交付集两份分布；另有预设「只报不拒」，换策略只重跑 aggregate |
| D59 | **只对新任务生效**：注册表 2.0 与 C2 2.0 之后建的任务用新格式；旧任务的报告与明细按旧格式只读打开，做不到就在旧任务页标「旧版本生成，新界面不支持」；不做迁移，旧任务可以删 |

需求方同日确认的取值，与上表同等效力：

| # | 事项 | 取值 |
|---|---|---|
| P18 | 默认策略 | 今天的硬门判废 → blocking；今天的可疑、弃权、转人工、标注分歧 → review（各自的裁决线）；其余 → info，包括原来软分模块的各子项。这是默认策略与今天唯一不同的一处：软分拒绝消失。看过报告再决定要不要把哪些子项提到 blocking，或者再做加权 |
| P19 | 区间输出 | 发现的 `frames` / `time_s` 可选；第一阶段只填现成的：完整性的坏位置、时间戳的跳变帧、视觉质量的冻结段、画面缺陷的时间段、任务成败的证据时间段、运动质量的空闲段与卡死段。不另写定位算法 |
| P20 | 只补映射就有的项 | 第一阶段一并补上：LABEL-3（任务文本来源是自产描述）、IMG-3（信息死亡帧比例）、TASK-1（开头 / 结尾空闲段）、AV-3（同步测不准）、MV-3（各相机滞后不一致）、LABEL-2（多份描述不一致）、SET-3（族分布、活动占比、时长离群）、ACT-6（动作语义预检结论） |

### 0.3 现状与改动对照

| | 现状 | 本篇 |
|---|---|---|
| 执行 | 档间串行的漏斗（流式交叠，但下一档只拿幸存者）；出错的条目不进后面的档 | 两块并行；段之间传全部条目；每个模块对每条都有记录 |
| 模块结果 | `verdict / passed / score / gate` 三态加分数，`details` 自定义 | `findings / assessed / unassessable / readings`，`details` 原样保留 |
| 判决 | 第一个失败的硬门定理由；软分加权低于 0.5 拒 | 策略表给每条发现定级；拒绝理由列全部 blocking；软分退役 |
| 问题的名字 | 完整性有原因码，其余靠评估工具从字段反推 | 每个模块一份细码目录，细码映射到分类表编号 |
| 覆盖 | 评估工具从对照表的规则反推「哪个模块评估哪一项」 | 注册表声明覆盖；记录写明 assessed / unassessable |
| 报告与首页 | 每个模块一个专用小节；任务详情只有状态与耗时 | 按检测项通用统计；任务详情每个模块一张统计卡；覆盖矩阵 |
| 评估 | `finding_map.json` 的规则把记录翻成检测项 | 直接读发现的 `item`；对照表只为旧格式的运行目录保留 |
| 契约 | C1 1.14、C2 1.0、C4 1.22.0 | C1 2.0、C2 2.0、C4 2.0.0；分类表成为 C6 |

## 1. 发现（finding）

### 1.1 记录 2.0

`checks/<module>/parts/*.jsonl` 每行仍是一条 episode 在一个模块下的记录（`result-record.schema.json` 2.0）：

```jsonc
{"episode_index": 34, "module": "video_action_sync",
 "status": "ok",                                   // ok | error；error 时 findings 为空、assessed 为空
 "findings": [
   {"code": "misaligned_all", "item": "AV-1", "severity": "high",
    "scope": null,                                 // 整条
    "readings": {"lag_s": -0.53, "corr_peak": 0.81, "cameras": ["front", "wrist"]},
    "message_zh": "全部可信相机一致提前 0.53 秒，超出容差 0.25 秒",
    "evidence": ["details/sync/ep000034.png"]},
   {"code": "lag_inconsistent", "item": "MV-3", "severity": "medium",
    "scope": {"cameras": ["front", "wrist"]},
    "readings": {"lag_front_s": -0.53, "lag_wrist_s": 0.02},
    "message_zh": "front 与 wrist 的滞后相差 0.55 秒"}],
 "assessed": ["AV-1", "AV-3", "MV-3"],             // 本模块在这一条上评估过的检测项
 "unassessable": [],                               // [{"item": "AV-1", "reason": "no_video", "message_zh": "…"}]
 "readings": {"per_camera": {...}},                // 不构成发现的读数，报告与明细用
 "details": {...},                                 // 模块自定义，原样保留 v1 的字段名（对账与明细页用）
 "evidence": [], "elapsed_s": 3.4, "error": null}
```

去掉的字段：`verdict`、`passed`、`score`、`gate`。弃权、可疑、转人工都是发现（§1.5），分数都是读数，拒不拒是策略（§4）。
`error` 的结构不变（D33 的出错明细）。`status = error` 的记录没有 assessed，评估与报告把它算「执行出错」。

### 1.2 发现的字段

| 字段 | 必填 | 含义 |
|---|---|---|
| `code` | 是 | 平台细码，模块内唯一；全局键是（模块，细码）。目录在注册表（§2.2） |
| `item` | 是 | 分类表的检测项编号；细码目录里定的，模块不自己写。只有 info 级的平台自有项允许 `null` |
| `severity` | 是 | `high` / `medium` / `low`，模块按自己的量给；合成样本的「明显 / 临界」对应 high / low |
| `scope` | 否 | 范围：`camera`（短名）或 `cameras`、`channel`、`arm`、`clock`、`file`。没有就是整条。写法与 expectation 的 `scope` 一致，相机用短名，评估侧照旧归一化 |
| `frames` | 否 | `[起, 止]`，episode 内从 0 数，两端包含（P19） |
| `time_s` | 否 | `[起, 止)`，秒，从这一条开头算；由帧号与帧率换算 |
| `readings` | 否 | 支撑这条发现的数值 |
| `message_zh` | 是 | 一句中文，带相机、位置和数值；报告、明细、裁决卡片直接显示，不再由 `report` 拼理由 |
| `evidence` | 否 | 证据文件的相对路径（图、曲线、片段） |

**数据集级发现**写在 `checks/<module>/dataset.json`（完整性模块已有这个文件），结构同上，多一个 `unit: "dataset"`；
评估按子集计一次，报告放在模块小节的「数据集级」一栏。本期有数据集级发现的模块：完整性（多余文件、近黑相机、条目表重叠）、
时间戳检查（时长离群）、运动质量（动作语义预检结论、活动占比均值）、技能画像（族分布、样本偏少的族）。

### 1.3 细码与检测项

- **细码归模块**。一个模块能看很多类问题，每类一个细码；一个细码只对应一个检测项。细码比检测项细（FILE-3 下面仍分截断、数据块 CRC、
  摘要区 CRC），一个细码分不清两个项时拆成两个细码。
- **一项可以被多个模块报**。发现按模块留出处；episode 级的「有没有这个问题」取并集；评估既算并集的指标，也算每个模块单独的指标（§6.1）。
- **分类表是契约 C6**：`docs/contracts/taxonomy.json`，内容就是 `tools/regression_samples/taxonomy.json` 的 `items`（编号、名称、`explain_zh`、
  kind、level、dimension、guards）加 `taxonomy_version`。编号只增不改，与样本集一致；平台绑定 1.1。注册表测试：每个细码的 `item`
  在分类表里；每个模块的 `covers` 是分类表编号；对照项（kind=control）不能是任何细码的 `item`。
- **分类表升版**：平台改绑新版本时只加编号；样本集 v1 冻结的分类表不动（它在 TOS 上）。仓库里 `tools/regression_samples/taxonomy.json`
  的 `platform_status` 一列是平台侧的注记，由注册表的覆盖声明生成（§6.2），不算改分类表。
- **阈值回到平台**：「平滑度低于 0.5 算 ACT-1」这类判定线写在模块参数里（`param_schema`，有缺省值），模块壳按它出发现；
  评估工具不再带阈值。

### 1.4 assessed 与 unassessable

- `assessed`：本模块声明覆盖的检测项（§2.3）减去 `unassessable` 的项。模块跑完、没在某个 assessed 项上出发现，就是「查过、没有」。
- `unassessable`：`[{item, reason, message_zh}]`。`reason` 取自一个小目录，平台通用：

| reason | 含义 | 例 |
|---|---|---|
| `embodiment_not_in_library` | 本体不在规格库或型号为空 | 运动学极限整项跳过（ACT-9 对照自动满足） |
| `no_state_columns` | 数据集没有状态量 | GenRobot 上的运动质量 |
| `no_video` / `no_action` | 缺这一路输入 | 只有动作没有视频的数据集 |
| `format_unsupported_by_module` | 模块不支持这种格式 | mcap 上的 EEF |
| `model_no_answer` | 模型没回答这一项 | 画面缺陷的 unknown |
| `single_description` | 只有一份描述，无从比较 | LABEL-2 |
| `not_applicable` | 别的原因，`message_zh` 写明 | 运动质量某子项对本数据集不适用 |

- 整个模块对这一条 `status = error` 时，assessed 为空，不写 unassessable；评估记「执行出错」，与 D33 一致。

### 1.5 弃权、可疑、转人工都是发现

今天的三态在新格式里都是发现，级别由策略给（§4.2）：

| 今天 | 新格式 |
|---|---|
| 硬门 `passed=False` | 一条 severity high 的发现，默认 blocking |
| 完整性「可疑」`passed=None` | 可疑码的发现（severity low / medium），默认 review，裁决线 `integrity_check` |
| 任务成败弃权 `uncertain` | `uncertain`（TASK-5，medium），默认 review，裁决线 `task_verdict` |
| EEF 转人工 | `unsettled`（MV-5，medium），默认 review，裁决线 `eef_check` |
| 标注分歧 | `label_disagreement`（LABEL-5，medium），默认 review，裁决线 `label` |
| 软分模块的分数 | 各子项的读数；低于子项阈值出发现，默认 info |
| 建议项（画面缺陷、EEF 模型意见） | 发现，默认 info |

人的回答落回来的方式不变（§4.4）：回答就是这条发现的结论。

## 2. 注册表（C1 2.0）

### 2.1 字段

```python
@dataclass(frozen=True)
class FindingCode:
    code: str                      # 模块内唯一
    item: str | None               # 分类表编号；只有 info 级的平台自有项允许 None
    name_zh: str                   # 界面上的名字
    severity: str                  # 缺省严重度：high / medium / low（模块可按量改）
    level: str                     # 默认策略下的级别：blocking / review / info（P18）
    review_line: str | None        # review 级：问人的裁决线（REVIEW_LINES 的 id）
    appealable: bool = False       # blocking 级：归因于它的拒绝可否复议（D42）
    scope_kind: str = "episode"    # episode / camera / channel / dataset，决定界面怎么分组

@dataclass(frozen=True)
class ModuleSpec:
    id: str; name_zh: str; summary_zh: str
    level: Literal["episode", "dataset"]
    needs: frozenset[str]
    block: Literal["cpu", "vlm"]                 # 在哪个块（§3.1）
    stage: str                                   # 块内的段：integrity / numeric / frame / dedup；autolabel / vlm / profile
    depends_on: tuple[str, ...]                  # 只剩数据依赖：autolabel、captions；不再有 numeric_gates / frame_gates / funnel_verdict
    codes: tuple[FindingCode, ...]               # 细码目录
    covers: tuple[str, ...]                      # 声明覆盖的检测项（codes 的 item 的并集，可以多）
    param_schema: dict; tables: tuple[TableSpec, ...]
    merge_units: Callable | None = None
    native: bool = False; rides_on: str | None = None
```

去掉的字段：`gate`、`input_scope`、`affects_dataset_verdict`、`produces_adjudication`（由 codes 推出）、`review_lines`（由 codes 推出）、
模块级 `appealable`（挪到细码）。`REVIEW_LINES` 目录不变（D43）。`STAGE_ORDER` 换成：

```python
BLOCKS = {"cpu": ("integrity", "numeric", "frame", "dedup"),
          "vlm": ("autolabel", "vlm", "profile")}
FULL_SET_STAGES = ("dedup", "profile")           # 全量步骤（§3.2）
```

`export()` 多出 `blocks`、`taxonomy_version`、`taxonomy`（分类表的 id、name_zh、explain_zh、dimension、kind）、每个模块的 `codes` 与 `covers`、
`unassessable_reasons` 目录。前端的检测项名、细码名全部从这里来，不在前端写表。`REGISTRY_VERSION = "2.0"`。

### 2.2 现有模块的细码目录（第一版）

默认级别按 P18：今天的硬门 → blocking，今天的可疑 / 弃权 / 转人工 → review，其余 info。来源一栏写明发现从哪个现成信号生成；
A 类算法不动，壳里读 `details` 生成。

**数据完整性**（设计 14 §4.2 的码原样保留，`row_invalid` 按原因拆开）

| 细码 | 项 | 严重度 | 默认级别 | 来源 |
|---|---|---|---|---|
| `file_empty` | FILE-1 | high | blocking | 文件为空或过小 |
| `video_missing` | FILE-1 | high | blocking | 原 `row_invalid`「视频文件不存在」 |
| `file_truncated`、`zero_filled` | FILE-2 | high | blocking | 不变 |
| `structure_invalid`、`crc_mismatch` | FILE-3 | high | blocking | 不变 |
| `cut_unreadable` | FILE-3 | high | blocking | 原 `file_truncated` 带「录制中断」 |
| `decode_failed` | FILE-4 | high | blocking | L3 |
| `decode_concealed` | FILE-4 | low | review（integrity_check） | L3 |
| `count_mismatch` | FILE-5 | medium | review（integrity_check） | 不变 |
| `length_mismatch` | FILE-5 | high | blocking | 原 `row_invalid`「长度 / 帧数」 |
| `values_invalid` | FILE-6 | high | blocking | 原 `row_invalid`「NaN / Inf / 二维 / dtype」 |
| `metadata_invalid` | FILE-8 | high | blocking | 原 `row_invalid`「fps 非法 / 时间边界非法」 |
| `table_inconsistent` | FILE-10 | medium | review（integrity_check） | 不变 |
| `timestamps_invalid` | STRM-4 | high | blocking | 原 `row_invalid`「时间戳」 |
| `action_missing` | STRM-1 | high | blocking | 原 `row_invalid`「action 缺失」 |
| `stream_missing` | STRM-1 | medium | review（integrity_check） | 多数比较 |
| `rate_outlier` | STRM-3 | medium | review（integrity_check） | 多数比较 |
| `cut_off` | FILE-3 | low | review（integrity_check） | 录制中断但读得出 |
| `duplicate_content` | SET-1 | medium | review（integrity_check） | 数据集级比较，记在条目上 |
| `orphan_files`（数据集级） | — | low | info | 平台自有项 |
| `dark_camera`（数据集级） | STRM-1 | low | info | v1 的先验 |
| `table_overlap`（数据集级） | FILE-10 | medium | info | 条目表重叠或空缺 |

**时间戳检查**（今天失败种类由评估工具从字段名反推，改为模块自己出码）

| 细码 | 项 | 严重度 | 默认级别 | 来源 |
|---|---|---|---|---|
| `gap` | STRM-3 | high | blocking | `gap_frames`；区间 = 跳变处的帧号 |
| `jitter` | STRM-3 | medium | blocking | `jitter_ratio` |
| `out_of_order` | STRM-4 | high | blocking | `ts` / `frame` |
| `fragment`、`single_stamp` | STRM-5 | high | blocking | 短于 `min_duration_s`、只有一个时间戳 |
| `duration_outlier`（数据集级） | SET-3 | low | info | 时长离群（P20） |

**运动学极限**

| 细码 | 项 | 严重度 | 默认级别 | 来源 |
|---|---|---|---|---|
| `joint_limit`、`ee_reach` | ACT-4 | high | blocking | 越限明细 |
| `velocity_limit`、`ee_translation_velocity`、`ee_rotation_velocity` | ACT-4 | high | blocking | 越限明细。注：注入的锯齿与跳变样本今天就是被末端速度上限抓到的，第一版仍记 ACT-4，等运动质量的尖刺检测改进后再议 |
| 本体不在规格库 | — | — | unassessable `embodiment_not_in_library` | 预检三态 |

**运动质量**（综合分退役，子项各自出发现；阈值进 `param_schema`，缺省 0.5）

| 细码 | 项 | 严重度 | 默认级别 | 来源 |
|---|---|---|---|---|
| `smoothness_low` | ACT-1 | medium（< 0.2 为 high） | info | `smoothness` |
| `spike` | ACT-2 | medium（< 0.2 为 high） | info | `spike` |
| `gripper_jitter` | ACT-5 | medium | info | `gripper_jitter` |
| `actuator_saturation` | ACT-4 | medium | info | `actuator_saturation` |
| `stuck` | ACT-8 | medium | info | `stuck_joints`；区间 = 卡死段 |
| `fluency_low` | TASK-8 | low | info | `fluency` |
| `idle_opening`、`idle_closing` | TASK-1 | low | info | 开头 / 结尾空闲秒数，区间（P20） |
| `action_semantics_undetermined`（数据集级） | ACT-6 | medium | info | 运行期动作语义预检「无法判断」（P20）；命中 profile 或推断成功只记读数 |
| `active_ratio`（数据集级读数） | SET-3 | — | 读数 | 活动占比均值（P20） |
| 没有状态量 | — | — | unassessable `no_state_columns` | 原「不适用」 |

**视觉质量**（阈值进参数：冻结 0.95、曝光 0.6、清晰度 0.6、信息死亡 0.6）

| 细码 | 项 | 严重度 | 默认级别 | 来源 |
|---|---|---|---|---|
| `frozen` | IMG-1 | high | info | `frozen_ratio`；区间 = `frozen_runs`（P19） |
| `exposure_low` | IMG-2 | medium | info | `exposure` |
| `sharpness_low` | IMG-4 | medium | info | `sharpness` |
| `information_death` | IMG-3 | medium | info | 完好性子项（灰度 std < 3 的帧比例，P20） |
| `dead_or_padded` | STRM-1 | medium | info | `camera_liveness` |

**视频-动作同步**

| 细码 | 项 | 严重度 | 默认级别 | 来源 |
|---|---|---|---|---|
| `misaligned_all` | AV-1 | high | blocking | 全部可信相机一致错位（含负时差） |
| `camera_misaligned` | AV-1 | medium | info | 单路可信相机错位（今天只标注） |
| `suspect` | AV-1 | low | info | 疑似 |
| `undecidable` | AV-3 | low | info | 测不准：相关峰值低于 `corr_min` 而动作有运动（P20，线索） |
| `lag_inconsistent` | MV-3 | medium | info | 可信相机之间的滞后相差超过 `lag_tol_s`（P20） |

**EEF–视频一致性**

| 细码 | 项 | 严重度 | 默认级别 | 来源 |
|---|---|---|---|---|
| `inconsistent` | MV-5 | high | blocking，可复议 | 判废 |
| `unsettled` | MV-5 | medium | review（eef_check） | 转人工 |
| `opinion_mismatch` | MV-5 | low | info | 没有夹爪参考时的模型意见 |
| `record_mismatch` | MV-5 | low | info | 与数据集记录的比对 |

**任务成败判定**

| 细码 | 项 | 严重度 | 默认级别 | 来源 |
|---|---|---|---|---|
| `failure` | TASK-5 | high | blocking，可复议 | 判失败；区间 = `evidence` 的时间段（P19） |
| `uncertain` | TASK-5 | medium | review（task_verdict） | 弃权 |
| `recovery` | TASK-12 | low | info | 中途回落后完成 |
| `label_conflict_suspect` | LABEL-5 | medium | review（label） | 判废护栏 |
| `task_text_missing` | LABEL-3 | low | info | 任务文本来源是「自产caption」（P20） |
| `completion`（读数） | TASK-5 | — | 读数 | 完成度估计 |

**镜头画面缺陷**（随任务成败）

| 细码 | 项 | 严重度 | 默认级别 | 来源 |
|---|---|---|---|---|
| `glitch` | IMG-5 | minor → low，severe → medium | info | 带时间段 |
| `shake` | IMG-6 | 同上 | info | 带时间段 |
| `contamination` | IMG-7 | 同上 | info | 带时间段与种类 |
| unknown | — | — | unassessable `model_no_answer` | 模型没答 |

**精确去重**

| 细码 | 项 | 严重度 | 默认级别 | 来源 |
|---|---|---|---|---|
| `duplicate` | SET-1 | high | blocking，可复议 | 重复组；哪条留下由 aggregate 定（§4.5），模块只报组 |

**技能画像**

| 细码 | 项 | 严重度 | 默认级别 | 来源 |
|---|---|---|---|---|
| `label_disagreement` | LABEL-5 | medium（高置信）/ low（复核档） | review（label） | 标注分歧；「看法不稳」降级为 info |
| `descriptions_conflict` | LABEL-2 | medium | info | 数据集自带多份描述归到不同族（P20）；只有一份时 unassessable `single_description` |
| `task_text_missing` | LABEL-3 | low | info | 同任务成败（并集） |
| `undersampled_family`（数据集级） | SET-3 | low | info | 样本偏少的族（P20） |
| 族分布（数据集级读数） | SET-3 | — | 读数 | `family_tree` |

对照项 FILE-9、STRM-8、IMG-10、IMG-11、ACT-9、TASK-9、SET-2 不是任何细码的 `item`，评估按它们守的项看误报。
分类表其余没有细码的项是检测缺口，清单在设计 16 §3.10，归 F11.7。

### 2.3 覆盖声明

`covers` 是模块在正常情况下评估的检测项，由细码目录推出，可以多列「查了但永远没有细码」的项（本期没有）。记录的 `assessed`
= `covers` − `unassessable`。报告的「本次质检范围」据此列出覆盖矩阵（§5.2）。

### 2.4 加一个模块要改哪些地方（更新 05 §7）

1. `core/` 或 `extensions/` 里的算法；2. 注册表加 `ModuleSpec`：块、段、细码目录（每个细码的项、严重度、默认级别、裁决线）、覆盖、参数；
3. 壳：把算法结果写成记录 2.0；4. 要人工复核的，裁决线写在细码上，新种类照 D43 加目录与执行规则；
5. 报告与前端零改动：模块小节、任务详情的统计卡、明细的概要都按检测项通用渲染；专用视图可选。
不再需要：在 `verdict.py` 声明 hard / soft 与权重；在前端加档名。

## 3. 执行：两块并行

### 3.1 块与段

```
  选中的全部 episode
      │
      ├─ CPU 块（CPU 池）                                 ├─ VLM 块（VLM 闸门）
      │   integrity   数据完整性                           │   autolabel  无标注补描述（只对无标注条目）
      │       │ 传全部条目                                 │       │ 数据依赖：成败判定要先有描述
      │   numeric     时间戳 · 运动学 · 运动质量             │   vlm        任务成败(+画面缺陷) · EEF
      │       │ 传全部条目                                 │       │ 数据依赖：画像归纳要等全部描述
      │   frame       视觉质量 · 视频-动作同步               │   profile    技能画像（全量步骤）
      │       │ 块内全部段做完                              │
      │   dedup       精确去重（全量步骤）                   │
      │                                                   │
      └──────────────────┬────────────────────────────────┘
                         ▼
                  aggregate（策略判决，§4）→ report → export → verify
```

- **段只为效率**：帧档两个模块共享一次解码（D18 的这一半保留），各段有自己的并发宽度与内存准入；段之间传的是「本块的全部条目」，
  上一段的结果不过滤下一段。出错的条目照样进下一段（取代 P10）。
- **块之间没有数据依赖**。EEF 是混合模块：放 VLM 块，它的 CPU 测量阶段照旧从 CPU 池拿名额（设计 14 §2.2 完整性 L3 的做法）。
- **autolabel 不是模块**，仍是 VLM 块的第一段，只对无标注条目跑；`task_success` 对它的依赖是数据依赖（`depends_on`）。
- **坏文件上的模型调用**：VLM 块裁片失败在发请求之前，照 D33 记 `error`，不发模型请求、不烧重试；这一条若有完整性的 blocking 发现，
  照 D35 直接拒。不加「完整性判坏就跳过 VLM」的门，块之间保持独立。

### 3.2 全量步骤

去重与技能画像各依赖全集：去重要全部条目的字节，画像归纳要全部描述。两者是各自块的最后一段，等本块前面的段对全集跑完才启动；
进度里单独一行「全量步骤」。去重对全集找重复组（比只对通过集合找更准），**只报组**，哪条留下由 aggregate 按当前判决定（§4.5）；
画像给全部条目归类，交付集的分布在 report 时按 `passed` 名单算，不多花模型调用。它们不再 `depends_on` 判决，上游判决变了也不 `stale`。

### 3.3 Daemon 派发与 planner

- **计划**（`plan.schema.json` 2.0）：每个 stage 多 `block`（cpu / vlm）、`after`（同块里前一段的 id，根段没有）、`full_set`（全量步骤）；
  `hard_gates` 删除；`episodes` 对每一段都是任务的选中集。planner 校验 `after` 不跨块。
- **派发**（`daemon/orchestr/episode_pipeline.py`）：层从一条链改成两棵链，两个根同时启动；`_finish_stage` 写给下一段的名单 = 本段的输入
  （不再 `store.survivors`）；一条 episode 在本段有记录（ok 或 error）就交给下一段；`held_by_downstream` 只在块内生效；全量步骤在前一段
  完成全集后启动。CPU 池（D54）与 VLM 闸门（04 §2.2）不变；两块同时跑时 CPU 块按名额、VLM 块按闸门，互不影响。
- **即时结果**（C4 的 `PipelineEpisode`）：每条写各模块的状态与发现数，再给一个「临时判决」——用同一套策略函数对已有发现算，标明是临时的；
  最终判决只在 aggregate 时定。
- **episode_state**：`survivors()` 退役，换成 `completed(modules)`。

### 3.4 暂停、续跑、重试

- 暂停 / 停止 / 系统暂停不变（SIGTERM 收尾当前条目）；`check --resume` 不变。
- **重试**只补跑出错的（模块 × episode）对：没有后段依赖它，不必「从出错的那一档接着往后跑」（D25 的这一句改成这样）。
- **继续运行**复用已完成的（模块 × episode）结果，两块各自从未完成处继续。
- 子任务「执行裁决」的链不变：`adjudicate-apply → check task_success（改标条目）→ aggregate → check skill_profile --incremental → report`。

### 3.5 成本与资源

- 多出的模型开销 = 以前被 CPU 段拦下的条目比例 × 每条的模型成本；实测基线上这个比例很小。
- 两块同时读同一份视频：数据集先拉到本地缓存（D44），帧档的内存准入不变；临时盘按 04 §2.3 的口径。
- 复核队列不会因此膨胀：将被拒的条目不再问人（§4.4，D42 的延伸）。

## 4. 判决：策略层

### 4.1 策略表

策略是「发现 → 级别」的函数：

```yaml
# 预设 default：空覆盖，级别全部取细码目录的默认（P18）
# 预设 report_only：
overrides:
  - match: {level: any}            # 所有发现
    level: info
```

规则按顺序匹配，`match` 可以写 `module` + `code`、`item`、`severity`、`level`；第一条命中的生效，没命中取细码的默认级别。
任务参数 `policy: {preset: default | report_only}`，默认 `default`；生效的完整表在任务开始时冻结进 `run.json`（同 P17），
每个结果版本另存一份 `revisions/rNNNN/policy.json`，报告头上写明预设名。站点配置可以登记更多预设（`site.yaml` 的 `policies`），本期不做编辑界面。

### 4.2 默认策略

就是 §2.2 每个细码的「默认级别」那一列。与今天比，只有一处不同（P18）：原软分模块（运动质量、视觉质量）的子项是 info，
不再有「综合分低于 0.5 拒」。其余：今天硬门判废的码 blocking；今天进裁决线的 review；建议项 info。

### 4.3 aggregate 算法

对每条 episode（`aggregate` 仍是纯计算、每次全量重算）：

1. 收集所有已勾选模块对它的记录；按策略给每条发现定级。
2. **有 blocking 发现 → drop**，`reasons` 列出全部 blocking 发现（模块、细码、项、`message_zh`、可否复议）；有模块出错的照样列在 `error_modules`，
   理由末尾注明「另有 X 执行出错，不影响结论」（D35）。
3. 否则**任一已勾选模块对它出错或没有记录 → held**（D24、D33、P11），`reasons` 是出错的模块与原因。
4. 否则 **keep**。review 级发现按各自的裁决线进 `review.json`；held 的条目解决前不问人（今天的规则）。
5. 人工决定（§4.4）作为覆盖再算一遍；`discard` 压过一切。
6. 去重（§4.5）在人工决定之后、写清单之前定留哪条。

`verdicts.jsonl` 2.0 每行：`{episode_index, verdict: keep|drop|held, blocking: [{module, code, item}], review: [{module, code, item, line}],
info_count, error_modules, reason}`。`passed / reject / held / review` 四份清单的结构不变，`reasons[]` 的条目多 `code`、`item`，`kind` 取
`finding | human | execution_error | duplicate`。`keep.txt` 保留为交付名单（导出与画像的交付集分布用）。

### 4.4 人工复核与复议

- 裁决线（D43）绑定在细码上：review 级的发现带 `review_line`，卡片内容就是 `message_zh` 加证据；一条 episode 的几条 review 发现合在一张卡片。
- 人的回答是这条发现的结论：`integrity_check` 的 broken → 这条发现升为 blocking（kind human）、intact → 结论为「无此问题」；
  `eef_check` 的 inconsistent / consistent 同理；`task_verdict` 的 failure → blocking（human）、success → 撤销 `failure` 发现；
  `label` 线的改标重判照 D39；`unsure` 不改任何东西、仍计待裁；`discard` 压过一切。
- **复议按发现**（D42 的推广）：一条被拒的 episode 可复议，当且仅当它的全部 blocking 发现都来自 `appealable` 的细码（第一版：
  `task_success.failure`、`eef_video_consistency.inconsistent`、`dedup.duplicate`）、且没有人工已定的结论；「恢复为可用」撤销这些发现的 blocking
  级别，再按 §4.3 重算（别的模块出错的仍 held，P11）。物理与结构硬门的拒绝仍是终局。
- 已被拒的条目不再出 review 卡片（D42 延伸到所有裁决线）。

### 4.5 去重与画像

- 去重模块对全集报重复组（`group_id`、组内成员、`duplicate_of` 留作读数）。aggregate 在人工决定之后，对每个组选 canonical =
  遍历顺序里第一条「没有因别的原因被拒」的；其余成员得到 `dedup.duplicate` 的 blocking 发现（可复议）。人工恢复过的条目不再被去重（v1 的规则）。
- 画像的归类对全集算；报告给两份分布：全集与交付集（按 `passed`）。标注分歧对全集审计，但已被拒的条目不出卡片。

### 4.6 与今天不同的地方

| | 今天 | 本篇 |
|---|---|---|
| 拒绝理由 | 第一个失败的硬门 | 全部 blocking 发现 |
| 软分 | 加权均值 < 0.5 拒 | 退役；子项 info（P18） |
| 复议 | 按归因模块 | 按发现 |
| 去重的输入 | 通过集合 | 全集；留「未因别的原因被拒的第一条」 |
| 画像 | 交付集 | 全集归类，双分布 |
| 弃权 | `passed=None` | review 级发现 |
| 判决能否不重跑而改 | 不能 | 换预设只重跑 aggregate |

## 5. 报告与界面

### 5.1 report.json（C2 2.0）

- `overview` 多 `policy`（预设名、版本）、`coverage`（`taxonomy_version`、`covered`、`not_covered`、`unassessable: [{item, reason, count}]`）、
  `findings_by_item: [{item, episodes, blocking, review, info}]`；`reject_reasons` 按项与细码计（`[{item, code, module, count}]`）。
- 每个模块小节的 `summary` 多 `assessed_episodes`、`items: [{item, code, level, episodes, share, by_camera?: [{camera, episodes}]}]`
  （`share` 的分母是本模块评估过的条数）、`unassessable: [{reason, count}]`、`score_hist`（有读数的子项）、数据集级发现。
  1.0 的汇总键（06 §6.2）保留，专用视图照旧能用。

### 5.2 报告页

- 「本次质检范围」多一块**覆盖矩阵**：本次覆盖分类表 N / 71 项，按维度列出覆盖、未覆盖、评估不了（带原因）；点一项跳到报的模块小节。
- 模块小节与勾选模块一一对应（需求硬要求，不变）。每个小节：关键数字（评估条数、检出条数、按级别计数）+ 通用图「检出项 → 条数与占比」
  （按级别分色，相机范围的项再按相机分组）+ 有读数的子项分数分布 + 数据集级发现。已有专用视图的模块保留专用图，通用图放在前面。
- 质检总览的「判废原因分布」改按检测项。

### 5.3 任务详情（首页）

- 模块表换成**模块统计卡**（每行一个模块，可展开）：状态、评估 / 选中条数、出错条数，一条「检出项」条图（同 §5.2 的通用图，取自同一份 `summary.items`），
  有读数的给分数分布；「去报告」跳到小节。数据来自 `GET /tasks/{id}/report`，运行中每次结果版本刷新；运行中没有报告时显示即时结果（§3.3）的发现数。
- 分档进度卡改成**两块**：CPU 块与 VLM 块两张并排卡片，每张里按段一根进度条，全量步骤单独一行；运行时间线按块画。
  「进度条从 50/50 变成 49/49」的说明去掉——段之间不再减少条目。
- 页头、Token、执行时间线、人工裁决入口不变。

### 5.4 Episode 明细

- 版式不变。判决概要从一条理由变成**全部发现的列表**：按 blocking / review / info 分组，每条写模块、细码名、项、`message_zh`、范围、区间（有则可点跳视频时刻）、
  可否复议；review 的给「去裁决」。
- 每个模块一块照旧显示读数；`unassessable` 的项写明原因；「未进入这一档」的写法退役，出错的写出错原因。
- 顶部筛选多两项：按级别、按检测项（`GET /tasks/{id}/episodes` 的条目带 `items` 与 `levels`）。

### 5.5 进度与 SSE

- C3 的 stage id：`integrity`、`numeric`、`frame`、`dedup`、`autolabel`、`vlm`、`profile`（`post_verdict`、`profile_vlm` 退役）；进度行多一个可选的 `block`。
- C4 的 `StageProgress` 多 `block`、`full_set`；`PipelineEpisode` 的 `last_stage` / `next_stage` 改成 `stages: {stage: done|error|running|waiting}`
  与 `provisional`（临时判决）。

### 5.6 旧任务（D59）

前端按 `report.json` 的 `schema_version` 与注册表版本分流：旧格式走现有组件，新格式走新组件；旧任务的 Episode 明细与裁决页同理。
分流成本过高的页面在旧任务上显示「此任务由旧版本生成，新界面不支持」。Daemon 不迁移旧任务的运行目录。

## 6. 评估对接

### 6.1 score.py 2.0

- 新格式的运行目录：直接读 `findings[].item`、`scope`、`assessed`、`unassessable`；不再需要规则表。TP / FN / FP / TN 的定义不变（设计 16 §8.4）；
  `not_assessed` 来自记录（项不在任何已跑模块的 `assessed` 里），`error` 来自 `status`，不再靠「模块有没有记录」推断。
- 多模块报同一项：并集进总指标；另出每个模块单独的 precision / recall，用来判断该调谁。
- 对照项仍按分类表的 `guards` 看误报；SET-4 仍看预检；数据集级按子集计一次（读 `dataset.json`）。
- `finding_map.json` 只为旧格式的运行目录保留（基线 `1b30fb224` 还是旧格式，要拿来对比），新格式不读它；两种格式由脚本自动识别。
- `--by-lineage`、`--baseline --max-drop --min-support` 不变。

### 6.2 分类表的同步

`tools/regression_samples/taxonomy.json` 的 `platform_status` 一列改为从注册表生成（`python -m regression_samples.coverage_from_registry`）：
有 blocking / review 级细码的项写「能判」，只有 info 级的写「有读数」，有细码但带条件的写「部分」并附条件，没有细码的写「没有」；
对照项写「能处理」。契约测试：`docs/contracts/taxonomy.json` 的 items 与工具目录那份的 items 一致（编号、名称）。
IMG-5 / 6 / 7 的现状今天就落后于注册表 1.14，F12.1 一并改。

### 6.3 区间

发现带 `frames` / `time_s` 的项（P19），评估先只统计「有区间的发现占比」，定位误差的指标等样本集 v2 与更多模块有区间后再加（设计 16 §17.3 的保留意见不变）。

## 7. 契约与接入清单

| 契约 | 改动 |
|---|---|
| C1 注册表 → 2.0 | `block`、`stage`、`codes`、`covers`、`BLOCKS`、`FULL_SET_STAGES`；去掉 `gate`、`input_scope`、`affects_dataset_verdict`、模块级 `appealable` / `review_lines` / `produces_adjudication`；`export()` 带分类表与细码；`modules.json` 重新导出 |
| C6 分类表（新） | `docs/contracts/taxonomy.json`（1.1）；进 `CONTRACTS.lock` |
| C2 → 2.0（`common.schema.json` 的 `schema_version` 升 2.0） | `result-record`：`status / findings / assessed / unassessable / readings`，去掉 `verdict / passed / score / gate`；`check`：`episodes` 计数改 `{total, ok, error}`，多 `findings`；`verdict-line`：`blocking / review / info_count`；`final-list`：`reasons[]` 多 `code / item`，`kind` 加 `finding`；`report`：§5.1；`plan`：`block / after / full_set`，去 `hard_gates`；`examples/` 全部换新，合法与不合法各补 |
| C3 1.2 | stage id 目录更新；进度行可选 `block` |
| C4 → 2.0.0 | 注册表 schema；stage 枚举；`StageProgress`、`PipelineEpisode`；任务参数 `policy`；报告与 episode 列表的新字段；`frontend` 跑 `npm run gen:api` |
| C5 | `TaskModule.episodes_total` 的注释改为「选中条数」；`stale` 规则只看数据依赖；任务多 `policy` 列（迁移第 7 步） |
| 对账带格式 | `tools/parity` 的规范化记录认 2.0（§8.1） |

设计文档要改的条款：00 §4 数据流图与 D18 / P10 / D35 / D42 / D49 / D50 的注记；02 §3.5、§3.6；04 §2（分档改两块）；05 §1、§6、§7；
06 §3、§6；07 §4.2、§5；13 的「逐机位画面缺陷」里 `affects_dataset_verdict` 的说法；14 §4 的三种结论改为发现；16 §8.4 加 2.0 的读法；
`docs/contracts/SUMMARY.md`；CLAUDE.md 的「质检漏斗」一段。

## 8. 对账与验收

### 8.1 对账

- A 类守卫不受影响：算法文件不动。
- 对账工具的规范化记录认 2.0：`details` 与 `readings` 逐位比，发现按（细码、项、范围、区间）集合比；v1 黄金基线的**判决级**对账退役
  （D15 的范围缩到记录级：同一条目同一模块的 `details` 逐位一致）；v2 自录基线在 F12.3 后重录一次（判决变了），F12.4 后再录一次
  （记录应只多不少：以前被拦下的条目现在有记录，已有记录逐位不变）。
- 对账回放仍是「任何可能影响判决的改动先跑」的门。

### 8.2 验收口径

| feature | 验收 |
|---|---|
| F12.1 | 契约文件、示例、锁、`modules.json`、`gen:api` 全部更新且 CI 契约步骤绿；分类表契约测试通过；`platform_status` 由注册表生成 |
| F12.2 | 每个模块的记录通过 2.0 schema；§2.2 的每个细码至少一个单测夹具能触发；P20 八项各有一个夹具；`details` 与改前逐位一致（对账） |
| F12.3 | 默认策略下，除软分拒绝外，评估集基线上的 keep / drop / held 与改前一致（逐条对比，差异只有软分那一类并逐条列出）；`report_only` 下全部 keep；复议、裁决、改标重判、重新导出的端到端用例通过 |
| F12.4 | 两块交叠执行的端到端用例（假模型闸门控制时序）：CPU 段出错的条目在 VLM 块有记录；全量步骤在块内前段完成后启动；暂停 / 停止 / 续跑 / 重试 / 崩溃恢复用例通过；对账记录只多不少 |
| F12.5 | 任务详情、报告、明细、裁决页按 §5 实现；旧任务可打开或明确提示；前端测试与 README 手动验证步骤更新 |
| F12.6 | anchor 与 anchor-nc 全量实跑（含模型模块），score 2.0 出分，与 `1b30fb224` 基线逐项对比，gap 报告回写设计 16 §3 |

## 9. 分阶段

| feature | 内容 | 依赖 |
|---|---|---|
| F12.1 契约与注册表 | 分类表契约、注册表 2.0、C2 / C3 / C4 / C5 改动、示例与锁、前端类型、设计文档条款 | — |
| F12.2 发现产出 | 每个模块的壳按细码目录出记录 2.0（含 P20 八项、P19 现成区间）；阈值进参数；对账工具认 2.0 | F12.1 |
| F12.3 策略判决 | 策略表与两个预设；aggregate 2.0（全部理由、held、复议按发现、去重 canonical、画像双分布）；`adjudicate-apply` 适配；report 2.0 的汇总；基线重录 | F12.2 |
| F12.4 两块并行 | 注册表的块；planner 两块计划；Daemon 两根派发、不过滤、全量步骤；即时结果；暂停 / 续跑 / 重试适配；基线再录 | F12.3 |
| F12.5 报告与前端 | 覆盖矩阵、通用模块小节、任务详情统计卡、两块进度、明细概要与筛选、旧任务分流；设计 07 与 README | F12.3（F12.4 可并行） |
| F12.6 评估对接与全量实跑 | score 2.0、`finding_map` 退为旧格式专用、`platform_status` 生成；全量实跑与 gap 报告 | F12.3；全量实跑要 F12.4、F12.5 |

## 10. 本期不做

- 新的检测算法：设计 16 §3.10「没有」的 20 项与「部分」里要改算法的（F11.7）。本篇只保证它们加进来时「加模块、出发现」就够。
- 区间定位算法与区间级打分（P19 只填现成的）。
- 自定义策略的编辑界面、按客户的策略库；跨任务策略。
- 旧任务迁移（D59）。
- 发现之间的「佐证」规则（例如两个模块同时报才 blocking）：留给看过报告之后。

## 参考

- 设计 05（注册表）、06 §3（判决）、14 §4（原因码）、13-video-native（画面缺陷记录）、16 §3 / §8.4（分类表与打分）。
- 回归样本集交付说明（飞书）：`https://bytedance.us.larkoffice.com/docx/OWYsdl943otwhnxDa9QuyZmgsWb`。
