# 18 · 数据可视化：独立的「可视化」页面、报告里的迷你播放器与 mcap 字段映射

> 状态：**定稿 v1.0（2026-10-03）**—— 需求方三轮答复（§8.1–§8.3）与给定的架构都已并入，决策 D60–D64 在 `00-overview.md` §7；ReRun 的参考版本定为 0.38.1（§3.1）；第二期的内容先记在 §10。实现从 F13.1 开始（§9）。分支 `feat/data-visualizer`（从 `feat/curator-v2` 分出，worktree `~/ws/daft-viz`）。
> 需求账本：阶段 13（F13.0–F13.8）。
> 静态稿：`frontend/mockups/visualize.html`（完整版，独立页面）、`episode-visualize-mini.html`（报告里的迷你版）、`dataset-add-mcap.html`（添加数据集的 mcap 配置），手动验证步骤在 `frontend/mockups/README.md`「第三批」。

## 0. 开工指引

读的顺序：§1 背景 → §2 范围与两种布局的功能对照 → §4 架构与数据供给（读取器 → 统一展示模型 → 视图，这是实现上最要紧的一节）→ §5 播放器与页面规格 → §6 字段映射模版 → §7 契约与接口 → §8 已定的决策与需求方三轮答复 → §9 工作包 → §10 第二期（不在本阶段）。
静态稿先看一遍（三页，双击即可打开），对着 §5 核对。

要动的地方：

| 组件 | 位置 | 要做的 |
|---|---|---|
| 契约 | `docs/contracts/openapi.yaml`（C4）、`docs/contracts/cli/preflight.schema.json`（C2）、新增 `docs/contracts/viz-mapping.schema.json`（C7）与 `examples/` | §7 的端点、预检描述符扩展、映射模版 Schema；升版本、刷新锁、`npm run gen:api` |
| Daemon | `backend/daemon/routes/`（新 `viz.py`）、`results/`（新 `viz/`：读取器、展示模型、曲线、帧包、转封装与转码）、`repo/`（数据集的 `viz_mapping`、模版表、迁移）、`orchestr/browse.py` | 统一展示模型与两个读取器、索引与曲线接口、相机供给（直连 / 转封装 / 帧包 / 转码）、mcap 探测与映射存储、转码开关 `CURATOR_VIZ_TRANSCODE` |
| 内核 | `backend/curation/ingest/mcap_reader.py`、`cli/containers.py`、`streams/clip.py`、`cli/lerobot_meta.py` | summary 里补 schema 与编码；按映射模版解消息；JPEG 帧包与 fMP4 转封装；预检写出 features / 相机编码 / 分段候选 |
| 前端 | `frontend/src/features/visualizer/`（新）、`pages/visualize/`（新路由 `/visualize`）、`components/AppLayout.tsx`（侧栏「数据集 › 可视化」）、`pages/report/EpisodesTab.tsx`、`pages/adjudication/EpisodeCard.tsx`、`pages/datasets/*`（「可视化」新窗口、「可视化（旧）」）、`features/datasets/AddDatasetDrawer.tsx`、`locales/zh.ts`、`mocks/` | 播放器、独立页面与左侧栏、报告与裁决的弹窗、mcap 配置表单 |
| 部署 | `docs/design/09-deployment.md` §2.1、rerun 仓库 dataverse Chart 的模板 | 环境变量 `CURATOR_VIZ_TRANSCODE`（缺省开）、转码缓存目录与上限 |
| 文档 | 本篇、`07-frontend.md` §2 / §4.4 / §5 / §6、`03-rest-api.md`、`05-registry-and-preflight.md`、`00-overview.md` §7（已有 D60–D64）、各 README | 实现时同步 |

## 1. 背景与目标

今天控制台的「可视化」是跳到同一部署里的 ReRun 网页查看器（D48），地址经 Daemon 代签（D55，设计 15）。用下来的问题：

- **慢**：ReRun 网页端对 `.mcap` / `.mp4` 是整文件下载完再导入，mcap 解析在 wasm 里单线程跑；LeRobot 导入器只有原生端，网页端能开是因为 F10 做了特殊处理。打开一条 episode 要等很久。
- **TOS 支持弱**：代签链路能用，但 ReRun 的加载模型（整文件）和 TOS 的按区间读天然不合。
- **mcap 字段没法按客户定义映射**：ReRun 对 mcap 只有「选哪些 decoder、过滤哪些 topic」，没有声明式的「这个 topic 是相机、那个字段是关节位置」的配置；自定义 protobuf schema（如 ABC-130k 的 `RobotState`）只能看成一堆原始字段。
- **界面与火山不统一**。

目标：在控制台里内置一个数据播放器 ——

1. 多路相机与运动曲线在**同一条进度条**下同步播放；
2. **两种布局**：独立的「可视化」页面（完整版：信息全、可自定义，左侧选数据集与 episode），质检报告 / 人工裁决里的弹窗（迷你版：固定布局、只看核心信息）；
3. mcap 数据集在**添加时确认一份字段映射模版**（内置 + 自带），可视化与质检都按它读；
4. 架构上是**读取器 → 统一展示模型 → 视图**（D61），以后加格式只加读取器；
5. 做完后报告 Episode 明细里的「各机位视频」退役；跳 ReRun 的旧入口保留一个版本，文字改为「可视化（旧）」。

本期不做（另立后续阶段，不在本篇的 F13 范围里；但统一展示模型与读取器接口按全集定，留好位）：Lance 读取器、三维场景（末端轨迹、点云、URDF）、深度图渲染、浏览器内解 mcap / 解 parquet、我们自己的标注标准、ReRun 入口的最终下线。

## 2. 范围：三个交付面与两种布局

| 交付面 | 在哪 | 内容 |
|---|---|---|
| 完整版 | 侧栏「数据集」分组下的二级项「可视化」（另一项是「数据集列表」），路由 `/visualize?dataset=<id>&ep=<n>`；数据集列表 / 详情的「可视化」在新窗口打开它 | 左侧可收起的侧栏（选数据集 → 列 episode）+ 播放器 + 「数据集信息」树状浏览 |
| 迷你版 | 质检报告 Episode 明细抽屉、人工裁决卡片、任务详情的 Episode 流水线 → 「可视化」弹窗 | 只有播放器；布局由发现决定；进度条标出发现的区间；可跳到完整版 |
| mcap 配置 | 添加数据集抽屉（格式识别为 mcap 时出现）；数据集详情的「mcap 配置」入口 | 探测 → 模版起草 → 表格确认 → 保存 / 另存为模版 / 导入导出 |
| 外部标注文件 | 添加数据集抽屉（任何格式，选填）；数据集详情可换 | 上传一个 JSON 或按 episode 编号命名的 zip（Argus 风格），存为上传件挂在数据集上，读取器合并成一条「外部标注」轨 |

两种布局的功能对照（编号对应需求原文 4.2 的 (1)–(8)）：

