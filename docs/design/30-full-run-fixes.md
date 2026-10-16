# 30 · 全量实跑暴露的五个问题

> 状态：**已实施并重跑验证（2026-10-15，F28.1–F28.6，镜像 `a1ff17f6e`）**。D98–D102 登记在 00 §7；账本阶段 28（F28.1–F28.6）；§7 是落地记录。
> 来源：2026-10-15 样本集 v2 的 99 个子集全部登记到 dataverse 的质检台，按「只报不拒、不开 EEF–视频一致性、VLM doubao-seed-2-1-lite 不开思考」
> 全量跑了一遍（84 个任务、562 条，设计 16 §7.9）：15 个任务共 41 条执行出错、待补跑，2 个任务在汇总时失败，crafted 的 DROID 标定子集 6 条全被拒。
> 需求方要求修，并定了两处口径：视频开头解不出的那段报 FILE-4、只报告（info）；数据集里没有任务描述就按没有标注处理，不加声明字段、不改数据集。

## 0. 开工指引

1. **五件事互不依赖**，各是一个 feature（F28.1–F28.5），F28.6 是修完后的重跑与打分。改的多是 B 类：`adapters/video_input.py`、
   `adapters/decode.py`、`pipeline/episode_checks.py`、`cli/aggregate.py`、`extensions/integrity/declared.py`。唯一的 A 类是 `ingest/mcap_reader.py`
   （§3 的 repeated 字段，加一个判断、不改算法），在 `tools/parity/a_class_declared.json` 登记新哈希与理由，对账的黄金基线回放守着判决不变。
2. **注册表 8.5 → 8.6**，加两个细码：视觉质量的 `head_undecodable`（FILE-4，info）、数据完整性的 `index_range_invalid`（FILE-9，blocking，不可复议）；
   回归样本分类表的平台注记重新生成。C2、C3、C4 的结构不变。
3. **不做**：给没有任务描述的数据集补描述（需求方：没有就按没有标注）；crafted DROID 标定子集的封装（要不要修等需求方确认，§4）；
   mcap 映射起草的改进（ABC-130k 的映射本身没错，§3）。

## 1. 给模型的视频按预算降档重编（D98）

**现象**：22 条在任务成败的「视频准备」报 `episode clip exceeds video byte limit`，GenRobot、MolmoAct2、HIW-500、RH20T、双 UR5e 都有。

**根因**：`prepare_videos` 给一次请求定一个总字节预算（缺省 32 MB），几路相机按顺序扣减；第一路编得大，后面的相机分到的就少，超了直接报错，
没有退路。片段按 crf 20、长边 720 编，长条目或画面细碎的相机很容易超。

**做法**：

- 预算按还没编的相机均分：第 k 路拿 `剩余 / (相机数 − k)`，前面省下的留给后面。
- 一路超了就降档重编，一档比一档省：长边 720 → 540 → 448 → 360，crf 20 → 26 → 30 → 34；请求要的抽帧率（`fps`）不变，模型读到的帧时刻不变。
- 降到最低档还超才报错（仍是执行出错）。
- 片段元数据多记 `encode`（实际用的长边与 crf），记录里能看出哪一路降过档；没超的相机参数与请求内容和原来一字不差。

## 2. seek 失败从头解；开头解不出是 FILE-4 的 info 发现（D99）

**现象**：16 条 GenRobot（mcap）在视觉质量、视频-动作同步、任务成败三处同时执行出错，原因都是 `PermissionError: [Errno 1] Operation not permitted`。
设计 16 §11 第 6 条早就记过：这批视频开头缺关键帧，应当报成解码问题。

**根因**：解帧（`decode._decode_once`）和编片段（`video_input._encode`）都先 `seek` 到窗口起点之前最近的关键帧（`any_frame=False`）。
视频的第一个包不是关键帧时，起点之前没有关键帧，FFmpeg 的 `av_seek_frame` 返回 -1，PyAV 把它当成 errno 1 抛出。解码包装器（`incidents.wrap_decode`）
记下这条就成了执行出错（D33），而 D93 只认解码器明确判定的损坏，接不住它。

**做法**：

- seek 失败（`PermissionError`，即 FFmpeg 返回 -1）时重新打开、不 seek，从头解码，窗口起点之前的帧照常丢掉。不再抛错，也就没有执行出错。
- 从头解码时记下第一个包与第一个能解出的帧的时刻：后者比前者（或窗口起点，取晚的）晚过一帧以上，这一段就是「开头解不出」。
  解帧把它交给调用方（可选的 `notes` 参数），视觉质量按相机报 `head_undecodable`：FILE-4、低严重度、info，范围写到相机，读数写起止秒数，
  例「robot0_sensor_camera0_compressed 开头 0.00–1.07 秒解不出（缺关键帧）」。后面的画面照常判。
