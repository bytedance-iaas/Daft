# 14 数据完整性检查

> 状态：**定稿**（2026-09-24，需求方逐条确认）；F7.1–F7.4 已实现，§3–§8 已按实现更正。决策见 `00-overview.md` §7 的 D50–D52。
> 来源：需求方提出预检的两处空缺——「没有通用的 CRC / 文件损坏检测」「没有按采集协议核对一次采集应有多少文件、多少路传感器流」；
> 讨论后定为：预检保持精简，只加几项不多读数据的警告；读数据的重活放进质检里新开的「数据完整性」模块。
> 采集协议与采集端校验和清单要用户额外提供，本期不做（§9）。

## 给实施 agent 的开工指引（先读这一节）

1. **分支与纪律**：直接在 `feat/curator-v2` 上做。A 类目录（`core/`、`registry/`、`ingest/`、`dataset_level/` 等，见 `CLAUDE.md`）一行不改：
   本模块要用的 v1 函数（`ingest/validate.py` 的 `validate_episode_row`、`stats_prior_warnings`）只调用、不修改。新代码放
   `backend/curation/extensions/integrity/`。每个 feature 一个 commit；提交前在 `backend/` 下跑
   `../.venv/bin/python -m pytest -q curation/tests --ignore=curation/tests/test_environment.py`、`../.venv/bin/python -m pytest -q tests`，
   再在仓库根跑 `PYTHONPATH=tools .venv/bin/python -m pytest -q tools/parity/tests`。提交信息英文，不加模型 co-author。
   台账 `feature_list.md`（F7.x）与 `claude-progress.txt` 只在本地更新，不入库。
2. **读什么**：本篇 → `05-modules-and-preflight.md` §3（预检）与 §7（加一个模块要改哪些地方）→ `02-cli-contract.md` §3.5（`check`）→
   `04-concurrency-and-vlm-merge.md` §2（分档与并发）→ 设计 12 里 EEF 模块接入判决与裁决线的做法（D49，本模块照着接）。
3. **顺序**：F7.1 预检三项 → F7.2 注册表、新档与判决接入（先用一个只做 L1 的最小实现跑通全链路）→ F7.3 数据集级、L1、L2 →
   F7.4 L3 逐帧解码 → F7.5 故障样本验收与集群实测。
4. **不做**：采集协议对账、采集端校验和清单（§9）；预检里不再加别的检查；不改抽帧档的解码方式。

## 0. 摘要

### 0.1 一句话

预检多三条不多读数据的警告；质检多一个默认勾选、排在最前面的硬门「数据完整性」，读文件结构、整读做 CRC 与零填充检查、
把 v1 的逐条结构校验挪到这里给出明确原因，必要时逐帧解码；有问题的 episode 判废或标为可疑，别的条目照常往下走。

### 0.2 决策（需求方 2026-09-24 确认）

| # | 决策 |
|---|---|
| D50 | 新增「数据完整性」模块：新开 `integrity` 档，排在漏斗最前，硬门，默认勾选（进「完整」「快速」两个预设），可以取消。逐条给出通过、判废或可疑。判废只落在有问题的 episode 上，不可复议；可疑的留在 passed，进裁决线「完整性存疑」，与其它落在 passed 的裁决线一样计入待裁，照 D24 先交付；基础设施原因读不到按 D33 记 error。逐帧解码测试是模块参数，默认关。采集协议与采集端校验和清单本期不做 |
| D51 | 勾选了数据完整性时，`validate_episode_row` 不通过的条目由它判废并写明原因，不再走「读行失败 → error → held」；不勾选时照旧。源文件缺失照 D40 跳过 |
| D52 | 预检新增三项（0 字节或过小的文件、mcap 录制中断、mcap 摘要区 CRC），只写进 `warnings`，不改变任何模块的可用性，也不多读样本 |

### 0.3 现状与本篇补上的空缺

| 检查 | 现状 | 本篇 |
|---|---|---|
| meta 可解析、`info.json` 必需字段、数据与视频文件在不在 | 预检已有；缺文件的条目按 D40 跳过 | 不变 |
| 0 字节、过小的文件 | 无 | 预检警告（§1） |
| mcap 录制中断、摘要区 CRC | 预检只说「没有摘要区，质检时整读」 | 预检警告（§1）；质检里 L2 给出结论 |
| 零填充、格式标识、parquet 尾部元数据、mp4 的 moov | 代码现成（`cli/verify.py` 的 `content_problem`），但只查交付物 | L1（§3.2），复用那段代码 |
| 帧数、行数与 episode 长度对账 | 无 | L1 |
| 文件中段零填充、mcap 数据块与数据区 CRC | 无 | L2（§3.3） |
| NaN/Inf、时间戳、各列帧数、视频时间段（v1 的 `validate_episode_row`） | 读行时失败 → error → held | L2，判废并写明原因（D51） |
| 解码失败 | 抽帧档对通过数值档的条目解码，抛异常才记 error | 抽帧档不变；L3 可选（§3.4） |
| v3 episode 表自洽、多余文件、内容相同的文件、近乎全黑的相机 | 无（近黑相机提示 v1 有，v2 没接上） | 数据集级（§3.1） |
| mcap 某条缺一路别的条都有的 topic、某 topic 频率明显偏低 | 预检只有「topic 与第一条不同」的警告 | 与同数据集多数条目比（§3.5） |

