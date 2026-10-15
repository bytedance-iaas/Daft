# 29 · 声明了却缺的数据：给判决，不记出错、不剔除

> 状态：**已实施（2026-10-14）**。D95–D97 登记在 00 §7；账本阶段 27（F27.1–F27.6）；§7 是落地时的细化。
> 来源：2026-10-14 用户问数据完整性有没有「传感器流缺失检测」（C6 STRM-1）。在对账夹具上逐项注入实测：数据集声明了、条目里却没有或读不出的数据
> ——缺 `action` 列、缺 info.json 声明的 `observation.state` 列、state 有几帧为空或各帧维度不一、parquet 行数与 episode 表不符、mcap 缺 `/action`
> topic——完整性全记成执行出错，进待补跑，重试多少次都一样；LeRobot v2 缺某一路视频的条目照 D40 剔除，没有判决（设计 16 §11 第 12 条）；
> 整条 state 维度与声明不符（6 维对 7 维）直接通过。用户拍板：勾选数据完整性时缺源文件的条目判废、不再剔除，不勾选时照 D40；只对新任务生效。
> 修订：D40（缺源文件剔除）加一个例外；D51（读取器的其它异常记执行出错）改为先诊断。

## 0. 开工指引

1. **原则**：读不了的判废；读得了、但和声明或和别的记录对不上的转人工。后一类原来就有（`count_mismatch`、`stream_missing`、`table_inconsistent`、
   `rate_outlier`），这一期补前一类，并把 mcap 映射里写明了的流也拿来对照。
2. **只动完整性和缺源文件的那几处剔除**。A 类文件（v1 的读取器、`validate_episode_row`）一行不改：诊断写在完整性自己的读行里，v1 读取器失败之后才做，
   读得通的条目一个字节也不多读；列在不在、v2 的行数在 L1 读 parquet 尾部元数据时顺带比对。
3. **不做**（§6）：schema 根本没声明、业务上本该有的传感器（要采集协议，设计 14 §9）；lance；平台不读的声明列只查在不在。

## 1. 读不了的数据是判废发现（D95）

完整性读一条的数据行（`extensions/integrity/rows.py`，v1 的读取函数）失败时，先按数据集的声明找原因，找到了就是发现：

| 情形 | 细码 | 检测项 | 在哪儿查出 | 理由（示例） |
|---|---|---|---|---|
| info.json 声明了 `action`，parquet 里没有这一列 | `action_missing` | STRM-1 | L1：parquet 尾部元数据的列名 | `data/chunk-000/episode_000001.parquet 里没有 action 列（info.json 声明了）` |
| 声明的其它非视频列不在（`observation.state`、`timestamp`、`frame_index`……） | `column_missing`（新） | STRM-1 | 同上 | `… 里没有 observation.state 列（info.json 声明了）` |
| v2：parquet 行数与 episode 表的 `length` 不符 | `length_mismatch` | FILE-5 | L1：parquet 尾部元数据的行数 | `数据有 69 帧，episode 表记 79 帧` |
| v3：本条在共用数据文件里的切片长度与 `length` 不符 | `length_mismatch` | FILE-5 | 读行失败后诊断 | 同上 |
| `action` / `observation.state` 有空值 | `values_invalid` | FILE-6 | 读行失败后诊断 | `observation.state 有 10 帧为空（帧 71–80）` |
| `action` / `observation.state` 各帧维度不一 | `values_invalid` | FILE-6 | 同上 | `observation.state 各帧维度不一：7 维 50 帧、6 维 10 帧` |
| `timestamp` 有空值 | `timestamps_invalid` | STRM-4 | 同上 | `timestamp 有 3 帧为空（帧 4、9、17）` |
| mcap 缺映射要求的动作 topic | `action_missing` | STRM-1 | v1 读取器的报错（它说缺的是哪个 topic） | `缺动作 topic /action（这条 mcap 里没有它）` |
| mcap 一路相机都没有 | `video_missing` | FILE-1 | 同上 | `没有一路相机的视频 topic` |

- 都是 blocking、不可复议（完整性的判废一向如此）；`report_only` 下照样拒（策略版本 3 的「完整性的 blocking 仍是 blocking」）。其它模块照 D92 不读这条、
  只写跳过记录——原来它们读这条也全报执行出错。
