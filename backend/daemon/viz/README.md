# 数据可视化（Daemon 侧，阶段 13–14，设计 18、19）

读取器把数据集读成统一展示模型（C4 2.4.0 的 `VizDataset` / `VizEpisode` / `VizSeries`），网页控制台的完整版（`/visualize`）与迷你版（报告、裁决、任务详情里的弹窗）只认这个模型。
数据集级接口读登记，任务级接口读任务冻结的输入（D27），两组共用读取器。格式层的逻辑（特征、相机编码、曲线分组、标注识别、曲线读取与抽稀、转码）在内核
`backend/curation/viz/`，预检（`curation preflight`）与 Daemon 用的是同一份。

## 文件

| 文件 | 内容 |
|---|---|
| `status.py` | 登记能不能可视化（`DatasetItem.viz`）、mcap 映射的状态（`DatasetDetail.viz_mapping`） |
| `source.py` | 数据源：数据集级（登记的地址、密钥、文件清单、映射、展示配置、外部标注）与任务级（任务冻结的输入、`preflight.json`、`source_manifest.json`、`run.json` 里的映射）；打开存储、签浏览器地址、本地文件只在数据集目录之内 |
| `lerobot.py` | LeRobot v2 / v3 读取器：`meta/` 的 episode 表、相机、曲线组、标注来源、字段树；一条 episode 的逐帧列只读一次（v3 只读它的行组）进缓存，episode 记录与曲线请求共用 |
| `lance.py` | Lance 读取器（设计 19 §4）：lerobot-lancedb 的三种布局（0.3 三表、0.1–0.2 视频两表、0.1–0.2 逐帧 JPEG）；元数据照 LeRobot 读（`meta/`，或只有表的根里的 `meta.lance`），一条 episode 的逐帧列从帧表按行窗读，视频从 videos 表的 blob 按 Range 出（`access: blob`），逐帧 JPEG 落成帧包；本地直接开表，TOS 经 S3 兼容端点按区间读、不整表拷贝 |
| `mcap.py` | mcap 读取器（设计 18 §6；浏览器内解码见设计 19 §3：`CURATOR_VIZ_CLIENT_DECODE` 开着时 H.264 / H.265 相机留 Annex-B 样本包与索引——从第一个关键帧起、带参数集 `config` 与 `codec_string`，由帧包路由出——`.mp4` 第一次被要时才转封装）：按确认的映射（C7）出展示模型；一条 episode 只顺序读一遍，产物（帧包、重封装的 mp4、深度帧包 `depth-<键>.frames` 与索引——设计 21 §5.4，映射 1.1 的 `depths`——曲线 `series.npz`、`episode.json`）落在磁盘缓存，按数据集指纹与映射版本分目录；探测结果按指纹缓存，前三个文件的 topic 不一致时警告 |
| `segments.py` | 切片（设计 21 §4）：按 GOP 从共用 mp4 里切出单条 episode（内核 `curation/viz/segment.py`，PyAV 流拷贝，只按区间读 `moov` 与这一条的字节），落在 `segment/<摘要>/ep<N>/`；转码的输入、开关下的切片播放与 `moov` 在头的版本都用它 |
| `depth.py` | LeRobot / Lance 的深度流（设计 21 §5.3）：一条 episode 的深度列按批读（v3 只读它的行组，Lance 按行窗扫帧表），在几个线程上编成 16 位 PNG，写进 `depth/<摘要>/ep<N>/<流>.frames` 与索引 `.json`（时刻、偏移、大小、2% / 98% 范围）；第一次请求在生成池里做、回 202 带进度。格式层在内核 `curation/viz/depth.py` |
| `display.py` | 展示配置（设计 21 §6，C4 `VizDisplay`）：存在登记的 `display_config` 列里（`{version, updated_at, config}`）；读的时候叠在缓存的读取器元数据上——相机的顺序、显示名与 `hidden`、字幕轨的主轨由这里做，LeRobot / Lance 的曲线分组由读取器换成配置里的组（字段树与曲线接口跟着走）；保存前按不带配置的模型逐项校验，数据集后来变了对不上的键读的时候跳过；任务级只取相机、分组与字幕轨 |
| `media.py` | 磁盘缓存（`CURATOR_VIZ_CACHE_DIR`，LRU，上限 `CURATOR_VIZ_CACHE_GB`；第一次扫描时删掉一小时以前的 `*.part`；删登记时删它当前指纹下的产物）、转码任务池（子进程 `python -m curation.viz.transcode`，`CURATOR_VIZ_TRANSCODE_WORKERS` 路，不占质检的 CPU 名额池；到点不出声的子进程也杀，Daemon 关停时杀掉在跑的）、带 Range 的本地文件应答 |
| `service.py` | 按格式挑读取器、内存缓存（按指纹）、相机地址（直连预签名 / Daemon 路由 / 帧包 / 转码兜底）、外部标注文件的解析；mcap 的探测、映射的校验与保存、模版库 |
| `eef_overlay.py` | EEF 模型意见的标记（设计 20，C4 2.6.0 `EefOverlay`）：读任务冻结的 trajectory.json（运行目录 `inputs/` 的副本，工作目录清理过先取回，再不行用上传件），只解析一条 episode，用内核 `eef_consistency/overlay.py` 算每帧图层，按 `media.uri` / `topic` 对上 `VizEpisode` 的相机；按任务、文件与 episode 缓存在内存，不落盘 |
| `../routes/viz.py` | 路由：数据集级的模型、episode 列表、元数据预览、episode、曲线、相机 `.mp4|.frames|.json`、外部标注、映射；探测与模版库；任务级的模型、episode、曲线、帧包（任务级的 `.mp4` 在 `routes/results.py`） |

