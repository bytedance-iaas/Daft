# 21 · 可视化第二期其余几项：深度图、v3 切片与转码对齐、展示配置、数据集信息原文与小修

> 状态：**实施稿 v1.0（2026-10-06）**——需求方 2026-10-06 定范围：深度图（含 mcap）、v3 切片 + 转码对齐、LeRobot 展示配置其余项（「保存为缺省布局」要配「恢复默认」）、
> 调研里看到的小问题与 bug 全修、「数据集信息」改用原文 KV；「moov 在尾」按「要」处理（§4.4，挂在切片播放开关下，缺省关）。三维场景、预生成仍在设计 18 §10。
> 本篇不改 D60–D67，只在上面加东西；§8 的四条取舍（D68–D71）是需求方定的范围加上答复里列出、需求方没有异议的缺省做法。
> 需求账本：阶段 15（F15.0–F15.8）。直接在 `feat/curator-v2` 上开发（阶段 13 / 14 合并后的约定）。

## 0. 开工指引

读的顺序：§1 范围 → §2 小问题与 bug（先做，F15.1）→ §3 数据集信息原文 → §4 v3 切片与转码对齐 → §5 深度图 → §6 展示配置 → §7 契约 → §8 取舍 → §9 工作包与验收。
落地顺序 F15.1 → F15.2 → F15.3 → F15.4 → F15.5 → F15.6 → F15.8；契约一次升到 C4 2.7.0（只加不改），各项落地时把自己的部分加进去，C7 升 `viz-mapping/1.1`（只加 `depths`）。

要动的地方：

| 组件 | 位置 | 要做的 |
|---|---|---|
| 契约 | `docs/contracts/openapi.yaml`（C4）、`docs/contracts/viz-mapping.schema.json`（C7）与 `examples/` | §7：数据流（深度）路由、`VizEpisode.streams`、`VizFrameIndex` 的 `png16` 与深度范围、展示配置三个接口与 Schema、`VizCamera.hidden`、`VizDataset.display`、字段树原文的说明、`from_ts` / `transcode_url` 的时间口径；C7 的 `depths`；锁与 `npm run gen:api` |
| Daemon | `daemon/viz/`：`service.py`、`media.py`、`lerobot.py`、`lance.py`、`mcap.py`、`source.py`，新 `depth.py`、`segments.py`、`display.py`；`routes/viz.py`、`routes/results.py`、`routes/datasets.py`（删登记清缓存）、`settings.py` | 深度帧包与 202 进度、切片与按切片转码、moov 在尾、展示配置的读写与生效、字段树原文、§2 的修补 |
| 内核 | `curation/viz/`：`lerobot_info.py`、`groups.py`、`series.py`、`mcap_probe.py`、`mcap_messages.py`、`mcap_mapping.py`、`mcap_episode.py`，新 `depth.py`、`segment.py` | 深度识别、16 位 PNG 编码、mcap 深度的探测 / 起草 / 扫描、按 GOP 切片、嵌套数值列展平 |
| 前端 | `features/visualizer/`（`Player.tsx`、`CellMenu.tsx`、`SidePanel.tsx`、`cells/VideoCell.tsx`、新 `cells/DepthCell.tsx`、`data.ts`）、新 `lib/vizDepth.ts`（PNG16 解码、色标）、`lib/vizLayout.ts`、`lib/vizDisplay.ts`（新）、`lib/vizMapping.ts`、`pages/visualize/DatasetInfo.tsx`、`features/datasets/`（`McapConfig.tsx`、新 `DisplayConfigDrawer.tsx`）、`pages/datasets/DatasetDetailPage.tsx`、`locales/zh.ts`、`mocks/` | 深度格子与叠放、展示配置（播放器里的保存 / 恢复、抽屉）、字段树原文、mcap 配置的「深度图」用途、换 episode 保留布局、§2 的前端修补 |
| 部署 | `docs/design/09-deployment.md` §2.1、Daemon README | `CURATOR_VIZ_SEGMENT`（缺省 `0`）；Chart 不用改 |
| 文档 | 本篇；18（§6.5 更正、§10 几行改指本篇、`#t=` 与缓存目录的旧写法）、00 §7（D68–D71）、03、07 §4.5、各 README | 实现时同步 |

不动的：质检的读取器（`ingest/` A 类）、预检（C2）、仓储接口（C5：`display_config` 列早已在，不加迁移步）。

## 1. 背景与范围

