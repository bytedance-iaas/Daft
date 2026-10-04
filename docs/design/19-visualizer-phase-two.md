# 19 · 可视化第二期先行三项：相机多于 9 路、浏览器内解码、Lance 读取器

> 状态：**实施稿 v1.0（2026-10-04）**——需求方 2026-10-04 指定第二期先做这三项（设计 18 §10 的三行），其余几行仍留在 18 §10。
> 本篇不改设计 18 的已定决策（D60–D64），只在上面加东西；§6 是本篇自己的三条取舍，需求方验收时一并确认，确认后写进 00 §7。
> 需求账本：阶段 14（F14.0–F14.4）。分支 `feat/data-visualizer`（worktree `~/ws/daft-viz`），与阶段 13 同一分支。

## 0. 开工指引

读的顺序：§1 范围 → §2 相机多于 9 路（只动前端）→ §3 浏览器内解码（Daemon 的 mcap 扫描 + 前端新格子）→ §4 Lance 读取器（Daemon 新读取器）→ §5 契约 → §6 取舍 → §7 工作包与验收。
三项互不依赖，落地顺序 F14.1 → F14.3 → F14.2（由易到难）；契约一次升到 C4 2.5.0（§5），三项共用。

要动的地方：

| 组件 | 位置 | 要做的 |
|---|---|---|
| 契约 | `docs/contracts/openapi.yaml`（C4） | 2.5.0，只加不改：`VizFormat.reader` 加 `lance`、`VizFormat.layout`；`VizAccess` 加 `blob`；`VizEpisodeCamera.samples_url`；`VizFrameIndex` 认 H.264 / H.265（`key[]`、`codec_string`）；锁与 `npm run gen:api` |
| 前端 | `src/lib/vizLayout.ts`、`features/visualizer/`（`Player.tsx`、新 `cells/SamplesCell.tsx`、新 `lib/sampleDecoder.ts`）、`SidePanel.tsx`、`visualizer.css`、`locales/zh.ts`、`mocks/` | 网格到 4×4、智能布局的多相机规则、窄格子、配色；WebCodecs 格子与退回；`blob` 读取方式；Lance 与多相机的模拟数据 |
| Daemon | `daemon/viz/mcap.py`、`service.py`、`status.py`、新 `daemon/viz/lance.py`、`media.py`（区间应答）、`settings.py`、`routes/results.py`（任务级 `.mp4` 分派） | 帧包路由出 H.264 / H.265 样本；转封装改为按需；Lance 读取器与 blob 的区间应答；开关 `CURATOR_VIZ_CLIENT_DECODE` |
| 内核 | `curation/viz/mcap_episode.py`、新 `curation/viz/annexb.py`、新 `curation/viz/lance_layout.py`、`remux.py` | 扫描时认关键帧与参数集、写样本索引；Lance 的布局识别、列名映射、按 episode 读帧表 |
| 部署 | `docs/design/09-deployment.md` §2.1、Daemon README | `CURATOR_VIZ_CLIENT_DECODE`（缺省 `1`）；Chart 不用改 |
| 文档 | 本篇、18（§10 三行改指本篇、§4.2 帧包写法订正）、07 §4.5、03、各 README | 实现时同步 |

不动的：质检的 Lance 读取器 `ingest/lance_reader.py`（A 类，仍只认 0.3 的三表、TOS 上整表拷贝）、预检（C2）、映射（C7）、仓储（C5）。

## 1. 背景与范围

设计 18 §10 第二期的三行，与 F13.8 实测（18 §9.6）对出来的缺口：

| 项 | 18 §10 的写法 | 实测 / 现状 |
|---|---|---|
| 相机多于 9 路 | 网格最大 3×3，RH20T 有 10 路放不全；4×3 网格或相机墙视图 | RH20T 在窄网格下智能布局是 2×3，只上 6 路；其余只能在「+」「更换」里一路一路换；相机配色 8 种一轮，第 9 路与第 1 路同色 |
| 浏览器内解码（备选） | WebCodecs 直读 mcap 的 H.264 / H.265 裸流，省掉 Daemon 转封装 | mcap 的视频相机全部由 Daemon 在扫描时转封装成 fMP4（`remux.py`），扫描时攒下的逐帧偏移随后丢掉；`codec_string` 是写死的通用串 |
| Lance 读取器 | 对应 lerobot-lancedb 版本的读取器，产出同一个展示模型 | 预检只认 lerobot-lance-convert 0.3 的三表（`lance`）；0.1–0.2 的两种布局有 `meta/info.json`，被当成缺数据文件的 LeRobot；可视化对 Lance 一律「第二期」 |