| 功能 | 完整版 | 迷你版 |
|---|---|---|
| (1) 统一进度条，视频与曲线同步 | ✓ | ✓ |
| (2) 不允许全屏；播放速度 1x / 1.5x / 2x；跳转到指定帧的输入框 | ✓ | ✓ |
| (3) 模块化格子（每格选视频或曲线）；布局模版「智能展示视频和运动曲线（默认）/ 仅视频 / 仅曲线 / 自定义」与格子数 | ✓ | 格子由发现决定，没有模版选择 |
| (5) 顶栏：数据集 + episode 号、帧率、任务描述（没有则「无任务描述」） | ✓ | ✓ |
| (6) 字幕栏：有分段标注才显示，实时跟随 | ✓ | ✓ |
| (7) 空格子显示「+」，点开选择内容 | ✓ | 无空格子 |
| (8) 格子右上角「更换」「放大（占满播放器）」；完整版另有「清空」 | ✓ | 更换、放大 |
| (8′) 右侧信息侧栏（分辨率、帧率、编码、字段、当前值…），可收起；打开时挤压格子，不覆盖 | ✓ | ✓ |
| 「平台转码」标签（D60） | ✓ | ✓ |
| 左侧栏选数据集与 episode；播放器下方的「数据集信息」树；走带上的上一条 / 下一条 | ✓ | — |
| 进度条上发现的区间色段（blocking 红 / review 橙 / info 灰）与证据帧点标；「发现」芯片切换聚焦；「在可视化页打开」 | — | ✓ |

格式矩阵（本期实现的两个读取器；Lance 第二期）：

| 格式 | 相机 | 曲线 | 任务描述 | 分段标注 |
|---|---|---|---|---|
| LeRobot v2.0 / v2.1 | `dtype=video` 的 feature（image 帧序列本期不做） | 数值 feature（`float32/64`，按 `names` 分组） | `tasks.jsonl` / 首帧 `task_index` | §4.5 的适配规则 |
| LeRobot v3.0 | 同上，按 episodes 表的 from / to 切 | 同上，按 `dataset_from/to_index` 行窗 | `tasks.parquet` / episodes 的 `tasks` | 同上，另有 `subtask_index` + `meta/subtasks.parquet`、`language` 列 |
| mcap | 映射里的 `cameras` | 映射里的 `series` | 映射里的 `task` | 映射里的 `segments` 或外部标注文件 |
| LeRobot + Lance | 第二期：对应版本的 Lance 读取器（今天的 `lance_reader` 只支持 lerobot-lance-convert ≥ 0.3，整表拷贝），产出同一个展示模型 | | | |

## 3. 调研结论

### 3.1 参考的三个工具

- **HF `lerobot/visualize_dataset`**：上排相机、语言指令卡、走带（上一条 / 播放 / 下一条 / 循环、滑杆、`24 / 24` 帧计数、空格与方向键）、下排按关节分组的曲线：同名的 `action`（虚线）与 `observation.state`（实线）叠画，图例里带当前值，勾选可隐藏某条。本篇的曲线格就是这个样子。
- **Pantheon data-board**：双机位 + 深色时间线上的「字幕」（`both grippers · settle above table · idle`）+ 关键事件列表（时刻 + 事件 + 标签）+ 右侧评估侧栏。本篇的字幕栏与进度条上的分段色段来自这里；它的标注格式见 §3.4。
- **ReRun**（参考版本 **0.38.1**：`~/ws/rerun` 已切到分支 `ref-0.38.1` = 「Release 0.38.1」提交 `5c524e9e19`，它的 `main`（fork `bytedance-iaas/rerun`，`107beb60f2`，`0.39.0-alpha.1+dev`）比发布点多 138 个提交，其中 mcap 相关的只有 lens 选择器支持字符串字面量、`Lenses::apply` 改签名、错误信息带完整上下文（#12942）这类重构，没有功能修复；LeRobot 多了导入诊断汇总与 `LargeUtf8` 任务文本。下面的描述以 0.38.1 为准，两版在这些点上一致）：
  - LeRobot：只认目录结构判版本；feature 键原样作实体路径（点不是分隔符）；`float32/64` → `Scalars` + 一次性的 `SeriesLines.names`；`video` 在 v2 是整文件 `AssetVideo` + 逐帧引用，在 v3 是 `VideoStream` 按 `[from, to)` 切 GOP（H.264 / H.265 有 B 帧或不在 GOP 边界要 ffmpeg）；时间轴只有 `frame_index`（有就只用它）或 `timestamp`；`string` / `task_index` / `subtask_index` / `language` → 文本；`int16`、其他 `int64`、`bool` 跳过；不读 `stats.json`、`robot_type`。默认布局不是导入器给的，是查看器的启发式：子树里不止一张彩图就每个相机一个 2D 视图，每个 `Scalars` 实体一个时序视图，超过 12 个视图改页签。
  - mcap：decoders（`ros2msg` 手写的 JointState / Imu / … → `ros2_reflection` → `protobuf` → `raw` 兜底）+ lenses（foxglove 13 个：CompressedImage、CompressedVideo（h264 / h265 / av1 / vp8 / vp9 → `VideoStream`）、RawImage、PoseInFrame(s)、FrameTransform(s)、PointCloud…；ROS 的 Image / CompressedImage（jpeg / png → `EncodedImage`，h264 → `VideoStream`）/ JointState（→ `<topic>/position|velocity|effort`，名字作标签）…）；实体路径 = topic；每条消息两个时间轴 `message_log_time` / `message_publish_time`，另加消息里的时间戳。**没有声明式的字段映射**，用户只能选 decoder、过滤 topic、设时间窗；网页端整文件下载后单线程解析。
  - 结论：我们要的「字段映射模版」就是 lenses 的声明式版本 —— 默认模版按 schema 自动归类（等价于 foxglove / ros2 lenses），再允许按 topic 覆盖；这也正是 ReRun 做不到而客户需要的。
  - 火山网关上现网的 ReRun（2026-10-03 用 Chrome 看了 `so101-pick-place-v2`）：左侧「数据来源」树按 episode 列出（每条是一个预转好的 RRD，`来源 RRD 版本 0.36.0`，3–4 MiB / 条，`File via SDK`），中间是启发式蓝图——`action`、`observation.state` 两个时序视图 + 每路相机一个 2D 视图 + `task` 文本，右侧选择面板，底部 `frame_index` 时间轴与数据流树。我们的完整版布局与它一一对应：左侧栏 ≈ 数据来源树，格子 ≈ 蓝图视图，信息侧栏 ≈ 选择面板，「数据集信息」树 ≈ 数据流树。

### 3.2 样本集的真实元数据（桶 `curation-robo-anchor`，anchor 63 子集 + anchor-nc 25 子集）