## 1. 预检新增三项（F7.1，D52）

预检仍然只读 metadata（D6、05 篇 §3）。三项都用已经在手的数据，只写进 `warnings`（字符串，C2 不变），不改变模块可用性：

| 项 | 读什么 | 警告写法（示意） |
|---|---|---|
| 0 字节或过小的文件 | 文件列表里的大小：`data/`、`videos/` 下的文件与 `*.mcap`；小到放不下该格式固定字节的也算（parquet 小于 12 字节、mcap 小于 45 字节、mp4 小于 512 字节——放不下 ftyp 加一个视频轨的 moov）。不用 v1 判「放不了」的 4 KiB：几帧低分辨率的合法短片就只有 3 KB | `2 files are empty or too small to be valid (2 episodes: 2, 5; videos/…/episode_000002.mp4 0 B, videos/…/episode_000005.mp4 300 B)` |
| mcap 录制中断 | 读摘要区失败的文件，看最后 8 字节是不是 mcap 结束标识（远端时这段在 `RangeFile` 取回的尾部里，本地多读 8 字节） | `1 episode (4) was cut off while recording (no mcap end marker); the checks read what is there` |
| mcap 摘要区 CRC | footer 里的 `summary_crc` 非 0 时，对摘要区到 footer `summary_offset_start` 字段为止的字节算 crc32（这些字节已经取回来了） | `1 episode (6) has a summary section that fails its CRC; the topics and counts read from it may be wrong` |

改动落在 `cli/preflight.py` 的 `_fill_supported`（LeRobot）与 `cli/preflight_containers.py`、`cli/containers.py` 的
`read_summary`（mcap：`McapSummary` 多两个字段 `truncated`、`summary_crc_ok`；footer 由 `read_footer` 自己解析，mcap 库不校验
摘要区 CRC）。lance 的视频在表里，不查第一项。原来那条「没有摘要区」的警告不再重复录制中断与空文件的条目。控制台把三条译成中文
（`frontend/src/lib/integrity.ts` 的 `warningText`）。

## 2. 数据完整性模块（F7.2）

### 2.1 注册表条目（C1 1.11）

| 字段 | 值 |
|---|---|
| `id` / `name_zh` | `data_integrity` / 数据完整性 |
| `summary_zh` | 检查每条 episode 的文件是否完整、可读：结构、零填充、CRC、逐条结构校验，可选逐帧解码 |
| `level` / `gate` / `stage` | `episode` / `hard` / `integrity`（新档） |
| `needs` | `raw_bytes`（总是满足） |
| `depends_on` | 无 |
| `review_lines` / `appealable` | `integrity_check` / `False`（同 D42：结构硬门的拒绝是终局） |
| `input_scope` / `affects_dataset_verdict` | `funnel` / `True` |
| `param_schema` | 一个参数 `decode_test`（§5.3） |

另加一个声明位，替代现在按 `eef_input` 认「v2 自己跑的硬门」的写法（`registry.native_ids()`、`aggregate`、`reporting`、`check` 里写死的 EEF）：
`ModuleSpec.native = True` 的模块由 v2 自己执行，`aggregate` 在调用边界把它的门加进 v1 的判决配置；`native_ids()` 改为读这个位。
EEF 模块与本模块都置位。

### 2.2 新档 `integrity`

- `STAGE_ORDER` 变为 `integrity → numeric → frame → vlm → post_verdict → profile_vlm`。本模块是这一档唯一的模块。
- 它主要耗 I/O：计划里是一个 `cpu` 档，并发取计划的 CPU 并发（站点默认 8），不参与数值档、抽帧档之间的 CPU 份额划分
  （`cpu_shares_for` 只分这两档）；流水线给每层定宽时，没有份额、也没有 VLM 闸门的层取它自己的 `concurrency`
  （`daemon/orchestr/episode_pipeline.py`；原来会退成 1）。开了 L3 时这一档也吃 CPU，与抽帧档叠加，由计划的并发上限兜着；
  L3 默认关，本期不再细分。