- 编片段（任务成败）同样从第一个能解出的帧开始编，片段的 `start_s` 本来就记着它比窗口起点晚多少，提示里照常说明。
- 只报一次：同步与任务成败也会遇到同一段，但 FILE-4 的发现只由视觉质量出，免得一路相机报三条。

## 3. 汇总不因读不了元数据失败；protobuf repeated 字段取数（D100）

**现象**：两个任务在 `aggregate` 失败——`genrobot_mcap_structure_injected`（故意注入了 mcap 结构损坏）报
`RecordLengthLimitExceeded`，ABC-130k 报「topic 存在但按 fields='position' 没取出任何数值」。

**根因**：

- `aggregate` 为了给交付清单写每条的任务描述（`task_text`），用 v1 的读取器一次读出全部条目的元数据行；任何一条读不了，整个任务就失败。
  坏 mcap 由数据完整性报了 FILE-3（`structure_invalid` 等），各模块也都有记录，只差这一步。
- ABC-130k 的动作消息是自定义 protobuf `RobotState`，`position` 是 repeated double（每条 6 个值），映射没写错。mcap 读取器的 `_flatten_numeric`
  只认 list、tuple、protobuf 消息与 dict，protobuf 的 repeated 字段在 Python 里是 `RepeatedScalarContainer`，被当成认不出的东西返回空。

**做法**：

- `aggregate` 先整批读；整批失败时逐条读，读不了的条目没有任务描述（交付清单里来源记「无」），日志写一条警告与条数，判决不受影响。
- `_flatten_numeric` 把有长度、可迭代、又不是字符串 / 字节 / dict / protobuf 消息的东西当序列展平（protobuf 的 repeated 字段、numpy 数组）。

## 4. v3 的行区间落在数据文件 index 之外是 FILE-9（D101）

**现象**：crafted 的 `droid_extrinsics_mv5` 6 条全被完整性按 FILE-5「数据有 0 帧，episode 表记 184 帧」拒了。

**根因**：样本的封装不一致——从 DROID 1.0.1 挑出 6 条、保留原编号重新打包时，episode 表的 `dataset_from_index` / `dataset_to_index` 改成了子集内的
0–1240，数据表的 `index` 却保留了原数据集的全局编号（第 5 条是 1453–1636）。平台照 v1 的切法（`index` 减去文件首行的 `index`）取出 0 行。
这正是 FILE-9「索引与引用对不上」说的情形，按设计 29 却落进了 FILE-5。这个子集的期望只写了 MV-4（标定），封装问题不在设计里；
要不要修样本由需求方定，平台这边先把诊断改对。

**做法**：完整性逐条核 v3 时，本条的行区间与数据文件的 `index` 没有交集、文件里却有这条（`episode_index`）的行，报 `index_range_invalid`：
FILE-9、blocking、不可复议（读不了，口径同 D95），例「episode 表的行区间 0–184 不在数据文件的 index 里（这条在文件里是 1453–1636）」，不再报 FILE-5。
行区间有交集、只是长度不符的，照旧是 `length_mismatch`。

## 5. 相机视频比条目短时照样判（D102）

**现象**：3 条 GenRobot（clean_container）在任务成败报 `video ends before the requested episode window`。

**根因**：编片段时要求视频覆盖整个窗口（容差一帧的十分之一），一路相机比条目早结束就报错。

**做法**：有多少送多少——片段编到视频结束为止，片段元数据记 `video_end_s`（比窗口终点早时才有），给模型的说明里写「这一路录到第 x 秒就结束了，
条目到第 y 秒」。完全没有帧的照旧报错。相机提前断流本身是数据完整性的事（STRM-2 / STRM-3），这里不另出发现。

## 6. 验证

- 单测：降档与最低档仍超（F28.1）；首包非关键帧的合成视频——seek 失败不抛错、报出开头解不出的区间（F28.2）；aggregate 逐条退路、
  protobuf repeated 字段（F28.3）；行区间与 index 不交（F28.4）；视频提前结束（F28.5）。
- 契约与锁、回归样本工具、对账的黄金基线回放；CLI、结果读取、Daemon 相关套件。
- F28.6：需求方出镜像后，在 dataverse 重跑出过错的 17 个任务，用 `score.py` 对比修前修后，结果写回本篇 §7。