| 维度 | 看到的情况 | 对设计的影响 |
|---|---|---|
| 相机路数 | 1（PushT、FastUMI 单臂）到 10（RH20T `cam_<序列号>` × 10）；SO-101 五路、HABIT 五路、双 UR5e 四路 RGB-D | 格子要能放 1–3 列 × 1–3 行；超过 9 路的默认只上前几路，其余在「+」里选 |
| 分辨率 / 编码 | 96×96 到 1280×720；绝大多数 `av1 yuv420p`；**FastUMI 是 `mpeg4`（MPEG-4 Part 2，浏览器放不了）** | 需要「播不了就由 Daemon 转码」的兜底（§4.2，D60） |
| 深度 | `observation.images.front.depth`、`observation.depths.*` 为 `uint16` 列（不是视频） | 本期不渲染，树里可见、「+」里置灰 |
| state / action | 维度 2–29；`names` 有列表（`shoulder_pan.pos …`）、字典（`{"motors": [...]}`）、缺失（RH20T 全部 `null`）三种；RH20T 另有 `observation.state.ee_pose / joint / gripper`、`force / torque / robot_ft`；HABIT 有 60 多个 `robot0.* / robot1.*` 字段；Galaxea 把 state / action 拆成 `observation.state.left_arm`、`action.left_arm` 等十几列 | 曲线分组规则要能处理没有名字（用 `dim_i`）、字段很多（默认只上 state / action，其余在「+」里）和拆列（按前缀合成一组）的情况 |
| 任务与分段 | 任务文本在 `tasks.jsonl / parquet`；分段的写法五花八门（§3.4） | 分段标注要「预检探测 + 用户确认」，不能硬编码一种字段名 |
| mcap（GenRobot UMI） | foxglove protobuf：`/robotN/sensor/camera0/compressed`（`CompressedImage` JPEG 1280×720 30 Hz）、`/robotN/vio/eef_pose`（`PoseInFrame` 30 Hz）、`/robotN/sensor/magnetic_encoder`（50 Hz）、IMU 200 Hz、`camera_info`、`robot_info`、`system_info`；没有任务字段 | 内置 UMI 模版（已有识别逻辑）；JPEG 相机要用帧包（§4.2） |
| mcap（ABC-130k） | `foxglove.CompressedVideo`：顶部双目 **H.265**、腕部 H.264，30 Hz；自定义 schema `RobotState / GripperState`（200–270 Hz，`/left-arm-state`、`/left-arm-action`…）；`/instruction` topic 与 `episode-metadata`（`task_name`、相机型号与分辨率） | 自定义 protobuf 要按描述符展开数值叶子让用户确认；state / action 成对的 topic 叠画；H.265 要看浏览器能力 |

### 3.3 平台现状（代码）

- 前端：Arco 原生主题，页面壳 `AppLayout`，侧栏是 概览 / 质检（质检任务、人工裁决）/ 数据集 / 系统和资源配置；数据集详情页没有页签。「可视化」按钮只在数据集列表与详情页头（F10.3），报告里没有。报告 Episode 明细的「各机位视频」是 `features/media/SyncedVideos` + `SignedMedia`（`GET /media/sign`，任务级）+ `syncPlayback.SyncController`（按 episode 时间对齐、2 s 预读、跟随任一路）。发现的区间 `time_s` 可点（所有机位一起跳），`frames` 只显示、不换算（EpisodeView 没有 fps）。
- Daemon：`GET /datasets/episodes` 只给 LeRobot 相机的预签名地址（mcap 为空）；mcap 相机只有任务级的 `GET /tasks/{id}/episodes/{i}/cameras/{cam}.mp4`（内存里封装：JPEG → **MJPEG mp4，浏览器 `<video>` 放不了**；H.264 Annex-B 转封装；H.265、PNG、RawImage 拒绝；这个路由没写进 C4）。`POST /datasets/{id}/sign` 能签任意键的 GET（Range 不参与签名，一个地址可复用）。**没有任何接口返回逐帧的 state / action 序列**。
- 内核：mcap 的映射只在站点配置 `ingest.mcap_mapping`（全局、无校验、不按数据集），默认 `/action`、`/observation.state`、`/task`、`/observation.images.` 前缀，另有内置 UMI 识别；`McapSummary` 不保留 schema 名与消息编码；只用 `log_time`；预检描述符里没有 features / names / 分辨率 / 编码 / topic 清单。F5.16 的 EEF `record_mapping` 是模块参数（上传件），不在数据集上。Lance 读取器只支持 lerobot-lance-convert ≥ 0.3 的三表布局，TOS 上整表拷到源缓存。

### 3.4 分段标注的现成格式（2026-10-03 核实）

没有一个通用标准，但有几种成型的写法，统一标注模型（§4.5）要能装下它们：

| 来源 | 写法 | 读成什么 |
|---|---|---|
| LeRobot v3（官方，最新 `lerobot`） | 逐帧 `subtask_index`（int64）+ `meta/subtasks.parquet`（`subtask_index`, `subtask`）；ReRun 也认 | 片段：连续相同 `subtask_index` 的帧为一段，名称查表 |
| LeRobot v3 的 `dtype: language` 列（HIW-500 等） | `language_persistent` / `language_events`：每帧一个列表，元素 `{role, content, style('subtask'…), timestamp, camera, tool_calls}`；persistent 从 `timestamp` 起持续有效，events 是瞬时的 | persistent 的 `style=subtask` → 片段（下一条起点为终点）；events → 事件 |
| Galaxea（LeRobot v2.1） | 逐帧 `task_index` 在一条 episode 里变化（7 个步骤文本在 `tasks.jsonl` 里，中英文用 `@` 连接）；`coarse_task_index` 是整条的任务；`quality_index` / `coarse_quality_index` 指向 `tasks.jsonl` 里的 `qualified` / `unqualified` | 片段：`task_index` 的连续段；片段质量取同一段的 `quality_index`；条目任务取 `coarse_task_index` |
| HABIT（LeRobot v2.0） | 逐帧 `low_level_task_index` + `meta/subtasks.jsonl`，`human_role_subtask_index` + `meta/human_subtasks.jsonl`；布尔段列 `is_intervention_segment` / `is_high_jerk_segment` / `is_error_segment`；episodes 行里有 `low_level_tasks[]`、`task_status`（如 `recovered`） | 片段（两套：机器人分步、人的分步）；布尔列 → 带标志的片段；条目标签 `task_status` |
| RSS 2026（LeRobot v2.1） | 逐帧字符串列 `subtask`（这个数据集全是 `TODO` 占位） | 字符串连续段 → 片段；全是占位时不显示（预检已有 FILE-8 / LABEL-3 的提示） |
| Pantheon Argus（外部标注，开源） | 每条 episode 一个 JSON：`timeline[]`（`t_s`, `end_s`, `arm`, `verb_class`, `object`, `carry_phase`, `contribution` = advancing / wasteful / idle, `progress`）、`key_events[]`、`completion{outcome, goal frame}`、`operator_mistakes`、`recovery`、`data_issues`、`state_changes`、`scene_graph`；已发布的 `labels-2026-09-28` 用的是 `event_labels[]` 同一套字段 | `timeline` → 片段（名称 `verb_class`，部位 `arm`，贡献 `contribution`）；`key_events` → 事件；`completion` → 条目标签。h200-14 上有本地副本 `raw/pantheon_labels/published_labels_2026-09-28`，可作外部标注文件的接入样本 |
| mcap | 没有约定；映射里指定 `segments` 的 topic 与字段，或附件 JSON | 片段 / 事件 |

## 4. 架构与数据供给

### 4.0 读取器 → 统一展示模型 → 视图（D61）

```mermaid
flowchart LR
    A["LeRobot v2 / v3"] --> D["LeRobot 读取器"]
    B["MCAP"] --> E["MCAP 读取器与消息解码"]
    C["LeRobot + Lance"] --> F["对应版本的 Lance 读取器"]
    D --> G["统一展示模型<br/>记录、时间轴、数据流、标注"]
    E --> G
    F --> G
    G --> H["视频、曲线、三维场景<br/>字段浏览、片段与标注"]
```

