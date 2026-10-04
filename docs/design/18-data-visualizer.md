# 18 · 数据可视化：数据集的「可视化」页签、报告里的迷你播放器与 mcap 字段映射

> 状态：草案 v0.1（2026-10-03）—— UI 静态稿与方案，待需求方评审后定稿。分支 `feat/data-visualizer`（从 `feat/curator-v2` 分出）。
> 需求账本：阶段 13（F13.1–F13.8）。候选决策 D60–D63 见 §8，拍板后写进 `00-overview.md` §7。
> 静态稿：`frontend/mockups/dataset-visualize.html`（完整版）、`episode-visualize-mini.html`（报告里的迷你版）、`dataset-add-mcap.html`（添加数据集的 mcap 配置），手动验证步骤在 `frontend/mockups/README.md`。

## 0. 开工指引

读的顺序：§1 背景 → §2 范围与两种布局的功能对照 → §4 数据怎么供给（这是实现上最要紧的一节）→ §5 播放器规格 → §6 字段映射模版 → §7 契约与接口 → §8 待拍板的决策与问题 → §9 工作包。
静态稿先看一遍（三页，双击即可打开），对着 §5 核对。

要动的地方：

| 组件 | 位置 | 要做的 |
|---|---|---|
| 契约 | `docs/contracts/openapi.yaml`（C4）、`docs/contracts/cli/preflight.schema.json`（C2）、新增 `docs/contracts/viz-mapping.schema.json`（C7）与 `examples/` | §7 的端点、预检描述符扩展、映射模版 Schema；升版本、刷新锁、`npm run gen:api` |
| Daemon | `backend/daemon/routes/`（新 `viz.py`）、`results/`（新 `viz_index.py`、`series.py`、`frames.py`）、`repo/`（数据集的 `viz_mapping`、模版表、迁移）、`orchestr/browse.py` | 可视化索引、曲线接口、相机供给（直连 / 转封装 / 帧包）、mcap 探测与映射存储 |
| 内核 | `backend/curation/ingest/mcap_reader.py`、`cli/containers.py`、`streams/clip.py`、`cli/lerobot_meta.py` | summary 里补 schema 与编码；按映射模版解消息；JPEG 帧包与 fMP4 转封装；预检写出 features / 相机编码 / 分段来源 |
| 前端 | `frontend/src/features/visualizer/`（新）、`pages/datasets/DatasetDetailPage.tsx`、`pages/report/EpisodesTab.tsx`、`pages/adjudication/EpisodeCard.tsx`、`features/datasets/AddDatasetDrawer.tsx`、`locales/zh.ts`、`mocks/` | 播放器、数据集页签、报告与裁决的弹窗、mcap 配置表单 |
| 文档 | 本篇、`07-frontend.md` §4.4 / §5 / §6、`03-rest-api.md`、`05-registry-and-preflight.md`、`00-overview.md` §7、各 README | 实现时同步 |

## 1. 背景与目标

今天控制台的「可视化」是跳到同一部署里的 ReRun 网页查看器（D48），地址经 Daemon 代签（D55，设计 15）。用下来的问题：

- **慢**：ReRun 网页端对 `.mcap` / `.mp4` 是整文件下载完再导入，mcap 解析在 wasm 里单线程跑；LeRobot 导入器只有原生端，网页端能开是因为 F10 做了特殊处理。打开一条 episode 要等很久。
- **TOS 支持弱**：代签链路能用，但 ReRun 的加载模型（整文件）和 TOS 的按区间读天然不合。
- **mcap 字段没法按客户定义映射**：ReRun 对 mcap 只有「选哪些 decoder、过滤哪些 topic」，没有声明式的「这个 topic 是相机、那个字段是关节位置」的配置；自定义 protobuf schema（如 ABC-130k 的 `RobotState`）只能看成一堆原始字段。
- **界面与火山不统一**。

目标：在控制台里内置一个数据播放器 ——