不在本篇：18 §10 其余各行（三维场景、深度图、自己的标注标准、预生成、moov 在尾、v3 片段切分、展示配置其余项、连播对比、ReRun 下线）；质检读 Lance 的方式。

## 2. 相机多于 9 路（F14.1，只动前端）

### 2.1 现状

`lib/vizLayout.ts`：格子数可选 1×1、2×1、2×2、3×2、3×3（`GRID_SIZES`），`MAX_COLS = MAX_ROWS = 3`；智能布局 = 全部相机 + 至多两组标了 `smart` 的曲线，网格宽 ≥ 1000 px 用 3 列、否则 2 列，行数到 3 为止，放不下的直接截掉；「仅视频」至多 3 列。
迷你版「1 × N，N ≤ 3」借用了同一个 `MAX_COLS`。后端（两个读取器、C7 映射的 32 路上限）没有 9 路的限制。

### 2.2 规则

- **网格最大 4 列 × 4 行**：格子数下拉加 4×3、4×4（原有五档不变）。
- **智能布局**：内容（相机 + 至多两组曲线）**不超过 9 格时与今天完全相同**；超过 9 格时，网格宽 ≥ 1240 px 用 4 列、否则 3 列，行数 = ⌈内容 / 列数⌉、到 4 为止。
  放不下时（窄网格的 2 × 3 放不下 7–9 格，或相机超过 14 路），两组曲线保留，相机按模型顺序放满其余格子；多出来的相机照旧在「+」「更换」里选，
  网格下面写一行「另有 N 路相机没放上来，在格子的「更换」里选」。模版算出的格子数不在下拉里的（2 × 3、3 × 4）也列进下拉。
- **仅视频**：不超过 9 路时同今天（至多 3 列）；超过 9 路时 4 列（宽 < 1240 px 时 3 列），到 4 行。**仅曲线**：2 列，行数上限随之到 4。
- **迷你版不变**：仍是 1 × N、N ≤ 3，常量与完整版分开（`MINI_MAX_CELLS`）。
- **窄格子**：格子宽 < 280 px 时，格子右上角的工具只留图标（文字进悬停提示），相机名照旧截断；格子高度规则不变（约 0.66 倍格宽，160–420 px）。
- **配色**：相机的色点在 16 种颜色里轮换（前 8 种与曲线配色相同，后 8 种新增），16 路以内不重色。

### 2.3 性能

16 个 `<video>` 同时解码：用 `backend/scripts/make_cams_dataset.py` 造的两份本地数据集量 1x / 2x 下的漂移——`cams_10`（10 路 640×360 @10 fps，仿 RH20T）、
`cams_16`（16 路 640×360 @30 fps）。目标同 F13.4：1x 下各路相对时钟的偏差 < 1 帧。

| 数据集与布局（内置浏览器，本机 Daemon，2026-10-04） | 1x 最大偏差 | 2x 最大偏差 |
|---|---|---|
| `cams_10`：10 路 @10 fps（一帧 100 ms），智能展示 4 × 3 | 8.2 ms | 5.1 ms |
| `cams_16`：16 路 @30 fps（一帧 33 ms），仅视频 4 × 4 | 6.2 ms | 8.5 ms |
| `cams_16`：智能展示 4 × 4（14 路 + 两组曲线） | 8 ms | — |

时钟与墙钟一致（播 2.81 s 走了 2.80 s；2x 走了 5.64 s）。量的时候标签页要在前台：退到后台的标签页计时器被浏览器节流，时钟会被「等视频」拖住，数不可用。

## 3. 浏览器内解码（F14.2）

### 3.1 现状

mcap 的 H.264 / H.265 相机：`mcap_episode.scan` 顺序读一遍文件，把每条消息的 Annex-B 字节接到 `<相机>.annexb.part`，同时在内存里记下每帧的时刻、偏移、长度；读完由 `remux.py`
（PyAV 原始流解复用 + 流拷贝）转成 fMP4，再删掉 Annex-B 文件和这份逐帧索引。浏览器用 `<video>` 播 fMP4；`<video>` 播不了（多为 HEVC）再要平台转码（ABC-130k 一路约 64 s）。
F13.8：转封装在扫描之后约 0.05 s 出第一帧，冷开的大头是扫描本身（ABC-130k 一条 29 s）。

### 3.2 做法

**Daemon（`CURATOR_VIZ_CLIENT_DECODE=1`，缺省开）**