| 项 | 来源 | 现状（2026-10-06 调研） |
|---|---|---|
| 深度图 | 18 §10 | LeRobot 深度在模型里是置灰的 `depth` 流（「第二期渲染」），字节从不读；样本 so101_depth、dual_ur5e_rgbd 的深度是 parquet 里逐帧的 `uint16 [H, W]`，单位毫米、0 为空洞；mcap 的深度（RoboMIND：`foxglove.CompressedImage` 里的 16 位灰度 PNG）被当成普通 PNG 相机，浏览器只解出 8 位，几乎全黑 |
| v3 片段切分 | 18 §10 | 播放器直连整个分块 mp4、按 `from_ts` 定位（`#t=` 早已不用）；样本 v3 分块 200–500 MB，`moov` 都在头、66–314 KB，直连首帧不慢。要转码时（Safari 没有 AV1 硬解、mpeg4 等）Daemon 先把整个分块从 TOS 下到本地；同一分块的两条同时转码写同一个 `.part`；**转码产物以这条 episode 的起点为 0，播放器却按分块里的 `from_ts` 定位，每个分块除第一条外画面错位**（Lance 共用 mp4 同理） |
| moov 在尾 | 18 §10 | 72 个 LeRobot 样本子集里 52 个的 mp4 把 `moov` 放在文件尾，直连 TOS 时首帧前多一次往返 |
| LeRobot 展示配置其余项 | 18 §6.5、§10 | `display_config` 只是库里的一列，没有接口写它；字幕轨只是播放器里的临时状态，相机顺序没有界面——18 §6.5「本期只做分段轨选择与相机顺序」并未落地，所以这里整套做；另外换 episode 时布局、隐藏的曲线会被重置 |
| 数据集信息原文 | 需求方 2026-10-06 | 「数据集信息」树的属性名是我们起的中文（分辨率、编码、帧率、读取方式…），需求方要求一律用数据集原文的 KV |
| 小问题与 bug | 调研（§2） | 13 处，见 §2 |

不在本篇：三维场景、预生成、我们自己的标注标准、连播与对比、ReRun 入口下线、数据集文件夹（都仍在 18 §10）。

## 2. 小问题与 bug（F15.1）

| # | 问题 | 修法 |
|---|---|---|
| 1 | v3 / Lance 共用 mp4 的相机走平台转码时画面错位：转码产物的 0 是这条 episode 的起点（`transcode.py` 的 `--from`），episode 记录给的仍是分块里的 `from_ts`，播放器按它 seek | 口径改为「`from_ts` / `to_ts` 是 `url` 自己的时间」：`access: transcode` 的相机给 `from_ts = 0`、`to_ts = to − from`；退回 `transcode_url` 时播放器按 `(0, to_ts − from_ts)` 绑定（§4.5） |
| 2 | 转码前把整个 TOS 分块下到 `source/`，同一分块两条同时转码写同一个 `.part`（没锁） | 切片做输入（§4.3）；切片之外仍要整块拷贝的地方（Lance blob 的回退）按目标文件加锁 |
| 3 | 转码超时只在子进程往 stderr 写行时才检查，不出声的卡死永远不杀；Daemon 关停时不管在跑的转码 | 计时器到点就杀子进程；`VizService` 登记到 Daemon 的关停钩子，关停时杀掉在跑的转码与切片 |
| 4 | 进程崩溃留下的 `*.part` 不记账也不清 | 缓存第一次扫描时删掉一小时以前的 `*.part`（正在写的不动） |
| 5 | mcap 的扫描产物命中时不刷新访问时间，淘汰次序靠文件系统的 atime（`relatime` 下不准） | 命中时刷新（`episode.json`、`series.npz`、相机与深度的产物） |
| 6 | 同一路相机放进两个格子时，后一个格子把前一个从时钟上摘掉（`attach` 用相机键作 id），前一个不再同步 | 每个格子用自己的 id 挂时钟 |
| 7 | 字段树里 uint16 深度列在「其他字段」又列了一次 | 深度只在「深度图」组里（§3） |
| 8 | mcap 的点云、原始图像（`RawImage`、`sensor_msgs/Image`）被起草成相机（「编码 unknown 本期不支持」）：探测只看有没有 `data` 字节 | 探测先认点云（`PointCloud`、`PointCloud2`：忽略）与原始图像（16UC1 / mono16 / 32FC1 → 深度，§5.4；其他编码 → 相机，写明「原始图像（raw）本期不支持」） |
| 9 | 曲线组的字段路径带 `*` 或列表下标（设计 18 §6.2 的示例 `pose.position.*`、`poses.0.position`）时，模型按第一条消息的字段分组数不出线数，只给一条线，曲线只画出第一维（调研时以为 `PoseInFrames` 起草出空曲线：foxglove 的 schema 叫 `PosesInFrame`，原本就按全部位姿起草，曲线不空） | 探测记下第一条消息的数值叶子路径，按它数这类路径的线数 |
| 10 | float32 的二维深度列也会进曲线分组（几千个 `dim_i`） | 曲线分组排除深度特征（`curve_groups` 现成的 `exclude`） |
| 11 | 二维数值列（`[2, 7]` 的位姿）读成 Python 列表，曲线全空 | `column_values` 把嵌套数值列表按行展平（行优先） |
| 12 | 删登记不清缓存，产物只能等 LRU 慢慢挤掉 | 删登记时删它当前指纹下的产物目录（转码、源拷贝、mcap、Lance 帧包、深度、切片）；旧指纹的照旧靠 LRU |
| 13 | 文档过时：18 §4.2 / §4.4 的 `#t=` 写法、§4.1 的缓存目录结构、§6.5「本期做了分段轨与相机顺序」、C4 `from_ts` 的说明 | 改成实际做法 |