1. 多路相机与运动曲线在**同一条进度条**下同步播放；
2. **两种布局**：数据集详情页里的完整版（信息全、可自定义），质检报告 / 人工裁决里的迷你版（固定布局、只看核心信息）；
3. mcap 数据集在**添加时确认一份字段映射模版**（内置模版 + 自带模版），可视化与质检都按它读；
4. 做完后报告 Episode 明细里的「各机位视频」退役，改为一个「可视化」入口弹出迷你版。

本期不做：3D（末端轨迹、点云、URDF）、深度图渲染、浏览器内解 mcap / 解 parquet、ReRun 入口的最终下线（见 D63）。

## 2. 范围：三个交付面与两种布局

| 交付面 | 在哪 | 内容 |
|---|---|---|
| 完整版 | 数据集详情 → 新页签「可视化」（`/datasets/:id#viz`） | 播放器 + 「数据集信息」树状浏览 + Episode 列表 |
| 迷你版 | 质检报告 Episode 明细抽屉、人工裁决卡片 → 「可视化」弹窗 | 只有播放器；布局由发现决定；进度条标出发现的区间 |
| mcap 配置 | 添加数据集抽屉（格式识别为 mcap 时出现）；数据集详情的「mcap 配置」入口 | 探测 → 模版起草 → 表格确认 → 保存 / 另存为模版 |

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
| 播放器下方：数据集信息树、Episode 列表、上一条 / 下一条 | ✓ | — |
| 进度条上发现的区间色段（blocking 红 / review 橙 / info 灰）与证据帧点标；「发现」芯片切换聚焦 | — | ✓ |

格式矩阵（本期）：

| 格式 | 相机 | 曲线 | 任务描述 | 分段标注 |
|---|---|---|---|---|
| LeRobot v2.0 / v2.1 | `dtype=video` 的 feature（image 帧序列本期不做） | 数值 feature（`float32/64`，按 `names` 分组） | `tasks.jsonl` / 首帧 `task_index` | 预检探测到的分段字段（§4.5） |
| LeRobot v3.0 | 同上，按 episodes 表的 from / to 切 | 同上，按 `dataset_from/to_index` 行窗 | `tasks.parquet` / episodes 的 `tasks` | 同上 |
| mcap | 映射里的 `cameras` | 映射里的 `series` | 映射里的 `task` | 映射里的 `segments` |
| Lance | 本期不做（表格式 blob 视频要整表读；需求方未要求） | | | |

## 3. 调研结论

### 3.1 参考的三个工具

- **HF `lerobot/visualize_dataset`**：上排相机、语言指令卡、走带（上一条 / 播放 / 下一条 / 循环、滑杆、`24 / 24` 帧计数、空格与方向键）、下排按关节分组的曲线：同名的 `action`（虚线）与 `observation.state`（实线）叠画，图例里带当前值，勾选可隐藏某条。本篇的曲线格就是这个样子。
- **Pantheon data-board**：双机位 + 深色时间线上的「字幕」（`both grippers · settle above table · idle`）+ 关键事件列表（时刻 + 事件 + 标签）+ 右侧评估侧栏。本篇的字幕栏与进度条上的分段色段来自这里。
- **ReRun**（`~/ws/rerun`，注意：这个检出是 `main @ 107beb60f2`，`Cargo.toml` 写 `0.39.0-alpha.1+dev`，比 0.38.1 发布点多 138 个提交；LeRobot 部分改动很大，mcap 基本没变）：
  - LeRobot：只认目录结构判版本；feature 键原样作实体路径（点不是分隔符）；`float32/64` → `Scalars` + 一次性的 `SeriesLines.names`；`video` 在 v2 是整文件 `AssetVideo` + 逐帧引用，在 v3 是 `VideoStream` 按 `[from, to)` 切 GOP（H.264 / H.265 有 B 帧或不在 GOP 边界要 ffmpeg）；时间轴只有 `frame_index`（有就只用它）或 `timestamp`；`string` / `task_index` / `language` → 文本；`int16`、其他 `int64`、`bool` 跳过；不读 `stats.json`、`robot_type`。默认布局不是导入器给的，是查看器的启发式：子树里不止一张彩图就每个相机一个 2D 视图，每个 `Scalars` 实体一个时序视图，超过 12 个视图改页签。
  - mcap：decoders（`ros2msg` 手写的 JointState / Imu / … → `ros2_reflection` → `protobuf` → `raw` 兜底）+ lenses（foxglove 13 个：CompressedImage、CompressedVideo（h264 / h265 / av1 / vp8 / vp9 → `VideoStream`）、RawImage、PoseInFrame(s)、FrameTransform(s)、PointCloud…；ROS 的 Image / CompressedImage（jpeg / png → `EncodedImage`，h264 → `VideoStream`）/ JointState（→ `<topic>/position|velocity|effort`，名字作标签）…）；实体路径 = topic；每条消息两个时间轴 `message_log_time` / `message_publish_time`，另加消息里的时间戳。**没有声明式的字段映射**，用户只能选 decoder、过滤 topic、设时间窗；网页端整文件下载后单线程解析。
  - 结论：我们要的「字段映射模版」就是 lenses 的声明式版本 —— 默认模版按 schema 自动归类（等价于 foxglove / ros2 lenses），再允许按 topic 覆盖；这也正是 ReRun 做不到而客户需要的。

