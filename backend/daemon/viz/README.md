# 数据可视化（Daemon 侧，阶段 13，设计 18）

读取器把数据集读成统一展示模型（C4 2.4.0 的 `VizDataset` / `VizEpisode` / `VizSeries`），网页控制台的完整版（`/visualize`）与迷你版（报告、裁决、任务详情里的弹窗）只认这个模型。
数据集级接口读登记，任务级接口读任务冻结的输入（D27），两组共用读取器。格式层的逻辑（特征、相机编码、曲线分组、标注识别、曲线读取与抽稀、转码）在内核
`backend/curation/viz/`，预检（`curation preflight`）与 Daemon 用的是同一份。

## 文件

| 文件 | 内容 |
|---|---|
| `status.py` | 登记能不能可视化（`DatasetItem.viz`）、mcap 映射的状态（`DatasetDetail.viz_mapping`） |
| `source.py` | 数据源：数据集级（登记的地址、密钥、文件清单、映射、展示配置、外部标注）与任务级（任务冻结的输入、`preflight.json`、`source_manifest.json`、`run.json` 里的映射）；打开存储、签浏览器地址、本地文件只在数据集目录之内 |
| `lerobot.py` | LeRobot v2 / v3 读取器：`meta/` 的 episode 表、相机、曲线组、标注来源、字段树；一条 episode 的逐帧列只读一次（v3 只读它的行组）进缓存，episode 记录与曲线请求共用 |
| `mcap.py` | mcap 读取器（设计 18 §6）：按确认的映射（C7）出展示模型；一条 episode 只顺序读一遍，产物（帧包、重封装的 mp4、曲线 `series.npz`、`episode.json`）落在磁盘缓存，按数据集指纹与映射版本分目录；探测结果按指纹缓存，前三个文件的 topic 不一致时警告 |
| `media.py` | 磁盘缓存（`CURATOR_VIZ_CACHE_DIR`，LRU，上限 `CURATOR_VIZ_CACHE_GB`）、转码任务池（子进程 `python -m curation.viz.transcode`，`CURATOR_VIZ_TRANSCODE_WORKERS` 路，不占质检的 CPU 名额池）、带 Range 的本地文件应答 |
| `service.py` | 按格式挑读取器、内存缓存（按指纹）、相机地址（直连预签名 / Daemon 路由 / 帧包 / 转码兜底）、外部标注文件的解析；mcap 的探测、映射的校验与保存、模版库 |
| `../routes/viz.py` | 路由：数据集级的模型、episode 列表、元数据预览、episode、曲线、相机 `.mp4|.frames|.json`、外部标注、映射；探测与模版库；任务级的模型、episode、曲线、帧包（任务级的 `.mp4` 在 `routes/results.py`） |

内核（`backend/curation/viz/`）：`lerobot_info.py`（features、names 的几种写法、相机编码与 `needs_transcode`、RFC 6381 编码串）、`groups.py`（曲线分组 §5.3）、
`annotations.py`（标注识别 §4.5：`subtask_index`、`language_*`、逐帧 `task_index`、`*_index` 查表、文字列、`*_segment` 布尔段、成败 / 质量 / 评分 / `task_status`、Argus 外部标注）、
`series.py`（按行组读 episode 的列、min / max 抽稀）、`transcode.py`（PyAV 转 H.264 fMP4）；mcap 的 `mcap_messages.py`（解码、数值叶子与字段路径、
画面编码与尺寸、显示用的变换）、`mcap_probe.py`（summary 加每个 topic 的首条消息）、`mcap_mapping.py`（三个内置模版、起草与按覆盖率匹配、校验、派生质检映射
`check_mapping`）、`mcap_episode.py`（一条 episode 的一遍扫描）、`remux.py`（H.264 / H.265 Annex-B 流拷贝成 fMP4，H.265 标 `hvc1`）。

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

## 自动化测试

```bash
../.venv/bin/python -m pytest -q tests/viz        # 内核单测与接口测试（含 mcap），约 20 秒
```
