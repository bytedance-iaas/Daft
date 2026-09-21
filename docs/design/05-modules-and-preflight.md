# 05 模块注册表与预检

## 1. 模块注册表

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
    produces_adjudication: bool                     # 是否会产生人工裁决条目
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
| `task_success` | 任务成败判定 | episode | hard | `video`,`vlm` | vlm | 硬门②，autolabel | **是** |
| `dedup` | 精确去重 | dataset | dedup | `raw_bytes` | post_verdict | 漏斗判决 | 否 |
| `skill_profile` | 技能画像 | dataset | none | `video`,`vlm` | post_verdict | 漏斗判决，dedup，autolabel | **是** |

几点要说明，都来自 v1 代码：

- **v1 里这是「6 + 2」**。前六个是配置里的 `checks`（`pipeline/config.py` 的 `KNOWN_CHECKS`），在漏斗里逐条跑、
  参与判决；`dedup` 和 `skill_profile` 是数据集级的附加项，**在判决之后、只对 keep 集合跑**。
  产品上统称八个模块没问题，但注册表必须把这个区别表达出来（`stage=post_verdict` + `depends_on`），
  planner 才排得对顺序，上游结果变化时才知道谁该重新同步。
- **`dedup` 会剔除条目**：重复组里只留一条，其余改判拒绝（理由「与 X 字节级完全重复」）。
  它不是打分也不是一票否决，单列一种门 `dedup`。先按动作哈希找碰撞组，只对碰撞组再算视频内容哈希。
  留下的是**遍历顺序里最先出现的那一条**，所以这一步**不许并发**：v1 注释写明，并发会让同一份数据
  两次跑出不同的 `duplicate_of`。planner 给它的并发恒为 1，站点配置和任务上限都改不了。
  由此有两个集合要分清：检查通过集合（keep）和去重后的交付集合，技能画像吃的是后者。
- **`skill_profile` 不判废任何一条**，产出的是数据集级结论（技能分布）。但它内部的**标注分歧检出**
  会产生人工裁决条目 —— 它是技能画像的第三步，不是第 9 个模块（见 §5）。
- `timestamp_check` 里包含「残段」规则（时长低于 `min_duration_s`），不是单独的模块。
  `task_success` 内部有取证仲裁子链，有自己的开关，同样不是单独的模块。
- **`autolabel`（无标注补 caption）不是第九个模块**，是 `task_success` 和 `skill_profile` 的共享前置步骤：
  没勾这两个模块就不跑；它没有自己的报告小节，产出体现在「数据包完整性」和任务文本的来源标注里。
  它对某条 episode 失败（调用重试用尽、或解码失败）时，这一条按执行出错处理：不再往后走，待补跑（D24、D33）。
  v1 在这种情况下给空串、让成败判定拿空任务文本照跑，v2 不这么做。
- `task_success` 和 `skill_profile` 是仅有的两个 VLM 模块。它们之间能不能合并请求，见 04 篇 §4。

## 2. 能力需求（`needs`）与预检的对应

| 能力 | 预检怎么判 | 判不到的后果 |
|---|---|---|
| `timestamps` | LeRobot parquet 有时间列 | `unsupported` |
| `action` / `state` | `info.json` 的 features 里有 `action` / `observation.state` | `unsupported` |
| `video` | `info.json` 声明了相机，且文件清单里有对应视频 | `unsupported` |
| `embodiment_profile` | `info.json` 的 `robot_type`（或用户补选的型号）能在规格库里查到 | 见 §4 三态 |
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
   │     lerobot v2 / lerobot v3 / mcap / lancedb / rrd / unknown
   │
   ├─ ② 非 LeRobot v2/v3 → supported=false，八个模块**全部标灰**，结束（D6）
   │     标灰原因文案：「当前版本仅支持 LeRobot v2/v3，检测到 <格式>」
   │
   ├─ ③ 结构校验：搬 v1 `ingest/validate.py` 的 validate_info
   │     必需字段缺失 → supported=false，把缺什么原样告诉用户（拒收要说清楚，客户能照着修）
   │
   ├─ ④ 读 meta：episode 数、相机、fps、robot_type、features、有/无任务标注的条数
   │     匹配数据集语义 profile（`ingest/dataset_profiles/*.yaml`，按 robot_type 等命中），回报命中了哪个
   │
   ├─ ⑤ 逐模块算 availability（三态）
   │
   └─ ⑥ 记下 meta 文件的指纹，连同全量文件清单的指纹一起记在数据集登记上（D36）。任务开始时再比一次：
        对不上说明预检之后数据被改过，弹框请用户确认后重新预检（D37，03 篇 §3 第 5 步）