- **读取器**（Daemon 内，`results/viz/readers/<format>.py`）只认自己的格式，产出统一展示模型；每个读取器实现同一个接口：
  - `probe(dataset)`：数据集级描述（相机、数值流、任务文本来源、分段候选、字段树）；读 meta / summary，不读数据本体；
  - `episode(dataset, index)`：一条 episode 的记录：时间轴、每路相机的来源与时间范围、每个数值流的读取句柄、标注；
  - `series(dataset, index, stream, from, to, points)`：数值流的采样；
  - `media(dataset, index, camera)`：相机的供给方式（直连地址 / 转封装 / 帧包 / 转码）与字节源；
  - `fields(dataset)`：字段浏览用的树与各节点详情。
- **统一展示模型**（JSON，C4 里的 `VizDataset` / `VizEpisode`，C7 的映射模版就是 mcap 读取器的配置）：

| 实体 | 字段 | 说明 |
|---|---|---|
| 记录 `Dataset` | `id`、`format`（kind、version、reader）、`fps`、`episode_indices`、`cameras[]`、`streams[]`、`annotation_sources[]`、`field_tree` | 数据集级，按 meta 指纹缓存 |
| 记录 `Episode` | `index`、`duration_s`、`frames`、`task`、`cameras[]`（key、media 方式、url、from_ts、to_ts、transcoded）、`streams[]`、`annotations` | 一条 episode |
| 时间轴 `Timeline` | `kind`（`frame` / `timestamp`）、`fps`、`t0`、`frame_reference` | episode 时间为唯一时钟（D64） |
| 数据流 `Stream` | `key`、`kind`（`video` / `image_sequence` / `series` / `depth` / `pointcloud` / `transform` / `text` / `event`）、`name`、`unit`、`dims[]`、`role`（state / action / other）、`pair_with`、`source` | 相机是 `video` 或 `image_sequence`（JPEG 帧包）；曲线是 `series`；`depth` / `pointcloud` / `transform` 本期只进字段树，供第二期的三维场景与深度图 |
| 标注 `Annotations` | `segments[]`（`start_s`, `end_s`, `label`, `quality`, `contribution`, `arm`, `source`）、`events[]`（`t_s`, `label`, `outcome`, `source`）、`labels{}`（成败、评分、`task_status` …）、`tracks[]`（多套分段时每套一条轨） | §4.5 |
| 字段树 `FieldTree` | 节点 `{path, kind, dtype, shape, names, detail, stream_key?}` | 完整版的「数据集信息」 |

- **视图**只认模型：视频格、曲线格、字幕栏与分段色段、字段浏览；三维场景（`transform` / `pointcloud` / URDF）与深度图是第二期的视图，模型里先留位。

### 4.1 原则

- 沿用 D16：**浏览器能直接播的媒体直连 TOS**（预签名，D55 的签名接口与现有续签逻辑）；Daemon 不中转能直连的字节。
- Daemon 只做三类事：**浏览器播不了的相机的转封装 / 帧包 / 转码**，**本地挂载数据集的字节**（没有 TOS 可签，Daemon 自己带 Range 出文件；旧的 ReRun 入口不支持本地数据集，新入口支持——需求方 2026-10-03），以及**小体积的派生数据**（曲线、标注、索引）。全部按 `episode × 相机` 或 `episode × 数据流` 粒度，带 Range，先内存 LRU、再磁盘缓存（`<CURATOR_DATA_DIR>/viz/<dataset_id>/<meta 指纹>/ep<N>/…`，指纹变了即作废）。缓存与转码产物只放本地数据盘，不上 TOS（需求方 2026-10-03）。
- 一切都是**按需、首次打开时生成**；可选的「预生成」放第二期（登记后后台跑一遍，给大数据集）。

### 4.2 相机供给矩阵（D60）

| 来源 | 编码 | 浏览器能否直接播 | 供给方式 | 格子上的标签 |
|---|---|---|---|---|
| LeRobot v2 | av1 / h264 | 能 | 预签名直连整个 mp4 | — |
| LeRobot v3 | av1 / h264 | 能 | 预签名直连 + `#t=from,to`（现有 `withFragment`），播放器按 `from_ts` 对齐；大文件可选由 Daemon 按 GOP 切出该 episode（第二期） | — |
| LeRobot | mpeg4 part 2、其他浏览器不支持的编码 | 不能 | Daemon 转码为 H.264 fMP4（首次慢，磁盘缓存；预检时就标出「需要转码」） | 「平台转码」 |
| mcap `CompressedImage`（JPEG） | — | 不能当 `<video>` | **JPEG 帧包**：Daemon 输出 `frames.bin`（头部 = 每帧偏移 / 长度 / 时间戳的索引，正文 = 原 JPEG 字节），播放器用 `createImageBitmap` + canvas 逐帧绘；随机访问靠 Range，不转码。单路 1280×720 30 Hz 约 2 MB/s | — |
| mcap `CompressedVideo` h264 | h264 | 能（要 fMP4） | Daemon 转封装为 fMP4（无重编码；现有 `_mux_annexb` 做成流式、带 Range） | — |
| mcap `CompressedVideo` h265 | hevc | 看平台（Safari 能；Chrome 要硬解） | 转封装为 `hvc1` fMP4；播放器探测 `canPlayType`，不能播时向 Daemon 要转码版本 | 转了才标「平台转码」 |
| mcap `RawImage` / PNG | — | 不能 | 本期不支持；预检与映射表里标出 | — |
| 本地挂载的数据集（任一格式） | 同上各行 | 同上 | 没有预签名可用：Daemon 直接出本地文件的字节（`access: local`，带 Range），再按上面各行决定是否转封装 / 帧包 / 转码 | 同上 |

转码开关：容器环境变量 `CURATOR_VIZ_TRANSCODE`（缺省 `1`，开；设 `0` 关闭后播不了的相机在格子里显示原因）。转码由 Daemon 调 ffmpeg 子进程，单独的小并发池（缺省 2 路），不占质检的 CPU 名额池（阶段 9）；磁盘缓存有上限（缺省 20 GB，LRU），产物只在本地数据盘、不上 TOS（需求方 2026-10-03 确认）。凡是转过码的画面，格子左上角相机名旁边标橙色「平台转码」，侧栏「读取方式」也写明原始编码。

### 4.3 曲线供给

`GET /datasets/{id}/episodes/{index}/series?stream=<key>&from=<s>&to=<s>&points=<n>` → 列式 JSON（`t[]`、`series[{name, role, values[]}]`、`unit`、`total_points`）。

- 缺省 `points=2000`：按 min / max 抽稀保峰值；放大某段时按区间再取，到全精度为止。曲线组是读取器预先定义的数据流（§5.3），一次请求一组。
- LeRobot：读 parquet 列（v2 整文件、v3 行窗 `dataset_from/to_index`），复用现有 `dsfs.read_parquet`，但要**列投影**（今天是整表读）；拆成多列的（Galaxea 的 `observation.state.left_arm` 等）按前缀合成一个流。
- mcap：按映射解消息（现有 `_extract_source` 的字段展开），重采样到参考时间轴（§4.4），`pair_with` 的 state / action topic 对齐后同组输出。
- 体量：一条 20 s × 30 fps × 14 维 × 两套 = 约 70 KB JSON；没必要上 Arrow，先 JSON。

### 4.4 时间轴与帧号（D64）