内核（`backend/curation/viz/`）：`lerobot_info.py`（features、names 的几种写法、相机编码与 `needs_transcode`、RFC 6381 编码串）、`groups.py`（曲线分组 §5.3）、
`annotations.py`（标注识别 §4.5：`subtask_index`、`language_*`、逐帧 `task_index`、`*_index` 查表、文字列、`*_segment` 布尔段、成败 / 质量 / 评分 / `task_status`、Argus 外部标注）、
`series.py`（按行组读 episode 的列、min / max 抽稀）、`transcode.py`（PyAV 转 H.264 fMP4）；mcap 的 `mcap_messages.py`（解码、数值叶子与字段路径、
画面编码与尺寸、显示用的变换）、`mcap_probe.py`（summary 加每个 topic 的首条消息）、`mcap_mapping.py`（三个内置模版、起草与按覆盖率匹配、校验、派生质检映射
`check_mapping`；1.1 的深度 topic 按主干配相机）、`mcap_episode.py`（一条 episode 的一遍扫描，深度 topic 在几个线程上转成 16 位 PNG；样本包的索引与按需转封装 `remux_samples`）、`depth.py`（16 位 PNG 编解码、深度单位与范围、mcap 深度编码的识别与还原：png16、ROS compressedDepth、16UC1 / 32FC1 原始图）、`annexb.py`（只看 NAL 头与 SPS / PPS 前几个字节：关键帧、参数集、B 帧、`codec_string`）、`remux.py`（H.264 / H.265 Annex-B 流拷贝成 fMP4，H.265 标 `hvc1`）；Lance 的 `lance_layout.py`（三种布局的识别、列名映射——帧表 schema 元数据的 `source-column-name-map` 或点换下划线、按行窗读一条 episode、`meta.lance`、videos 表的行号、S3 兼容端点的参数）。

mcap 的时间：零点是映射里各 topic 的第一条消息；帧号基准缺省是第一组 `role=action`（与质检的行同一口径），没有才用第一路相机；`check_clock` 是质检的锚
（第一条 action 消息相对零点的秒数）与 action 的频率，迷你版用它把发现的帧号换成时刻。一条 episode 按文件顺序扫一遍，相机字节边读边落盘；H.264 / H.265 从第一个
关键帧开始转封装（之前的 P 帧解不出来，相机的 `offset_s` 随之后移，警告 `leading_frames`）；footer 坏了的文件读到断点为止（警告 `truncated`）。
预检只要找到 mcap 文件就算 mcap 数据集（可视化看映射，不看质检按缺省 topic 读不读得了）；质检读取器读不了的字段与 H.265 相机在探测与映射的应答里警告 `checks_gap`。映射确认后，预检与任务的每条 CLI 命令都带
`--set ingest.mcap_mapping=<派生的质检映射>`；任务开始时映射（版本、原文、派生结果）冻结进 `run.json` 的 `viz_mapping`，之后改映射只影响新任务。

## 配置

