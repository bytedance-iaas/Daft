# 14 数据完整性检查

> 状态：**定稿**（2026-09-24，需求方逐条确认）。决策见 `00-overview.md` §7 的 D50–D52。
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
| 0 字节或过小的文件 | 文件列表里的大小：`data/`、`videos/` 下的文件与 `*.mcap`；小到放不下该格式固定字节的也算（parquet 小于 12 字节、mcap 小于 45 字节、mp4 小于 512 字节——放不下 ftyp 加一个视频轨的 moov）。不用 v1 判「放不了」的 4 KiB：几帧低分辨率的合法短片就只有 3 KB | `3 files are empty or too small (episode 12: videos/.../episode_000012.mp4 0 B, ...); the data integrity module will reject their episodes` |
| mcap 录制中断 | 读摘要区失败的文件，看最后 8 字节是不是 mcap 结束标识（远端时这段在 `RangeFile` 取回的尾部里，本地多读 8 字节） | `2 episodes (4, 9) were cut off while recording (no mcap end marker); the checks read what is there` |
| mcap 摘要区 CRC | footer 里的 `summary_crc` 非 0 时，对摘要区到 footer `summary_offset_start` 字段为止的字节算 crc32（这些字节已经取回来了） | `1 episode (7) has a summary section that fails its CRC; the topics and counts preflight shows for it may be wrong` |

改动落在 `cli/preflight.py` 的 `_fill_supported`（LeRobot）与 `cli/preflight_containers.py`、`cli/containers.py` 的
`read_summary`（mcap：`McapSummary` 多两个字段 `truncated`、`summary_crc_ok`）。lance 的视频在表里，第一项只看 `meta/` 以外的对象大小。

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
- 它主要耗 I/O：L1、L2 用线程池（`integrity.io_concurrency`，缺省 16），不占数值档、抽帧档的 CPU 份额。开了 L3 时要 CPU：
  `daemon/orchestr/pipeline.py` 的 `cpu_shares_for` 把它和抽帧档同样对待，从 CPU 预算里分一份。
- 流式漏斗（设计 13）里它是第一层：一条过了就交给数值档，不等整批。判废的条目不进后面的档（硬门，与现有漏斗同一机制）。
- planner 的耗时估算（`planner/estimates.py`）加这一档：L1 按文件数 × 每文件请求数；L2 按总字节 ÷ 带宽；L3 按帧数 ÷ 各编码的解码速度（§6）。

### 2.3 怎么读数据

- 文件访问一律经 `cli/storage.py` 的 `Storage`（本地与 TOS 同一接口：`list`、`read_range`、流式读）。TOS 列文件时保留对象的
  CRC64（`hash_crc64_ecma`，现在被 `_iter` 丢掉），`ObjectInfo` 加 `crc64` 字段，`source_manifest` 不变。
- **mcap 在 TOS 上**：用现有源缓存（F5.13：每个 episode 文件只下载一次）。本档是第一个读的，由它触发下载，后面的档直接用本地副本，
  所以 L2 对 mcap 不增加 TOS 流量。
- **LeRobot 在 TOS 上**：L2 把每个文件流式读一遍（不落盘）；抽帧档仍按现在的方式经预签名 URL 读，所以 TOS 读流量约多一倍。
- **LeRobot v3**：一个数据文件、一个视频文件装多条 episode。按文件做一次、结果按条分发：进程内按对象键记忆结果，同一文件加锁，
  第一条触发读取，同文件的其它条复用；判定时再按每条的时间段或行区间决定受不受影响（§4.3）。
- **行数据**：L2 的逐条结构校验用现有 `pipeline/rows.py` 的 `RowSource.get` / `ContainerRowSource`（它们本来就调
  `validate_episode_row`），不另写读取器。

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
- `crc`：这个文件用什么校验过（`mcap_chunk`、`mcap_data` 或 `null`），报告据此统计「CRC 覆盖了多少文件」。
- 数据集级的发现不写进逐条记录，写 `checks/data_integrity/dataset.json`（§3.1）。

