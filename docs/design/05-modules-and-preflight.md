# 05 模块注册表与预检

## 1. 模块注册表

> **注册表 2.0（2026-10-01，设计 17，D56–D58，F12.1 已落地）**：模块不再按漏斗的「门」描述，改成三件事——
> **在哪跑**：`block`（`cpu` / `vlm`）与块内的段 `stage`（CPU 块 `integrity → numeric → frame → dedup`，VLM 块
> `vlm`；D70 起每段都是逐条的，没有全量步骤；D72 起没有补描述段），两块并行、互不过滤，`depends_on` 只留数据依赖的位置
> （3.2 起为空）；**能报什么**：`codes`，每个细码对应分类表（C6，`docs/contracts/taxonomy.json` 1.1）的一项，带缺省严重度、
> 默认策略下的级别（blocking / review / info，P18）、review 级的裁决线、blocking 级可否复议；**覆盖什么**：`covers`
> （细码的项，加只给读数的项 `also_covers`）。去掉了 `gate`、`input_scope`、`affects_dataset_verdict`、`produces_adjudication`、
> `review_lines`、模块级 `appealable`——后三项由细码推出（代码里仍是 `ModuleSpec` 的属性）。导出的 JSON 另带
> `blocks`、`taxonomy`、`finding_levels`、`unassessable_reasons`。细码目录见设计 17 §2.2。
> 过渡期已结束（F12.3 策略判决、F12.4 两块执行）：后端的过渡表 `gates_v1.py` 已删除，「参与不参与判决」看 `rides_on`；
> 前端 `src/lib/registry.ts` 的 `funnelGate` 只给 F12.5 之前的旧视图用。
> 下文是 1.x 的写法，保留作对照；块、段、细码与覆盖以本节为准。

八个模块，**一期数量不变**。注册表是单一事实源：CLI、Daemon、前端三方的模块清单、
中文名、能力要求、所属的档全部从它生成（前端经 `GET /api/v1/modules` 拿到），不允许任何一方硬编码。

```python
@dataclass(frozen=True)
class ModuleSpec:
    id: str
    name_zh: str
    level: Literal["episode", "dataset"]            # 轨迹级 / 数据集级
    gate: Literal["hard", "soft", "dedup", "none"]  # 一票否决 / 打分项 / 剔除重复 / 仅产出
    needs: frozenset[str]                           # 能力需求，见 §2
    stage: Literal["numeric", "frame", "vlm", "post_verdict"]   # 档，见 04 篇 §2
    depends_on: tuple[str, ...]                     # 输入依赖谁的结果；上游变了本模块变 stale
    produces_adjudication: bool                     # 是否会产生人工裁决条目（含可被复议的拒绝）
    review_lines: tuple[str, ...]                   # 产生哪几种复核，取自复核目录 REVIEW_LINES（D43）
    appealable: bool                                # 归因于它的拒绝可否复议（D42）
    param_schema: dict                              # 任务里可覆盖的模块参数（JSON Schema）
    merge_units: Callable | None                    # VLM 模块可选：声明可合并的提问，见 04 篇 §4.2
```

定稿（W2）：代码在 `backend/curation/contracts/modules.py`，另加两个字段 —— `summary_zh`（界面上的一句话说明）和 `tables`（报告明细表的 id 与可排序字段白名单，03 篇 §6）；导出为 `docs/contracts/modules.json`，`GET /api/v1/modules` 原样返回。模块参数的取值沿用 v1：`sync_plots`、`evidence_frames` 都是 `flagged | all | off`。

| id | 中文名 | 级别 | 门 | 需要 | 档 | 依赖 | 产生裁决 |
|---|---|---|---|---|---|---|---|
| `timestamp_check` | 时间戳检查 | episode | hard | `timestamps` | numeric | — | 否 |
| `kinematic_limits` | 运动学极限 | episode | hard | `action`,`embodiment_profile` | numeric | — | 否 |
| `motion_quality` | 运动质量 | episode | soft | `action`,`state` | numeric | — | 否 |
| `visual_quality` | 视觉质量 | episode | soft | `video` | frame | 硬门① | 否 |
| `video_action_sync` | 视频-动作同步 | episode | hard | `video`,`action` | frame | 硬门① | 否 |
| `task_success` | 任务成败判定 | episode | hard | `video`,`vlm` | vlm | 硬门②；无标注条目不判（D72） | **是** |
| `camera_defects` | 镜头画面缺陷 | episode | none | `video`,`vlm` | vlm | 随 `task_success` | 否（建议项） |
| `dedup` | 精确去重 | dataset | dedup | `raw_bytes` | post_verdict | 漏斗判决 | 可复议（D42） |