| 环境变量 | 缺省 | 说明 |
|---|---|---|
| `CURATOR_VIZ_TRANSCODE` | `1` | 浏览器放不了的相机（mpeg4 等）由平台转码（D60）；`0` 关掉后这类相机在格子里写明原因，`?transcode=1` 回 404 `transcode_disabled` |
| `CURATOR_VIZ_CACHE_DIR` | `<CURATOR_SCRATCH_DIR>/viz-cache` | 转码产物、为转码取回的源文件；只在本地盘、不上 TOS，可丢 |
| `CURATOR_VIZ_CACHE_GB` | 20 | 缓存上限，超了按最近最少使用淘汰 |
| `CURATOR_VIZ_TRANSCODE_WORKERS` | 2 | 同时转码的路数 |
| `CURATOR_VIZ_CLIENT_DECODE` | `1` | mcap 的 H.264 / H.265 相机另出样本包，浏览器用 WebCodecs 自己解码，转封装改为按需（设计 19 §3）；`0` 回到扫描时就转封装 |
| `CURATOR_VIZ_SEGMENT` | `0` | `1`：LeRobot v3 / Lance 的相机由 Daemon 出这条 episode 的切片（`access: remux`，`.mp4?segment=1`），`moov` 在尾的单条 mp4 出 `moov` 在头的版本（设计 21 §4.3–§4.4）；`0` 照旧由浏览器直接读。转码不管开关如何都以切片为输入（TOS 上不再整块下载分块文件） |
| `CURATOR_VIZ_LANCE_S3_ENDPOINT` | 空 | 读 TOS 上 Lance 表走的 S3 兼容端点；空 = 按地区用 `tos-s3-<地区>`（内网部署用 ivolces），测试或代理时改。TOS 的端点按虚拟主机风格用，桶名由 Daemon 补进主机名（`<桶>.tos-s3-<地区>…`），这里不用写桶 |

## 手动验证步骤

在 `backend/` 下（仓库根的 `.venv`；worktree 里用主检出的 `.venv`）。

1. **造两份本地数据集**（v2.1：两路相机，`wrist` 是 mpeg4；v3.0：三条 episode 在一个数据文件里）：

   ```bash
   L=${TMPDIR:-/tmp}/curator-local && mkdir -p $L/inputs
   ../.venv/bin/python -c "from tests.viz.fixtures import make_v2, make_v3; make_v2('$L/inputs/viz_v2'); make_v3('$L/inputs/viz_v3')"
   ```

2. **起 Daemon**：`.claude/launch.json` 的 `curator-daemon-local`（`/curation` 前缀、不鉴权、本地数据根 `$L/inputs`），下面的 `B=localhost:8080/curation/api/v1`。

3. **登记并看展示模型**：

   ```bash
   D=$(curl -s -X POST -H 'Content-Type: application/json' $B/datasets -d '{"input":{"source":"local","uri":"viz_v2"}}' | jq -r .id)
   curl -s $B/datasets/$D/viz | jq '.format, [.cameras[] | {key, codec, access, reason}], [.streams[] | {key, name, smart}], [.annotation_sources[] | {key, format, primary}]'
   curl -s $B/datasets/$D | jq '.viz, .preflight.dataset.camera_info, .preflight.dataset.segment_sources'
   ```

   预期：`reader` 为 `lerobot`；`front` 是 `local`、`wrist` 是 `transcode`，原因写「原始编码 mpeg4……由平台转为 H.264」；曲线组 `observation_state`（状态与动作）与 `observation_state.gripper`（夹爪）
   是 `smart`，`observation_force` 不是；标注来源 `low_level_task_index`（机器人分步，primary）、`flags`（标志段）、`subtask`（文字列）、`episode:task_status`。
   登记详情的 `viz.state` 是 `ready`，预检里有 `camera_info`（wrist 的 `needs_transcode: true`）与 `segment_sources`。

4. **episode 列表**：`curl -s "$B/datasets/$D/viz/episodes?sort=duration&order=desc" | jq '[.items[].index], .total'` → `[2,0,1]`、`3`；`?q=ep1` 只剩 1。

5. **一条 episode 与曲线**：

   ```bash
   curl -s $B/datasets/$D/episodes/1/viz | jq '.duration_s, .frames, [.cameras[] | {key, access, url, transcode_url}], [.annotations.tracks[] | {key, primary, labels: [.segments[].label]}], .annotations.labels, .annotations.warnings'
   curl -s "$B/datasets/$D/episodes/1/series?stream=observation_state&points=100" | jq '.total_points, .downsampled, (.lines | length), .lines[0].name'
   ```

   预期：时长 2.4 s、24 帧；`front` 的 `url` 是 Daemon 的 `.mp4`、`transcode_url` 带 `?transcode=1`；机器人分步三段「reach / grasp / lift the cup」为 primary，标志段一段；
   条目标签 `task_status: recovered`；警告「标注格式不支持：subtask 列全是占位文字 TODO」。曲线 24 点、没抽稀、16 条线（8 维 × 状态 / 动作）。

6. **相机字节**：`curl -s -o /dev/null -w '%{http_code} %{size_download}\n' -H 'Range: bytes=0-99' $B/datasets/$D/episodes/0/cameras/front.mp4` → `206 100`；
   `curl -s -o /dev/null -w '%{http_code}\n' $B/datasets/$D/episodes/0/cameras/wrist.mp4` 第一次多半 `202`（`curl -s` 看正文：`{"state":"pending","progress":…,"message":"平台转码中"}`），
   过一两秒再请求是 `200`，存下来用 `ffprobe` 看是 h264。缓存目录 `$TMPDIR/curator-scratch/viz-cache/transcode/` 下多出这个文件。