- 流式漏斗（设计 13）里它是第一层：一条过了就交给数值档，不等整批。判废的条目不进后面的档（硬门，与现有漏斗同一机制）。
- planner 的耗时估算（`planner/estimates.py`）加这一档：每条按 0.5 秒（L1 + L2，§6 的量级）÷ 并发；L3 不计入，计划的说明里写明。

### 2.3 怎么读数据

- 文件访问一律经 `cli/storage.py` 的 `Storage`（本地与 TOS 同一接口：`list`、`read_range`、流式读）。TOS 列文件时保留对象的
  CRC64（`hash_crc64_ecma`，现在被 `_iter` 丢掉），`ObjectInfo` 加 `crc64` 字段，`source_manifest` 不变。
- **mcap 在 TOS 上**：用现有源缓存（F5.13：每个 episode 文件只下载一次）。本档是第一个读的，由它触发下载，后面的档直接用本地副本，
  所以 L2 对 mcap 不增加 TOS 流量。
- **LeRobot 在 TOS 上**：L2 把每个文件流式读一遍（不落盘）；抽帧档仍按现在的方式经预签名 URL 读，所以 TOS 读流量约多一倍。
- **LeRobot v3**：一个数据文件、一个视频文件装多条 episode。按文件做一次、结果按条分发：进程内按对象键记忆结果，同一文件加锁，
  第一条触发读取，同文件的其它条复用；判定时再按每条的时间段或行区间决定受不受影响（§4.3）。
- **行数据**：L2 的逐条结构校验用本模块自己的读行（`extensions/integrity/rows.py`，§3.3）：v1 的同一套读取函数，不先解析数据集语义。
- **文件结构**：与 `cli/verify.py` 查交付物是同一思路（文件头、标识、尾部元数据），但本模块要拿到帧数、行数和每帧的时间，所以在
  `extensions/integrity/files.py`、`mp4.py` 里另写，不改 `verify`。

### 2.4 结果记录

`result-record` 的 `details` 本来就是按模块自定的对象。本模块的写法：

```json
{
  "outcome": "reject",
  "reason": "文件被截断：wrist 相机的视频在 12.3 MB 处结束，moov 缺失",
  "tiers": {"L1": true, "L2": true, "L3": false},
  "findings": [
    {"level": "reject", "code": "file_truncated", "tier": "L1",
     "file": "videos/chunk-000/observation.images.wrist/episode_000017.mp4", "camera": "wrist",
     "message": "文件在 12.3 MB 处结束，找不到 moov", "args": {"size": 12303311}}
  ],
  "files": [
    {"file": "videos/chunk-000/observation.images.wrist/episode_000017.mp4", "size": 12303311,
     "tiers": ["L1"], "crc": null}
  ]
}
```

- `passed`：有 `reject` 级发现 → `False`（`verdict = fail`）；只有 `suspect` 级 → `None`（`verdict = abstain`）；都没有 → `True`。
- `reason` 取第一条最高级别的发现，报告与裁决卡片直接显示它。
- `crc`：这个文件用什么校验过（`mcap_chunk` 或 `null`），报告据此统计「CRC 覆盖了多少文件」；`files` 里另有帧数、行数，
  v3 共用视频还有本条的时间段 `window_s`。位置确定的发现带 `span_s`（文件自己的时间轴，秒；`null` 表示到文件末尾）。
- 数据集级的发现不写进逐条记录，写 `checks/data_integrity/dataset.json`（§3.1）。

## 3. 检查项

### 3.1 数据集级（本档第一次调用时算一次）

写 `checks/data_integrity/dataset.json`：`findings` 是数据集级发现（`level = dataset`），`episode_findings` 是由此落到具体条目上的
可疑项。范围是任务的选择（`--selection`），没给就是整个数据集（流水线常驻进程一批一批收条目，不能按第一批算）。

| 检查 | 读什么 | 结论 |
|---|---|---|
| LeRobot v3 episode 表自洽：`dataset_from_index` / `dataset_to_index` 首尾相接、从 0 开始，区间长度等于 `length`；各相机 `to_timestamp − from_timestamp` 与 `length / fps` 相差不超过容差 | `meta/episodes/*.parquet`（预检也读） | 区间重叠或空缺 → 数据集级 `table_overlap`，涉及的条目可疑 `table_inconsistent`；单条长度或时间段对不上 → 可疑 `table_inconsistent` |
| 多余文件：`data/`、`videos/` 下不被任何 episode 引用的文件 | 文件列表 | 数据集级 `orphan_files` |
| 内容相同的文件：两条 episode 的视频文件或 mcap 文件内容相同（LeRobot v2 与 mcap；v3 的文件本来就共用） | TOS 用列表里的 CRC64；没有就用单段上传的 ETag（MD5）+ 大小；本地路径用大小 + 头尾各 64 KiB 的 sha256；都拿不到就不比 | 涉及的条目都可疑 `duplicate_content`。parquet 的重复交给「精确去重」 |
| 近乎全黑的相机（v1 的 `stats_prior_warnings`，只调用） | `meta/stats.json` | 数据集级 `dark_camera` |