## 3. 检查项

### 3.1 数据集级（本档第一次调用时算一次）

写 `checks/data_integrity/dataset.json`（`findings` 同 §2.4 的写法，`level` 为 `dataset` 或按条的 `reject` / `suspect`）。
续跑时发现已存在且输入指纹相同就不重算。

| 检查 | 读什么 | 结论 |
|---|---|---|
| LeRobot v3 episode 表自洽：`dataset_from_index` / `dataset_to_index` 连续不重叠、合计等于 `total_frames`；各相机 `to_timestamp − from_timestamp` 与 `length / fps` 相差不超过 1 帧 | `meta/episodes/*.parquet`（预检也读） | 区间重叠、合计对不上 → 数据集级警告，涉及的条目可疑；视频时间段超出视频文件时长的判定在 L1 做 |
| 多余文件：`data/`、`videos/` 下不被任何 episode 引用的文件 | 文件列表 | 数据集级警告 |
| 内容相同的文件：两条 episode 的视频文件或 mcap 文件内容相同 | TOS 用列表里的 CRC64（没有就用 ETag + 大小）；本地路径用大小 + 头尾各 64 KiB 的 sha256 | 两条都可疑（`duplicate_content`）。parquet 的重复交给「精确去重」 |
| 近乎全黑的相机（v1 的 `stats_prior_warnings`，只调用） | `meta/stats.json` | 数据集级警告 |

### 3.2 L1 结构（每个文件读头尾，2–3 次小范围读取）

复用 `cli/verify.py` 的 `content_problem`（文件头全零、parquet 头尾 `PAR1` 与尾部元数据可解析、mcap 两端标识、mp4 顶层 box 找 moov），
抽成 `cli/verify.py` 与本模块共用的函数，再补：

| 格式 | 补的检查 | 结论 |
|---|---|---|
| 所有 | 0 字节，或小于该格式的最小字节数（§1） | 判废 `file_empty` |
| mp4 | 顶层 box 大小之和等于文件大小（抓 moov 在前、mdat 被截断）；打开容器读头部（PyAV，不解码）拿帧数与时长 | box 越过文件末尾或没有 moov → 判废 `file_truncated`；帧数与 episode 长度相差超过 `integrity.count_tolerance_frames`（缺省 1）→ 可疑 `count_mismatch`；v3 里本条的时间段超出文件时长 → 判废 `file_truncated` |
| parquet | 尾部元数据里的行数 | v2：与 `length` 不等 → 可疑 `count_mismatch`；v3：本条的行区间超出文件行数 → 判废 `file_truncated` |
| mcap | 摘要区 CRC；数据块索引的偏移 + 长度不越过摘要区起点；有无结束标识 | 摘要区 CRC 不符或索引越界 → 判废 `structure_invalid`；没有结束标识 → 交给 L2 读到哪算哪，读出的内容由后面各项判 |
| 所有 | 格式标识、尾部元数据读不出 | 判废 `structure_invalid`；文件头全零 → 判废 `zero_filled` |

### 3.3 L2 整读（每个字节读一遍）

| 检查 | 做法 | 结论 |
|---|---|---|
| mcap 数据块与数据区 CRC | mcap 读取器 `validate_crcs=True` 读一遍（CRC 为 0 的块跳过，记下「未写 CRC」） | 不符 → 判废 `crc_mismatch` |
| 中段零填充 | mp4 的 mdat、压缩过的 mcap 数据块：按 64 KiB 对齐的整块全零（`integrity.zero_block_bytes`）。压缩数据不会合法地出现这么长的零；parquet 不扫（未压缩的全零列会误报，交给读取与结构校验）；未压缩且没写 CRC 的 mcap 块也不扫（原始黑帧会误报） | 判废 `zero_filled` |
| v1 的逐条结构校验（D51） | `RowSource.get` / `ContainerRowSource`：action 非空、二维、浮点、没有 NaN/Inf；时间戳与 action 等长且严格递增；state 帧数一致；视频文件在、时间边界合法；fps 合法 | 不过 → 判废 `row_invalid`，`message` 原样用 v1 的报错 |
| 读不出（pyarrow、mcap 读取器报错） | 同上 | 判废 `structure_invalid` |