7. **v3**：同样登记 `viz_v3`，`episodes/1/viz` 的相机带 `from_ts: 3.0`、`to_ts: 5.4`；轨 `subtask_index`（primary）与 `language_persistent` 都是三段；
   曲线 `total_points` 24（只读了这条的行组）。

8. **外部标注**：

   ```bash
   printf '{"event_labels":[{"t_s":0,"end_s":1,"verb_class":"reach","object":"cup"}],"key_events":[{"t_s":0.5,"label":"touch"}]}' > /tmp/episode_000001.json
   (cd /tmp && zip -q labels.zip episode_000001.json)
   U=$(curl -s -X POST -H 'Content-Type: application/zip' --data-binary @/tmp/labels.zip "$B/uploads?kind=viz_annotations&name=labels.zip" | jq -r .upload_id)
   curl -s -X PUT -H 'Content-Type: application/json' $B/datasets/$D/annotations -d "{\"upload_id\":\"$U\"}" | jq .annotations
   curl -s $B/datasets/$D/episodes/1/viz | jq '[.annotations.tracks[] | select(.key=="external")][0].segments[0].label, .annotations.events'
   ```

   预期：上传件 `summary.episodes` 为 1；数据集的 `annotations` 写着文件名与条数；episode 1 多一条「外部标注」轨（`reach · cup`）和事件 `touch`。
   不认识的 JSON（没有 `timeline` / `event_labels`）上传时 400，逐个文件说「不是认得的标注格式」。

9. **转码关掉**：用 `CURATOR_VIZ_TRANSCODE=0` 重启，`wrist` 的 `access` 变成 `unsupported`、原因写明开关，`?transcode=1` 回 404 `transcode_disabled`。

以下是 mcap（F13.3），Daemon 照常开着转码。

10. **造两份 mcap 数据集**（GenRobot 式：JPEG 相机、末端位姿、夹爪编码器、200 Hz IMU、metadata 里的任务；ABC-130k 式：H.265 与 H.264 两路
    `CompressedVideo`、`repeated double q` 的状态 / 动作、`/instruction`、分段 topic）：

    ```bash
    ../.venv/bin/python -c "from tests.viz.mcap_fixtures import make_umi, make_abc; make_umi('$L/inputs/viz_umi'); make_abc('$L/inputs/viz_abc')"
    ```

11. **登记后映射待确认**：

    ```bash
    M=$(curl -s -X POST -H 'Content-Type: application/json' $B/datasets -d '{"input":{"source":"local","uri":"viz_umi"}}' | jq -r .id)
    curl -s $B/datasets/$M | jq '.viz, .viz_mapping'
    curl -s $B/datasets/$M/viz | jq '.mapping, .warnings'
    curl -s $B/datasets/$M/episodes/0/viz | jq .error.details
    ```

    预期：`viz.state` 是 `mapping_pending`、`viz_mapping.state` 是 `none`；模型的 `mapping.state` 是 `none`、警告 `mapping_pending`；episode 回 400，`reason: mapping_pending`。

12. **探测与确认**：

    ```bash
    curl -s -X POST -H 'Content-Type: application/json' $B/viz/mcap-probe -d "{\"input\":{\"dataset_id\":\"$M\"}}" > /tmp/probe.json
    jq '.files, .matched, [.topics[] | {topic, schema, use, codec, rate_hz}]' /tmp/probe.json
    jq '{mapping: .draft}' /tmp/probe.json | curl -s -X PUT -H 'Content-Type: application/json' $B/datasets/$M/mapping -d @- | jq '.state, .version, .check_mapping'
    ```

    预期：2 个文件，命中 `builtin:umi`（覆盖率 1）；相机 `jpeg`，IMU 的用途是 `ignore`。确认后是第 1 版，`check_mapping` 的 action 是 `eef_pose` 的 `pose` 加编码器的 `value`，
    `action_names` 是 `robot0_x … robot0_qw, robot0_gripper`，`profile: umi_das`。再 `PUT` 一次版本加一；把一路相机改成不存在的 topic 再 `PUT`，400，`details.errors` 逐条说哪里不对（「数据集里没有 topic …」）。