### 3.2 样本集的真实元数据（桶 `curation-robo-anchor`，anchor 63 子集 + anchor-nc 25 子集）

| 维度 | 看到的情况 | 对设计的影响 |
|---|---|---|
| 相机路数 | 1（PushT、FastUMI 单臂）到 10（RH20T `cam_<序列号>` × 10）；SO-101 五路、HABIT 五路、双 UR5e 四路 RGB-D | 格子要能放 1–3 列 × 1–3 行；超过 9 路的默认只上前几路，其余在「+」里选 |
| 分辨率 / 编码 | 96×96 到 1280×720；绝大多数 `av1 yuv420p`；**FastUMI 是 `mpeg4`（MPEG-4 Part 2，浏览器放不了）** | 需要「播不了就由 Daemon 转码」的兜底（§4.2） |
| 深度 | `observation.images.front.depth`、`observation.depths.*` 为 `uint16` 列（不是视频） | 本期不渲染，树里可见、「+」里置灰 |
| state / action | 维度 2–29；`names` 有列表（`shoulder_pan.pos …`）、字典（`{"motors": [...]}`）、缺失（RH20T 全部 `null`）三种；RH20T 另有 `observation.state.ee_pose / joint / gripper`、`force / torque / robot_ft`；HABIT 有 60 多个 `robot0.* / robot1.*` 字段 | 曲线分组规则要能处理没有名字（用 `dim_i`）和字段很多（默认只上 state / action，其余在「+」里）的情况 |
| 任务与分段 | 任务文本在 `tasks.jsonl / parquet`；HIW 有 `language_persistent / language_events`；RSS 的 `subtask` 列全是占位；Galaxea 自带分步（起止 + 合格与否）；HABIT 有分步与成败 | 分段标注要「预检探测 + 用户确认」，不能硬编码一种字段名（§4.5） |
| mcap（GenRobot UMI） | foxglove protobuf：`/robotN/sensor/camera0/compressed`（`CompressedImage` JPEG 1280×720 30 Hz）、`/robotN/vio/eef_pose`（`PoseInFrame` 30 Hz）、`/robotN/sensor/magnetic_encoder`（50 Hz）、IMU 200 Hz、`camera_info`、`robot_info`、`system_info`；没有任务字段 | 内置 UMI 模版（已有识别逻辑）；JPEG 相机要用帧包（§4.2） |
| mcap（ABC-130k） | `foxglove.CompressedVideo`：顶部双目 **H.265**、腕部 H.264，30 Hz；自定义 schema `RobotState / GripperState`（200–270 Hz，`/left-arm-state`、`/left-arm-action`…）；`/instruction` topic 与 `episode-metadata`（`task_name`、相机型号与分辨率） | 自定义 protobuf 要按描述符展开数值叶子让用户确认；state / action 成对的 topic 叠画；H.265 要看浏览器能力 |