## 3. 「数据集信息」改用原文 KV（F15.2）

需求方 2026-10-06：「数据集信息」树里的属性一律用数据集原文的 KV，不翻译。

- **节点名**：数据集自己的名字——LeRobot 的特征键（`observation.images.front`，不再是去前缀的 `front`）、元数据文件的路径、mcap 的 topic、metadata 记录名、附件名、Lance 表名。
- **属性**（`detail`）：元数据里这一项的原文键值，按原来的顺序：
  - LeRobot / Lance 特征：`info.json` 里这个特征的整条（`dtype`、`shape`、`names`、`info` 或 `video_info` …）；
  - 元数据文件：`size`（字节）；目录：`objects`、`bytes`（与登记清单的摘要同名）；
  - mcap topic：`schema.name`、`schema.encoding`、`message_encoding`、`message_count`，相机与深度另有第一条消息的 `format`（或 `encoding`）与画面的 `width` / `height`，数值消息另有 `fields`（字段路径与个数）；metadata 记录：原样；附件：`media_type`、`data_size`；
  - Lance 表：`num_rows`、`columns`。
  嵌套的对象用点连起键（`info.video.codec`），列表写成 JSON 文本；契约的 `detail` 仍只放标量（§7）。
- **不再放**我们自己的解读（读取方式、条数、智能布局、用途、频率、标注格式支持与否……）：它们在播放器的信息侧栏、警告与 mcap 配置里已经有。
- **组织**：分组标题（相机、深度图、状态与动作、任务与标注、元数据、其他字段、文件、Lance 表；mcap 的 Topic / Metadata / Attachments）是页面结构，照旧。「状态与动作」改为列数据集的数值特征（不再列我们拼出的曲线组），每个特征节点指向画它的曲线组（「加入播放器」放那一组）；深度特征单独一组「深度图」，「加入播放器」放深度格子。
- 前端只显示 `detail`（文件节点另有 `path`），不再把 `dtype` / `shape` / `names` 换成标签。

## 4. v3 切片与转码对齐（F15.3）

### 4.1 现状

见 §1。直连播放靠浏览器自己读 `moov` 再按区间取数据，`moov` 在头时首帧只多一个小请求；10-05 又给预签名地址签上了缓存头、十分钟内签名不变，所以对直连而言切片收益很小，还要让字节绕道 Daemon（与 D16 相悖）。真正需要切片的是转码：一条 episode 只占分块的几十分之一，却要把整块（最多 500 MB）先下到本地。

### 4.2 切片工具（`curation/viz/segment.py`）

- `cut(src, out, start, end)`：用 PyAV 打开源 mp4（本地文件，或经 `RangeFile` 按区间读的 TOS 对象 / Lance blob），`seek` 到 `start` 之前最近的关键帧，按解码顺序把包原样写进新 mp4（流拷贝，不开编码器），写到第一个时间不早于 `end` 的关键帧为止（按 GOP 切，保证能解）；时间整体平移，切片的 0 是起头那个包的解码时刻 `start_s`（没有 B 帧时就是那个关键帧的时刻）。输出普通 mp4、`moov` 在头（`faststart`：本地盘写完再挪，浏览器拖动不用扫分片）。
- 只读这一条用得到的字节：`moov`（几十到几百 KB）加这一段的数据；返回 `{start_s, end_s, packets, bytes, b_frames}`，与产物一起落盘（`.json`）。
- 开放式 GOP（有 B 帧、非 IDR 的 I 帧）在切口前可能少最后一两帧，写进警告；LeRobot 编出的 AV1 / H.264 都是闭合 GOP。

### 4.3 用在哪