13. **episode、帧包、曲线**：

    ```bash
    curl -s $B/datasets/$M/episodes/1/viz | jq '.timeline.frame_reference, (.timeline.frame_times | length), .task, [.cameras[] | {key, access, url, index_url, offset_s}]'
    curl -s $B/datasets/$M/episodes/1/cameras/robot0_sensor_camera0_compressed.json | jq '.count, .codec, .width, .height, .offset[2], .size[2]'
    curl -s -o /tmp/f2.jpg -w '%{http_code}\n' -H "Range: bytes=<offset[2]>-<offset[2]+size[2]-1>" $B/datasets/$M/episodes/1/cameras/robot0_sensor_camera0_compressed.frames
    curl -s "$B/datasets/$M/episodes/1/series?stream=robot0_vio_eef_pose" | jq '.total_points, [.lines[].name]'
    ```

    预期：帧号基准 `/robot0/vio/eef_pose`、20 帧，任务「tidy up 1」；相机 `access: frames`，`offset_s` 0.05（相机晚半拍）；帧索引 20 帧 64×48；区间读回 206，
    `/tmp/f2.jpg` 是一张 JPEG；曲线 20 点、7 条线 `robot0_x … robot0_qw`。第一次打开某条 episode 要扫一遍文件，缓存目录 `viz-cache/mcap/` 下多出它的目录。

14. **H.265 / H.264 重封装**：同样登记 `viz_abc`。它没有 `/action`，质检按缺省读不了：登记的 `format` 是 `unsupported`，`viz.state` 仍是 `mapping_pending`，
    `GET /datasets/<id>/mapping` 的警告 `checks_unreadable`。探测出的草稿命中不了内置的 UMI，由 `builtin:foxglove` 起草（`/left-arm-state` 与 `/left-arm-action` 自动配对，
    任务来自 `/instruction`，分段来自 `/subtask`），探测应答带两条 `checks_gap`（质检读不出 `q` 这个 repeated 字段、读不了 H.265）。确认草稿后登记按新映射重新预检，
    `viz.state` 变成 `ready`，应答仍带这两条 `checks_gap`。`episodes/0/viz` 的两路相机都是 `access: remux`，`camera_top` 的 `codec_string` 是 `hvc1.1.6.L120.90`；
    `curl -s -o /tmp/top.mp4 $B/datasets/<id>/episodes/0/cameras/camera_top.mp4 && ffprobe -v error -show_entries stream=codec_name,codec_tag_string,nb_frames /tmp/top.mp4`
    → `hevc`、`hvc1`；带 `Range: bytes=0-31` 回 206。曲线组 `left-arm-state` 是状态与动作叠画的 12 条线，状态 40 点（相机两倍的频率）；分段轨一段 `reach`（0–1 s）。

15. **模版库**：`curl -s $B/viz/templates | jq '[.items[].id]'` 是三个内置模版；把 14 步的草稿另存：

    ```bash
    curl -s -X POST -H 'Content-Type: application/json' $B/viz/templates -d "{\"name\":\"ABC 双臂\",\"mapping\":$(jq .draft /tmp/probe_abc.json)}" | jq .id
    ```

    （`/tmp/probe_abc.json` 是 14 步探测的应答。）同名再存回 409 `name_taken`；再探测 `viz_abc`，`matched.template_id` 变成这个模版；`DELETE /viz/templates/<id>`（带 JSON 头）回 204，
    删内置模版回 400。没登记的目录也能探测：`-d '{"input":{"source":"local","uri":"viz_abc"},"template":"builtin:ros2"}'`。

16. **质检用上映射**：在控制台对 `viz_umi` 建一个只选数值模块的任务并开始。运行目录 `$L/data/runs/<task_id>/run.json` 的 `viz_mapping` 带着版本 1、映射原文与
    `check_mapping`；任务日志的调试行（`GET /tasks/<task_id>/logs?level=debug`）`run: curation preflight …`、`run: curation check …` 都带 `--set ingest.mcap_mapping=…`。之后再改映射，这个任务的 `run.json` 不变，任务级的
    `GET /tasks/<task_id>/episodes/0/viz` 仍按第 1 版出，且带 `check_clock`（`offset_s` 0、`fps` 10）。

17. **整套样本集**（F13.8，设计 18 §9.6）：`scripts/viz_sample_check.py` 对着一个运行中的 Daemon 逐个子集像「可视化」页那样打开：登记（本地挂载；
    mcap 按探测起草的映射原样确认）、数据集模型、episode 列表、首尾两条 episode 的记录、每路相机经播放器用的地址解出首帧（本地文件或预签名地址按 `from_ts` 定位；
    转封装 / 转码等 202 结束；帧包读索引与第一张 JPEG）、每组曲线按 1200 点；首条再开一次量热缓存；最后量缓存目录。每个子集一行 JSON，另写一份汇总：

    ```bash
    ../.venv/bin/python scripts/viz_sample_check.py --api http://127.0.0.1:8080/curation/api/v1 \
        --cache-dir $CURATOR_VIZ_CACHE_DIR --out /tmp/viz_check.jsonl /path/to/samples/*/*
    ```

    全部子集通过时退出码 0；失败的子集、相机与曲线在汇总的 `failed` 里写明原因。