- 扫描时逐条消息认 NAL 单元（新 `curation/viz/annexb.py`，只看 NAL 头与 SPS 前几个字节，不解码）：关键帧（H.264 IDR，类型 5；H.265 IRAP，类型 16–23）、参数集（H.264 SPS / PPS，H.265 VPS / SPS / PPS）。
- 写出的**样本包** `<相机>.annexb`：
  - 从第一个关键帧开始（之前的帧解不出来，照旧丢掉并警告 `leading_frames`，与转封装同口径）；
  - 每个关键帧样本自带参数集（消息里没有就把最近一次见到的参数集接在前面），所以从任何关键帧都能起解；
  - 一条消息一个样本，字节首尾相接。
- 样本索引写进 `episode.json` 的相机条目：`t[]`（episode 秒）、`offset[]`、`size[]`、`key[]`、`codec_string`（从 SPS 算：`avc1.PPCCLL`、`hvc1.…`）、`width`、`height`。
- **转封装改为按需**：`.mp4` 第一次被要时才由同一份样本包转成 fMP4（落盘缓存，之后直接出）；浏览器自己解码时 Daemon 不再转封装，缓存里也不再有一份 fMP4。
  `?transcode=1` 照旧以转封装结果为输入。
- 样本包与索引走现有的帧包路由：`…/cameras/{key}.frames`（Range）与 `.json`（`VizFrameIndex`，`codec` 为 `h264` / `h265` 时带 `key[]`、`codec_string`）。
  episode 记录里这路相机：`access` 仍是 `remux`（`url` 是按需转封装的 `.mp4`），另给 `index_url` 与新字段 `samples_url`。
- 开关为 `0` 时与今天完全相同：扫描里就转封装，删掉 Annex-B，不给 `samples_url`。开关进 mcap 缓存目录的指纹，切换后不会混用旧产物。
- 找不到关键帧、或找不到参数集的相机：不给 `samples_url`，扫描里当场转封装（与开关为 `0` 相同），浏览器走 `<video>`。

**播放器（新 `cells/SamplesCell.tsx`，解码调度在 `lib/sampleDecoder.ts`）**

- 有 `samples_url` 且 `VideoDecoder.isConfigSupported({codec: codec_string, codedWidth, codedHeight})` 为真 → 用 WebCodecs 解，画在 canvas 上；否则用 `<video>` 播 `url`，再不行要转码（与今天同一条路）。
- 时钟规则与 JPEG 帧包相同（D64）：画「时刻 ≤ t 的最后一帧」；格子不拖住时钟（与 `FramesCell` 一样只订阅时钟）。
- 解码：要第 i 帧时从 i 之前最近的关键帧起解（`configure` 不带 `description`，即 Annex-B）；顺播时向前多解若干帧；向后跳或跳出当前 GOP 就 `reset` 重来。
  解出的 `VideoFrame` 立刻转成 `ImageBitmap` 并 `close()`（不占硬件解码器的帧池），按帧号缓存一小段（按字节数封顶）。
- 字节按 GOP 用 Range 取（一个关键帧到下一个关键帧，连续的几段并成一次请求），与帧包同一套取法。
- 解码报错（`error` 回调、`configure` 失败）→ 这一路改用 `<video>`（`url`），不再回来；信息侧栏的「读取方式」写「浏览器解码（WebCodecs）」或「转封装」。

### 3.3 为什么不让浏览器直接读 mcap

18 §10 的原话是「WebCodecs 直读 mcap」。本篇仍由 Daemon 扫描、浏览器只做解码，理由：

- mcap 的块（chunk）里各 topic 交错存放、通常 zstd / lz4 压缩，浏览器要看一路相机也得把整个文件取下来解压，ReRun 慢就慢在这里（18 §1）；
- 要在浏览器里带 zstd / lz4 的 wasm 解压器与 mcap 解析库（新依赖）；
- 从浏览器带 Range 取客户桶里的对象要桶开 CORS，这不在我们手里（视频直连靠 `<video>` 不受 CORS 约束，`fetch` 不行）；
- 时间轴、帧号基准、曲线、质检时钟仍要 Daemon 读同一遍文件。

所以「省掉的」是 Daemon 的转封装与 fMP4 缓存，不是扫描。扫描本身的冷开耗时（ABC-130k 29 s）要靠「预生成」（18 §10）解决，不在本篇。

### 3.4 边界

- B 帧：与转封装相同按解码顺序出帧（播放顺序可能不对，警告 `b_frames`）；样本集的机器人相机都没有 B 帧。
- HEVC 仍要平台解码器：WebCodecs 与 `<video>` 依赖同一套硬件解码，多数 Linux 上的 Chrome 两条路都不行，照旧走平台转码。
- 帧率与时刻：样本时刻就是消息时刻，不均匀的帧间隔照原样显示。