- **转码的输入**（缺省就用）：TOS 上的 LeRobot v3 相机与 Lance blob 相机要转码时，先切出这一条再转（`--from from − start_s --to to − start_s`），不再整块下载；本地数据集照旧直接读本地文件。转码产物仍以这条 episode 的起点为 0。
- **切片播放**（开关 `CURATOR_VIZ_SEGMENT=1`，缺省 `0`）：LeRobot v3 与 Lance 的相机（一个 mp4 装多条 episode 的）改由 Daemon 出这条 episode 的切片（`access: remux`，`url` 是 Daemon 的 `.mp4`）。第一次要时切（几百毫秒到一两秒，阻塞到切好），之后从缓存出；episode 记录里的 `from_ts` / `to_ts` 换成切片里的时间（`from − start_s`），`start_s` 在出记录时用一次 `seek` 读出（只读 `moov` 与一个包，按文件缓存）。
- 缓存：`segment/<指纹摘要>/ep<N>/<相机>.mp4` 与 `.json`，与其他产物同一个上限与淘汰。

### 4.4 moov 在尾（同一开关）

开关开着时，单条 episode 的 mp4（LeRobot v2，或 v3 一个文件只装一条的）若 `moov` 在 `mdat` 之后（读开头几十字节的盒子头即可判断，按文件缓存），Daemon 出一份 `moov` 在头的版本（整文件流拷贝，同 §4.2，`access: remux`），浏览器第一次请求就拿到索引。开关关着时与今天一样直连。

为什么不缺省开：预生成不在本期，`moov` 在头的版本只能在第一次打开时由 Daemon 读一遍整个文件再给出；Daemon 与 TOS 同在 cn-beijing 时这一遍很快，但字节改由 Daemon 中转、多占它的带宽与盘。是否默认打开等部署环境实测后再定（§9 F15.8）。

### 4.5 时间口径（修 §2 第 1 条）

- episode 记录的 `from_ts` / `to_ts`：这条 episode 在 **`url` 所给媒体** 里的起止（直连整个分块：分块里的时间；切片：切片里的时间；`access: transcode`：0 与 `to − from`）。
- `transcode_url` 的 0 永远是这条 episode 的起点：播放器退回转码时按 `(0, to_ts − from_ts)` 绑定（`to_ts` 或 `from_ts` 为空时结尾不限）。
- mcap 的相机没有 `from_ts`，照旧用 `offset_s`。

### 4.6 落地时的细化（F15.3，2026-10-06）

- 切片是普通 mp4（`faststart`），播放器那边不用改：`access: remux` 的相机照常是 `<video>`，按 episode 记录里切片内的 `from_ts` / `to_ts` 绑定；
  切片的地址带 `?segment=1`（C4 2.7.0 的 `segment` 参数），与直接读原文件的 `.mp4` 地址不同，浏览器缓存不会把两种字节混用。
- 测试里三条各 120 帧的 320×240 噪声画面放进一个 H.264 分块，切中间一条只读了不到一半的字节（`moov` 加这一条的几个 GOP）；
  夹具的 v3 视频 GOP 为 10 帧，第 1 条（30–53 帧）的切片从第 29 帧的关键帧起，`from_ts = 0.1`。
- 开关开着时，episode 记录为每路相机读一次 `moov` 与一个包定出切片起点（按文件与 `from_ts` 缓存），`moov` 在尾的判断只读顶层盒子头（按文件缓存）；
  切片在第一次请求 `.mp4?segment=1` 时切（同一路同一条只切一次，加锁）。

## 5. 深度图（F15.4 LeRobot / Lance，F15.5 mcap）

### 5.1 数据（实测，h200-14）

| 数据集 | 存法 | 尺寸 | 值 |
|---|---|---|---|
| so101_depth（LeRobot v3） | `observation.images.front.depth`，parquet 里逐帧 `list<list<uint16>>` | 480×640 | 中位 671、2%–98% 为 494–1147：毫米；约 10% 为 0（空洞） |
| dual_ur5e_rgbd（LeRobot v3） | `observation.depths.camera_{front,top,left_wrist,right_wrist}` | 240×424 | 中位 1263，2%–98% 为 304–1923 |
| RoboMIND（mcap，h200-14 已下载、未进样本集） | `/front-depth` 等，`foxglove.CompressedImage`，`format: png`，16 位灰度 PNG | 640×480 | 中位 931：毫米 |

一条 so101 episode（609 帧）读深度列 2.4 s；16 位 PNG：PIL 缺省压缩 75 KB / 帧、33 ms，numpy 做 Sub 滤波 + zlib 3 级 87 KB / 帧、10.5 ms（单线程）；30 fps 下一路约 2.3–2.6 MB/s。

### 5.2 深度帧包（`png16`）