### 3.2 L1 结构（每个文件读头尾，几次小范围读取）

mp4 的结构自己解析（`extensions/integrity/mp4.py`）：顶层 box 逐个读头，moov 整个读回，再从视频轨的样本表（`stts`、`stsz`、`stsc`、
`stco` / `co64`）算出每一帧的解码时间、字节位置和大小。于是截断或零块落在哪个字节，就能换算成哪一秒——LeRobot v3 一个文件装多条，
要靠它判断波及哪几条。与 PyAV 在 moov 在前、在后和 AV1 三种文件上对过帧数。

| 格式 | 检查 | 结论 |
|---|---|---|
| 所有 | 0 字节，或小于该格式的最小字节数（§1） | 判废 `file_empty` |
| 所有 | 文件头 4 KiB 全零 | 判废 `zero_filled` |
| parquet | 头尾 `PAR1`、尾部元数据可解析 | 结尾没有标识 → 判废 `file_truncated`；读不出 → 判废 `structure_invalid` |
| mp4 | 顶层 box；moov；样本表 | 没有完整的 moov → 判废 `file_truncated`（文件被截断时）或 `structure_invalid`；有帧的字节落在文件末尾之外 → 判废 `file_truncated`，时间段从第一个缺失帧起；本条（v3 是本条的时间段内）的帧数与 episode 长度相差超过 `integrity.count_tolerance_frames`（缺省 1）→ 可疑 `count_mismatch` |
| mcap | 开头标识；footer 的摘要区 CRC；数据块索引的偏移 + 长度不越过摘要区起点；有无结束标识 | 摘要区 CRC 不符、索引越界、摘要区读不出 → 判废 `structure_invalid`；没有结束标识 → 录制中断，交给 L2 与读行判断（§3.3） |

### 3.3 L2 整读（每个字节读一遍）

| 检查 | 做法 | 结论 |
|---|---|---|
| mcap 数据块与数据区 CRC | mcap 读取器 `emit_chunks=True, validate_crcs=True` 读一遍，每个写了 CRC 的块解压后比对（没写 CRC 的块跳过、计数） | 不符 → 判废 `crc_mismatch`；读不下去（zstd / lz4 解压失败、记录损坏）→ 判废 `structure_invalid` |
| 中段零填充 | mp4 的 mdat、压缩过又没写 CRC 的 mcap 数据块：64 KiB 对齐的整块全零。压缩数据不会合法地出现这么长的零；parquet 不扫（未压缩的全零列会误报，交给读取）；写了 CRC 的 mcap 块靠 CRC | 判废 `zero_filled`（mp4 带时间段） |
| parquet 数据页 | pyarrow 整个读一遍 | 读不出 → 判废 `structure_invalid` |
| v1 的逐条结构校验（D51） | 本模块自己的读行（`extensions/integrity/rows.py`）：与漏斗的读行用同一套 v1 读取函数，但不先解析数据集语义——那一步要读前 100 条的数据，一条早期坏数据就会让所有条目都读不了，而且与 `validate_episode_row` 查的东西无关。查 action 非空、二维、浮点、没有 NaN/Inf；时间戳与 action 等长且严格递增；state 帧数一致；视频文件在、时间边界合法；fps 合法 | 不过 → 判废 `row_invalid`，理由原样用 v1 的报错；数据帧数与 episode 长度对不上 → 可疑 `count_mismatch` |
| mcap 录制中断 | 没有结束标识的文件 | v1 的读取器能读出内容 → 可疑 `cut_off`；读不出 → 判废 `file_truncated` |

读行失败时分三种：v1 的校验不过 → `row_invalid`；文件本身已经查出判废级的问题 → 以文件的结论为准；都不是（读取器的其它异常）→
执行出错。基础设施错误（TOS 超时、5xx、源缓存下载失败）不是结论：按 D33 记 incident，整条 `verdict = error`，待补跑；
但同一条里别的文件已经确定判废时，判废照旧（D35 的精神）。源数据在运行中变了照 D27 结束命令（退出码 6）。

### 3.4 L3 逐帧解码（模块参数 `decode_test`，默认关）