18. **Lance（设计 19 §4，F14.3）**：把一份本地 LeRobot 数据集转成 lerobot-lancedb 的三种布局（脚本照上游三版转换器写表，不依赖 lerobot）：

    ```bash
    ../.venv/bin/python scripts/make_lance_dataset.py $L/inputs/viz_v3 $L/inputs/lance_03 --layout 0.3
    ../.venv/bin/python scripts/make_lance_dataset.py $L/inputs/viz_v3 $L/inputs/lance_02v --layout 0.2-video --name lift
    ../.venv/bin/python scripts/make_lance_dataset.py $L/inputs/viz_v2 $L/inputs/lance_02f --layout 0.2-frames --name lift
    ```

    三份都登记成本地数据集：`lance_03` 的格式是 `lance`，两份 0.1–0.2 的预检当成 LeRobot v3（只有 `meta/` 被认出来），列表里三份的 `viz.state` 都是 `ready`。
    `curl -s $B/datasets/$D/viz | jq .format` 分别是 `{"kind":"lance","version":"v3","reader":"lance","layout":"lance-0.3"}`、`…"lance-0.2-video"`、`…"lance-0.2-frames"`；
    前两份的相机 `access` 是 `blob`，`episodes/1/viz` 里相机的 `from_ts` / `to_ts` 与 `viz_v3` 的同一条一样（一个 mp4 装三条 episode），
    `curl -s -H 'Range: bytes=0-99' $B/datasets/$D/episodes/1/cameras/<相机>.mp4 | cmp - <(head -c 100 $L/inputs/viz_v3/videos/<特征>/chunk-000/file-000.mp4)` 无输出；
    曲线 `series?stream=observation_state` 与 `viz_v3` 的逐点相同。第三份的相机是 `frames`，`.json` 的 `count` 等于这条的帧数、`codec` 是 `jpeg`，按 `offset` / `size` 取的每段都是 `FF D8` 开头。
    字段树最后一组「Lance 表」列出各表的行数与列。把 `lance_03` 的 `meta/` 挪走（只留三张表）再看一遍：元数据从 `meta.lance` 读，`viz/meta?path=meta/info.json` 里有 `"storage_format": "lance"`。
    TOS 上的 Lance 数据集按 S3 兼容端点读，自动化测试里用一个最小的本地 S3（`tests/viz/fake_s3.py`）跑同样的流程。
    有 TOS 密钥时登记一份 TOS 上的 0.3 数据集（如 `tos://galbot/so101-pick-place-lance/`）：`/viz` 的 `format.layout` 是 `lance-0.3`，episode 能开，相机 `Range: bytes=0-31` 返回 206、第 5–12 字节是 `ftypisom`。
    把一份 0.3 副本的 `meta/episodes/chunk-000/file-000.parquet` 换成 Git LFS 指针文本（`version https://git-lfs.github.com/spec/v1` 开头）再登记：
    `/viz` 与 `/viz/episodes` 是 404，`message` 写「…file-000.parquet 是 Git LFS 指针文件（N 字节的占位），不是数据…」（以前是 500）。

19. **浏览器内解码（设计 19 §3，F14.2）**：缺省开着（`CURATOR_VIZ_CLIENT_DECODE=1`）。第 14 步的 `viz_abc` 确认映射后打开一条 episode：
    `curl -s $B/datasets/$D/episodes/0/viz | jq '.cameras[] | {key, access, samples_url, index_url}'` 每路都是 `remux`，另有 `samples_url`（`.frames`）与 `index_url`（`.json`）；
    `curl -s $B/datasets/$D/episodes/0/cameras/camera_wrist.json | jq '{codec, count, codec_string, k: .key[:3], config: (.config|length)}'` 是 `h264`、20 帧、`avc1.64…`、`[true,false,false]`、一段 base64；
    这时缓存目录里有 `camera_wrist.annexb`、还没有 `camera_wrist.mp4`。`curl -s -o /tmp/w.mp4 $B/datasets/$D/episodes/0/cameras/camera_wrist.mp4` 才转封装（之后 `episode.json` 里这路 `mp4: true`），
    `ffprobe /tmp/w.mp4`（或 PyAV）有 20 帧。用 `CURATOR_VIZ_CLIENT_DECODE=0` 重启：相机没有 `samples_url`，`.json` 回 404，扫描时就转封装，与阶段 13 一样。