### 3.3 平台现状（代码）

- 前端：Arco 原生主题，页面壳 `AppLayout`，数据集详情页**没有页签**，只有一叠卡片；「可视化」按钮只在数据集列表与详情页头（F10.3），报告里没有。报告 Episode 明细的「各机位视频」是 `features/media/SyncedVideos` + `SignedMedia`（`GET /media/sign`，任务级）+ `syncPlayback.SyncController`（按 episode 时间对齐、2 s 预读、跟随任一路）。发现的区间 `time_s` 可点（所有机位一起跳），`frames` 只显示、不换算（EpisodeView 没有 fps）。
- Daemon：`GET /datasets/episodes` 只给 LeRobot 相机的预签名地址（mcap 为空）；mcap 相机只有任务级的 `GET /tasks/{id}/episodes/{i}/cameras/{cam}.mp4`（内存里封装：JPEG → **MJPEG mp4，浏览器 `<video>` 放不了**；H.264 Annex-B 转封装；H.265、PNG、RawImage 拒绝；这个路由没写进 C4）。`POST /datasets/{id}/sign` 能签任意键的 GET（Range 不参与签名，一个地址可复用）。**没有任何接口返回逐帧的 state / action 序列**。
- 内核：mcap 的映射只在站点配置 `ingest.mcap_mapping`（全局、无校验、不按数据集），默认 `/action`、`/observation.state`、`/task`、`/observation.images.` 前缀，另有内置 UMI 识别；`McapSummary` 不保留 schema 名与消息编码；只用 `log_time`；预检描述符里没有 features / names / 分辨率 / 编码 / topic 清单。F5.16 的 EEF `record_mapping` 是模块参数（上传件），不在数据集上。

## 4. 架构与数据供给

### 4.1 原则

- 沿用 D16：**浏览器能直接播的媒体直连 TOS**（预签名，D55 的签名接口与现有续签逻辑）；Daemon 不中转能直连的字节。
- Daemon 只做两类事：**浏览器播不了的相机的转封装 / 帧包**，以及**小体积的派生数据**（曲线、分段、索引）。全部按 `episode × 相机` 或 `episode × 曲线组` 粒度，带 Range，先内存 LRU、再磁盘缓存（`<CURATOR_DATA_DIR>/viz/<dataset_id>/<meta 指纹>/ep<N>/…`，指纹变了即作废）。
- 一切都是**按需、首次打开时生成**；可选的「预生成」放第二期（登记后后台跑一遍，给大数据集）。

### 4.2 相机供给矩阵

| 来源 | 编码 | 浏览器能否直接播 | 供给方式 |
|---|---|---|---|
| LeRobot v2 | av1 / h264 | 能 | 预签名直连整个 mp4 |
| LeRobot v3 | av1 / h264 | 能 | 预签名直连 + `#t=from,to`（现有 `withFragment`），播放器按 `from_ts` 对齐；大文件可选由 Daemon 按 GOP 切出该 episode（第二期） |
| LeRobot | mpeg4 part 2、其他浏览器不支持的编码 | 不能 | Daemon 转码为 H.264 fMP4（CPU，首次慢，磁盘缓存；预检时就标出「需要转码」） |
| mcap `CompressedImage`（JPEG） | — | 不能当 `<video>` | **JPEG 帧包**：Daemon 输出 `frames.bin`（头部 = 每帧偏移 / 长度 / 时间戳的索引，正文 = 原 JPEG 字节），播放器用 `createImageBitmap` + canvas 逐帧绘；随机访问靠 Range，不转码。单路 1280×720 30 Hz 约 2 MB/s，内网没问题 |
| mcap `CompressedVideo` h264 | h264 | 能（要 fMP4） | Daemon 转封装为 fMP4（无重编码；现有 `_mux_annexb` 做成流式、带 Range） |
| mcap `CompressedVideo` h265 | hevc | 看平台（Safari 能；Chrome 要硬解） | 转封装为 `hvc1` fMP4；播放器探测 `canPlayType`，不能播时请求转码版本（可配置是否允许转码） |
| mcap `RawImage` / PNG | — | 不能 | 本期不支持；预检与映射表里标出 |