- 每路相机把本条的时间段从头到尾解码（LeRobot 直接读文件或预签名地址；mcap 读 v1 读取器转出的本地视频），只解码、不转 RGB。
- 实测：PyAV 里给解码器设 `err_detect` 不起作用，被掩盖的错误也不会打 `corrupt` 标记；但 FFmpeg 会记错误日志，而 PyAV 默认关着日志。
  所以本进程把 FFmpeg 日志开到 ERROR、在每次解码的线程里捕获（不落到 stderr，C3 纪律不变）。
- `InvalidDataError` 这类数据错误 → 判废 `decode_failed`；只有错误日志、没抛异常 → 可疑 `decode_concealed`；
  解出的帧数与 episode 长度对不上 → 可疑 `count_mismatch`（L1 已判的相机不重复）。网络、签名这类异常照抽帧档的办法重试 3 次，仍失败算执行出错。
- 抽帧档的解码不变：它只覆盖通过数值档的条目、只认抛出的异常（D33），与 L3 不冲突。

### 3.5 与同数据集多数条目比（mcap）

没有采集协议（§9）时，拿同一数据集的多数条目当参照，只用摘要区（与预检读的是同一份），不多读；至少要有 3 条有摘要区的条目：

- 某个 topic 在至少 `integrity.majority_ratio`（缺省 0.8）的条目里有消息，本条却没有 → 可疑 `stream_missing`；
- 某个 topic 本条的频率（消息数 ÷ 文件时长）低于全数据集该 topic 中位数的 `integrity.rate_outlier_ratio`（缺省 0.8）→ 可疑 `rate_outlier`。
  只比连续的流（中位消息数至少 10 条）：只写一两条的 topic（任务文本、标定）谈不上频率，时长不同就会误报。

LeRobot 不需要这一项：缺文件由 D40 处理，帧数由 L1 / L2 对账。

阈值在站点配置的 `integrity:` 段（`count_tolerance_frames`、`majority_ratio`、`rate_outlier_ratio`、`min_peers`），经
`pipelineConfigOverride` 覆盖，缺省值在 `extensions/integrity/judge.py` 的 `DEFAULTS`；不做成任务参数。

## 4. 判决

### 4.1 三种结论

| 结论 | 结果记录 | 去向 |
|---|---|---|
| 通过 | `passed = True` | 进下一档 |
| 判废 | `passed = False`（`fail`） | reject，原因写 `reason`；不进后面的档；不可复议 |
| 可疑 | `passed = None`（`abstain`） | 进下一档，最终留在 passed，出一张「完整性存疑」裁决卡片（§4.4） |
| 读不到（基础设施） | `verdict = error` | held、待补跑（D24、D33） |

`aggregate` 对可疑的处理照 EEF 的做法（`passed=None` 的硬门 → 保留并问人）：原来按 EEF 写死的三处（人的回答当作模块结果、
判废理由改成人的话、待裁卡片）改成一张表 `HUMAN_GATES` 驱动，EEF 与本模块共用。

### 4.2 原因码

| 码 | 级别 | 档 | 中文 |
|---|---|---|---|
| `file_empty` | 判废 | L1 | 文件为空或过小 |
| `file_truncated` | 判废 | L1 / L2 | 文件被截断（mcap：录制中断且读不出数据） |
| `zero_filled` | 判废 | L1 / L2 | 文件里有成块的零填充 |
| `structure_invalid` | 判废 | L1 / L2 | 文件结构损坏，读不出 |
| `crc_mismatch` | 判废 | L2 | CRC 校验不符 |
| `row_invalid` | 判废 | L2 | 数据不合规（v1 结构校验） |
| `decode_failed` | 判废 | L3 | 视频解码失败 |
| `count_mismatch` | 可疑 | L1 / L2 / L3 | 帧数或行数与记录的长度对不上 |
| `cut_off` | 可疑 | L1 | 录制中断，读得到的部分完好 |
| `duplicate_content` | 可疑 | 数据集级 | 与另一条的文件内容完全相同 |
| `stream_missing` | 可疑 | 多数比较 | 缺一路其他条都有的 topic |
| `rate_outlier` | 可疑 | 多数比较 | 某 topic 频率明显低于其他条 |
| `decode_concealed` | 可疑 | L3 | 解码器掩盖了错误 |
| `table_inconsistent` | 可疑 | 数据集级 | episode 表里本条前后不一致 |
| `orphan_files` | 数据集级 | 数据集级 | 不属于任何 episode 的文件 |
| `dark_camera` | 数据集级 | 数据集级 | 近乎全黑的相机（v1 的先验） |
| `table_overlap` | 数据集级 | 数据集级 | episode 表的帧区间重叠或空缺 |

码表在 `extensions/integrity/findings.py`；前端中文名在 `frontend/src/locales/zh.ts` 的 `integrityCodes`；
报告与裁决卡片显示 `reason`（中文，已带文件与相机）。