20. **EEF 标记叠加（设计 20、22 §3）**：跑一条勾了「EEF–视频一致性」的任务（dataset2 见前端 README「EEF 轨迹叠加」；UMI 数据见 EEF extension README 的 `export-umi`）。
    `curl -s $B/tasks/$T/episodes/0/eef-overlay | jq '.cameras[] | {camera_id, viz_camera, times: .times_s[:4], hands: [.hands[].title], layers: [.layers[] | {id, group, kind, default_on, in_model}]}'`：
    `viz_camera` 等于 `curl -s $B/tasks/$T/episodes/0/viz | jq '[.cameras[].key]'` 里的一项；`times_s` 是每个样本帧所配画面帧在播放器里的时刻（LeRobot 为帧号 / fps；mcap 为该消息在扫描里的时间，
    首个关键帧之前为 null，与这一路的 `offset_s` 对得上）；普通 EEF 有 `point`、`finger_axis`、`axis`（`default_on: false`）、`axis_x/y/z`、`trail_past`、`trail_future`，
    给了夹爪参考的任务再多 `observed_point`、`observed_trail`、`residual`；`layers[].frames` 的长度等于这条的样本帧数。
    `ls $D/data/runs/$T/checks/eef_video_consistency/` 没有 `opinion/`。没勾 EEF 的任务回 404，`error.details.reason` 是 `no_eef_module`。

21. **转码的时间口径（设计 21 §4.5，F15.1）**：把第 1 步的 `viz_v3` 复制一份 `viz_v3_mpeg4`，`meta/info.json` 里 `observation.images.top` 的 `video.codec` 改成 `mpeg4`（字节仍是 H.264，转码器照样读）后登记。
    `curl -s $B/datasets/$D/episodes/1/viz | jq '.cameras[] | {key, access, from_ts, to_ts}'` 是 `transcode`、`0`、`2.4`（这条 24 帧）；等 `.mp4` 转好（202 期间带进度）后
    解出 24 帧，第一帧白竖线在 x = 26（共用文件里第 30 帧的位置），即这条 episode 的第一帧。原样的 `viz_v3` 第 2 条的 `transcode_url` 同理从这条的第一帧开始。
    删掉 `viz_v3_mpeg4` 的登记后，缓存目录 `transcode/` 下它的产物目录随之消失。

22. **切片（设计 21 §4，F15.3）**：用 `CURATOR_VIZ_SEGMENT=1` 重启 Daemon。`viz_v3` 第 1 条：
    `curl -s $B/datasets/$D/episodes/1/viz | jq '.cameras[] | {key, access, url, from_ts, to_ts}'` 是 `remux`、`…/top.mp4?segment=1`、`from_ts` 为这条在切片里的起点（0.1 左右，切片从它前面最近的关键帧起）、
    `to_ts − from_ts = 2.4`；`curl -s -o /tmp/s.mp4 "$B/datasets/$D/episodes/1/cameras/top.mp4?segment=1"` 后用 PyAV 解出约 25 帧，`from_ts` 处那帧白竖线在 x = 26；
    `python3 -c "import struct;d=open('/tmp/s.mp4','rb').read(64);print(d[4:8], d[struct.unpack('>I',d[:4])[0]+4:][:4])"` 打出 `b'ftyp' b'moov'`（索引在前）。
    `viz_v2` 的 `front`（PyAV 写的 mp4，`moov` 在尾）也是 `remux`、`from_ts` 为空，取回的文件 `moov` 在前、帧数不变。缓存目录里多了 `segment/`。不设开关时这两路照旧 `local`，`?segment=1` 回 404 `segment_disabled`。
    切一条要读多少：`../.venv/bin/python scripts/viz_slice_reads.py $L/inputs/viz_v3`（或 `tos://桶/前缀`，TOS 密钥照命令行的输入角色放在环境变量里）按 Daemon 的读法切头、中、尾三条，
    逐条列出文件大小、读的字节与 GET 数、切片大小与耗时——读的字节应只比切片多一两 MB（两头各一块与文件头），而不是整个文件。

23. **深度图（设计 21 §5，F15.4）**：`../.venv/bin/python -c "from tests.viz.fixtures import make_v3_depth; make_v3_depth('$L/inputs/viz_depth')"` 后登记。
    `curl -s $B/datasets/$D/viz | jq '.streams[] | select(.kind=="depth") | {key, name, depth}'` 有两路：`observation_images_front_depth`（配对 `front`）与
    `observation_depths_top`（float32 米，没有同名相机，`pair_camera` 为 null）；`curl -s $B/datasets/$D/episodes/1/viz | jq '.streams'` 列出两路的 `.frames` / `.json`。
    第一次 `curl -si $B/datasets/$D/episodes/1/streams/observation_images_front_depth.json` 是 202（`深度图生成中`，带进度），稍后 200：`codec: png16`、9 帧、`depth.unit: mm`；
    用索引里第 0 帧的 `offset` / `size` 按 Range 取出那一段存成 `f0.png`，`python3 -c "from PIL import Image;import numpy as np;print(np.asarray(Image.open('f0.png'))[10,20])"`
    打出 `834`（`500 + 10·20 + 5·10 + 7·12`，与 parquet 第 12 行同一个值）。缓存目录里多了 `depth/`。