### 4.3 曲线供给

`GET /datasets/{id}/episodes/{index}/series?group=<key>&from=<s>&to=<s>&points=<n>` → 列式 JSON（`t[]`、`series[{name, role, values[]}]`、`unit`、`total_points`）。

- 缺省 `points=2000`：按 min / max 抽稀保峰值；放大某段时按区间再取，到全精度为止。曲线组是预先定义的（§5.3），一次请求一组。
- LeRobot：读 parquet 列（v2 整文件、v3 行窗 `dataset_from/to_index`），复用现有 `dsfs.read_parquet`，但要**列投影**（今天是整表读）。
- mcap：按映射解消息（现有 `_extract_source` 的字段展开），重采样到参考时间轴（§4.4），成对的 state / action topic 对齐后同组输出。
- 体量：一条 20 s × 30 fps × 14 维 × 两套 = 约 70 KB JSON；没必要上 Arrow，先 JSON。

### 4.4 时间轴与帧号（候选 D62）

- 播放器只有一个时钟：**episode 时间 t（秒）**，从 episode 的第一帧算起。视频、曲线、字幕、进度条都用它。
- 帧号：LeRobot `frame = round(t × fps)`；mcap `frame` = 帧号基准 topic（缺省第一路相机）的消息序号。跳帧输入框按这个定义。
- 相机帧率与数据 fps 不同（或 mcap 各 topic 各有节奏）时，每路按自己的时间戳取 ≤ t 的最近一帧；曲线按自己的采样时刻画，不插值。
- v3 的 `from_ts`：直连视频的 `currentTime = t + from_ts`（现有 `SyncController` 的做法）。

### 4.5 任务文本与分段标注

- 任务文本：LeRobot 用现有 `tasktext` 的优先级（人工改标 > 原始标注 > 自动补标；数据集页里只有原始标注）；mcap 按映射的 `task`。没有就显示「无任务描述」。
- 分段标注（字幕栏与进度条上的色段）：
  - LeRobot：预检探测候选 —— episodes 表里的分步字段（Galaxea 的 `subtasks`、HABIT 的 steps）、`language_events` 这类事件列、按帧的布尔段列（`is_intervention_segment`…）、逐帧 `task_index` 变化；候选写进预检描述符，用户在数据集详情（或映射表）里勾选哪个当「分段」（没有就不显示）。
  - mcap：映射里的 `segments`（topic 的 start / end / label 字段，或附件 JSON）。
  - 质检任务产出的区间（TASK-1 动作起止、ACT-7 人工接管…）本期不进字幕栏，只作为「发现」色段出现在迷你版里；以后可以合并。

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

- 复用 `SignedMedia` 的签名 / 续签（30 s 内过期即续签）与 `syncPlayback` 的对齐思路；新播放器自己驱动 `<video>`（直连）和 canvas（帧包），不再用原生控件（不允许全屏）。
- 「各机位视频」（`SyncedVideos`）在报告与裁决页退役；人工裁决卡片里的三路视频也换成迷你版弹窗（07 §6 说裁决与报告用同一个播放器）。
- ReRun 入口：见 D63。

## 5. 播放器规格

### 5.1 顶栏

`数据集名 · ep N` ｜ 格式 ｜ `fps` ｜ `帧数 · 时长` ｜ `任务：…`（省略号，悬停全文；没有则灰字「无任务描述」）｜ 右侧：完整版有「布局模版」（智能 / 仅视频 / 仅曲线 / 自定义）与格子数（1×1、2×1、2×2、3×2、3×3），两种布局都有「信息」按钮（侧栏开关）。

### 5.2 格子