- 一条有好几处问题时每处一条发现；L1 已经判废的（缺列、v2 行数不符），读行失败不再另找原因。
- 诊断不出原因的读取异常仍是执行出错（D51 的口径收窄成「找不到原因的」）；存储读失败（超时、5xx）仍照 D33 待补跑。
- mcap 的动作 topic 在、但按映射的字段路径取不出数值，是映射写得不对（整个数据集都一样），仍是执行出错，不算这条数据缺。
- LeRobot 读得通的条目，v1 的逐条校验照旧（D51：NaN/Inf、时间戳不严格递增……按原因拆成细码，设计 17 §2.2）。

## 2. 维度与声明不符、mcap 映射写明的流（D96）

**维度**：LeRobot 读行成功后，`action` 与 `observation.state` 每帧的维度和 info.json 的 `shape[0]` 比，不等就是 `values_invalid`（blocking）：
`observation.state 每帧 6 维，info.json 声明 7 维`。数据读得出来，但按声明读它的训练、可视化会取错维度或直接报错；整个数据集都写错的，每条都判废，
改正 info.json 后另建任务。只比平台读的这两列；mcap、lance 没有逐列的维度声明，不比。

**列在不在**：L1 本来就读 parquet 的尾部元数据（`parquet_l1`），列名顺带记下（`FileReport.columns`），和 info.json 里 `dtype` 不是 `video` 的特征逐个比对，
不多读。LeRobot v3 一个数据文件装好几条：文件缺列，文件里的条目都判废（同一个文件只分析一次，设计 14 §2.3）。平台不读的列（例如
`observation.effort`）只查在不在，不查空值与维度。

**mcap 映射写明的流**：数据集登记时确认过 mcap 映射（C7 第一层，D62），检查读取用的映射（`ingest.mcap_mapping`）就写明了状态的 topic 与各路相机的
topic。某条 episode 没有其中一个（或只有 0 条消息），记可疑 `stream_missing`（review，裁决线「完整性存疑」），理由写「缺映射里写明的 topic」；
不再要求同数据集的多数条目都有它（设计 14 §3.5 的多数比较照旧，同一个 topic 只报一条）。读得出来、只是少了一路，所以是存疑，不是判废；
动作 topic 缺了读取器读不下去，按 §1 判废。没确认过映射的数据集用的是命名约定（`/action`、`/observation.state`……），约定不算声明，不做这一项。

## 3. 缺源文件：勾选完整性时判废（D97，修订 D40）

**勾选了数据完整性**：缺数据文件或某一路视频的条目不再剔除。完整性在数据集级那一遍（建文件计划时）就认出来，每个缺的文件一条判废发现：

| 缺什么 | 细码 | 检测项 | 理由（示例） |
|---|---|---|---|
| 数据 parquet | `data_missing`（新） | FILE-1 | `数据文件不存在：data/chunk-000/episode_000004.parquet` |
| 某一路相机的视频 | `video_missing` | FILE-1 | `wrist 相机的视频文件不存在：videos/chunk-000/observation.images.wrist/episode_000000.mp4` |

- 它的其它文件一个不读：v2 里这些文件不在源清单的 `objects` 里（`snapshot` 照 D40 没记它们），读了就违反 D27；结论已经是判废，读了也不改变什么。
- LeRobot v3 原来就不剔除：共用文件缺了，文件里的条目原先由 v1 的逐条校验报 `video_missing`（理由带绝对路径），读不了数据文件的记执行出错；
  现在两种都在建计划时报出，理由写对象键。
- 其它模块照 D92 只写跳过记录；进 reject、计入总数；报告与控制台不再把它列在「缺源文件、未参与质检」里。

**没勾选数据完整性**：照 D40 剔除。没有模块能给出这条的结论，与其挂在待补跑里（重试永远救不回来），不如剔除并列出。

**只对新任务生效**：Daemon 开跑时在 `run.json` 记 `missing_sources`：勾选了完整性是 `judged`，否则 `left_out`。之前开跑的任务（`run.json` 没有这个键）
照旧剔除，续跑不改变它的条目范围。

**谁怎么认**：