24. **mcap 深度图（设计 21 §5.4，F15.5）**：
    `../.venv/bin/python -c "from tests.viz.mcap_fixtures import make_rgbd; make_rgbd('$L/inputs/viz_rgbd')"` 造一份 12 帧的合成 RGB-D（深度的四种写法：
    16 位 PNG、16UC1、32FC1 米、ROS compressedDepth；还有一路 rgb8 原始图与一路点云），登记后 `POST $B/viz/mcap-probe`（`{"input": {"dataset_id": "<编号>"}}`）：
    `/front-depth`、`/raw-depth`、`/float-depth`、`/wrist/depth/compressedDepth` 的 `use` 都是 `depth`，`image.codec` 依次 `png16`、`raw16`、`raw32f`、`cdepth`；
    `/raw-color` 是 `camera`、注「原始图像（raw）本期不支持」，`/cloud` 是 `ignore`。草稿是 `viz-mapping/1.1`，`depths` 四路，`/front-depth` 配 `/front-camera`、
    `/raw-depth` 配 `/raw-color`（主干都空），另两路不配。把草稿 PUT 到 `$B/datasets/$D/mapping` 确认后，`$B/datasets/$D/episodes/0/viz` 的 `streams` 四路，
    `.json` 直接 200（深度随 episode 一遍扫描生成，没有 202），`codec: png16`、12 帧、`depth` 范围 630–1337；四路各按索引取第 5 帧存成 PNG，
    `[10, 20]` 处都是 `785`（`500 + 10·20 + 5·10 + 7·5`，32FC1 由米换成毫米、compressedDepth 去掉 12 字节头）。
    真数据：h200-14 上 RoboMIND 的 `/data08/yichen/dataset/raw/Voxel51__RoboMIND/data/ur/1018_102225/episode.fo.mcap`（40 MB）拷成
    `$L/inputs/robomind_ur/episode_0.mcap` 后登记，详情页「mcap 配置」里 `/top-depth` 起草为「深度图」、「叠放的相机」是 `/top-camera`，确认后可视化页
    「+」→「深度图」→ `top-depth`：伪彩，色标 649–2425 mm；悬停 (320, 240) 是 1119 mm，与 PIL 解 mcap 里第一条 `/top-depth` 的 PNG 同一像素一致；
    「设置」里「叠在 RGB 上」后抽屉、机械臂的轮廓与相机对得上，连播时深度与相机同帧。

25. **展示配置（设计 21 §6，F15.6）**：对第 1 步的 `viz_v2`：`curl -s $B/datasets/$D/viz/display | jq '{config, version, g: [.defaults.groups[].key], n: (.defaults.dimensions | length), t: [.defaults.tracks[].key]}'`
    是 `config: null`、`version: 0`、三组自动分组与它们的全部维度、分段类的标注来源。存一份：
    `curl -s -X PUT $B/datasets/$D/viz/display -H 'Content-Type: application/json' -H 'Idempotency-Key: display-try-1' -d '{"config": {"cameras": [{"key": "wrist", "name": "腕部"}, {"key": "front", "hidden": true}], "curves": {"groups": [{"key": "arm", "name": "arm", "unit": "rad", "smart": true, "lines": [{"source": "observation.state", "dim": 0, "name": "j0", "role": "state"}, {"source": "action", "dim": 0, "name": "j0 cmd", "role": "action"}]}]}, "track": "flags"}}' | jq .version`
    是 1；`$B/datasets/$D/viz` 的相机依次是「腕部」与 `front`（`hidden: true`），曲线组只剩 `arm`（单位 rad），字段树里 `observation.state` 与 `action` 指向它，`annotation_sources` 里 `flags` 是主轨；
    `$B/datasets/$D/episodes/1/series?stream=arm` 两条线的数值与 parquet 的 `observation.state[0]`、`action[0]` 一样。把相机写成 `top` 再 PUT 回 400
    `validation_failed`，`details.errors` 是 `cameras.0.key：数据集里没有相机 top`。`curl -s -X DELETE $B/datasets/$D/viz/display -H 'Idempotency-Key: display-try-2' | jq '{config, version}'`
    是 `null`、2，模型回到自动分组。控制台上：可视化页「布局 → 保存为缺省布局」后刷新、换一个浏览器打开都是这个布局。

## 自动化测试

```bash
../.venv/bin/python -m pytest -q tests/viz        # 内核单测与接口测试（含 mcap），约 20 秒
```