基础设施错误（TOS 超时、5xx、签名过期重试用尽）不是结论：按 D33 记 incident，整条 `verdict = error`，待补跑。
文件在列表里却读到 404（读取过程中被删）同理，下次运行时由 D27 / D40 处理。

### 3.4 L3 逐帧解码（模块参数 `decode_test`，默认关）

- 每路相机从头到尾解码：LeRobot 用 PyAV，打开解码器的严格错误检测（`err_detect=crccheck+bitstream+buffer+explode`，实测不增加耗时）；
  mcap 的 JPEG 帧逐帧解码，H.264 帧按 F5.13 的办法转本地 mp4 后同样处理。只解码、不转 RGB。
- 解码抛错 → 判废 `decode_failed`；解码器报告了被掩盖的错误（帧带 corrupt 标记或解码日志里的错误计数）但没有失败 → 可疑 `decode_concealed`；
  解出的帧数与 episode 长度对不上 → 可疑 `count_mismatch`（L1 已判的不重复）。
- 抽帧档的解码不变：它只覆盖通过数值档的条目、只认抛出的异常（D33），与 L3 不冲突。

### 3.5 与同数据集多数条目比（mcap）

没有采集协议（§9）时，拿同一数据集的多数条目当参照，只用预检也在读的摘要区，不多读：

- 某个 topic 在至少 `integrity.majority_ratio`（缺省 0.8）的条目里有消息，本条却没有 → 可疑 `stream_missing`；
- 某个 topic 本条的频率（消息数 ÷ 文件时长）低于全数据集该 topic 中位数的 `integrity.rate_outlier_ratio`（缺省 0.8）→ 可疑 `rate_outlier`。

LeRobot 不需要这一项：缺文件由 D40 处理，帧数由 L1 对账。

阈值都在 `pipeline/default.yaml` 的新段 `integrity:` 里，站点经 `pipelineConfigOverride` 覆盖；不做成任务参数。

## 4. 判决

### 4.1 三种结论

| 结论 | 结果记录 | 去向 |
|---|---|---|
| 通过 | `passed = True` | 进下一档 |
| 判废 | `passed = False`（`fail`） | reject，原因写 `reason`；不进后面的档；不可复议 |
| 可疑 | `passed = None`（`abstain`） | 进下一档，最终留在 passed，出一张「完整性存疑」裁决卡片（§4.4） |
| 读不到（基础设施） | `verdict = error` | held、待补跑（D24、D33） |

`aggregate` 对可疑的处理照 EEF 的做法（`passed=None` 的硬门 → 保留并问人），把现在写死的 EEF 改成按注册表的 `review_lines` 驱动。

### 4.2 原因码

| 码 | 级别 | 档 | 中文 |
|---|---|---|---|
| `file_empty` | 判废 | L1 | 文件为空或过小 |
| `file_truncated` | 判废 | L1 | 文件被截断 |
| `zero_filled` | 判废 | L1 / L2 | 文件里有成块的零填充 |
| `structure_invalid` | 判废 | L1 / L2 | 文件结构损坏，读不出 |
| `crc_mismatch` | 判废 | L2 | CRC 校验不符 |
| `row_invalid` | 判废 | L2 | 数据不合规（v1 结构校验） |
| `decode_failed` | 判废 | L3 | 视频解码失败 |
| `count_mismatch` | 可疑 | L1 / L3 | 帧数或行数与记录的长度对不上 |
| `duplicate_content` | 可疑 | 数据集级 | 与另一条的文件内容完全相同 |
| `stream_missing` | 可疑 | 多数比较 | 缺一路其他条都有的 topic |
| `rate_outlier` | 可疑 | 多数比较 | 某 topic 频率明显低于其他条 |
| `decode_concealed` | 可疑 | L3 | 解码器掩盖了错误 |