- 播放器只有一个时钟：**episode 时间 t（秒）**，从 episode 的第一帧算起。视频、曲线、字幕、进度条都用它。
- 帧号：LeRobot `frame = round(t × fps)`；mcap `frame` = 帧号基准 topic（缺省第一路相机）的消息序号。跳帧输入框按这个定义。
- 相机帧率与数据 fps 不同（或 mcap 各 topic 各有节奏）时，每路按自己的时间戳取 ≤ t 的最近一帧；曲线按自己的采样时刻画，不插值。
- v3 的 `from_ts`：直连视频的 `currentTime = t + from_ts`（现有 `SyncController` 的做法）。

### 4.5 任务文本与标注（D64）

- 任务文本：LeRobot 用现有 `tasktext` 的优先级（人工改标 > 原始标注 > 自动补标；数据集页里只有原始标注）；Galaxea 这类整条任务在 `coarse_task_index` 的按读取器规则取；mcap 按映射的 `task`。没有就显示「无任务描述」。
- 统一标注模型：**片段**（起止、名称、质量 / 贡献、执行部位、来源）、**事件**（时刻、名称、结果、来源）、**条目标签**（成败、评分、`task_status` 等），多套分段并存时各成一条「轨」（如 HABIT 的机器人分步与人的分步），字幕栏显示用户选定的那条轨，其他轨在进度条上方以细条叠放（最多 3 条）。
- 来源与适配（§3.4 的表）。口径（需求方 2026-10-03）：**已知格式直接支持、自动识别；识别不出的只警告「标注格式不支持」，不显示字幕；我们自己的标注标准后续另立**，不在本篇：
  - LeRobot 读取器按优先级识别：`subtask_index` + `meta/subtasks.*` → `language_persistent`（`style=subtask`）→ 逐帧 `task_index` 在一条里有变化 → 其他 `*_index` + 同名 `meta/*.jsonl` 查表（HABIT 的 `low_level_task_index`）→ 逐帧字符串列（RSS 的 `subtask`，全是占位不算）；布尔 `is_*_segment` 列 → 带标志的片段；`language_events` → 事件；`*quality_index` / `task_status` / `next.success` / `meta.rating` → 片段质量或条目标签。识别到的来源写进预检描述符（`segment_sources`），几套并存时各成一轨，字幕栏取优先级最高的一轨，其他轨在信息侧栏里切；有疑似分段字段但对不上这些写法的（比如字符串列全是占位、索引列没有查表文件），预检与播放器警告「标注格式不支持」。
  - mcap 读取器按映射里的 `segments`（topic 的 start / end / label 字段，或附件 JSON）；映射没写就不显示，不警告。
  - 外部标注文件也算已知格式：Pantheon Argus 风格的每条 episode 一个 JSON（`timeline` / `key_events` / `completion`，或已发布标注里的 `event_labels`）。**添加数据集时可选上传**（需求方 2026-10-03）：一个 JSON（单条）或一个 zip（每条 episode 一个 JSON，按编号命名），按已知格式校验后存为 Daemon 上传件（复用 `POST /uploads` 的机制，`kind=viz_annotations`）挂在数据集上，详情页可换；读取器合并成一条轨「外部标注」，识别不出的格式提示「标注格式不支持」。
  - 质检任务产出的区间（TASK-1 动作起止、ACT-7 人工接管…）本期不进字幕栏，只作为「发现」色段出现在迷你版里；以后与自己的标注标准一起定。

### 4.6 迷你版的证据来源

- `EpisodeView.findings[].finding` 的 `frames` / `time_s` / `scope.camera(s)`（记录 2.0，设计 17 §1.2）→ 进度条下方的色段，颜色按级别（blocking `#F53F3F`、review `#FF7D00`、info `#86909C`），聚焦的那条加外圈；单帧用点标。`frames` 换算成秒需要 fps —— C4 的 `EpisodeView` 补 `fps`（§7）。
- `EpisodeView.evidence[]`（证据帧）→ 进度条上的点标，点开跳到那一帧。
- 智能布局（按发现所属模块）：

| 模块 | 迷你版格子 |
|---|---|
| visual_quality、camera_defects、data_integrity（视频类） | 范围里的相机 + 另一路相机（1 × 2） |
| video_action_sync、eef_video_consistency、motion_quality、kinematic_limits、timestamp_check | 范围里的相机 + 该臂的关节曲线（1 × 2）；有任务产出的同步曲线时可加第三格（第二期） |
| task_success、skill_profile、dedup、标注类 | 全部相机（1 × N，N ≤ 3；更多的收进「更换」） |

### 4.7 与现有部件的关系

- 复用 `SignedMedia` 的签名 / 续签（30 s 内过期即续签）与 `syncPlayback` 的对齐思路；新播放器自己驱动 `<video>`（直连 / 转封装 / 转码）和 canvas（帧包），不再用原生控件（不允许全屏）。
- 「各机位视频」（`SyncedVideos`）在报告与裁决页退役；人工裁决卡片里的三路视频也换成迷你版弹窗（07 §6 说裁决与报告用同一个播放器）。
- ReRun 入口：按 D63 保留一个版本，文字「可视化（旧）」，位置不变（数据集列表操作列、详情页头）。

## 5. 播放器与页面规格

### 5.0 完整版页面（`/visualize`，D63）

- 侧栏「数据集」变成有二级的分组（像「质检」）：「数据集列表」与「可视化」；路由 `/visualize?dataset=<id>&ep=<n>`，没有参数时选最近打开的数据集。
- 页面左侧是**可收起的侧栏**（292 px，收起成一个把手）：顶部数据集下拉（可搜索，列出已登记且格式支持的数据集；mcap 映射未确认的置灰并写原因）→ 数据集摘要一行（episode 数、相机路数、fps、帧数、体积、分段来源）→ episode 筛选（编号 / 任务描述）与排序（编号 / 时长 / 无分步在前）→ episode 列表（编号、时长、任务描述一行、分步小色块；当前条高亮）。点一条即播放。
- 右侧主区：播放器 + 「数据集信息」树（§5.7）。页头有「新建质检任务」（带着当前数据集）与「数据集详情」。
- 数据集列表与详情页头的「可视化」用 `target=_blank` 打开这一页并带 `dataset`；旁边是「可视化（旧）」。

### 5.1 顶栏

`数据集名 · ep N` ｜ 格式 ｜ `fps` ｜ `帧数 · 时长` ｜ `任务：…`（省略号，悬停全文；没有则灰字「无任务描述」）｜ 右侧：完整版有「布局模版」（智能 / 仅视频 / 仅曲线 / 自定义）与格子数（1×1、2×1、2×2、3×2、3×3），两种布局都有「信息」按钮（侧栏开关）。

### 5.2 格子

- CSS grid，列数 1–3，行数 1–3；格子高度随可用宽度变（约 0.66 倍格子宽，160–420 px）；信息侧栏或左侧栏打开 / 收起时格子跟着变，智能布局不够放三列就降到两列。
- 视频格：4:3 画面在格子里等比居中（黑边），左上角相机名芯片（带相机色点；转码的带橙色「平台转码」），左下角 `mm:ss.s · 帧 N`。
- 曲线格：标题 `曲线组名 · 单位`；图区（网格、x 轴秒、y 轴自适应）；每个维度一种颜色，**实线状态、虚线动作**；竖线光标 + 时间标签；点图跳转；下方图例：色样、名字、状态值 / 动作值，点图例隐藏 / 显示该维度。调色板：Arco 的蓝 / 青 / 橙 / 紫 / 绿 / 品红 / 金 / 青柠 8 色循环。
- 空格子（仅完整版）：虚线框 + 「+ 选择要看的内容」。
- 格子工具（悬停或聚焦时出现在右上角）：更换（弹出菜单：相机 / 运动曲线 / 其他——深度图、末端轨迹等置灰并写明原因）、放大 / 还原（占满播放器区域，其他格子隐藏）、清空（仅完整版）。
- 聚焦：点格子加蓝框；信息侧栏跟着它。