- `snapshot` 不变：缺文件的 v2 条目仍记进源清单的 `skipped_episodes`（缺哪些键），这是事实，与选了哪些模块无关；它的其它文件也不进 `objects`。
- Daemon 的条目范围（`RunBase.selection()`）：`missing_sources` 是 `judged`——`run.json` 还没写（生成计划时）就看是否勾选了完整性——不去掉这些条目，
  计划、各段、总数都含它们。
- `check` 带源清单时（`runctx.leave_out_skipped`）：这次调用跑完整性，或 `run.json` 是 `judged`，或完整性在这个运行目录里已有这条的记录，就不去掉；
  否则照 D40 去掉。源清单守卫对这类条目只核对缺的文件仍然缺（补上了算源数据变了，退出码 6），它的其它文件不核对，也没有命令读。
- 完整性段（`check_stage`）：缺源文件的条目不再「不写结果行」，完整性按文件计划的发现判它；没在计划里的（手工给了计划之外的条目），读行时
  v1 报的缺文件同样换成这两个细码。
- `aggregate`、报告、重试（`pipeline/skipped.all_skipped`）：「跳过」是源清单的 `skipped_episodes` 加上读到才发现的，再减去完整性有记录的条目。
  完整性对它出错（例如别的文件读失败）时它在 held 里，重试补跑。
- 手工跑（运行目录里没有 `run.json`）：`check --modules data_integrity` 判这些条目；之后别的模块按完整性的记录写跳过记录；没跑过完整性的照 D40。

## 4. 契约与接入清单

| 位置 | 改什么 |
|---|---|
| C1 注册表 8.1 | 数据完整性加 `data_missing`（FILE-1）、`column_missing`（STRM-1），都是 blocking；`modules.json`、`CONTRACTS.lock` |
| C2 | 不变：`check` 输出的 `skipped_missing_source`、报告的 `integrity.skipped_episodes` 仍是「被剔除的」，勾选完整性的新任务里不再出现 |
| C4 | 不变 |
| `run.json` | 多 `missing_sources`（设计 06 §1） |
| `extensions/integrity/` | `findings.py` 内部码；`files.py` 的 parquet L1 记列名；`judge.py` 建计划时认缺文件、列对照、v2 行数、维度、mcap 映射写明的流、诊断结论；`rows.py` 读行失败后的诊断（`DataProblem`） |
| `pipeline/check_stage.py` | 完整性段不再因缺源文件不写结果行 |
| `pipeline/skipped.py` | `all_skipped` 减去完整性有记录的条目；`judges_missing` 读 `run.json` |
| `cli/runctx.py`、`cli/check.py` | `leave_out_skipped` 的新规则；源清单守卫 |
| `cli/preflight.py`、`cli/snapshot.py` | 警告与日志的说法：勾选完整性时判废，不勾选时剔除 |
| `daemon/orchestr/runbase.py`、`planning.py` | `selection()`；`run.json` 的 `missing_sources` |
| 前端 | `integrityCodes` 补新码；预检警告的中译 |
| 回归工具 | `taxonomy.json` 的平台注记重新生成（`coverage_from_registry`） |

## 5. 验收

故障从对账工具的夹具派生（8 条 LeRobot v2.1、8 条 mcap、6 条 v3），每条只注入一种：

| 故障 | 期望 | 测试 |
|---|---|---|
| v2 缺一路视频，勾选完整性、带源清单 | 判废 `video_missing`，数值段写跳过记录，终判进 reject、计入总数，报告没有「跳过」 | `tests/cli/test_integrity_declared.py` |
| 同上，不勾选完整性 | 照 D40 剔除 | 同上；`tests/cli/test_missing_source.py`（原有） |
| v2 缺数据 parquet | 判废 `data_missing` | `tests/cli/test_integrity_declared.py` |
| v2 缺 `action` 列 / 缺 `observation.state` 列 | 判废 `action_missing` / `column_missing` | 同上 |
| v2 state 几帧为空 / 几帧维度不一 | 判废 `values_invalid` | 同上 |
| v2 整条 state 维度与声明不符 | 判废 `values_invalid` | 同上 |
| v2 parquet 行数与 episode 表不符 | 判废 `length_mismatch` | 同上 |
| v3 数据文件缺列 / 缺视频文件 | 文件里的条目都判废（`column_missing` / `video_missing`） | 同上 |
| v3 一条的 state 有空值 | 只判这一条 `values_invalid`，同文件的其它条通过 | 同上 |
| mcap 缺 `/action` | 判废 `action_missing` | 同上 |
| mcap 确认过映射，一条缺映射写明的相机 topic | 可疑 `stream_missing`（理由写「映射里写明的」） | 同上 |
| 干净的夹具 | 结果不变 | 原有用例 |
| Daemon：勾选完整性的新任务 | 选择与计划含缺源文件的条目，`run.json` 是 `judged`；旧任务照旧 | `tests/orchestr/test_units.py` |