- 与 JPEG 帧包同一套（D60）：正文是各帧字节首尾相接，按 Range 读；索引是 `VizFrameIndex`，`codec: png16`。
- **每帧是一张标准的 16 位灰度 PNG**（大端，单位见索引）：mcap 里本来就是 16 位灰度 PNG、不隔行的原样转发（不重编码）；其他来源由 Daemon 编码（numpy 做 Sub 滤波 + zlib 3 级，多线程）。存成 PNG 的好处：存下来的任何一帧都能直接用看图工具打开核对。
- 索引另有 `depth`：`unit`（缺省 `mm`）、`scale`（像素值 × scale = unit 下的值，缺省 1）、`invalid`（空洞值，0）、`lo` / `hi`（这条 episode 有效像素的 2% / 98% 分位，抽样约 30 帧算，作缺省色标范围）。
- 浏览器用自己的 PNG16 解码器（`lib/vizDepth.ts`：拆块、浏览器自带的 `DecompressionStream('deflate')` 解压、五种行滤波还原）得到 `Uint16Array`，不加依赖；`createImageBitmap` 会把 16 位降成 8 位，所以不用它。

### 5.3 LeRobot / Lance 读取器

- **识别**：`dtype` 为 `uint16` / `uint32` / `float32` / `float64`，形状 `[H, W]` 或 `[H, W, 1]`，`min(H, W) > 16`，键名含 `depth`。这样的特征是深度流（`kind: depth`，`available: true`），不进曲线分组（§2 第 10 条）。`video.is_depth_map` 的视频仍按普通相机播（样本集里没有，编码方式各家不同）。
- **配对**：与名字对应的相机配成一对（`observation.images.front.depth` ↔ `observation.images.front`；`observation.depths.camera_front` ↔ `observation.images.camera_front`）：去掉 `depth` / `depths` 词、`depths` 段换成 `images` 后与相机的特征键或短名比，恰好一个对上才配。配上的写进流的 `depth.pair_camera`。
- **读**：一条 episode 的这一列——v2 读它自己的 parquet，v3 只读含这条的行组、按行里的 `episode_index` 过滤（同曲线，§9.1 的规则），Lance 按行窗扫帧表——按 32 帧一批流式读，转成 `uint16`（浮点按米 × 1000 取整、截到 0–65535，单位仍是毫米），编码后追加写盘；内存与 episode 长短无关。帧时刻用这条 episode 的时间轴（与曲线同一份），条数对不上时取短的并警告。
- **生成与等待**：第一次请求 `.json` / `.frames` 时在 Daemon 的小池子里生成（与转码同样的作业表：202 + `VizMediaPending` 带进度，失败 500 写明原因），之后从缓存出（`depth/<指纹摘要>/ep<N>/<流>.frames` 与 `.json`）。实测量级：so101 一条约 3–5 s，dual_ur5e 四路一条各约 2–3 s（本地盘）。
- **接口**：`GET …/episodes/{index}/streams/{stream}.frames`、`.json`（数据集级与任务级各一对）；episode 记录新加 `streams[]`（`key`、`kind`、`url`、`index_url`、`offset_s`、`reason`）。

### 5.4 mcap

- **探测**（`mcap_probe` 的新 `kind: depth`，`codec` 为下列之一）：
  - `CompressedImage`（foxglove / ROS）里的 PNG，IHDR 是 16 位灰度 → `png16`；
  - `format` 含 `compressedDepth` 的 ROS 压缩深度（12 字节头 + PNG）→ `cdepth`（16UC1 直接用；32FC1 是反深度量化，按头里的两个参数还原成米）；`rvl` 写明不支持；
  - `RawImage` / `sensor_msgs/Image`，`encoding` 为 `16UC1` / `mono16` → `raw16`，`32FC1` → `raw32f`（米）；其他编码的原始图像仍是相机（「原始图像（raw）本期不支持」）。
- **映射**（C7 `viz-mapping/1.1`，只加不改）：新增 `depths[]`：`{topic, name, schema?, pair_with?（相机 topic）, unit?（mm / m，源数据的单位；16 位缺省 mm，32 位缺省 m）}`。起草时深度 topic 进 `depths`，按去掉 `depth` / `camera` / `color` / `rgb` / `image` / `compressed` / `raw` 等词后的主干与相机配对（`/front-depth` ↔ `/front-camera`，`/camera/depth/image_raw` ↔ `/camera/color/image_raw`）。校验：topic 不重复、`pair_with` 必须是映射里的相机。质检派生的映射不看 `depths`（判决不变）；已确认的旧映射里被当成相机的深度 topic 照旧是相机，改用途要用户在「mcap 配置」里改。
- **扫描**：深度 topic 与相机同一遍读，逐条消息变成 16 位 PNG（可原样转发的直接写，其余解码再编码），写 `<键>.frames`，索引进 `episode.json` 的 `depths`；抽样约 30 帧算 2% / 98% 分位。
- **mcap 配置**：用途下拉加「深度图」，深度 topic 可选「叠放的相机」。