### 4.3 边界

- **判废只落在有问题的条目上。** LeRobot v3 共用文件的例外：moov 缺失、文件头损坏这类整个文件打不开的，文件里的条目全部判废；
  截断、零填充这类有位置的，只判时间段与坏位置重叠的条目（按样本表换算，§3.2）。mcap 与 LeRobot v2 一条一个文件，不存在这个问题。
- **源文件缺失**照 D40：不质检、不进清单、报告列出，本模块不另出结论。
- **不勾选本模块时**，一切照旧：`validate_episode_row` 不过仍是读行失败 → error → held（D51 只在勾选时生效）。
- **本模块整体失败**（例如全部条目都因同一个基础设施原因读不到）照 P10：它的门视为未生效，后面的档照跑，全部条目待补跑。

### 4.4 裁决线「完整性存疑」

复核目录加一项（D43），`adjudicate-apply` 加一条执行规则：

```python
ReviewLine("integrity_check", "integrity_suspect", "完整性存疑", "passed", True,
           (("intact", "数据无误，保留"), ("broken", "确有问题，判废"), ("unsure", "拿不准")))
```

- 决定的取值用 `intact` / `broken`，不用 `keep` / `discard`：`discard` 在 v1 的裁决里是「整条弃用、压过一切」的专用词（Daemon 的
  裁决目录按它加规则），这里的意思是「本模块判废」，要与之分开。
- `counts_as_pending = True`：和其它落在 passed 的裁决线（标注分歧、任务成败弃权、EEF 与画面核对）一样计入待裁数，
  任务停在「已完成（待裁决 N 条）」（D10）；条目照 D24 先交付，裁决不挡交付。
- 「数据无误，保留」→ 本模块的结果当作通过；「确有问题，判废」→ 当作 `passed = False`，进 reject，理由「人工裁决判为数据确有问题」；
  「拿不准」→ 保持原样、仍在待裁。人的回答写进 `human-decisions/integrity_checks.csv`（v1 的词：数据无误 / 数据确有问题 / 拿不准）。
- 卡片内容：`reason`；裁决页对没有专用视图的线按目录通用渲染（D43），不需要专用视图。

## 5. 报告与界面

### 5.1 报告小节（一一对应，05 篇 §6）

- 统计：通过 / 判废 / 可疑条数；按原因码的分布；查过的文件数与字节数，其中 CRC 覆盖了多少；各档是否开启、耗时。
- 数据集级发现（`dataset.json`）逐条列出。
- 已有的「数据包完整性」小节（预检的格式、警告、跳过的条目）不变，加一行指向本小节。

### 5.2 Episode 明细

本模块一块：结论与 `reason`，下面按文件列出每项检查的结果（哪一档、通过或发现了什么）。

### 5.3 新建任务第二屏的参数

```yaml
decode_test:
  type: boolean
  default: false
  title: 逐帧解码测试
  description: >-
    把每路相机从头到尾严格解码一遍，能发现文件结构完好、但画面数据已经损坏的条目，
    包括解码器自己掩盖掉的错误。耗时相当于把全部视频完整解码一次
```

（2026-09-25 需求方删掉了原来说明里的具体耗时与「不开启时…」一句；耗时量级见 §6。模块卡片上的说明也去掉了「坏了的判废，可疑的交人工裁决」。）

## 6. 耗时估算

**实测吞吐**（2026-09-24，Apple M3 Pro 单核，样例片段）：

| 项目 | 吞吐 |
|---|---|
| H.264 640×480 解码 | 约 1700 帧/秒 |
| AV1 640×480 解码（dav1d；LeRobot 默认编码） | 约 710 帧/秒 |
| H.264 1280×720 解码 | 约 340 帧/秒 |
| JPEG 640×480 解码 | 约 1740 帧/秒 |
| 打开严格错误检测 | 不增加耗时 |
| mcap 整读并校验数据块 CRC（zstd） | 约 1.6 GB/s |
| crc32 / sha256 / md5 / CRC64（crcmod） | 6.4 / 3.1 / 0.84 / 0.43 GB/s |

**集群假设（未实测，F7.5 在 galbot 命名空间的 Pod 里测）**：服务器单核按 M3 的一半；CPU worker 8 个（站点默认）；
TOS 每次范围读约 30 ms，单 Pod 带宽 0.3–1 GB/s。

**场景**：1000 条，每条 30 秒，3 路 640×480 30 fps 相机（约 270 万帧）；视频约 38 GB（AV1，LeRobot 默认参数）或 14 GB（H.264），
mcap（JPEG）约 39 GB。