前端文案进 `frontend/src/locales/zh.ts`；报告与裁决卡片显示 `reason`（中文，已带文件与相机）。

### 4.3 边界

- **判废只落在有问题的条目上。** LeRobot v3 共用文件的例外：moov 缺失、文件头损坏这类整个文件打不开的，文件里的条目全部判废；
  截断、零填充这类有位置的，只判时间段或行区间与坏位置重叠的条目。mcap 与 LeRobot v2 一条一个文件，不存在这个问题。
- **源文件缺失**照 D40：不质检、不进清单、报告列出，本模块不另出结论。
- **不勾选本模块时**，一切照旧：`validate_episode_row` 不过仍是读行失败 → error → held（D51 只在勾选时生效）。
- **本模块整体失败**（例如全部条目都因同一个基础设施原因读不到）照 P10：它的门视为未生效，后面的档照跑，全部条目待补跑。

### 4.4 裁决线「完整性存疑」

复核目录加一项（D43），`adjudicate-apply` 加一条执行规则：

```python
ReviewLine("integrity_check", "integrity_suspect", "完整性存疑", "passed", True,
           (("keep", "数据无误，保留"), ("discard", "确有问题，判废"), ("unsure", "拿不准")))
```

- `counts_as_pending = True`：和其它落在 passed 的裁决线（标注分歧、任务成败弃权、EEF 与画面核对）一样计入待裁数，
  任务停在「已完成（待裁决 N 条）」（D10）；条目照 D24 先交付，裁决不挡交付。
- 「保留」→ 本模块的结果当作通过；「确有问题，判废」→ 当作 `passed = False`，进 reject；「拿不准」→ 保持原样。
  都照 EEF 的写法：人的回答作为本模块的结果（`aggregate` 的 overrides）。
- 卡片内容：`reason`、全部 `findings`、涉及的文件；通用渲染即可，不需要专用视图。

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
    包括解码器自己掩盖掉的错误。耗时相当于把全部视频完整解码一次：1000 条、每条 30 秒、
    3 路 640×480 相机，H.264 约 7 分钟，AV1 约 16 分钟（8 路 CPU 并发），随条数线性增长。
    不开启时，结构检查、整读和 CRC 校验已经能发现截断、零填充和 mcap 的数据损坏。