几点要说明，都来自 v1 代码：

- **v1 里这是「6 + 2」**。前六个是配置里的 `checks`（`pipeline/config.py` 的 `KNOWN_CHECKS`），在漏斗里逐条跑、
  参与判决；`dedup` 是数据集级的附加项，**在判决之后、只对 keep 集合跑**（另一项技能画像已下线，D68）。
  注册表必须把这个区别表达出来（`stage=post_verdict` + `depends_on`），planner 才排得对顺序，
  上游结果变化时才知道谁该重新同步。
- **`dedup` 会剔除条目**：重复组里只留一条，其余改判拒绝（理由「与 X 字节级完全重复」）。
  它不是打分也不是一票否决，单列一种门 `dedup`。先按动作哈希找碰撞组，只对碰撞组再算视频内容哈希。
  留下的是**遍历顺序里最先出现的那一条**，所以这一步**不许并发**：v1 注释写明，并发会让同一份数据
  两次跑出不同的 `duplicate_of`。planner 给它的并发恒为 1，站点配置和任务上限都改不了。
  由此有两个集合要分清：检查通过集合（keep）和去重后的交付集合，报告里的交付统计吃的是后者。
- `timestamp_check` 里包含「残段」规则（时长低于 `min_duration_s`），不是单独的模块。
  `task_success` 的取证仲裁子链 D71 起、判废护栏 D73 起不再触发（代码留给 v1 的 `rejudge`）：每条恰好一次模型请求。
- **没有任务标注的条目不补描述**（D72）：`task_success` 对它写一条「没判」记录（`no_task_text`，只报告），其余检查照常；预检只在 notes 里说有多少条会这样。
  它对某条 episode 失败（调用重试用尽、或解码失败）时，这一条按执行出错处理：不再往后走，待补跑（D24、D33）。
  v1 在这种情况下给空串、让成败判定拿空任务文本照跑，v2 不这么做。
- **`camera_defects` 是 `task_success` 的随附模块**（`rides_on="task_success"`，registry 1.14）：它的答案来自
  `task_success` 每路相机的复核请求里多问的一个字段（设计 13「逐机位画面缺陷」），没有自己的模型调用。
  凡 `task_success` 在跑它就出结果，用户不勾选、预检里跟随宿主的可用性、新建任务的模块界面不显示它，
  报告里有它的小节；`affects_dataset_verdict=False`，只出结果不影响判决。单独 `--modules camera_defects`
  是参数错误。
- `task_success` 是唯一会自己发请求的 VLM 模块（`camera_defects` 随附、`eef_video_consistency` 另有自己的链）。
  请求怎么合并，见 04 篇 §4。

人工复核也由注册表声明（D43，注册表 1.2）。`REVIEW_LINES` 是复核种类目录，每一种写明编号、标题、
条目在哪份清单里（`passed` 还是 `reject`）、未判时算不算「待裁」、可选的判断和按钮名：

| 种类 | 标题 | 条目在 | 算待裁 | 判断 | 由谁产生 |
|---|---|---|---|---|---|
| `task_verdict` | 任务成败弃权 | passed | 是 | 判成功 / 判失败 / 拿不准 / 整条弃用；答案可带 `new_label`（人改写的任务描述，D68） | `task_success`（弃权，以及判废护栏拦下的「标注与画面疑似不符」） |
| `reject_appeal` | 被拒复议 | reject | 否（可以复议，不是必须） | 恢复为可用 / 维持拒绝 / 拿不准 | `appealable` 的模块：`task_success`（只归因于它的拒绝）、`dedup` |

物理与结构硬门（时间戳、运动学、同步）和软分拒绝不可复议，是终局。
**一张卡片一个问题**（D68，注册表 3.0）：一条 episode 的每条问题线只问一次，答案也只有一个。改写标注不是独立的一问 ——
判成败时可以在同一个答案里带上 `new_label`；带着成败结论给的改标直接采信、不再重判，只给改标、成败答「拿不准」的按新描述重判（D39）。
「后续问题」（原 `follow_ups`）机制已去掉。