### 5.5 播放器

- **深度格子**（`cells/DepthCell.tsx`）：按时钟画「时刻 ≤ t 的最后一帧」，不拖住时钟、挂 `attachSource` 预取（同 `FramesCell`，复用 `FramePack`，解码函数换成 PNG16 → 上色）；生成中显示进度（「深度图生成中 N%」）。
  - 上色：缺省 turbo，另有灰度；范围缺省索引里的 `lo`–`hi`，可改（最小、最大毫米数，或「自动」）；空洞（0）透明，底色深灰。
  - 悬停显示该像素的值（`x, y · 671 mm`，空洞写「空洞」）；格子右下角色标条写范围。
  - **叠放**：配上了相机、且宽高比一致时，格子工具里可开「叠在 RGB 上」：下层是那一路相机（直连 / 帧包 / 浏览器解码，照常同步），上层是伪彩深度，透明度滑杆缺省 50%。宽高比不一致时开关置灰并写原因。
  - 色标、范围、叠放与透明度存在格子里（换 episode 保留；可随「保存为缺省布局」存进展示配置，§6）。
- **菜单**：「+」/「更换」里新加「深度图」一组；「数据集信息」里深度节点可「加入播放器」。**不进智能布局**，「仅视频」也不放（一路 2–3 MB/s，按需加）。
- **信息侧栏**：键（原文）、分辨率、单位、本条范围（`lo`–`hi`）、配对的相机、读取方式（「16 位深度帧包」）、当前帧号与时间、鼠标处的值。
- 迷你版的「更换」里也有深度（任务级接口同样有深度路由），智能布局不放。

### 5.5a 落地时的细化（F15.4，2026-10-07）

- 本机实测（`curator-daemon-local`，本地盘，h200-14 拷来的 so101_depth）：第 0 条 609 帧 640×480，第一次请求到深度帧包做好约 3.1 s（4 个编码线程），
  54.7 MB、每帧约 90 KB，索引的范围 494–1147 mm；悬停 (329, 240) 读到 619 mm，与 parquet 第 0 帧同一像素的值相同；叠在 front 相机上碗与机械臂的轮廓对得上；
  两路相机加深度连播 5 秒，时钟 4.3 s 时两路视频都在 4.285 s、深度在第 129 帧。
- 深度格子往前预解 12 帧（0.4 s）、缓存 32 帧：预解的帧数要少于缓存的帧数，否则当前帧会被挤出缓存，时钟一直等它（第一版用了 30 / 24，播放卡在开头）。
  再往后约 6 s 只取字节不解码（与帧包同一套），时钟按它判断是否攒够。
- 悬停的读数显示在右下角色标处（替换范围文字），不另开浮层：小格子里顶部已经有相机名与格子工具。
- 设置面板挂在网格外层、按格子位置定位（同「更换」菜单）：格子是 `overflow: hidden`，小格子会把面板裁掉；Esc 与点外面关闭。
- 「数据集信息」的属性名改成原文后变长（`info.video.is_depth_map`）：属性名一列按内容宽、最多占 45%，等宽字体；窗口窄于 1180 px 时树在上、详情在下。

### 5.6 不做

深度视频（`video.is_depth_map`）的伪彩、由深度加内参反投影的点云（三维场景，18 §10）、RVL 编码的 ROS 压缩深度。

## 6. 展示配置（F15.6）

### 6.1 现状与更正

18 §6.5 写「本期只做分段轨选择与相机顺序」，实际都没有落地（§1）。所以本篇按整套做；mcap 数据集的曲线分组仍由字段映射定，其余几项与 LeRobot / Lance 一样可配。

### 6.2 内容（`VizDisplayConfig`，存在登记上，所有人共用一份）

| 部分 | 内容 | 谁用 |
|---|---|---|
| `layout` | 布局模版（智能 / 仅视频 / 仅曲线 / 自定义）、格子数、每格内容（自定义时；视频 / 曲线 / 深度 / 空）、每个深度格子的上色与叠放 | 完整版 |
| `cameras` | 顺序、隐藏（不进模版布局，「+」里仍可选）、显示名（原文键照旧在信息侧栏） | 完整版、迷你版 |
| `curves` | `groups`：曲线分组覆盖（每组：键、名字、单位、进不进智能布局、各条线 = 特征 + 维度 + 角色 + 名字；LeRobot / Lance）；`hidden`：每组隐藏的线 | 完整版、迷你版（分组）；完整版（隐藏） |
| `track` | 缺省字幕轨（标注来源的键） | 完整版、迷你版 |
| `playback` | 缺省倍速（1 / 1.5 / 2）与循环 | 完整版 |