### 5.3 曲线分组（缺省规则）

1. 只对数值数据流建组；`observation.state` 与 `action` 同名维度叠成一组。
2. 维度 ≤ 8：一组；更多的按 `names` 的公共前缀切（`left_* / right_*`、`arm_left_* / arm_right_*`、`kLeft* / kRight*`），切不开的每 7 维一组。
3. 夹爪维度（名字含 `gripper`）单独一组。
4. 没有名字的用 `dim_0 … dim_n`。
5. 拆成多列的数据集（Galaxea 的 `observation.state.left_arm` / `action.left_arm`…）按列名前缀合成流，`observation.state.X` 与 `action.X` 配对。
6. 其他数值流（`observation.eef_pose`、`force`、`torque`、HABIT 的几十个 `robot0.*`）都建组，但不进「智能」布局，只在「+」/「更换」里出现。
7. mcap 的组就是映射里的 `series` 条目（`pair_with` 把 state / action 两个 topic 叠成一组）。

### 5.4 走带与进度条

- 按钮：上一帧、播放 / 暂停、下一帧；速度 1x / 1.5x / 2x；时间 `mm:ss.s / mm:ss.s`；帧输入框（回车跳转）与总帧数；循环开关；完整版另有「上一条 / 下一条」（选具体哪条在左侧栏）。
- 进度条：主轨 + 把手，点击 / 拖动跳转，悬停显示时间、帧号、所在步骤；上方细条是分段标注的色段（相邻两段颜色交替，`unqualified` 为橙），点色段跳到段首；下方细条是发现的区间色段（迷你版）。
- 键盘：空格 播放 / 暂停，← → 逐帧，Shift + ← → 跳 1 秒（焦点在播放器内才生效，不抢输入框）。
- 不允许全屏：没有全屏按钮，`<video>` 不带原生控件，不响应双击。

### 5.5 字幕栏

有分段标注才显示：`步骤 i/n · 名称 · s–e s`，不合格的带橙色标签；右侧注明来源（如「meta/episodes.jsonl 的 subtasks」）。两段之间的空档显示「（无标注）」。多条轨时字幕栏显示选定的轨，切换在信息侧栏的「标注」一节。

### 5.6 信息侧栏

- 视频格：键名、分辨率、编码（转码的写「原始编码 → 播放用 h264（平台转码）」）、帧率、帧数、时长；来源文件、时间范围（v3 的 from–to）、读取方式（直连 / 转封装 / 帧包 / 平台转码）；当前帧与时间；「在新标签打开源文件」。
- 曲线格：状态 / 动作字段与维度、单位、采样；序列列表（勾选显示、色样、名字、当前值 状态 / 动作）；一句说明。
- 空格子 / 未聚焦：提示文字。

### 5.7 完整版的下半页：「数据集信息」

- 左树右详情。树：相机（每路：分辨率 · 编码，转码的带标签）、状态与动作（数值流与 shape）、任务与标注（任务文本、分段标注的各条轨与来源、episodes 表）、元数据（`info.json`、`stats.json`、`README.md`）、文件（`data/`、`videos/`，来自登记时的指纹清单，不再逐个访问 TOS）；mcap 换成 topic / schema / metadata / attachments。详情：属性表、JSON 预览、「加入播放器」。
- 树就是统一展示模型里的 `FieldTree`，和格式无关。

### 5.8 错误与空态

- 相机播不了（编码不支持且转码已关闭）：格子里显示原因与建议（开转码 / 换相机），其他格照常。
- 签名过期：自动续签，期间格子上蒙一层「重新获取地址…」。
- mcap 映射没有相机 / 曲线：格子里提示去「mcap 配置」。
- 曲线组太大（> 50 万点）：先给下采样版本，提示放大查看细节。
- 加载中：格子骨架 + 顶栏「正在生成可视化索引」（首次打开 mcap 或转码要几秒到几十秒，转码显示进度）。

## 6. 字段映射模版（mcap 配置，D62）

### 6.1 定位

数据集级配置，可视化与质检共用；**内置模版自动起草，用户在表格里确认**，可另存为模版给同一套采集系统的其他数据集复用，也可以直接导入自带的 JSON。契约 C7：`docs/contracts/viz-mapping.schema.json`（`schema_version: viz-mapping/1.0`）。它就是 mcap 读取器的配置。

### 6.2 数据模型

```json
{
  "schema_version": "viz-mapping/1.0",
  "name": "GenRobot UMI（robot0 / robot1）",
  "base": "builtin:umi",
  "episode_files": "episode_{index}.mcap",
  "timeline": { "source": "log_time", "frame_reference": "/robot0/sensor/camera0/compressed" },
  "cameras": [
    { "topic": "/robot0/sensor/camera0/compressed", "name": "robot0 相机", "schema": "foxglove.CompressedImage" }
  ],
  "series": [
    { "topic": "/robot0/vio/eef_pose", "name": "robot0 末端位姿", "schema": "foxglove.PoseInFrame",
      "fields": ["pose.position.x", "pose.position.y", "pose.position.z", "pose.orientation"],
      "transforms": { "pose.orientation": "quat_xyzw_to_rpy" }, "units": { "position": "m", "angle": "rad" }, "role": "state" },
    { "topic": "/left-arm-action", "name": "左臂关节", "fields": ["q"], "role": "action", "pair_with": "/left-arm-state" }
  ],
  "task": { "metadata_key": "task_name" },
  "segments": null,
  "ignore": ["/robot0/sensor/imu", "/robot0/sensor/camera0/camera_info"]
}
```

| 字段 | 含义 |
|---|---|
| `base` | 起草用的内置模版（`builtin:foxglove` / `builtin:ros2` / `builtin:umi`）或 `null`（纯自带） |
| `timeline.source` | `log_time`（缺省）/ `publish_time` / `message_timestamp`（消息里的时间戳字段） |
| `timeline.frame_reference` | 帧号基准 topic（§4.4） |
| `cameras[]` | 相机 topic、显示名、可选 `schema`；编码与尺寸由探测得出，不写进模版 |
| `series[]` | 曲线组：topic、显示名、要展开的字段（点路径，支持 `*`）、可选变换（四元数转欧拉角、deg→rad）、单位、`role`（`state` / `action` / `other`）、`pair_with`（叠画的另一 topic） |
| `task` | `{ "metadata_key" }` / `{ "topic", "field" }` / `null` |
| `segments` | `{ "topic", "start_field", "end_field", "label_field" }` / `{ "attachment" }` / `null` |
| `ignore[]` | 明确忽略的 topic（没列的 topic 按「未映射」提示） |

质检用的 `ingest.mcap_mapping`（action / state / task / video）由它派生：`role=action` 的组 → `action`，`role=state` → `state`，`cameras` → `video_topics`。站点配置里的 `ingest.mcap_mapping` 降级为「没有数据集映射时的缺省」。

### 6.3 内置模版