## 4. Lance 读取器（F14.3）

### 4.1 现状

- 质检：`ingest/lance_reader.py`（A 类）只认 lerobot-lance-convert ≥ 0.3 的三表，只读本地路径；TOS 上的数据集由 CLI 的源缓存把三张表整表拷到本地再读。
- 预检的格式识别（`cli/lerobot_meta.detect_format`）：有 `frames.lance` + `videos.lance` → `lance`；否则根下有 `meta/info.json` → `lerobot`（所以 0.1–0.2 的布局被当成缺数据文件的 LeRobot）；其他 `.lance` 表 → `lancedb`（不支持）。
- 可视化：`lance` 一律回「Lance 数据集的可视化读取器在第二期」；C4 的 `VizFormat.reader` 只有 `lerobot` / `mcap`。

### 4.2 要认的三种布局（lerobot-lancedb 各版，2026-10-04 对着上游源码核对）

| 布局（`VizFormat.layout`） | 上游版本与命令 | 目录 | 帧表 | 相机 |
|---|---|---|---|---|
| `lance-0.3` | 0.3.x，`lerobot-lance-convert` | `frames.lance`、`videos.lance`、`meta.lance`、`meta/`（`info.json` 带 `storage_format: "lance"`） | 一行一帧、按 `index` 排序（第 N 行即绝对帧 N）；列名是特征名的点换下划线，向量是定长列表，语言列保持嵌套 | `videos.lance` 一行一个源 mp4：`video_key`、`chunk_index`、`file_index`、`file_size`、`moov_offset`、`moov_size`、`kf_indices`、`kf_positions`，字节在 blob v2 列 `video_bytes` |
| `lance-0.2-video` | 0.1–0.2，`lerobot-convert-to-lance-video` | `<名>.lance`、`<名>_videos.lance`、`meta/` | 同上，只有数值特征（float32 定长列表），没有相机列 | `<名>_videos.lance`：`video_key`、`chunk_index`、`file_index`、`video_bytes`（blob v1：`large_binary` + `lance-encoding:blob`） |
| `lance-0.2-frames` | 0.1–0.2，`lerobot-convert-to-lance` | `<名>.lance`、`meta/` | 同上，另有每路相机一列 JPEG 字节（`binary`） | 没有视频，每帧一张 JPEG |

三种的 `meta/` 都是源 LeRobot v3.0 数据集的 meta 原样拷贝：episode 表里有每条的 `dataset_from_index` / `dataset_to_index` 与每路相机的 `chunk_index` / `file_index` / `from_timestamp` / `to_timestamp`
（一个 mp4 装多条 episode）。0.3 的远端根只拷了三张表时，`meta/` 从 `meta.lance`（`path`、`data` 两列）取。

### 4.3 读取器（`daemon/viz/lance.py`，格式层在 `curation/viz/lance_layout.py`）

- **选读取器**：预检是 `lance`，或预检是 `lerobot` 而文件清单里有 `.lance` 表（0.1–0.2 的两种）→ Lance 读取器。布局按目录认（上表），认不出、或没有 `meta/info.json` 也没有 `meta.lance` 的（例如图像直接嵌在单表里、没有 LeRobot 元数据的数据集）→ 不支持，写明原因。
- **元数据**：与 LeRobot 读取器同一套（`info.json` 的特征、相机与编码、fps、episode 表、任务、标注来源、曲线组），读取器直接继承 LeRobot 读取器，只换「逐帧列」与「相机字节」两处。
- **逐帧列**（曲线与标注列）：帧表按行窗取（`index` 在 `[dataset_from_index, dataset_to_index)`；episode 表没有行窗时按 `episode_index` 过滤），只取要的列，进同一个内存缓存，episode 记录与曲线请求共用。
  列名：先看帧表 schema 元数据里的 `source-column-name-map`（质检读取器的约定），再试「点换下划线」，最后试原名。
- **相机**：
  - 视频（`lance-0.3`、`lance-0.2-video`）：blob 里是原样的 mp4，没有独立对象可签名。浏览器能放的编码由 Daemon 按 Range 从 blob 出字节（新的读取方式 `blob`，路由同 `.mp4`），
    一个 mp4 装多条 episode 时照旧给 `from_ts` / `to_ts`；浏览器放不了的编码（mpeg4 等）先把这个 mp4 从 blob 落到缓存，再走平台转码。
  - 逐帧 JPEG（`lance-0.2-frames`）：按 episode 从帧表取这一列，落成帧包（与 mcap JPEG 同一格式与路由，`access: frames`）。