```

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
| C1 `contracts/modules.py` → `modules.json` | 1.11：`Stage` 加 `integrity`、`STAGE_ORDER`；`ModuleSpec.native`；新模块与 `decode_test`；复核目录加 `integrity_check` |
| C2 `docs/contracts/cli/` | 档名在 `plan.schema.json` 里是开放模式，不用改；本模块 `details` 的写法记进 `docs/contracts/cli/` 的说明（`details` 本身仍是开放对象）；报告 schema 的新小节；`CONTRACTS.lock` 刷新 |
| C4 `openapi.yaml` | 模块与裁决线是数据驱动（D43），不改；但档名写成了枚举（模块的 `stage`、计划的 `stages`、进度的 `last_stage`），三处加 `integrity`，再跑 `npm run gen:api` |
| `cli/preflight.py`、`cli/preflight_containers.py`、`cli/containers.py` | §1 三项 |
| `cli/storage.py` | `ObjectInfo.crc64` |
| `cli/verify.py` | 结构检查抽成共用函数 |
| `cli/check.py`、`pipeline/check_stage.py`、`pipeline/funnel.py` | 新档的分派与逐条循环；`FUNNEL_STAGES` |
| `extensions/integrity/`（新） | 数据集级、L1、L2、L3、多数比较、结果记录 |
| `pipeline/aggregate.py`、`pipeline/reporting.py` | `native` 位取代写死的 EEF；可疑 → 保留并问人；报告小节 |
| `pipeline/default.yaml` | `integrity:` 段（`io_concurrency`、`count_tolerance_frames`、`zero_block_bytes`、`majority_ratio`、`rate_outlier_ratio`） |
| `planner/plan.py`、`planner/estimates.py` | 新档的并发与耗时估算 |
| `daemon/orchestr/pipeline.py`、`runs.py`、`rules.py`、`results/live.py` | 新的一层、CPU 份额、分档进度 |
| `adjudicate-apply` 与 Daemon 裁决迁移 | `integrity_check` 的执行规则（「保留」「判废」的含义） |
| 前端 | 模块卡片、预设、参数表单零改动（数据驱动）；报告小节视图与 Episode 明细块（`sectionViews.tsx`、`episodeBlocks.tsx`）；原因码文案；分档进度里的新档名 |
| 对账 | A 类文件不改；合成数据上 v1 对 v2 的对账固定模块清单、不含本模块；v2 自录黄金基线加上本模块重录 |

## 8. 验收

故障样本从一份干净的小数据集（LeRobot v2、v3 各一份，mcap 一份）派生，每份只注入一种故障，真值写进样本清单：

| 故障 | 期望 |
|---|---|
| mp4 0 字节 | 预检警告；该条判废 `file_empty` |
| mp4 截断（moov 在尾部被截掉） | 判废 `file_truncated` |
| mp4 截断（moov 在前，mdat 被截） | 判废 `file_truncated` |
| mp4 mdat 中段 64 KiB 置零 | L2 判废 `zero_filled` |
| mp4 文件头置零 | 判废 `zero_filled` |
| v3 共用视频文件在中段截断 | 只有时间段落在截断点之后的条目判废，其余通过 |
| parquet 尾部元数据损坏 | 判废 `structure_invalid` |
| action 含 NaN；时间戳倒序 | 判废 `row_invalid`，原因与 v1 的报错一致；不勾选本模块时仍是 error → held |
| mcap 没有结束标识 | 预检警告「录制中断」；质检按读出的内容判 |
| mcap 摘要区翻转一个字节 | 预检警告；判废 `structure_invalid` |
| mcap 数据块翻转一个字节 | 判废 `crc_mismatch` |
| 两条的视频文件相同 | 两条都可疑 `duplicate_content`，出两张「完整性存疑」卡片，待裁数加 2，两条照常交付 |
| mcap 某相机 topic 频率减半 | 可疑 `rate_outlier` |
| 视频帧内比特翻转（开 L3） | 判废 `decode_failed` 或可疑 `decode_concealed`；关 L3 时不报 |
| TOS 读超时（假存储注入） | error → held，`--resume` 后通过 |

另外：①干净数据集上勾选本模块，判决与清单和不勾选时逐位一致（只多一个模块的通过记录）；②合成数据上 v1 对 v2 对账全过；
③裁决「保留」「判废」重算终判正确；④在 galbot 命名空间的 Pod 里实测 §6 的两个假设（单核解码速度、TOS 带宽），把估算表换成实测值。

## 9. 本期不做

| 项 | 为什么不做 | 以后怎么接 |
|---|---|---|
| 采集协议对账（每次采集应有哪几路流、频率、时长、维度、元数据） | 要有人按采集台架写配置 | 站点配置里的协议列表（按 `robot_type` 匹配，或模块参数指定）；数据集级加「声明的流集合对协议」，L1 / 多数比较加「频率、时长对协议」。全部条目都缺同一路流时只记数据集级警告，部分条目缺才逐条判废 |
| 采集端校验和清单（逐文件的大小与校验和、逐条的流与帧数） | 要采集端改录制程序 | 数据集根目录的清单文件（或模块参数给路径）；CRC64 与 TOS 列表直接比，其它算法在 L2 整读时顺带算；大小或校验和不符判废 |
| 预检里的其它检查 | 预检要秒级、只报可用性 | — |
| 把抽帧档的解码改成严格模式 | 会改变抽帧档的行为与对账 | 以后如要零成本覆盖，可让抽帧档把解码器报告的错误汇给本模块 |