```

**只读 metadata，不读样本数据**：预检要在新建任务页面上秒级返回。
代价是有些问题（某条 episode 的视频损坏）要到跑起来才发现 —— 这是对的取舍，
那些问题本来就该由质检模块报告，而不是预检。

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

## 5. 技能画像内部的「标注审计」

技能画像（M7）分三步，第三步产生人工裁决条目，前端要能正确呈现：

```
① caption      VLM 给每条 episode 生成画面描述，全员都打（autolabel 补过的直接复用，不重打）
② 归类文本     每条取一段用于归类的文本：**有原始标注用标注，没有才用 caption**，来源留痕
③ taxonomy     LLM 从全体归类文本归纳两级技能体系 → 自查合并（两两判断该不该并）
                → 给每条归族 → 补漏（没归进去的逐条再问）→ 族名翻译
④ 标注分歧检出  原始标注与 caption 各自映射进同一套族体系 → 归到不同族的挑出来
                → 动词等价类修正（place / move / grab 这类互不立案）→ 分档
                → 被挑出的条目重复打标 N 次，我方描述自己都不稳的降级
                → 按嫌疑排序的复核队列，交人工判定
```

第 ② 步是 v1 在 2026-08-16 定的「标注优先」：当时人工复核 29 条分歧，26 条是我方 caption 错、客户标注对。
所以**归类吃标注，caption 只作为分歧检出的另一端**；代码注释里说这是权宜之计，caption 准确率上去后
回退点就在 `grouping_text_and_source` 一处。搬运时这个选择必须保持，而且要有单测钉住。

没有原始标注的数据集（如 `umi_640_notask`）没有第 ③ 步可做，标注分歧队列为空，这是正常的。

⚠️ **立场纪律（代码注释里写死的，搬运时不得动摇）**：不预设谁对。
我方 caption 由 VLM 生成，实测有两个硬伤——可能整段看错，且不可复现
（方舟 temperature=0 同一条打 5 次得 5 种说法）。所以这一步只报分歧、不下结论，
并且会对「自己都不稳定」的条目做降级（`retier_by_caption_stability`）。

产品上的处理（需求方已确认）：
- 技能画像在报告里**正常输出自己的小节**（技能分布、两级体系、每族样本数）。
- 它产生的分歧复核队列，和任务成败判定的弃权队列一样，**在报告对应小节里给一个链接**，
  跳到统一的「人工裁决」页，并带上筛选参数 `?source=skill_profile`。
- 人工裁决页按「哪个模块要求的」分组展示，每条注明诉求（这条标注和画面对不上 / 这条成败判不了）。

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
2. 在注册表加一行 `ModuleSpec`。有参数的，`param_schema` 里每个参数带 `title`（表单上的字段名）、`description`、
   `default`，可选值写成 `oneOf` 的 `{const, title}`，必填的列进 `required` —— 新建任务的第二屏按它生成表单（D38）。
3. 在 `pipeline/verdict.py` 声明它如何参与判决（hard/soft/none + 权重）。
4. 如果是 VLM 模块，且形态是「抽 N 帧、问一次」：`merge_units` 放一个 `DeclaredMergeUnits`（`backend/curation/planner/merge.py`）——
   声明 `frame_policy`、`call_kind`、`units_per_episode`，并按 `(episode_index, context)` 给出这一条的提问单元
   （每个单元带 `prompt_part` 和 `parser`）。planner 读帧策略提出分组，`check` 进程逐条调用它拿单元，
   帧策略相同的模块会被自动合进同一个请求（见 04 篇 §4.2）。多步证据链式的模块不用声明，按自己的方式调用。
   新模块的调用种类用自己的名字（形如模块 id，C3 1.1），不必借 v1 的五种。
5. 报告小节渲染器：给一个默认表格渲染器兜底，需要定制才写。
6. 前端：零改动 —— 模块清单、中文名、标灰原因全部来自 API。

**第 6 条是这次重构的核心收益之一**：v1 里加一个模块要同时改 Gradio 页面，
v2 里前端对模块是数据驱动的。
