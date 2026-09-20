# 05 模块注册表与预检

## 1. 模块注册表

八个模块，**一期数量不变**。注册表是单一事实源：CLI、Daemon、前端三方的模块清单、
中文名、能力要求全部从它生成，不允许任何一方硬编码。

```python
@dataclass(frozen=True)
class ModuleSpec:
    id: str
    name_zh: str
    level: Literal["episode", "dataset"]   # 轨迹级 / 数据集级
    gate: Literal["hard", "soft", "none"]  # 一票否决 / 打分项 / 仅产出
    needs: frozenset[str]                  # 能力需求，见 §2
    stage: Literal["cpu-numeric", "cpu-visual", "vlm"]
    produces_adjudication: bool            # 是否会产生人工裁决条目
```

| id | 中文名 | 级别 | 门 | 需要 | 档 | 产生裁决 |
|---|---|---|---|---|---|---|
| `timestamp_check` | 时间戳检查 | episode | hard | `timestamps` | cpu-numeric | 否 |
| `kinematic_limits` | 运动学极限 | episode | hard | `action`,`embodiment_profile` | cpu-numeric | 否 |
| `motion_quality` | 运动质量 | episode | soft | `action`,`state` | cpu-numeric | 否 |
| `visual_quality` | 视觉质量 | episode | soft | `video` | cpu-visual | 否 |
| `video_action_sync` | 视频-动作同步 | episode | hard | `video`,`action` | cpu-visual | 否 |
| `task_success` | 任务成败判定 | episode | hard | `video`,`vlm`,`instruction` | vlm | **是** |
| `dedup` | 精确去重 | dataset | none | `raw_bytes` | cpu-numeric | 否 |
| `skill_profile` | 技能画像 | dataset | none | `video`,`vlm`,`instruction` | vlm | **是** |

两点要说明：

- **`dedup` 和 `skill_profile` 的 `gate=none`**：它们不判废单条轨迹，产出的是数据集级结论
  （重复组、技能分布）。但 `skill_profile` 内部的**标注分歧检出**会产生人工裁决条目 —— 
  它是技能画像的第三步，不是第 9 个模块（见 §5）。
- **`task_success` 和 `skill_profile` 是仅有的两个 VLM 模块**，也是合并优化的全部作用域。

## 2. 能力需求（`needs`）与预检的对应

| 能力 | 预检怎么判 | 判不到的后果 |
|---|---|---|
| `timestamps` | LeRobot parquet 有时间列 | 模块不可用 |
| `action` / `state` | parquet 有 `action` / `observation.state` 列 | 模块不可用 |
| `video` | `info.json` 声明了相机，且视频文件存在 | 模块不可用 |
| `instruction` | 有任务描述/标注字段 | 模块不可用 |
| `embodiment_profile` | `info.json` 的 `robot_type` 能在规格库里查到 | 见 §4 三态 |
| `vlm` | 任务里选了 VLM 后端 | 模块不可用 |
| `raw_bytes` | 总是满足 | — |

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
   ├─ ③ 读 meta：episode 清单、相机、fps、robot_type、列名
   │
   └─ ④ 逐模块算 availability（三态）
```

**只读 metadata，不读样本数据**：预检要在新建任务页面上秒级返回。
代价是有些问题（某条 episode 的视频损坏）要到跑起来才发现 —— 这是对的取舍，
那些问题本来就该由质检模块报告，而不是预检。

## 4. 三态 availability

| 态 | 含义 | UI 表现 |
|---|---|---|
| `available` | 可跑 | 默认勾选 |
| `needs_input` | 缺一个用户能补的信息 | 勾选框旁黄色感叹号，点开要求补充 |
| `unsupported` | 数据集不具备该模块所需能力 | 置灰，列在页面下方「不支持的模块」区，附「详细信息」 |

`needs_input` 目前只有一个来源：**运动学极限缺 `robot_type`**。行为完全照搬 v1（README 已固化）：

- 读到了、规格库支持 → `available`
- 读到了、规格库不支持（如 `koch`）→ `unsupported`，原因写明「机器人型号 koch 不在规格库」，
  **整项跳过，其余模块照常，任务不因此失败**
- 读不到 → `needs_input`，用户在新建任务页补选型号；选「跳过」则该模块置为未勾选
- 用户本来就没勾这个模块 → 不追问

规格库当前 9 款：agibot、aloha、franka、google_robot、pusht、so100、so101、ur5、widowx。

## 5. 技能画像内部的「标注审计」

技能画像（M7）分三步，第三步产生人工裁决条目，前端要能正确呈现：

```
① caption      VLM 给每条 episode 生成画面描述
② taxonomy     LLM 从全体 caption 归纳两级技能体系，给每条归族
③ 标注分歧检出  原始标注也映射进同一套族体系 → 归到不同族的挑出来
                → 按嫌疑排序的复核队列，交人工判定
```

⚠️ **立场纪律（代码注释里写死的，搬运时不得动摇）**：不预设谁对。
我方 caption 由 VLM 生成，实测有两个硬伤——可能整段看错，且不可复现
（方舟 temperature=0 同一条打 5 次得 5 种说法）。所以这一步只报分歧、不下结论，
并且会对「自己都不稳定」的条目做降级（`retier_by_caption_stability`）。

产品上的处理（按你的要求）：
- 技能画像在报告里**正常输出自己的小节**（技能分布、两级体系、每族样本数）。
- 它产生的分歧复核队列，和任务成败判定的弃权队列一样，**在报告对应小节里给一个链接**，
  跳到统一的「人工裁决」页，并带上筛选参数 `?source=skill_profile`。
- 人工裁决页按「哪个模块要求的」分组展示，每条注明诉求（这条标注和画面对不上 / 这条成败判不了）。

## 6. 模块 × 报告小节 一一对应

需求硬要求：「质检报告必须要和任务中的质检模块一一对应」。落地规则：

- 任务勾选了 N 个模块 → 报告就有 N 个小节，一个不多一个不少。
- 模块跑失败 → 小节照常存在，内容是错误信息 + 「重试此模块」按钮。
- 模块被标灰（未跑）→ 不出现在报告里，但报告开头的「本次质检范围」里列出，并注明未跑原因。

## 7. 扩展一个新模块要改哪些地方

本期不实现新模块，但框架要让「加一个模块」是件小事。清单：

1. 在 `core/checks/` 加纯函数实现（不 import daft、不碰 I/O）。
2. 在注册表加一行 `ModuleSpec`。
3. 在 `pipeline/verdict.py` 声明它如何参与判决（hard/soft/none + 权重）。
4. 如果是 VLM 模块：提供 `prompt_part` 和 `parser`，自动就能参与合并（见 04 篇 §4.2）。
5. 报告小节渲染器：给一个默认表格渲染器兜底，需要定制才写。
6. 前端：零改动 —— 模块清单、中文名、标灰原因全部来自 API。

**第 6 条是这次重构的核心收益之一**：v1 里加一个模块要同时改 Gradio 页面，
v2 里前端对模块是数据驱动的。