| 模版 | 规则 |
|---|---|
| `builtin:foxglove`（ReRun 同款） | 按 schema 归类：`CompressedImage` / `CompressedVideo` / `RawImage` → 相机；`PoseInFrame(s)` → 位置 + 姿态曲线；`JointState`（如经 ROS 桥）→ position / velocity / effort；`IMUMeasurement`、`MagneticEncoderMeasurement`、`Float*` → 曲线（高频的默认忽略，可改）；`CameraCalibration`、`FrameTransform(s)`、`Log`、`RobotInfo`、`SystemInfo` → 忽略；`Instructions` / `/instruction` / metadata 的 `task*` → 任务描述 |
| `builtin:ros2` | `sensor_msgs/Image`、`CompressedImage` → 相机；`JointState` → 曲线（`name` 作标签）；`geometry_msgs/PoseStamped`、`TwistStamped`、`WrenchStamped` → 曲线；`std_msgs/String` 的 `/task`、`/instruction` → 任务描述；`tf`、`CameraInfo` → 忽略 |
| `builtin:umi` | 现有的 UMI 识别：`/robotN/sensor/cameraN/compressed`、`/robotN/vio/eef_pose`、`/robotN/sensor/magnetic_encoder` |
| 自定义 protobuf schema（如 ABC-130k 的 `RobotState`） | 按描述符反射展开数值叶子（现有 `_extract_source` 的做法），表里列出候选字段让用户勾选；`*-state` / `*-action` 成对的 topic 自动 `pair_with` |

自动匹配：探测到的 topic 集合先与站点模版库（内置 + 团队）逐个比对（覆盖率最高且 ≥ 80% 的中）；都不中就用 `builtin:foxglove` 或 `builtin:ros2`（看消息编码）起草。

### 6.4 流程

1. 添加数据集 → 预检识别为 mcap → 抽屉里出现「mcap 配置」区块。
2. **探测**：读第一个（或指定的）文件的 summary 段 + 每个 topic 的首条消息：topic、schema 名与编码、频率、消息数、起止时间、画面尺寸与编码；多文件 topic 布局不一致时提示（现有 `_mapping_signature`）。TOS 上只有几次区间读，不下载整文件。
3. **起草**：按 §6.3 自动匹配，填好表格（用途、显示名、字段）。
4. **确认**：用户改用途 / 显示名 / 字段；时间轴、帧号基准、任务描述来源、分段标注来源四个下拉；摘要行与警告（没有相机、没有曲线、高频 topic、不支持的编码）。
5. **保存**：写到数据集（`Dataset.viz_mapping`，带版本号与更新时间）；「另存为模版」进站点模版库（名称、可见范围）；「导出 JSON」/「从 JSON 导入」支持自带模版（导入时按 C7 校验，不通过逐条报错）。
6. 质检任务开始时把映射冻结进 `run.json`；改映射只影响之后的新任务；数据集详情有「mcap 配置」入口可改。
7. 没确认映射的 mcap 数据集：列表里标「映射待确认」，「可视化」置灰，建任务时按站点缺省（与今天一致）。

### 6.5 对 LeRobot 的「展示配置」

LeRobot 不需要字段映射（`info.json` 已经够），但完整版允许保存一份轻量的「展示配置」（缺省布局、曲线分组覆盖、分段轨选择、相机顺序），同样挂在数据集上；本期只做分段轨选择与相机顺序，其余第二期。

## 7. 契约与接口改动

C4（`openapi.yaml`，升小版本）：

| 端点 | 用途 |
|---|---|
| `GET /datasets/{id}/viz` | 统一展示模型的数据集级记录：`cameras[{key, name, width, height, codec, fps, access: direct｜local｜remux｜frames｜transcode｜unsupported}]`、`streams[]`、`fps`、`episode_indices`、`annotation_sources[]`（含 `unsupported` 的警告）、`mapping_version`、`field_tree` |
| `GET /datasets/{id}/episodes/{index}/viz` | episode 级：`duration_s`、`frames`、`task`、`annotations`、`cameras[{key, url, from_ts, to_ts, kind: video｜frames, transcoded, expires_at}]` |
| `GET /datasets/{id}/episodes/{index}/series` | §4.3 |
| `GET /datasets/{id}/episodes/{index}/cameras/{camera}.mp4` / `.frames` | 转封装 / 转码 / 帧包，带 Range（把今天任务级那条未声明的路由一并声明）；转码未就绪时返回 202 + 进度 |
| `GET` / `PUT /datasets/{id}/mapping`、`POST /datasets/{id}/mapping/probe` | 映射的读写与探测 |
| `GET` / `POST` / `DELETE /viz-templates` | 站点模版库 |
| `POST /uploads?kind=viz_annotations`、`PUT /datasets/{id}/annotations` | 外部标注文件：上传件（JSON 或 zip，按已知格式校验）与挂到数据集上 / 换掉 |
| `GET /datasets?viz=1` | 左侧栏的数据集下拉：只列格式支持的，带「映射待确认」状态 |
| `EpisodeView` 补 `fps` 与 `dataset_id` | 迷你版把 `frames` 换算成秒、「在可视化页打开」 |

C2（`preflight.schema.json`，升小版本）：`dataset` 补 `features[{key, dtype, shape, names}]`、`cameras[]` 由短名改为对象（短名 + 分辨率 + 编码 + fps + `needs_transcode`，兼容旧字段）、`segment_sources[]`（识别到的分段来源，或 `unsupported` + 原因）；mcap 补 `topics[{topic, schema, encoding, rate_hz, count}]`。

C7（新）：`viz-mapping.schema.json` + `examples/viz-mapping/{umi,abc130k,invalid-*}.json`。

C5：`Dataset` 加 `viz_mapping`（JSON）、`viz_mapping_version`、`display_config`、`annotations_upload`（上传件编号，可空）；新表 `viz_templates`；迁移。

部署（09 §2.1 的环境变量表）：`CURATOR_VIZ_TRANSCODE`（缺省 `1`）、`CURATOR_VIZ_CACHE_GB`（缺省 20）、`CURATOR_VIZ_TRANSCODE_WORKERS`（缺省 2）；Chart 模板同步。

设计文档：03（端点）、05（预检描述符）、07（§2 侧栏与路由、§4.4 数据集页的两个「可视化」、§5 Episode 明细改为「可视化」弹窗、§6 裁决卡片同、§9 懒加载）、09 §2.1、00 §7（已加 D60–D64）。

## 8. 已定的决策与三轮答复

### 8.1 需求方 2026-10-03 对第一稿的答复（已落实）

| 第一稿的问题 | 答复 | 落在 |
|---|---|---|
| 完整版放页签还是新窗口 | 独立新窗口；侧栏「数据集」下加「可视化」子项；页面左侧可收起的侧栏先选数据集、再直接列 episode，点哪条看哪条；不把选 episode 放在页面最下方 | D63；§2、§5.0；静态稿 `visualize.html` |
| 迷你版保留哪些自定义 | 可以（保留更换 / 放大 / 侧栏，去掉布局模版与空格子） | D63；§2 |
| JPEG 帧包与 H.265 转码 | 暂时允许 Daemon 转码；留开关（容器环境变量，默认开）；转码的在页面上打「平台转码」标签 | D60；§4.2；静态稿里 right_wrist 演示 |
| 分段标注的标准格式 | 看 LeRobot 与 Pantheon 的，参考着来 | §3.4、§4.5、D64：统一标注模型 + 按格式适配 |
| ReRun 入口去留 | 先保留，链接文字改成「旧」 | D63；静态稿里写作「可视化（旧）」（见 8.2 第 2 条） |
| 架构 | 需求方给定：各格式读取器 → 统一展示模型（记录、时间轴、数据流、标注）→ 视频、曲线、三维场景、字段浏览、片段与标注 | D61；§4.0 |