| 档 | 耗时 | 瓶颈 |
|---|---|---|
| 数据集级 | 秒级 | — |
| L1 | 10–30 秒 | 请求次数 × 延迟 |
| L2 | AV1 或 mcap 0.6–2 分钟；H.264 0.3–0.8 分钟 | TOS 带宽 |
| L3 | AV1 约 16 分钟；H.264 约 7 分钟；mcap（JPEG）约 6.5 分钟；720p H.264（2 路 15 fps）约 11 分钟。16 个 worker 时减半 | CPU |

**10 万条**（设计 13 的规模）：L1 + L2 约 1–4 小时，流式漏斗里跑在前面，能被约 12 小时的 VLM 段盖住；L3 全开 AV1 约 26 小时，
所以默认关。抽帧档已经把通过数值档的条目完整解码一遍，L3 等于把解码的 CPU 成本翻倍。

## 7. 契约与接入清单

| 位置 | 改什么 |
|---|---|
| C1 `contracts/modules.py` → `modules.json` | 1.11：`Stage` 加 `integrity`、`STAGE_ORDER`；`ModuleSpec.native`（不进 JSON）；新模块与 `decode_test`；复核目录加 `integrity_check` |
| C2 `docs/contracts/cli/` | `decisions.schema.json` 钉住 `integrity_check` 的三个决定；`final-list.schema.json` 的说明加 `integrity_suspect`；档名在 `plan.schema.json` 里是开放模式、`details` 是开放对象，不用改；`CONTRACTS.lock` 刷新 |
| C4 `openapi.yaml` 1.16.0 | 档名枚举四处加 `integrity`（注册表的 `stages`、模块的 `stage`、流水线的 `last_stage` 与 `stage_processing_s`），`next_stage` 加 `numeric`；`ReviewLineId`、`DecisionInput` 的说明加 `integrity_check`；`npm run gen:api` |
| `cli/preflight.py`、`cli/preflight_containers.py`、`cli/containers.py` | §1 三项；`read_footer` |
| `cli/storage.py` | `ObjectInfo.crc64`（不参与相等比较与指纹） |
| `cli/check.py`、`pipeline/check_stage.py` | 新档的分派（`_integrity`）、`--pipeline-next numeric`、`StageRun._integrity` 与自己的读行 |
| `extensions/integrity/`（新） | `findings`（码表）、`mp4`（结构与样本表）、`files`（L1 / L2）、`rows`（读行）、`decode`（L3）、`judge`（数据集级、多数比较、逐条判定）、`report`（小节、明细表） |
| `pipeline/aggregate.py`、`pipeline/adjudication.py` | `HUMAN_GATES` 取代写死的 EEF；新档进 `FUNNEL_STAGES`、硬门在本档即判死；`integrity_check` 的执行规则与 CSV 副本 |
| `pipeline/reporting.py`、`pipeline/timing.py`、`export/report.py` | 报告小节、`integrity_findings` 明细表、report.md；处理耗时认新档；`CHECK_CN` 加「数据完整性」（判决行的理由用它，与 EEF 同样的做法，不是 A 类） |
| `daemon/results/live.py` | 「Episode 流水线」实时面板只把 v1 的模块交给 v1 的 `apply_check_selection`（它不认识 v2 自有的模块、会报错；EEF 原本也有这个隐患） |
| `planner/plan.py`、`planner/estimates.py` | 新档排在最前；耗时估算 |
| `daemon/orchestr/episode_pipeline.py`、`runs.py`、`rules.py` | 流水线新的一层与它的宽度、漏斗档名 |
| 前端 | 模块卡片、预设、参数表单零改动（数据驱动）；报告小节视图与 Episode 明细块（`sectionViews.tsx`、`episodeBlocks.tsx`）；原因码与档名文案；漏斗档名收成一个常量 `FUNNEL_STAGES`（`lib/taskView.ts`）；预检警告的中译 |
| 对账 | A 类文件不改（只调用 `validate_episode_row`、`stats_prior_warnings` 与 v1 的读取函数）；对账工具的默认链不含本模块（v1 没有它，加了 v1 对 v2 就不对等），`run-v2 --modules` 可以带上它录一盘 v2 基线再回放（`tools/parity/v2run.py`、`tests/test_v2_parity.py`） |

## 8. 验收

故障样本从干净的小数据集派生（LeRobot v2：对账工具的 8 条夹具；LeRobot v3：`tests/export/v3_fixture` 的 6 条共用文件夹具；
mcap：对账工具的 8 条 mcap 夹具），每条只注入一种故障。测试在 `backend/tests/cli/test_integrity.py`、
`test_integrity_adjudication.py`、`test_preflight_integrity.py`：