- CSS grid，列数 1–3，行数 1–3；格子高度随可用宽度变（约 0.66 倍格子宽，160–420 px）；侧栏打开时格子变窄，智能布局不够放三列就降到两列。
- 视频格：4:3 画面在格子里等比居中（黑边），左上角相机名芯片（带相机色点），左下角 `mm:ss.s · 帧 N`。
- 曲线格：标题 `曲线组名 · 单位`；图区（网格、x 轴秒、y 轴自适应）；每个维度一种颜色，**实线状态、虚线动作**；竖线光标 + 时间标签；点图跳转；下方图例：色样、名字、状态值 / 动作值，点图例隐藏 / 显示该维度。调色板：Arco 的蓝 / 青 / 橙 / 紫 / 绿 / 品红 / 金 / 青柠 8 色循环。
- 空格子（仅完整版）：虚线框 + 「+ 选择要看的内容」。
- 格子工具（悬停或聚焦时出现在右上角）：更换（弹出菜单：相机 / 运动曲线 / 其他——深度图、末端轨迹等置灰并写明原因）、放大 / 还原（占满播放器区域，其他格子隐藏）、清空（仅完整版）。
- 聚焦：点格子加蓝框；侧栏跟着它。

### 5.3 曲线分组（缺省规则）

1. 只对数值 feature 建组；`observation.state` 与 `action` 同名维度叠成一组。
2. 维度 ≤ 8：一组；更多的按 `names` 的公共前缀切（`left_* / right_*`、`arm_left_* / arm_right_*`、`kLeft* / kRight*`），切不开的每 7 维一组。
3. 夹爪维度（名字含 `gripper`）单独一组。
4. 没有名字的用 `dim_0 … dim_n`。
5. 其他数值 feature（`observation.eef_pose`、`force`、`torque`、HABIT 的几十个 `robot0.*`）都建组，但不进「智能」布局，只在「+」/「更换」里出现。
6. mcap 的组就是映射里的 `series` 条目（`pair_with` 把 state / action 两个 topic 叠成一组）。

### 5.4 走带与进度条

- 按钮：上一帧、播放 / 暂停、下一帧；速度 1x / 1.5x / 2x；时间 `mm:ss.s / mm:ss.s`；帧输入框（回车跳转）与总帧数；循环开关；完整版另有「上一条 / episode 下拉 / 下一条」。
- 进度条：主轨 + 把手，点击 / 拖动跳转，悬停显示时间、帧号、所在步骤；上方细条是分段标注的色段（相邻两段颜色交替，`unqualified` 为橙），点色段跳到段首；下方细条是发现的区间色段（迷你版）。
- 键盘：空格 播放 / 暂停，← → 逐帧，Shift + ← → 跳 1 秒（焦点在播放器内才生效，不抢输入框）。
- 不允许全屏：没有全屏按钮，`<video>` 不带原生控件，不响应双击。

### 5.5 字幕栏

有分段标注才显示：`步骤 i/n · 名称 · s–e s`，不合格的带橙色标签；右侧注明来源（如「meta/episodes.jsonl 的 subtasks」）。两段之间的空档显示「（无标注）」。

### 5.6 侧栏

- 视频格：键名、分辨率、编码 · 像素格式、帧率、帧数、时长；来源文件、时间范围（v3 的 from–to）、读取方式（直连 / 转封装 / 帧包）；当前帧与时间；「在新标签打开源文件」。
- 曲线格：状态 / 动作字段与维度、单位、采样；序列列表（勾选显示、色样、名字、当前值 状态 / 动作）；一句说明。
- 空格子 / 未聚焦：提示文字。

### 5.7 完整版的下半页

- 「数据集信息」：左树右详情。树：相机（每路：分辨率 · 编码）、状态与动作（数值 feature 与 shape）、任务与标注（任务文本、分段标注、episodes 表）、元数据（`info.json`、`stats.json`、`README.md`）、文件（`data/`、`videos/`，来自登记时的指纹清单，不再逐个访问 TOS）；mcap 换成 topic / schema / metadata / attachments。详情：属性表、JSON 预览、「加入播放器」。
- 「Episode 列表」：编号、帧数、时长、任务描述、分段（小色块）、操作「播放」；当前条高亮；筛选、分页。

### 5.8 错误与空态