## 2. 能力需求（`needs`）与预检的对应

| 能力 | 预检怎么判 | 判不到的后果 |
|---|---|---|
| `timestamps` | LeRobot parquet 有时间列（mcap 总有：时间轴是动作 topic 的 `log_time`） | `unsupported` |
| `action` / `state` | `info.json` 的 features 里有 `action` / `observation.state`（mcap：摘要区里有动作 / 状态 topic） | `unsupported` |
| `video` | `info.json` 声明了相机，且文件清单里有对应视频（mcap：有相机 topic；Lance：视频在 `videos.lance` 表里） | `unsupported` |
| `embodiment_profile` | `info.json` 的 `robot_type`（mcap：元数据记录里的型号；或用户补选的型号）能在规格库里查到 | 见 §4 三态 |
| `vlm` | 任务里选了 VLM 后端 | `needs_input`：去选一个或先去添加 |
| `raw_bytes` | 总是满足 | — |

**没有任务标注不是「不可用」的理由。** v1 对无标注条目的处理是先由模型补一句描述再判
（`run.py` 的 `judge_text_and_source`：有标注用标注，没有才用自产 caption），整份数据集都没有标注也照跑 ——
对账基线 `umi_640_notask` 就是这种数据集。预检只统计有/无标注的条数，
在 VLM 模块上给一条提示：「N 条没有任务标注，将先由模型补充任务描述」。

## 3. 预检流程