| 故障 | 期望 | 测试 |
|---|---|---|
| 干净数据集 | 全部通过；夹具里故意做的字节级复制品（3、7）两条都可疑 `duplicate_content` | `test_a_clean_dataset_passes_but_its_byte_copies` |
| mp4 0 字节 | 预检警告；该条判废 `file_empty` | `test_empty_and_tiny_videos_are_warned_about`、`test_each_kind_of_damage_rejects_its_episode` |
| mp4 截断（moov 在尾部被截掉） | 判废 `file_truncated` | 同上 |
| mp4 截断（moov 在前，mdat 被截） | 判废 `file_truncated`，时间段从第一个缺失帧起 | `test_a_truncated_faststart_video_is_placed_in_time` |
| mp4 mdat 中段 64 KiB 置零 | L2 判废 `zero_filled`，时间段落在对应的帧上 | `test_a_zeroed_block_inside_the_video_data` |
| mp4 文件头置零 | 判废 `zero_filled` | `test_each_kind_of_damage_rejects_its_episode` |
| parquet 结尾被截 | 判废 `file_truncated` | 同上 |
| action 含 NaN；时间戳倒序 | 判废 `row_invalid`，理由是 v1 的原话；不勾选本模块时仍是 error | 同上、`test_without_the_module_a_bad_row_is_still_an_error` |
| v3 共用视频文件在中段截断 | 只有时间段在截断点之后的条目（2、3）判废，其余通过 | `test_v3_clean_and_a_shared_video_cut_in_the_middle` |
| mcap 录制中断（截在记录中间） | 预检警告「录制中断」；v1 读取器读不出 → 判废 `file_truncated` | `test_a_recording_cut_off_is_named`、`test_mcap_damage` |
| mcap 摘要区翻转一个字节 | 预检警告；判废 `structure_invalid` | `test_a_summary_crc_failure_is_named`、`test_mcap_damage` |
| mcap 数据块翻转一个字节 | 判废 `crc_mismatch`（压缩块解不开时 `structure_invalid`） | `test_mcap_damage`、`test_mcap_chunk_crc` |
| mcap 某相机 topic 频率减半 | 可疑 `rate_outlier`；只写一条的 `/task` 不参与 | `test_mcap_damage` |
| 视频里一帧的字节被打乱（开 L3） | 判废 `decode_failed`（轻微翻转被掩盖时可疑 `decode_concealed`）；关 L3 时通过；换了 `decode_test` 的 `--resume` 全部重做 | `test_the_decode_test_finds_what_the_structure_cannot` |
| 读文件时存储返回 503（注入） | 该条 error、有 `read` incident；`--resume` 后通过 | `test_a_storage_failure_is_an_error_not_a_finding` |

另外：①干净数据集上勾选本模块，passed / reject / held 与不勾选时逐项相同，待裁清单只多出可疑条目的卡片
（`test_on_a_clean_dataset_the_verdicts_do_not_change`）；②判废终结漏斗、不可复议，可疑进「完整性存疑」，「数据无误」「数据确有问题」
「拿不准」照 §4.4 重算（`test_integrity_adjudication.py`）；③合成数据上的对账不受影响（默认链不含本模块），带上本模块录的 v2 基线
回放逐位一致、三份清单与默认基线相同（`test_a_golden_with_the_data_integrity_gate_replays_exactly`）；Daemon 端到端：完整性层排在最前、
实时面板读得到、可疑条目进裁决队列并计入待裁（`test_the_data_integrity_layer_goes_first_and_its_episodes_are_readable_live`）；
④在 galbot 命名空间的 Pod 里实测 §6 的两个假设（单核解码速度、TOS 带宽），把估算表换成实测值（F7.5，未做）。

## 9. 本期不做

| 项 | 为什么不做 | 以后怎么接 |
|---|---|---|
| 采集协议对账（每次采集应有哪几路流、频率、时长、维度、元数据） | 要有人按采集台架写配置 | 站点配置里的协议列表（按 `robot_type` 匹配，或模块参数指定）；数据集级加「声明的流集合对协议」，L1 / 多数比较加「频率、时长对协议」。全部条目都缺同一路流时只记数据集级警告，部分条目缺才逐条判废 |
| 采集端校验和清单（逐文件的大小与校验和、逐条的流与帧数） | 要采集端改录制程序 | 数据集根目录的清单文件（或模块参数给路径）；CRC64 与 TOS 列表直接比，其它算法在 L2 整读时顺带算；大小或校验和不符判废 |
| 预检里的其它检查 | 预检要秒级、只报可用性 | — |
| 把抽帧档的解码改成严格模式 | 会改变抽帧档的行为与对账 | 以后如要零成本覆盖，可让抽帧档把解码器报告的错误汇给本模块 |