- 相机播不了（编码不支持且不允许转码）：格子里显示原因与建议（转码 / 换相机），其他格照常。
- 签名过期：自动续签，期间格子上蒙一层「重新获取地址…」。
- mcap 映射没有相机 / 曲线：格子里提示去「mcap 配置」。
- 曲线组太大（> 50 万点）：先给下采样版本，提示放大查看细节。
- 加载中：格子骨架 + 顶栏「正在生成可视化索引」（首次打开 mcap 要几秒）。

## 6. 字段映射模版（mcap 配置）

### 6.1 定位

数据集级配置，可视化与质检共用；**内置模版自动起草，用户在表格里确认**，可另存为模版给同一套采集系统的其他数据集复用，也可以直接导入自带的 JSON。契约 C7：`docs/contracts/viz-mapping.schema.json`（`schema_version: viz-mapping/1.0`）。

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

LeRobot 不需要字段映射（`info.json` 已经够），但完整版允许保存一份轻量的「展示配置」（缺省布局、曲线分组覆盖、分段字段选择、相机顺序），同样挂在数据集上；本期只做分段字段选择与相机顺序，其余第二期。

## 7. 契约与接口改动

C4（`openapi.yaml`，升小版本）：

| 端点 | 用途 |
|---|---|
| `GET /datasets/{id}/viz` | 数据集级索引：`cameras[{key, name, width, height, codec, fps, access: direct｜remux｜frames｜transcode｜unsupported}]`、`groups[{key, title, unit, series[{name, role}]}]`、`fps`、`episode_indices`、`segments_source`、`mapping_version` |
| `GET /datasets/{id}/episodes/{index}/viz` | episode 级：`duration_s`、`frames`、`task`、`steps[{start_s, end_s, label, quality}]`、`cameras[{key, url, from_ts, to_ts, kind: video｜frames, expires_at}]` |
| `GET /datasets/{id}/episodes/{index}/series` | §4.3 |
| `GET /datasets/{id}/episodes/{index}/cameras/{camera}.mp4` / `.frames` | 转封装 / 转码 / 帧包，带 Range（把今天任务级那条未声明的路由一并声明） |
| `GET` / `PUT /datasets/{id}/mapping`、`POST /datasets/{id}/mapping/probe` | 映射的读写与探测 |
| `GET` / `POST` / `DELETE /viz-templates` | 站点模版库 |
| `EpisodeView` 补 `fps` 与 `dataset_id` | 迷你版把 `frames` 换算成秒、跳到完整版 |

C2（`preflight.schema.json`，升小版本）：`dataset` 补 `features[{key, dtype, shape, names}]`、`cameras[]` 由短名改为对象（短名 + 分辨率 + 编码 + fps，兼容旧字段）、`segment_candidates[]`；mcap 补 `topics[{topic, schema, encoding, rate_hz, count}]`。

C7（新）：`viz-mapping.schema.json` + `examples/viz-mapping/{umi,abc130k,invalid-*}.json`。

C5：`Dataset` 加 `viz_mapping`（JSON）、`viz_mapping_version`、`display_config`；新表 `viz_templates`；迁移。

设计文档：03（端点）、05（预检描述符）、07（§4.4 数据集页签、§5 Episode 明细改为「可视化」弹窗、§6 裁决卡片同、§9 懒加载）、00 §7（D60–D63）。

## 8. 待拍板的决策与问题

候选决策：

- **D60 可视化的数据供给**：能直连的媒体直连（沿用 D16 / D55）；浏览器播不了的相机由 Daemon 按 episode × 相机 现场转封装（无重编码）或出 JPEG 帧包，转码只作兜底且可配置关闭；曲线由 Daemon 读表 / 解消息后下采样给出。全部按需生成、按指纹缓存。
- **D61 字段映射归数据集**：mcap 数据集的映射模版存在数据集实体上、有版本；任务开始时冻结到 `run.json`；站点配置 `ingest.mcap_mapping` 降级为缺省；模版库是站点级的。
- **D62 时间轴与帧号**：播放器以 episode 时间为唯一时钟；帧号 = `round(t × fps)`（LeRobot）/ 帧号基准 topic 的消息序号（mcap）。
- **D63 「各机位视频」退役与 ReRun 入口**：报告 Episode 明细与人工裁决卡片统一用迷你版弹窗；数据集列表 / 详情头的「可视化」改为进完整版页签；ReRun 入口挪到「更多」里保留一个版本，验收后下线。