## 6. 不在这一期

| 项 | 为什么 / 以后怎么接 |
|---|---|
| schema 没声明、业务上本该有的传感器 | 要有人按采集台架写采集协议（设计 14 §9）；协议有了，数据集级加「声明的流集合对协议」 |
| lance | 读取器不同（表里存视频），缺列时 v1 读取器的报错另行诊断 |
| 平台不读的声明列的空值与维度 | 不读就不查；要查得整列读，L1 做不到 |
| 维度错误的数据集级汇总（整个数据集都写错时只报一条） | 流水线逐条判，开头不知道全体；现在每条都判废，理由相同 |

## 7. 落地记录

- **诊断在失败之后才做**：读得通的条目一个字节也不多读。v2 读行失败时再读一遍这条的 parquet 找原因；v3 是 `pipeline/rows.v3_group_rows`：
  v1 的 `_rows_v3_group` 一次读整个数据文件、一条读不了全文件失败，这时按声明逐条看切片，有问题的条拿出来（`DataProblem`），剩下的条再让 v1 读一遍
  ——值与第一遍相同，多读一次文件。数值段、画面段走的也是这条路，所以同文件的其它条在那些模块里也读得到了（原来全记执行出错）；在完整性以外的模块里，
  被拿出来的那条仍是执行出错（完整性选了时它被 D92 跳过，碰不到）。
- **列与 v2 行数在 L1**：`parquet_l1` 读尾部元数据时记下列名（`FileReport.columns`）；v1 读取器不管 info.json 都要读的 `action`、`timestamp` 也算在内
  （理由写「读数据要用它」）。v2 行数不符在 L1 就判废，读行时 v1 的 `ValueError` 不再另找原因。
- **mcap 的缺 topic 认 v1 读取器的原话**：`_require_topics` 写明缺的是哪个动作来源、还是一路相机都没有（A 类文件，原话冻结）；topic 在、但按字段路径取不出数值的
  是映射写错，仍是执行出错。缺的动作 topic 判废后，多数比较对同一个 topic 的 `stream_missing` 不再重复报。录制中断（没有结束标识）的文件读不出时仍记
  `cut_unreadable`，不归缺 topic。
- **映射写明的流**：读 `ingest.mcap_mapping` 的 `state` 与 `video_topics`（Daemon 对确认过映射的数据集用 `--set` 传进来）；没有摘要区的文件不比。
- **D97 的落点**：快照不变；`check` 带清单时的去留在 `runctx.leave_out_skipped`（这次调用跑完整性、`run.json` 是 `judged`、或完整性已有这条的记录）；
  完整性段对缺源文件的条不读行（`IntegrityJudge.misses_source`）；`pipeline/skipped.all_skipped` 减去完整性有记录的条；Daemon 的
  `RunBase.selection()` 看 `run.json` 的 `missing_sources`（还没写时看是否选了完整性），`planning.write_run_json` 写这个键。
- **实测**（对账夹具注入，命令与输出见完整性 README 第 8 步）：v2 的七种情形各得到对应细码的判废，没有一条执行出错；v3 的空值只判所在条目，
  同文件两条在完整性和数值段都照常评估；mcap 缺 `/action` 判废、缺一路相机存疑；Daemon 端到端：选了完整性的任务里删掉一路视频的条目进 reject、计入总数，
  `run.json` 是 `judged`；没选完整性的任务照旧剔除。
- **用例**：`tests/cli/test_integrity_declared.py`（12 条：v2、v3、mcap、源清单与 `run.json`、守卫、诊断的单元）、
  `tests/orchestr/test_e2e_main.py::test_with_data_integrity_an_episode_without_its_video_is_judged`；样本生成器多三份 `*_declared/`。
