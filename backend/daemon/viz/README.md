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
| `media.py` | 磁盘缓存（`CURATOR_VIZ_CACHE_DIR`，LRU，上限 `CURATOR_VIZ_CACHE_GB`）、转码任务池（子进程 `python -m curation.viz.transcode`，`CURATOR_VIZ_TRANSCODE_WORKERS` 路，不占质检的 CPU 名额池）、带 Range 的本地文件应答 |
| `service.py` | 按格式挑读取器、内存缓存（按指纹）、相机地址（直连预签名 / Daemon 路由 / 转码兜底）、外部标注文件的解析 |
| `../routes/viz.py` | 路由：数据集级的模型、episode 列表、元数据预览、episode、曲线、相机 `.mp4`、外部标注；任务级的模型、episode、曲线（任务级的 `.mp4` 在 `routes/results.py`） |

内核（`backend/curation/viz/`）：`lerobot_info.py`（features、names 的几种写法、相机编码与 `needs_transcode`、RFC 6381 编码串）、`groups.py`（曲线分组 §5.3）、
`annotations.py`（标注识别 §4.5：`subtask_index`、`language_*`、逐帧 `task_index`、`*_index` 查表、文字列、`*_segment` 布尔段、成败 / 质量 / 评分 / `task_status`、Argus 外部标注）、
`series.py`（按行组读 episode 的列、min / max 抽稀）、`transcode.py`（PyAV 转 H.264 fMP4）。

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

## 自动化测试

```bash
../.venv/bin/python -m pytest -q tests/viz        # 内核单测与接口测试，约 10 秒
```