与配置一起存版本号（每次保存加一）与更新时间。数据集变了（重新预检）以后配置里对不上的键不报错：格子变空、对不上的线不画、轨与相机按缺省，信息栏里写一行提示。

### 6.3 接口

- `GET /datasets/{id}/viz/display` → `VizDisplay`：`config`（没有时为 null）、`version`、`updated_at`，以及给编辑抽屉用的 `defaults`（自动的相机次序、自动分组）。
- `PUT /datasets/{id}/viz/display`（整份替换，按当前模型校验，逐条报错）→ `VizDisplay`；`DELETE`（恢复默认，清空整份）→ `VizDisplay`。两个写接口支持 `Idempotency-Key`，记审计事件 `dataset.update`。
- `VizDataset.display` 带当前生效的配置（任务级用登记上的，只取相机、分组与字幕轨）；相机顺序 / 隐藏 / 显示名、曲线分组、缺省字幕轨由 Daemon 在模型里直接生效（分组改了，`streams` 就是新的组，曲线接口按新组出），布局、隐藏的线与倍速由播放器生效。模型缓存的键加上配置的版本号（修 §2 调研里看到的旧问题：模型缓存不知道配置变了）。

### 6.4 在播放器里（完整版）

- 顶栏新加「布局」菜单：**保存为缺省布局**（存当前模版、格子数与每格内容、深度格子的设置、隐藏的线、字幕轨、倍速与循环；确认后提示「已保存，所有人打开这个数据集都是这个布局」）、**恢复默认布局**（删掉配置里这几项，当前画面回到平台缺省：智能布局、全部线、主轨、1x）。相机与分组的覆盖不受「恢复默认布局」影响。
- 打开数据集时按配置起：有保存的布局就用它（键对不上的格子变空），否则智能布局。
- **换 episode 保留当前布局**：同一数据集里点别的 episode，格子、隐藏的线、字幕轨、倍速与循环都留着（不再每条重置）；换数据集才回到那个数据集的缺省。

### 6.5 展示配置抽屉

数据集详情「可视化配置」卡片与可视化页页头各有「展示配置」入口，抽屉里：

- **相机**：一行一路——原文键、显示名、隐藏；上移 / 下移排序。
- **曲线分组**（LeRobot / Lance）：上面是分组列表（名字、单位、进智能布局、删除；新建分组），下面是全部数值维度的表（特征原文、维度、名字、角色 状态 / 动作 / 其他、所属分组或「不画」）；「恢复自动分组」。
- **字幕轨**：标注来源下拉；**播放**：缺省倍速与循环。
- 底部：保存、**恢复默认**（清空整份展示配置，二次确认）。

### 6.6 不做

个人的（只对自己生效的）配置：站点单租户、所有人看同一份（需求方答复的缺省）；曲线颜色、纵轴范围的手动设置。

## 7. 契约与接口改动

C4 升 **2.7.0**，只加不改：

| 改动 | 说明 |
|---|---|
| `VizStream.depth`（可空）；`kind: depth` 的流 `available: true` | `{width, height, unit, pair_camera}` |
| `VizEpisode.streams[]`（`VizEpisodeStream`） | 深度这类按 episode 取字节的流：`key`、`kind`、`url`、`index_url`、`offset_s`、`reason` |
| `GET /datasets/{id}/episodes/{index}/streams/{stream}.frames`、`.json`；任务级同样一对 | 深度帧包与索引；生成中 202 + `VizMediaPending` |
| `VizFrameIndex.codec` 加 `png16`；`VizFrameIndex.depth`（可选） | `{unit, scale, invalid, lo, hi}` |
| `GET` / `PUT` / `DELETE /datasets/{id}/viz/display`；`VizDisplayConfig`、`VizDisplay`、`VizDisplayPut` | §6.3 |
| `VizDataset.display`（可空）；`VizCamera.hidden` | §6.3 |
| `McapTopic.use` 加 `depth`；`McapTopic.image` 也给深度 topic | §5.4 |
| 说明文字 | `from_ts` / `to_ts` 与 `transcode_url` 的时间口径（§4.5）；`VizFieldNode.detail` 是元数据原文（§3）；`access: remux` 也指 v3 切片与 moov 在头的版本 |