## 7. 落地记录

2026-10-15 实施 F28.1–F28.5，照 §1–§5，注册表 8.6：

- **D98**：`prepare_videos` 先照原样编一遍（一个总预算、配置的长边、crf 20），超了才按相机均分剩余预算、逐档重编；片段元数据只在降过档时多
  `encode`，原样编得下的请求一字不变。
- **D99**：解帧（`decode._decode_once`）与编片段（`video_input._open_at`）seek 抛 `PermissionError` 时重新打开、从头解；解帧经 `notes` 交出
  `undecodable_head`，视觉质量的读数多同名键，发现推导报 `head_undecodable`。合成样本复现了 GenRobot 的情形：首包在 0 秒、第一个关键帧在 0.7 秒，
  seek 到 0 或 0.2 秒都抛 `[Errno 1] Operation not permitted`；首包早于 seek 目标时 FFmpeg 自己从首包开始，不出错。
- **D100**：`aggregate._task_text` 整批读失败时逐条读；`mcap_reader._is_sequence` 认 `collections.abc.Sequence`（protobuf 的两种 repeated 容器都登记成它）
  与 numpy 数组。连带：mcap 探测与映射的应答不再把按路径挑出的 repeated 字段算 `checks_gap`（ABC-130k 一类只剩 H.265 那一条）。
- **D101**：`declared.v3_episode_problems` 切片为空、而文件里按 `episode_index` 有这条的行时报 `index_range_invalid`；有交集只是长度不符的照旧。
- **D102**：`_encode` 视频提前结束时记 `video_end_s`，`video_content` 给模型多一句「这一路录到第几秒就结束了」，没有这一键时文字一字不变。
- **测试**：新增单测——降档与最低档仍超、两路相机分预算；首包非关键帧的片段与解帧；视频提前结束；aggregate 逐条退路；protobuf repeated 字段；
  v3 行区间与 index 不交；两个新细码的读数夹具。本机：契约 216、后端各套件 2192 通过 40 跳过（缺本地数据）、编排 / 密钥 / 部署（非慢）400、
  对账（含 A 类守卫与黄金基线回放）与回归样本工具 86、前端五项全过。
- **F28.6 重跑**（镜像 `a1ff17f6e`）：重跑受影响的 22 个子集——出过错的 17 个、另 4 个 GenRobot mcap（开头解不出的 FILE-4）、crafted 的 DROID
  标定子集（FILE-9）；原任务软删、照同样的配置新建，其余 62 个沿用。84 个任务成功 82、完成有出错 2、失败 0（原来失败 2 个）；
  573 条通过 540、拒收 24、待补跑 9（原来 562 条：499 / 22 / 41）；执行出错的记录从 77 条降到 4 条。
- **打分**（只算平台有检查的项）：anchor 精确率 0.94 → 0.95、召回率 0.53 → 0.59；三个样本集合计 0.93 / 0.49 → 0.94 / 0.54，再去掉一条都没报出来的
  8 项是 0.93 / 0.55 → 0.94 / 0.60。FILE-4 从 2/24 升到 18/24（18 条 `head_undecodable`，GenRobot 开头 0.9–1.1 秒解不出）；DROID 子集 6 条都报
  `index_range_invalid`（期望里没写 FILE-9，不计分）；anchor-nc、crafted 不变。
- **新露出来的两件**（不在本篇五项里，剩下的 9 条待补跑都是它们）：① ABC-130k 的相机是 H.265，质检读取器只读 JPEG 与 H.264：动作修好、预检通过之后，
  完整性读取报错，其余模块整段读不了这个数据集；② mcap 结构注入子集里完整性拒了两个坏文件，后面几段开跑前读「数据集前几条」定语义时读到坏文件、
  整段失败，5 条干净条目在各模块都没有记录（同 D100 一类：一个坏文件连累整段）。
- **未做**：crafted DROID 子集的封装要不要修，等需求方确认。

## 8. 重跑后新露出来的两件，与并发（2026-10-15，需求方：两件都修，并发先调成 8）

### 8.1 坏 mcap 不连累后面几段（D103）

**现象**：重跑后 `genrobot_mcap_structure_injected` 里完整性拒了两个坏文件（ep 2 读不出、ep 4 结构损坏），其余 5 条干净条目在时间戳、视觉质量、同步、
任务成败、重复检测都没有记录，成了待补跑。