要需求方定的问题：

1. 完整版放在数据集详情的**页签**（本稿）还是独立路由新窗口？需求 5 原文是「新窗口直接打开」，后来改成「弹窗打开迷你版」—— 本稿按：报告里弹窗（迷你版），数据集页里页签（完整版），页签上另给「在新窗口打开」。
2. 迷你版保留哪些自定义：本稿保留「更换」和「放大」与侧栏，去掉布局模版与空格子。
3. §4.6 的智能布局规则表与证据色段的颜色（沿用级别色）是否认可。
4. mcap 的 JPEG 相机用「帧包 + canvas」而不转码，首次打开要等几秒生成索引，可以接受？H.265 在 Chrome 没硬解时允不允许 Daemon 转码（CPU 成本）？
5. 曲线缺省分组规则（§5.3）与「实线状态 / 虚线动作」的画法。
6. 深度图、3D 末端轨迹、任务产出的同步曲线放第二期。
7. 「数据集信息」树的范围：本稿有 meta JSON 预览与文件清单；要不要 parquet 的逐行浏览？
8. 分段标注：按 §4.5「预检探测 + 用户确认」，还是定义一种我们自己的分段标注交付格式（与 TASK-1 对齐）？
9. 多 episode 连播 / 自动下一条要不要。
10. ReRun 入口按 D63 保留一个版本，还是这次就下线。
11. 迷你版除了报告与裁决，要不要也挂在任务详情的 Episode 流水线里。

## 9. 工作包与风险

| # | 内容 | 依赖 |
|---|---|---|
| F13.1 | 本篇定稿；C4 / C2 / C7 契约与示例、锁；设计 03 / 05 / 07 / 00 同步 | 需求方评审静态稿 |
| F13.2 | Daemon：LeRobot 的可视化索引与曲线接口（v2 / v3），`EpisodeView.fps`；缓存目录 | F13.1 |
| F13.3 | 内核 + Daemon：mcap 探测（summary 补 schema / 编码）、映射存储与模版库、按映射解曲线、JPEG 帧包、fMP4 转封装、转码兜底 | F13.1 |
| F13.4 | 前端：播放器核心（格子、模版、走带、进度条、曲线、同步、侧栏、字幕、键盘） | F13.1 |
| F13.5 | 前端：数据集「可视化」页签（完整版 + 数据集信息树 + Episode 列表）；列表 / 详情头的「可视化」改指向 | F13.2、F13.4 |
| F13.6 | 前端：报告 Episode 明细与裁决卡片的迷你版弹窗、证据色段；`SyncedVideos` 退役；ReRun 入口按 D63 | F13.4 |
| F13.7 | 前端：添加数据集的 mcap 配置（探测表、模版、导入导出、另存为模版）与详情页入口 | F13.3 |
| F13.8 | 验收：样本集 88 个子集逐个打开（含 RH20T 10 路、FastUMI mpeg4、ABC-130k H.265、深度列、无 names）；首帧时间与曲线接口时延指标；README 手动验证步骤 | 全部 |

风险：

- 编码兼容：mpeg4 / H.265 / JPEG 三类都不能直接当 `<video>` 播，Daemon 侧要三种供给方式；转码的 CPU 与 CPU 名额池（阶段 9）要协调。
- 浏览器直连 TOS 的 CORS 与预签名有效期（同设计 15 §6 的风险）；播放中续签。
- 大数据集：曲线组多（HABIT 60 多字段）、相机多（RH20T 10 路）时的首屏请求数，靠懒加载与「智能布局只上必要的」控制。
- 分段标注没有标准，探测规则会漏；靠「用户确认」兜底。
- 与 ReRun 并存期间两个入口的解释成本。