```
curation preflight --input ...
   │
   ├─ ① 探测格式：看目录结构与 meta 文件
   │     lerobot v2 / lerobot v3 / mcap / lance / lancedb / rrd / unknown
   │
   ├─ ② 不是 LeRobot v2/v3、mcap、Lance → supported=false，模块**全部标灰**，结束（D6；mcap 与 Lance 自 D44 起支持）
   │     标灰原因文案：「当前支持 LeRobot v2/v3、mcap 与 Lance（lerobot-lance-convert 0.3.0 起），检测到 <格式>」
   │     站点关掉了 mcap / Lance（ingest.mcap_enabled / ingest.lance_enabled）时同样全部标灰，原因码 format_disabled
   │
   ├─ ③ 结构校验：搬 v1 `ingest/validate.py` 的 validate_info
   │     必需字段缺失 → supported=false，把缺什么原样告诉用户（拒收要说清楚，客户能照着修）
   │
   ├─ ④ 读 meta：episode 数、相机、fps、robot_type、features、有/无任务标注的条数
   │     匹配数据集语义 profile（`ingest/dataset_profiles/*.yaml`，按 robot_type 等命中），回报命中了哪个
   │     编号不是 0 … count-1 时（保留源编号的子集、按文件名编号的 mcap）另写 `episode_indices`，选择都从它里面选（F12.8）
   │     给数据可视化另写几项（设计 18 §7，C2 只加可选字段）：`features`（info.json 的特征表，names 摊平成列表）、
   │     `camera_info`（与 `cameras` 同序：分辨率、编码、fps、`needs_transcode`——mpeg4 等浏览器放不了的标出来）、
   │     `segment_sources`（认出来的分段标注来源，认不出的写 supported=false 与原因：「标注格式不支持」）、mcap 的 `topics`
   │
   ├─ ⑤ 逐模块算 availability（三态）
   │
   └─ ⑥ 记下 meta 文件的指纹，连同全量文件清单的指纹一起记在数据集登记上（D36）。任务开始时再比一次：
        对不上说明预检之后数据被改过，弹框请用户确认后重新预检（D37，03 篇 §3 第 5 步）
```

**只读 metadata，不读样本数据**：预检要在新建任务页面上秒级返回。
代价是有些问题（某条 episode 的视频损坏）要到跑起来才发现 —— 这是对的取舍，
那些问题本来就该由质检模块报告，而不是预检。

2026-09-24 起（D52、设计 14）预检多三条警告，仍不多读数据：0 字节或过小的文件（看文件列表）、
mcap 录制中断（文件尾没有结束标识）、mcap 摘要区 CRC（摘要区的字节本来就在读）。它们只进 `warnings`，不改变模块可用性。
要读数据的完整性检查（结构、零填充、CRC、v1 的逐条结构校验、可选的逐帧解码）由质检最前面的「数据完整性」模块做（D50、D51）。

**mcap 与 Lance**（D44）的 ③④ 换一种读法，⑤⑥ 不变：

| | mcap | Lance（lerobot-lance-convert 0.3.0 起） |
|---|---|---|
| 元数据从哪来 | 每个 `.mcap` 文件的摘要区（通道、消息数、元数据记录）；TOS 上是几次按范围读，不读消息。没有摘要区的文件预检看不到 topic，给警告，质检时整读 | `meta/`（LeRobot v3.0，带 `storage_format: "lance"`），读法与校验同 LeRobot v3；缺了读 `meta.lance` 镜像 |
| 结构校验 | 全部 episode 都缺动作 topic 或都没有相机 topic → `metadata_invalid`，原因照 v1 写出实际见到的 topic 与 `ingest.mcap_mapping` 的用法 | v1 的 `validate_info`；三表齐但没有 `storage_format` 标记 → `metadata_invalid`（旧插件布局，v1 的原话） |
| 相机、帧率、型号 | topic 规则（缺省 `/action`、`/observation.state`、`/observation.images.<相机>`，UMI 的 topic 自动认）；时间轴取动作 topic 的 `log_time`，`fps` 为 null；型号在元数据记录里 | 同 LeRobot v3 |
| 任务标注 | `/task` topic 或元数据记录；只在 topic 里的，预检数它有标注，文字到质检时才读 | `meta/episodes` 的 `tasks` |
| 指纹（⑥） | 全部 `.mcap` 文件（它们既是元数据也是数据） | `meta/`，没有就 `meta.lance/` |

两种格式都不支持 EEF–视频一致性与它的复核（只读 LeRobot 的视频），这两个模块 `unsupported`，原因码 `format_unsupported_by_module`。

### 3.1 别和运行期的「动作语义预检」混了

v1 还有一个名字相近的东西：`ingest/semantics_preflight.py`。数据集语义 profile 没命中时，
它拿**样本数据**去验 5 种假设（关节-绝对 / 关节-增量 / 末端-绝对 / 末端-增量 / 无法判断），
决定 action 该怎么解释 —— 起因是 libero 的末端增量曾被当成关节角送去比 Franka 的关节极限，5 条全判废。

数据集语义 profile 声明的不只是「关节还是末端」，还有动作各维的含义与单位、旋转的表示法（rpy / rot6d）、
夹爪是哪几维以及极性（数值大是开还是合）、相机的角色（外部 / 腕部）、卡死判定用哪种策略。
这些都影响运动学极限、运动质量和取证仲裁的算法分支，随 `ingest/` 原样搬运。

它要读样本，所以**留在运行期**（数值档开头），原样搬运；结论和 v1 一样写进报告的「数据包完整性」小节。
新建任务页的预检不做这件事，只回报「profile 命中 / 未命中（运行时会做动作语义判断）」。

## 4. 三态 availability

| 态 | 含义 | UI 表现 |
|---|---|---|
| `available` | 可跑 | 默认勾选 |
| `needs_input` | 缺一个用户能补的信息 | 勾选框旁黄色感叹号，点开要求补充 |
| `unsupported` | 数据集不具备该模块所需能力 | 置灰，列在页面下方「不支持的模块」区，附「详细信息」 |

`needs_input` 有两个来源：VLM 模块还没选后端（去选一个即可），以及**运动学极限缺 `robot_type`**。
后者的行为完全照搬 v1（README 已固化）：

- 读到了、规格库支持 → `available`
- 读到了、规格库不支持（如 `koch`、`umi_dual_handheld_gripper`）→ `unsupported`，
  原因写明「机器人型号 koch 不在规格库（已支持：…）」，**整项跳过，其余模块照常，任务不因此失败**
- 读不到 → `needs_input`，用户在新建任务页补选型号；选「跳过」则该模块置为未勾选
- 用户本来就没勾这个模块 → 不追问

规格库当前 9 款：agibot、aloha、franka、google_robot、pusht、so100、so101、ur5、widowx
（按 id 和别名不分大小写匹配；查不到就是查不到，**绝不给默认值**）。
注意它和数据集语义 profile 是两个库：umi 有语义 profile，但没有运动学规格，所以运动学极限对它总是跳过。

## 5. 判废护栏的「标注与画面疑似不符」（D73 起不再产生）

技能画像连它内部的标注审计一起下线了（D68），判废护栏也下线了（D73）：模型判失败就是判废（可复议），不再拿画面描述核对标注。
`label_conflict_suspect` 细码与 `label_audit.json` 只在 D73 之前的结果版本里出现，读方照旧能读。

裁决页把它作为**任务成败那一问的一部分**呈现（一张卡片一个问题）：卡片列出原始标注与画面描述，
给出改写框（预填画面描述），人判成败时可以顺带带上改写后的描述（`new_label`）。

⚠️ **立场纪律（代码注释里写死的，搬运时不得动摇）**：不预设谁对。我方描述由 VLM 生成，实测有两个硬伤——
可能整段看错，且不可复现（方舟 temperature=0 同一条打 5 次得 5 种说法）。所以这一步只报分歧、不下结论。

没有原始标注的数据集（如 `umi_640_notask`）没有可比的两份文本，这个队列为空，是正常的。

## 6. 模块 × 报告小节 一一对应

需求硬要求：「质检报告必须要和任务中的质检模块一一对应」。落地规则：

- 任务勾选了 N 个模块 → 报告就有 N 个小节，一个不多一个不少。
- 模块跑失败 → 小节照常存在，内容是错误信息 + 「重试此模块」按钮。
- 模块被标灰（未跑）→ 不出现在报告里，但报告开头的「本次质检范围」里列出，并注明未跑原因。
- 「一一对应」说的是小节的组织方式，不妨碍横着看：报告页可以从任一模块的明细点进某一条 episode，
  看它在所有模块下的读数、证据和视频（v1「轨迹」页的对应物，见 07 篇 §5）。

## 7. 扩展一个新模块要改哪些地方

本期不实现新模块，但框架要让「加一个模块」是件小事。清单：

1. 在 `core/checks/` 加纯函数实现（不 import daft、不碰 I/O）。
2. 在注册表加一行 `ModuleSpec`（注册表 2.0，设计 17 §2.4）：块与段、细码目录（每个细码对应的分类表项、缺省严重度、
   默认级别、review 级的裁决线、blocking 级可否复议）、覆盖。有参数的，`param_schema` 里每个参数带 `title`（表单上的字段名）、
   `description`、`default`，可选值写成 `oneOf` 的 `{const, title}`，必填的列进 `required` —— 新建任务的第二屏按它生成表单（D38）。
   判定线也是参数（有缺省值），不写在评估工具里。
3. 壳把算法结果写成记录 2.0（发现、assessed、unassessable、读数，设计 17 §1）。拒不拒由策略表在 `aggregate` 定，
   不再在 `pipeline/verdict.py` 声明 hard / soft 与权重（F12.3 起）。
4. 如果是 VLM 模块，且形态是「抽 N 帧、问一次」：`merge_units` 放一个 `DeclaredMergeUnits`（`backend/curation/planner/merge.py`）——
   声明 `frame_policy`、`call_kind`、`units_per_episode`，并按 `(episode_index, context)` 给出这一条的提问单元
   （每个单元带 `prompt_part` 和 `parser`）。planner 读帧策略提出分组，`check` 进程逐条调用它拿单元，
   帧策略相同的模块会被自动合进同一个请求（见 04 篇 §4.2）。多步证据链式的模块不用声明，按自己的方式调用。
   新模块的调用种类用自己的名字（形如模块 id，C3 1.1），不必借 v1 的五种。
5. 报告小节渲染器：给一个默认表格渲染器兜底，需要定制才写。
   要人工复核的，在 review 级细码上写它的裁决线；是新的种类就往复核目录加一项（标题、条目所在的清单、
   是否算待裁、判断和按钮名），再在 `adjudicate-apply` 里加这种判断的执行规则。裁决页对没有专用视图的种类
   按目录通用渲染（标题 + 理由 + 每个判断一个按钮），契约不用改（D43）。
6. 前端：零改动 —— 模块清单、中文名、细码名、检测项名、标灰原因全部来自 API。

**第 6 条是这次重构的核心收益之一**：v1 里加一个模块要同时改 Gradio 页面，
v2 里前端对模块是数据驱动的。