- **存储**：本地挂载直接打开表目录；TOS 上的数据集用 Lance 自带的对象存储客户端**原地按区间读**（TOS 的 S3 兼容端点 `tos-s3-<地区>`，与 ReRun 用的同一个；密钥用数据集绑定的输入密钥，只在进程内、不进日志），
  不整表拷贝；公共缓存桶匿名读。
- **字段树**：在 LeRobot 的几组之外加「Lance 表」一组（每张表的行数与列）。
- 任务级（迷你版）同一个读取器：质检能跑的只有 `lance-0.3`，所以任务里出现的也只有它。

### 4.4 不做

- 质检的 Lance 读取器不动（A 类）；0.1–0.2 的布局质检照旧不支持（预检照旧把它们当 LeRobot 并报缺数据文件），可视化能看。
- 深度、图片特征与 LeRobot 读取器同样处理（深度进字段树、第二期渲染）。

## 5. 契约与接口改动

C4 升 **2.5.0**，只加不改，不加新路径：

| 改动 | 说明 |
|---|---|
| `VizFormat.reader` 加 `lance`；`VizFormat.layout`（可空） | Lance 的布局 `lance-0.3` / `lance-0.2-video` / `lance-0.2-frames`，其他格式为 null；0.1–0.2 的数据集 `kind` 报 `lance`（可视化的看法，不是预检的） |
| `VizAccess` 加 `blob` | Daemon 按 Range 出 Lance 表 blob 里的原样视频 |
| `VizEpisodeCamera.samples_url`（可空） | 浏览器内解码读的样本包（mcap H.264 / H.265，开关开着且有关键帧与参数集时才有），索引在 `index_url` |
| `VizFrameIndex.codec` 加 `h264` / `h265`；可选 `key[]`、`codec_string` | 样本包的索引：每帧是否关键帧、RFC 6381 编码串 |

C2、C5、C7 不变。部署（09 §2.1）：`CURATOR_VIZ_CLIENT_DECODE`（缺省 `1`），Daemon 的缺省，Chart 的环境变量约定表不变（要关时用 `extraEnv`）。

## 6. 本篇的取舍（需求方验收时确认，确认后写进 00 §7）

1. **网格最大 4×4**，不做「相机墙」视图：格子仍是一路一格，工具、放大、更换都照旧；9 格以内的智能布局与今天一样，超过 9 格才换 4 列。
2. **浏览器内解码缺省开、失败自动退回 `<video>`**；开着时 Daemon 不再预先转封装（按需才转）。由 Daemon 扫描、浏览器只解码，不让浏览器直接读 mcap（§3.3）。
3. **Lance 读取器认 lerobot-lancedb 的三种布局**；视频由 Daemon 从 blob 按区间出（没有可签名的独立对象）；TOS 上原地按区间读，不整表拷贝。

## 7. 工作包与验收

| # | 内容 | 验收 |
|---|---|---|
| F14.0 | 本篇；账本阶段 14；18 §10 三行改指本篇 | 需求方过目 |
| F14.1 | §2：网格到 4×4、智能与仅视频的多相机规则、迷你版常量分开、窄格子、16 色 | ①`cams_10`、`cams_16` 在宽 / 窄网格下按 §2.2 上屏，多出来的有提示 ②1x 漂移 < 1 帧（量值写回 §2.3）③`vizLayout` 单测覆盖新规则 ④前端五项检查 |
| F14.2 | §3：扫描写样本包与索引、按需转封装、开关；`SamplesCell` 与解码调度、退回 | ①GenRobot（H.264）与 ABC-130k（H.265）切片在内置浏览器里走 WebCodecs：播放、逐帧、跳转、倍速、循环都对，和 `<video>` 对同一时刻显示同一帧 ②不支持 / 出错时退回 `<video>`；开关关掉与今天一致 ③冷开耗时与缓存体积对比写回本节 ④单测：NAL 识别、参数集补齐、`codec_string`、调度（假解码器） |
| F14.3 | §4：Lance 读取器、`blob` 区间应答、frames 布局的帧包、存储（本地 / S3 兼容） | ①三种布局的本地数据集都能打开：曲线与帧表逐点一致、相机能播（含多条 episode 共用一个 mp4）、标注照常 ②经 S3 兼容存储（测试里起一个最小的本地 S3 服务）按区间读通过；TOS 实测在有密钥时补 ③任务级迷你版能开 Lance 任务 ④`tests/viz` 用例 |
| F14.4 | 验收：本机全量回归、需求方验收；指标写回本篇 | 需求方验收 |