**根因**：mcap / Lance 的读取源（`pipeline.rows.ContainerRowSource`）开跑前按 v1 的做法，拿任务所选条目的前若干条（至多 100）一次读进来定数据语义；
其中一个文件读不了（截断的 mcap，读取器报 `RecordLengthLimitExceeded`），整次读取就抛，这一段的每一条都记不上。LeRobot 的读取源在设计 28 §1 已经
改成整批失败时逐条读，mcap / Lance 这边漏了。

**做法**：语义样本整批读失败时逐条读，读不了的留在样本外；一条都读不出来才照旧报「数据集读不了」。读不了的那条若也是这一段要判的，取它时照旧是它
自己的错（`get` 报 `EpisodeReadError`）。读得通的样本读法与原来一字不差。

### 8.2 质检读 H.265（D104，需求方：不加开关）

**现象**：ABC-130k 的动作读得出来以后，它的四路相机是 H.265，质检读取器只认 JPEG 与 H.264 Annex-B，完整性读取报错，其余模块读不了这个数据集。

**核实**（需求方问：可视化能放 H.265 是不是因为转码了，能不能复用转码开关）：
- 可视化放 H.265 靠的不是转码：`CURATOR_VIZ_CLIENT_DECODE`（缺省开）把 mcap 的 H.264 / H.265 原始帧交给浏览器的 WebCodecs 解，需要时 Daemon 只
  转封装（`viz/remux.py`，H.265 标 `hvc1`），不重编码。`CURATOR_VIZ_TRANSCODE`（缺省开）管的是浏览器根本放不了的（mpeg4 等），由 Daemon 重编码。
- 质检不需要转码：镜像里的 PyAV 15.1.0 自带 `hevc` 解码器；质检读取器（`ingest/mcap_reader.py`，A 类）只是重封装时写死按 H.264 读，碰到 H.265 就主动
  拒收（「别产出一个能打开但解不出帧的 mp4」）。按 H.265 读、封进 mp4（`hvc1`）后，帧段直接解码，给模型的片段本来就重编成 H.264。
- 建议不挂在 `CURATOR_VIZ_TRANSCODE` 上：质检这边只解码、不转码，代价是解码 CPU 比 H.264 高一些；挂在「替浏览器重编码」的开关上会让人误会。

**做法**（需求方 2026-10-15：同意，不加开关）：`mcap_reader._codec_of` 认出 `h265` / `hevc`；`_materialize_videos` 对 H.265 也走 `_mux_annexb`，按 `hevc` 读 Annex-B、只复制流（`opaque=True`）、标 `hvc1`，第一个关键帧之前的包照留（D99 靠它们认出开头解不出），H.264 的路径一字不改。
映射应答的 `checks_gap` 不再列 H.265（`mcap_mapping.check_gaps`），接口契约 7.6.1 的变更记录写明（连同设计 30 其余客户看得见的变化）。A 类改动登记在
`tools/parity/a_class_declared.json`。单测用可视化的 ABC 一类夹具：顶部 H.265 相机封成 `hevc` / `hvc1` 的 mp4，20 帧全部解得出。

### 8.3 并发调到 8

2026-10-15 需求方让把 dataverse 质检台同时运行的任务数调到 8：`kubectl -n dataverse set env statefulset/dataverse-curation -c curation
CURATOR_MAX_RUNNING_TASKS=8`，Pod 滚动重启后健康与就绪检查正常。依据是第一轮全量的时间线：容器上限 32 核（请求 16），3 个任务同时跑只用约 5 核；
84 个任务墙钟 65 分钟，单任务中位只跑 44 秒、排队中位 14 分钟，59 分钟一直满 3 个。**这一项不在 Helm 发布 `dataverse` 里**，日后 `helm upgrade`
会冲掉，要加进 rerun 仓库 dataverse chart 的质检台环境变量。调度算法（按资源放行、小任务优先、给模型准备视频的编码并行）另议。
同日需求方让把缺省值直接改成 8，Helm 就不用改：`orchestr/config.py` 的缺省、Daemon 与编排 README、P1 / D54 的修订；依赖缺省 3 的编排用例（调度单测、三个任务共用 CPU 池的端到端）改为显式设 3。两个崩溃恢复演练原来靠轮询赶上「模型段在跑且已写出结果」，八条很快的 episode 可能在两次轮询之间判完，机器一忙就偶发超时（与并发上限无关：演练里同时只有一个任务在跑）；改为一次判一条、假模型只答第一问，和暂停、停止的用例同法，不再看时机。
新镜像上线后 kubectl 加的那个变量与缺省值相同，留着无妨，也可以删掉。