C7 升 **`viz-mapping/1.1`**：只加 `depths`；`1.0` 的映射照常有效。C2、C5 不变。部署（09 §2.1）：`CURATOR_VIZ_SEGMENT`（缺省 `0`），Daemon 的缺省，Chart 的环境变量约定表不变（要开时用 `extraEnv`）。

## 8. 本篇的取舍（D68–D71）

1. **D68 深度图**：深度数组列（LeRobot / Lance）与 mcap 的深度图（16 位 PNG、16UC1 / 32FC1 原始图、ROS compressedDepth）由 Daemon 打成每帧一张 16 位灰度 PNG 的帧包（mcap 里本来是 16 位 PNG 的原样转发），浏览器自己解码上色（缺省 turbo，范围取本条 2%–98%、可调，0 为空洞），悬停读毫米数；按名字与相机配对，宽高比一致时可叠放；不进智能布局。mcap 的深度 topic 写进映射的 `depths`（C7 1.1）。深度视频仍按普通相机播。
2. **D69 v3 切片与时间口径**：平台转码一律以按 GOP 切出的单条为输入（PyAV 流拷贝，只按区间读这一条），不再整块下载；`from_ts` / `to_ts` 指 `url` 自己的时间，`transcode_url` 的 0 是这条 episode 的起点。直连播放维持 D16；`CURATOR_VIZ_SEGMENT=1` 时 v3 / Lance 的相机由 Daemon 出单条切片、`moov` 在尾的单条 mp4 由 Daemon 出 `moov` 在头的版本，缺省关。
3. **D70 展示配置**：存在登记上、所有人共用一份（版本号与更新时间）：缺省布局、曲线分组覆盖（LeRobot / Lance）、相机顺序 / 隐藏 / 显示名、缺省字幕轨、缺省倍速与循环；播放器里「保存为缺省布局」与「恢复默认布局」，抽屉里「恢复默认」清空整份；迷你版只用相机、分组与字幕轨；同一数据集换 episode 保留当前布局。
4. **D71 数据集信息原文**：节点名与属性一律是数据集元数据的原文键值，嵌套的键用点连接、列表写成 JSON；分组标题是页面结构，保留中文。

## 9. 工作包与验收

| # | 内容 | 验收 |
|---|---|---|
| F15.0 | 本篇；账本阶段 15；18（§6.5 更正、§10 几行改指本篇）、00 §7（D68–D71）、CLAUDE.md 设计表 | 需求方过目 |
| F15.1 | §2 的 13 处 | ①v3（H.264 夹具，多条 episode 共用一个 mp4）第 2、3 条走转码时画面与时钟对齐（逐帧比对）②同一路相机放两格都同步 ③其余每条有单测或用例 ④前端五项、`tests/viz` |
| F15.2 | §3 | ①LeRobot / Lance / mcap 三种数据集的树里没有我们起的中文属性名，特征节点的属性与 `info.json` 条目逐键一致 ②「加入播放器」照常 ③前端与 `tests/viz` 用例 |
| F15.3 | §4：切片工具、按切片转码、开关下的切片播放与 moov 在头 | ①切片逐帧与原分块的这一段一致（解码比对）②TOS 上的 v3 转码只按区间读这一条（读的字节数）③开关开 / 关两种的 v3、Lance、v2（moov 在尾）都能播、与时钟对齐 ④用例 |
| F15.4 | §5.2、§5.3、§5.5：LeRobot / Lance 深度 | ①so101_depth、dual_ur5e_rgbd 的深度能看：解出的值与 parquet 逐像素一致、伪彩、范围、悬停读数、叠放 ②生成进度与缓存 ③单测（PNG16 编解码、上色、配对）④前端五项 |
| F15.5 | §5.4：mcap 深度 | ①RoboMIND 一个 episode：起草出 `depths` 并配上相机，深度与 RGB 同步、叠放 ②16UC1 / 32FC1 / compressedDepth 的合成夹具 ③映射 1.0 照常、判决不变（对账回放）④用例 |
| F15.6 | §6：展示配置 | ①保存为缺省布局后刷新、换人（换浏览器）打开都是这个布局；恢复默认布局回到智能布局 ②抽屉改相机顺序 / 隐藏 / 显示名、分组、字幕轨、倍速后模型与曲线接口跟着变；恢复默认清空 ③换 episode 布局不丢 ④接口与前端用例 |
| F15.8 | 验收：本机全量回归（前端五项、`tests/viz`、daemon、契约、cli、对账）；h200-14 上样本实测（深度两份、RoboMIND、v3 转码与切片的 TOS 读量）；README 手动验证步骤；需求方验收 | 需求方验收 |