### 8.2 需求方对第二稿八个问题的答复（2026-10-03，已落实）

| 问题 | 答复 | 落在 |
|---|---|---|
| 侧栏结构 | 「可视化」作为「数据集」下面的二级项 | §5.0；D63；静态稿侧栏「数据集 › 数据集列表 / 可视化」 |
| 旧入口文字 | 「可视化（旧）」可以 | 不变 |
| 转码资源缺省 | 可以；产物不上 TOS，放本地缓存 | §4.1、§4.2；D60 |
| 标注轨与外部标注 | 已知格式（§3.4 的几类）直接支持；不支持的警告「标注格式不支持」；我们自己的标注标准回头另立 | §4.5；D64 |
| 本地挂载的数据集 | 旧入口不管，新入口支持 | §4.1、§4.2 的 `access: local`；D60 |
| 第二期范围 | 可以；「第二期」指另立的后续阶段，不是本篇（已在 §1 写明） | §1、§9 |
| 公共数据集 | 匿名直连，可以 | §4.1 |
| 任务详情的 Episode 流水线 | 也挂迷你版 | §2；F13.6 |
| ReRun 参考版本 | 与 0.38.1 比对后无 mcap 功能修复；`~/ws/rerun` 切到 `ref-0.38.1`，以稳定版为准 | §3.1 |

### 8.3 需求方第三轮答复（2026-10-03，已落实，至此没有待确认项）

| 问题 | 答复 | 落在 |
|---|---|---|
| 「数据集」分组下第一个子项的名字 | 「数据集列表」 | §5.0、静态稿、D63 |
| 外部标注文件的放置 | 登记时上传，选填 | §2、§4.5、§7（`viz_annotations` 上传件、`Dataset.annotations_upload`）、F13.7、静态稿的「外部标注文件」上传框 |
| 自己的标注标准何时立项 | 不急，放到第二期一起 | §10 |

## 9. 工作包与风险

| # | 内容 | 依赖 |
|---|---|---|
| F13.1 | 本篇定稿（8.3 的答复并入）；C4 / C2 / C7 契约与示例、锁；设计 03 / 05 / 07 / 09 同步 | 需求方答复 8.3（不阻塞开工） |
| F13.2 | Daemon：统一展示模型与 LeRobot 读取器（v2 / v3）、可视化索引与曲线接口、`EpisodeView.fps`；本地挂载数据集的字节供给；缓存目录；转码开关与 ffmpeg 池、「平台转码」标记；分段来源识别与「标注格式不支持」警告 | F13.1 |
| F13.3 | 内核 + Daemon：mcap 读取器——探测（summary 补 schema / 编码）、映射存储与模版库、按映射解曲线、JPEG 帧包、fMP4 转封装、转码兜底 | F13.1 |
| F13.4 | 前端：播放器核心（格子、模版、走带、进度条、曲线、同步、信息侧栏、字幕、键盘、「平台转码」标签） | F13.1 |
| F13.5 | 前端：独立的「可视化」页面（侧栏子项、路由、左侧可收起的数据集 / episode 栏、数据集信息树）；数据集列表 / 详情头的「可视化」新窗口打开 + 「可视化（旧）」 | F13.2、F13.4 |
| F13.6 | 前端：报告 Episode 明细、裁决卡片与任务详情 Episode 流水线的迷你版弹窗、证据色段、「在可视化页打开」；`SyncedVideos` 退役 | F13.4 |
| F13.7 | 前端：添加数据集的 mcap 配置（探测表、模版、导入导出、另存为模版）、外部标注文件的选填上传（任何格式）与详情页入口 | F13.3 |
| F13.8 | 验收：样本集 88 个子集逐个打开（含 RH20T 10 路、FastUMI mpeg4 转码、ABC-130k H.265、深度列、无 names、Galaxea 拆列与分步、HABIT 多轨）；首帧时间与曲线接口时延指标；README 手动验证步骤 | 全部 |
| 第二期（§10，不在本阶段） | 见 §10 | F13.8 后另立设计篇与账本阶段 |

风险：

- 编码兼容：mpeg4 / H.265 / JPEG 三类都不能直接当 `<video>` 播，Daemon 侧要三种供给方式；转码的 CPU 与磁盘要有上限（8.2 第 3 条）。
- 浏览器直连 TOS 的 CORS 与预签名有效期（同设计 15 §6 的风险）；播放中续签。
- 大数据集：曲线组多（HABIT 60 多字段）、相机多（RH20T 10 路）时的首屏请求数，靠懒加载与「智能布局只上必要的」控制。
- 分段标注没有标准，探测规则会漏；靠「候选 + 用户确认」兜底。
- 与 ReRun 并存期间两个入口的解释成本（已用「可视化（旧）」区分）。

## 10. 第二期（另立阶段，先记在这里）

需求方 2026-10-03 定：下面这些不在本阶段（F13.x）做，等 F13.8 验收后另开设计篇与账本阶段。本阶段只保证统一展示模型与读取器接口给它们留好位（§4.0 的 `depth` / `pointcloud` / `transform` 流、`Annotations.tracks`、`FieldTree`）。

| 项 | 内容 | 本阶段预留 |
|---|---|---|
| Lance 读取器 | 对应 lerobot-lancedb 版本的读取器（今天的 `lance_reader` 只支持 ≥ 0.3 的三表布局、TOS 上整表拷贝），产出同一个展示模型 | 读取器接口；格式矩阵的 Lance 行 |
| 三维场景 | 末端轨迹（`observation.eef_pose`、mcap `PoseInFrame`）、点云、URDF 本体；新的视图类型「三维」 | `transform` / `pointcloud` 流进字段树，「+」菜单里置灰 |
| 深度图 | `uint16` 深度列与 mcap 深度流的渲染（伪彩、与 RGB 叠放） | `depth` 流进字段树 |
| 我们自己的标注标准 | 统一标注模型的序列化格式（片段 / 事件 / 条目标签 / 多轨），把质检产出的区间（TASK-1 动作起止、ACT-7 人工接管…）并进去，可导出、可回写数据集；外部标注的更多格式与在线编辑 | `Annotations` 模型；外部标注上传件 |
| 预生成 | 登记后后台生成可视化索引、帧包与转码产物（大数据集首次打开不等待） | 缓存目录与指纹规则 |
| v3 片段切分 | 按 GOP 切出单条 episode 的 mp4，替代 `#t=from,to` 直连整个分块文件 | `access: remux` |
| LeRobot 展示配置其余项 | 缺省布局、曲线分组覆盖、相机顺序之外的个性化 | `display_config` |
| 多 episode 连播与并排对比 | 自动下一条；两条 episode 同屏对比 | 播放器以 episode 时间为时钟，可扩展为两个时钟 |
| ReRun 入口下线 | 「可视化（旧）」下线，设计 15 的代签链路随之评估去留 | D63 |
| 浏览器内解码（备选） | WebCodecs 直读 mcap 的 H.264 / H.265 裸流，省掉 Daemon 转封装 | `access` 枚举可加 `client_decode` |
