# curation 命令行（v2，工作包 W3）

`curation` 是 Curator v2 的命令行入口，设计见 `docs/design/02-cli-contract.md`，契约见 `docs/contracts/`（C2 输出、C3 进度、C4 REST）。
一条命令只做一件事，命令之间靠运行目录里的文件交接，由 Daemon（或脚本）按固定顺序串起来（见下文「Daemon 的调用顺序」）。

| 命令 | 作用 | `--json` 契约 |
|---|---|---|
| `curation preflight` | 只读元数据：判格式、数 episode，给出每个模块「可跑 / 需补充 / 不支持」及原因（F2.7） | `cli/preflight.schema.json` |
| `curation plan` | 执行计划：分段、每段的并发度与 VLM 闸门、VLM 请求合并提议（纯计算，W6 的库） | `cli/plan.schema.json` |
| `curation snapshot` | 固化任务要读的源对象清单（键、大小、ETag 或修改时间），写 `source_manifest.json` | `cli/source-manifest.schema.json` |
| `curation check` | 跑**一段**的模块（`integrity`、`numeric`、`frame`、`duplicate`、`vlm_prep`、`vlm` 之一），每条 episode 一行结果 | `cli/check.schema.json` |
| `curation aggregate` | 按任务策略算判决，写一个结果版本的 passed / reject / held 三个清单、review 视图、`verdicts.jsonl` 与 `keep.txt` | `cli/aggregate.schema.json` |
| `curation adjudicate-apply` | 应用本任务的人工裁决（不调模型），列出接下来要重判的条 | `cli/adjudicate-apply.schema.json` |
| `curation report` | 一个结果版本的 `report.md` / `report.json` / `perf.json` / 明细表，最后写 `commit.json` | `cli/report-output.schema.json` |
| `curation verify` | 从交付目录并行回读每个关键文件，全部通过才写 `_COMPLETE` | `cli/verify.schema.json` |
| `curation stage-umi` | 把 UMI / TRUMI 原始会话（`dataset_plan.pkl` + `demos/`）转成 LeRobot v2.1 数据集和它的 `trajectory.json`，源目录只读 | — |
| `curation task …` | Daemon REST API 的薄客户端，给 Agent 和脚本用，输出带 `links` | `openapi.yaml` 里对应接口的响应，原样打印 |

另有三条运维命令 `ls`、`prune`、`fetch`（`cli/ops.py`），给在终端里操作的人用：输出中文文本，没有 `--json`，见下文「运维命令」。
v1 的 `run`、`rejudge`、`review-page`、`backends`、`public` 已经删除（设计 27，D89），v1 的代码留在 `release_v1` 分支。

## 代码结构

命令行（`curation/cli/`）：

| 文件 | 内容 |
|---|---|
| `app.py` | 参数解析与 `main()`（`curation.cli:main` 就是它）；运维命令转给 `ops.py` |
| `framework.py` | 输出纪律、C3 进度事件、错误信封与退出码、SIGTERM / SIGINT |
| `errors.py` | 退出码与对应的异常类 |
| `creds.py` | 凭证只从环境变量读；输入、输出两套访问密钥 |
| `storage.py` | 本地目录与 `tos://` 的统一读写（列举、按范围读、上传、删除、写 `_COMPLETE`） |
| `lerobot_meta.py` | 不读样本数据的 LeRobot 元数据读取：格式识别、episode 表、每条的文件键 |
| `containers.py` / `preflight_containers.py` | mcap 与 Lance（D44）：识别、给 mcap 文件编号、按范围读 mcap 的摘要区、读 Lance 的 `meta/`、TOS 数据的本地副本（`SourceCache`）、`source_info.json`；这两种格式的预检 |
| `runctx.py` | 流水线命令共用的部分：运行目录、`--input` / `--source-manifest`、VLM 参数与三个行为开关、闸门、配置、`VlmSession`（探活、传输策略、用量记账） |
| `preflight.py` / `plan.py` / `snapshot.py` / `check.py` / `aggregate.py` / `adjudicate.py` / `report_cmd.py` / `verify.py` / `stage_umi.py` / `task_client.py` | 各条命令 |
| `eef_check.py` / `eef_review.py` / `modparams.py` | EEF–视频一致性在 `check` 里的两半与模型复核；`--param` 的解析与校验 |
| `source_manifest.py` / `listing_cache.py` | `source_manifest.json` 的生成与校验（对不上退出码 6）；远端对象清单的缓存 |
| `episodes.py` / `inputs.py` | `--episodes` 语法（含 `@文件`）、`--input` / `--source` |
| `ops.py` | 运维命令 `ls`、`prune`、`fetch` |

编排壳（`curation/pipeline/`；算法是 A 类代码，原样搬来）：

| 文件 | 内容 |
|---|---|
| `records.py` | 结果行（`cli/result-record.schema.json`，记录 2.0）、分片文件、`results.jsonl` 压实、`inflight.json`、原子写 |
| `findings.py` | 从模块的回答推出发现、覆盖与读数（设计 17 §1–§2） |
| `policy.py` / `verdicts.py` | 判决策略，以及一条 episode 在策略下的判决（D58） |
| `check_stage.py` | 一次 `check` 调用：自建线程池、`--resume`、崩溃计数、VLM 段熔断 |
| `funnel.py` | 各模块逐条的检查函数（文件名沿用旧称），`check` 一条一条调 |
| `video_task.py` | 任务成败的视频判定：一条一次请求，`video-task/2`（D71） |
| `rows.py` | 逐条取样本行：LeRobot 走惰性扫描（`LeRobotDataSource`），mcap / Lance 走各自的读取器（`ContainerRowSource`） |
| `incidents.py` | 执行事故登记（D33）：模型调用、机位解码、崩溃 |
| `vlm_policy.py` | 传输策略：对冲开关、外层重试、用量记账（W6 的两本账） |
| `tasktext.py` | 任务描述的来源：原始标注或人工改标 |
| `skipped.py` | 缺源文件跳过的条（D40）：快照里的 `skipped_episodes` 与读到才发现的 `skipped_episodes.json` |
| `episode_state.py` | Daemon 流水线逐条交接的状态（SQLite） |
| `aggregate.py` / `adjudication.py` / `reporting.py` / `report_stats.py` | 聚合判决、人工裁决、报告和报告里的统计 |

报告（`curation/export/`）：报告正文 `report.py`（mcap / Lance 的数据包体检）、证据帧 `evidence.py`、同步曲线 `sync_plots.py`、
交付目录的安全写入 `safe_write.py`。

## 全局约定

**全局参数**（02 篇 §2）：原子命令都接受 `--json`、`--log-level`、`--config`、`--set k=v`、`--region` / `--input-region` / `--output-region`；
`curation task …` 只接受 `--json`、`--log-level`（连接参数另见下文）。参数写在子命令之后，例如 `curation preflight --input … --json`。

**输出纪律**：

- 带 `--json` 时，stdout 只有一个 JSON 文档：成功时是命令结果，非零退出时是错误信封（`cli/error.schema.json`）。stderr 每行一个 C3 事件
  （`progress` / `log` / `usage` / `throttle`）。库代码里的 `print` 不会混进 stdout，会转成 `log` 事件。
- 不带 `--json` 时，stdout 是给人看的结果，日志和进度走 stderr；不到 1 秒的阶段不打进度行。

**退出码**（02 篇 §4）：

| 码 | `error.code` | 含义 |
|---|---|---|
| 0 | — | 成功。**不等于每条都成功**：看 `--json` 里的逐状态计数和 `error_episodes`（`verify` 有坏文件时也是 0，看 `failed`） |
| 1 | `internal` | 命令自身的意外错误（bug），调用栈以 `error` 级日志写到 stderr |
| 2 | `usage` | 参数或配置错误；Daemon 调用时出现就是 bug |
| 3 | `input_unreachable` / `output_unreachable` / `daemon_unreachable` | 读不到输入数据集、交付目录，或（只有 `curation task`）Daemon 没有应答 |
| 4 | `module_failed` | 模块整体跑不了：VLM 端点不通、熔断、数据集读不了（语义样本坏了）、规格库里没有这个机器人型号 |
| 5 | `terminated` | 收到 SIGTERM：不再开始新的 episode，在做的做完、落盘后退出 |
| 6 | `source_changed` | 源数据和 `--source-manifest` 对不上 |
| 7 | `rejected` | `curation task`：Daemon 回了错误（4xx / 5xx，含 405 `method_not_allowed`），REST 错误体在 `error.details.rest_error` |
| 8 | `wait_timeout` | `curation task … --wait` / `wait`：到了 `--timeout` 任务还没结束，当时的任务 JSON 在 `error.details.task` |
| 130 | `interrupted` | 收到 SIGINT，立即中止（在飞的请求放弃，已落盘的结果不丢） |

**三个行为开关，默认全关**（02 篇 §1 第 4 条，需求的硬要求）：

| 开关 | 不给时 | 给了时 |
|---|---|---|
| `--concurrency N` | CPU 段一次一条；VLM 段的闸门都是 1，任何时刻只有一个模型请求在飞 | CPU 段同时 N 条；VLM 段按并行度 N 推导闸门（04 篇 §2.2） |
| `--retry N` | 一次调用就是一次尝试，失败就记成这条的执行错误 | 超时、连接错误、5xx、429 隔 1 s / 2 s / 4 s 再来，最多 N 次（429 至少等 `Retry-After`） |
| `--hedge` | 一次调用就是一个 HTTP 请求 | 对冲补发：过了超时线补发一次，5xx 串行重发一次 |

Daemon 按任务参数传 `--hedge --retry 3 --plan-stage …`；直接用命令行的人默认拿到的是一次直来直去的执行。

视频判定请求的超时随请求里的视频量走（2026-10-08）：配置的 `timeouts_s.probe`（默认 60 秒）只是下限，实际超时 = max(下限,
每帧 0.5 秒 × 机位数 × 秒数 × 采样帧率)，封顶 600 秒，对冲线随之后移。DROID 三路 18 秒 5 fps 的一条约 135 秒，几秒的短片仍是 60 秒。

**VLM**：`--vlm-backend <名>`（站点配置 `vlm_backends` 里的预设）或 `--vlm-endpoint <URL> --vlm-model <名>`（缺省取 `$CURATION_VLM_ENDPOINT` /
`$CURATION_VLM_MODEL`）；密钥只放在环境变量里，`--vlm-api-key-env <变量名>` 指明是哪个变量（缺省 `ARK_API_KEY`）。每条调模型的命令先
`GET /models` 探活一次，不通就退出码 4，不会逐条报错。

- `check --vlm-reasoning-effort <档位>`：给了才在每个模型请求里带 `reasoning_effort`，值原样转交、命令行不校验；不给时请求里没有这个字段。
- `check --no-vlm`（设计 25 D84）：这次调用没有模型后端（任务没选模型时 Daemon 传）。带模型开关的模块（EEF，`use_vlm`）只跑 CPU 那部分，
  模型一路记 `no_vlm_backend`；所选模块里有离不开模型的（`task_success`）时是用法错误。

**凭证只走环境变量**，没有任何参数接收密钥（argv 在 `ps` 里谁都看得见）：

| 用途 | 环境变量 |
|---|---|
| 读输入数据集 | `CURATION_INPUT_TOS_ACCESS_KEY` / `CURATION_INPUT_TOS_SECRET_KEY`（可选 `CURATION_INPUT_TOS_SESSION_TOKEN`） |
| 写、读交付目录 | `CURATION_OUTPUT_TOS_ACCESS_KEY` / `CURATION_OUTPUT_TOS_SECRET_KEY`（可选 `CURATION_OUTPUT_TOS_SESSION_TOKEN`） |
| 上面某一组没设时的回落 | `TOS_ACCESS_KEY` / `TOS_SECRET_KEY` |
| 模型 | `--vlm-api-key-env` 指定的变量（缺省 `ARK_API_KEY`） |
| `curation task …` | `CURATOR_URL`（含挂载前缀，如 `https://host/curation`，也可用 `--url`）、`CURATOR_USER` / `CURATOR_PASSWORD` |

一组变量只设了一半（只有 AK 或只有 SK）直接报错，不会悄悄回落到另一组；输入不借用输出的密钥，反过来也一样。地区的优先级：
`--input-region` / `--output-region` > `--region` > `TOS_REGION` > `TOS_ENDPOINT` 里的地区 > `cn-beijing`。

## 运行目录

流水线命令都在一个本地运行目录（`--run-dir`）里读写，Daemon 边跑边把它同步到交付目录（00 篇 §4.2）：

```
<run-dir>/
  run.json                                     开跑时冻结的任务设置：判决策略、数据集声明等（Daemon 写，aggregate 读策略）
  preflight.json  plan.json  source_manifest.json
  checks/<module>/parts/0001.jsonl …           每次 check 调用写一个新分片，逐条追加、逐行 fsync；编号高的分片优先
  checks/<module>/results.jsonl                压实后的当前结果（每条一行，按编号排序）
  checks/<module>/inflight.json                正在处理的 episode（进程被杀后留下，--resume 读它）
  checks/<module>/crashes.json                 每条 episode 害死进程的次数
  checks/video_action_sync/curves/ …           同步曲线
  checks/duplicate_check/groups.json           重复检测的遍历顺序、动作哈希撞车组、算过的指纹、确认的重复组（D87）
  checks/eef_video_consistency/ …              EEF 的缓存、证据图、生成的轨迹
  scratch/vlm/<episode>/eef_video_consistency/ EEF 的 CPU 半段留给模型半段的请求包（发完即删）
  skipped_episodes.json                        不带快照时，check 读到才发现缺源文件而跳过的条（D40）
  adjudication/{applied.jsonl, labels.json}    已应用的人工裁决、人工改标
  human-decisions/*.csv                        本任务裁决的 CSV 副本
  revisions/r0001/                             一个结果版本：verdicts.jsonl、keep.txt、passed/reject/held/review.json、policy.json、
                                               adjudications.json、report.md、report.json、perf.json、tables/*.parquet，
                                               最后是 commit.json（有它才算提交，之后不再改写）
  details/vlm_latency.csv  details/evidence/   每次模型请求的时延、取证图
  usage.jsonl                                  token 用量（C3 usage 行的落盘副本）
  source_info.json                             mcap / Lance（D44）：读取器给出的数据集信息与用到的机器人规格，report 据此写「数据包」一节
  logs/
```

## 各命令要点

**preflight**：`curation preflight --input <tos://… | 本地目录 | 公共数据集名> [--source tos|public|local] [--vlm-backend 名] [--embodiment-id 型号] [--modules a,b] [--param 模块.键=值] [--source-manifest 文件] [--declaration 文件] --json`

- 只列目录、只读 `meta/` 下的文件，不读 parquet 和视频（mcap 只读每个文件的摘要区，见下文「mcap 与 Lance 数据集」）。
- 认得 LeRobot v2 / v3、mcap 与 Lance（lerobot-lance-convert 0.3.0 起的三表布局，D44）。其余（`.rrd`、别的 Lance 表、其他 LeRobot 版本、
  认不出的目录）所有模块都是 `unsupported`，原因写明检测到的格式（`format_unsupported`）。
- `info.json` 结构校验用 `validate_info`，报错原文放进 `validation`（中文，照抄给用户）；其余原因文案用英文。
- 运动学极限：型号读到且在规格库里是 `available`；读到但不在规格库里是 `unsupported`，只跳过这一项；读不到（缺失、空串或 `unknown`）是
  `needs_input`，`input_hint.options` 列出规格库的 9 个型号。`--embodiment-id` 覆盖 `robot_type`。
- 任务成败判定：没传 `--vlm-backend` 是 `needs_input`（`input_hint.field = "vlm"`）；没有任务标注不会让它标灰，只在 `notes` 里提示这些条不判成败（D72）。
- EEF–视频一致性（注册表 5.2 起不必有模型）：没传 `--vlm-backend` 照常 `available`，`notes` 末尾一条 `vlm_backend_missing: …` 作提醒，
  `--param eef_video_consistency.use_vlm=false` 时不提醒。它的条目另有 `trajectory_source`（轨迹从哪来）、`cameras`（每路相机的安装方式、归属、
  能否画与原因）和 `applicable_params`（这个数据集上适用的参数：有第三视角相机才有夹爪参考）。
- `--declaration`：数据集声明（C7 `dataset-declaration/1.0`，设计 25 §3）。声明够了，EEF 的 `trajectory_source` 是 `generate`；声明里写了位姿或
  关节角记录，`subitems.record_consistency` 就按它核对列或 topic 在不在。不给声明时预检按元数据起草一份，只用来说缺什么（`missing`），从不拿它生成。
  `check` 同样收 `--declaration`（Daemon 传任务开跑时冻结的那份）。
- `--modules` 只报告所选模块。
- 列目录时发现缺文件的条写进 `warnings`：LeRobot v2 缺数据 parquet 或某个机位视频的条跳过（见 snapshot）；v3 的不跳，检查时读不了、记为出错。
- 编号不是 0 … count-1 时（没有重新编号的子集；mcap 按文件名 `episode_<N>.mcap` 编号），`dataset.episode_indices` 写出全部编号，写法同 `--episodes`
  （如 `1,3,5`、`2604-2626`），`warnings` 里另有一句提示；编号正常的数据集没有这个字段（F12.8）。
- LeRobot 数据集另写三项给数据可视化（设计 18 §7）：`dataset.features`（info.json 的特征表）、`dataset.camera_info`（与 `cameras` 同序：编码、尺寸、
  fps，浏览器放不了的编码如 mpeg4 标 `needs_transcode`）、`dataset.segment_sources`（认出来的分段标注写法，认不出的写 `supported: false` 和原因）。
  只读 `meta/`，每张小表最多读开头 16 KiB。
- 三条完整性警告（D52，设计 14 §1），都不多读数据、不改变模块可用性：数据与视频文件为空或小到放不下该格式的固定字节（parquet 小于 12 字节、
  mcap 小于 45 字节、mp4 小于 512 字节）；mcap 录制中断（文件尾没有结束标识）；mcap 摘要区的 CRC 不符。要读数据的检查归「数据完整性」模块。
- Git LFS 指针（2026-10-05）：认出格式后，清单里不到 1 KiB 的数据文件取前 5 个读开头；是 LFS 指针（没装 Git LFS 的 clone 传上来的）就判
  `metadata_invalid`，原因 `Git LFS pointer files instead of the data: <文件> … fetch the files (git lfs pull …, or hf download) and upload them again`。

**plan**：`curation plan --preflight pf.json --modules a,b,… [--episodes 表达式] [--unlabeled 表达式] [--vlm-parallelism N] [--backend-parallelism N] [--task-vlm-parallelism N] [--task-cpu-concurrency N] [--cpu-cores N] [--running-tasks N] [--site-config 文件] [--no-vlm] [--param …] [--out plan.json] --json`

- 不读数据、不联网。并行度取各层上限里最小的那个（D31），`limits.*.bound_by` 写明卡在哪一层；`--running-tasks` 大于 1 时均分。
- VLM 段的闸门由并行度 N 推导：`probe`（在飞的模型请求）是 N，`episode`（在飞的 episode）跟着它走，一条一次请求（D71）。站点可以在
  `vlm.gates` 里按 N 的参考值调这两把；v1 的 `endstate`、`arbitration`、`caption` 等旧闸门名会被忽略。`check` 用 `--plan-stage plan.json` 取自己那一段。
- `--episodes` 不给就是数据集的全部条目；给了就要都在数据集里（按预检的 `episode_indices`，没有它时是 0 … count-1），不在的是用法错误（退出码 2），
  提示写明数据集有哪些编号（F12.8）。

**snapshot**：`curation snapshot --input … [--episodes 表达式] --out <运行目录>/source_manifest.json --json`

- 记录 `meta/` 下全部文件、所选 episode 的 parquet 与各机位视频，以及数据集**前 100 条**的 parquet：数据集语义（控制模式、单位等）不管选了哪些条
  都按前 100 条的数值判定，它们一变，所选各条的判定就可能变。TOS 记 ETag，本地记 `mtime_ns`。
- `--episodes` 支持 `34`、`10-20`、`3,10-12` 和 `@文件`（每行一个表达式，`#` 之后是注释）；全部越界报参数错误，部分越界跑交集并警告。
- 之后读源数据的命令带上 `--source-manifest`，它要读的对象（元数据、语义样本、所选各条的文件）有一个变了，就在读之前以退出码 6 结束。
  快照时就缺、现在仍缺的文件不算变化。
- 远端数据集的对象清单只列一次：`snapshot` 把清单存在 `--out` 旁边的 `.source_listing.json`（隐藏，不交付），带 `--source-manifest` 的命令读它，
  不再向 TOS 列（`cli/listing_cache.py`）。一小时过期；用之前 HEAD 一下 `meta/` 下的对象，变了就重新列；Daemon 续跑、重试前删掉它。
- LeRobot v2 的某条缺数据 parquet 或任一机位的视频时，这条跳过（D40）：记进 `skipped_episodes`（写明缺了哪些文件），不进任何清单、不计入总数，
  报告的完整性一块列出。v3 数据集不跳，缺文件的条在检查时读不了，记为出错。mcap / Lance 记的对象不同，见下文。

**check**：`curation check --modules <一段的模块> --input … --run-dir … --episodes 表达式 [--source-manifest …] [--declaration …] [--selection …] [--part NNNN] [--resume] [--plan-stage …] [--done-out 文件] [--prep | --prepared] [--param 模块.键=值] [--max-episodes N] [VLM 参数] [行为开关] --json`

- 一次只跑一段（D18）：`data_integrity`（`integrity`，CPU 块最前，设计 14）、`timestamp_check,kinematic_limits,motion_quality`（`numeric`）、
  `visual_quality,video_action_sync`（`frame`，共用一次解码）、`duplicate_check`（`duplicate`，设计 26）、`eef_video_consistency --prep`（`vlm_prep`）、
  `task_success`（`vlm`，带上 `camera_defects`，可与 `eef_video_consistency --prepared` 同一调用）。混段是参数错误。
- 每条做完立刻追加到 `checks/<module>/parts/<part>.jsonl`（整行写入并 fsync）；`--part` 不给时取比现有最大编号大一的号。结束时重写 `results.jsonl`。
- **判废的发现、执行出错都不拦人**（D57）：`--done-out` 写出本段每个所选模块都有了记录的条，EEF 的 CPU 半段留好请求包的条也算。
  Daemon 的流水线不用这个文件（逐条交接）；手工按段跑时可以把它当下一段的 `--episodes @文件`。
- **出错与拿不准分开**（D33）：模型正常答了「看不清 / 拿不准」，是一条 review 级的发现（`uncertain`），不是错。一次模型调用最终失败、一路机位解码失败、
  样本读不了、进程两次死在这条上，这条记录就是 `status: error`，`error.incidents` 写明步骤、调用类型、机位、原因和尝试次数。单条出错不影响其他条，
  命令仍以 0 退出；`--json` 给逐状态计数、`error_episodes` 和输入集合的指纹 `input_digest`。
- `--resume`：跳过已有非错误结果的条。SIGTERM 时做完在手的条再退出（退出码 5）；SIGKILL 后 `inflight.json` 留着当时在手的条，下次 `--resume` 把它们的
  崩溃次数加一、重跑；同一条两次出现在死掉的进程手里就记为出错（步骤 `crash`）并跳过（P14）。中断后续跑的结果与一次跑完逐字段相同（耗时字段除外）。
- VLM 段熔断（P15）：开头连续 20 条都因为基础设施原因出错（连不上、超时、5xx、限流），整个模块以退出码 4 结束，不再烧配额。
- 不带 `--source-manifest` 时，读到才发现缺源文件的条（规则同 snapshot，只对 v2）不写结果行，列在 `--json` 的 `skipped_missing_source` 里并记进
  `skipped_episodes.json`，不计入总数；别的读失败照旧记为出错（聚合时 held）。
- `data_integrity`（D50、D51）：文件结构、整读（mcap CRC、零填充、parquet 数据页）、逐条结构校验，`--param data_integrity.decode_test=true` 时逐帧解码；
  坏了判废，可疑的留给人（`integrity_check`），存储读失败算出错。数据集级发现写 `checks/data_integrity/dataset.json`。详见[模块 README](../extensions/integrity/README.md)。
- **任务成败判定**每条恰好一次模型请求（D71）：多机位连续视频一起看，判失败就是判废（可复议），拿不准转人工；没有复核、仲裁和判废护栏。
  改了标的条（`adjudication/labels.json`）按新标注重判，同样一次请求，记录的 `details` 写明 `task_desc`（新标注）和 `task_desc_source`（`人工改标`）。
- **没有任务标注的条**（D72、D73）：不补描述、不判成败，但画面照看。`check --modules task_success` 给它发一次只问画面缺陷的请求（同样的机位、
  同样的三项缺陷定义，不问成败），写一条 `status: ok` 的记录：`details.skipped = "no_task_text"`，发现只有 LABEL-2 的 `task_text_missing`（仅报告），
  TASK-4 记为无法评估（`no_task_text`）；不转人工、不 held，`--resume` 不重做；请求失败按执行错误记，held 等重试。这条的去留由其余模块决定。
- `camera_defects`（镜头画面缺陷）跟着 `task_success` 走：`--modules task_success` 自动带上它，不必列出、也不能单独跑。判定请求里每路相机的回答多一个
  `camera_check` 字段（花屏 / 抖动 / 镜头污染 / 视觉遮挡，遮挡另给比例、位置与时长），没有额外的模型调用；记录里的 `arms` 块
  （各路相机骑的是不是数据集给它的那条臂）由 CPU 实测画面运动与各臂速度得出，也不发请求；它的发现都只报告。详见设计 13「逐机位画面缺陷」。
- **重复检测**（`duplicate_check`，D87，设计 26）只报告，逐条走：一条进来只哈希动作数据，动作哈希撞了才读这几条的画面算内容指纹（A 类
  `episode_fingerprint`：动作字节加每路相机的内容身份与时间窗），没有重复的数据集不额外读视频。一组完全相同的条目里每一条都得到一条 `duplicate`
  发现（SET-1，info），`readings.same_as` 是同组的其他条，`readings.group_id` 是组里最小的编号；不判废、不留第一条、不转人工、不能复议。
  组里先到的成员在这次调用结束（或暂停）时补写一条新记录，把后来的成员加进 `same_as`。结束时写 `groups.json`，`--resume` 和重试从已有记录与它重建状态。
  跨条目的状态在进程里，所以并发恒为 1。

**aggregate**：`curation aggregate --run-dir … --revision N [--modules a,b] [--episodes 表达式] [--input …] [--selection …] --json`

- 纯计算，秒级，每次全量重算。模块取 `--modules`，否则取 `plan.json`；episode 取 `--episodes`，否则取有结果的全部。
- 判决按策略（设计 17 §4，D58）：模块只报发现，任务的策略（`run.json` 的 `policy`，开跑时冻结，连同各模块细码的缺省级别；没有时是 `default`）
  给每条发现定级 blocking / review / info。有 blocking 发现即拒，每条 blocking 发现都是理由，对它执行出错的模块写进原因「另有…执行出错，不影响结论」（D35）；
  否则所需模块出错或没有记录即 held（「待补跑」）；否则通过，review 级发现进 review。`report_only` 只留数据完整性的 blocking，其余都是 info。
  换策略只要重跑 aggregate。
- 写进 `revisions/r<NNNN>/`：passed / reject / held（三者不相交、合起来是全部）、review 视图、这一版用的策略（`policy.json`）、机器判决行
  `verdicts.jsonl`（`blocking`、`review`、`info_count`、`error_modules`、`reason`）和交付的条 `keep.txt`。`verdicts.jsonl` 只看检查本身；`keep.txt`
  跟着已应用的人工裁决走：弃用的、人工判失败的移出，复议恢复的、对拒绝条目人工判成功的加入。没有裁决时两者一致。
- 三份清单的每条都带 `findings`（F12.5）：这一条的全部发现与它在这一版的级别。人的结论是它自己的 blocking 发现（带它的一句话），复议恢复的
  可复议发现降为 info，已了结的 review 发现不再列出；每条发现用 `index` 指向模块记录里的位置。
- 人工裁决的优先级：「弃用」压过一切（包括 held）；人工判了任务成败就以人为准，改标时顺手给的成败结论同样采信（改标的回答一变它就作废，见
  adjudicate-apply）；人的结论落在它回答的发现上（判失败、数据确有问题、EEF 不一致是人工的 blocking 发现，不可复议）；复议按发现（D42）：这条的
  blocking 发现全部可复议（注册表细码的 `appealable`，目前只有任务失败）且都不是人的结论才受理，恢复后这些发现降为只报告，回到 passed，
  另有模块对它执行出错的恢复后 held（P11）；「拿不准」只记录，这条留在队列里。改了标还没按新标注重判的条 held。
- review 视图的复核种类取自注册表（D42、D43）：任务成败（`task_verdict`）只问在 passed 里、task_success 拿不准、人还没下结论的条；EEF 冲突
  （`eef_check`，设计 25 §7.3）只问在 passed 里、EEF 两个渠道结论相反的条；数据完整性存疑（`integrity_check`）同理；被拒的条没有这些问题，
  拒绝可复议时给一项 `reject_appeal`；held 的条什么都不问。每一项都写明注册表的 `line`。
- 重复检测和别的模块一样只报发现（D87）：清单与复核项里没有 `duplicate_of`，拒绝原因里也没有 `kind: duplicate`；它只在主流程跑，人工裁决之后不重跑。
- 缺源文件被跳过的条（D40）不进任何清单、不计入总数（`counts.skipped`）。某个模块整体失败（退出码 4，没写出结果）时，它本该判的每一条都 held（D41）。
- `--input` 给了才在 passed 里写出交付用的任务描述（原始标注要从数据集的元数据里读）。已有 `commit.json` 的版本拒绝改写。

**adjudicate-apply**：`curation adjudicate-apply --run-dir … --decisions decisions.json --json`

- `decisions.json`（`cli/decisions.schema.json`，2.0）只含本任务尚未应用的裁决（D32）；按 id 去重，重复应用不改变任何东西。写
  `adjudication/applied.jsonl`、`adjudication/labels.json` 和 `human-decisions/*.csv`（中文用词，如「判失败」）。不是 2.0 的文件报参数错误。
- `--json` 的 `rerun_task_success` 是要按新标注重判的条（改了标、且没有人工判定成败的），`label_changes` 是这次生效的改标。
- 改标的同时给了成败结论（判成功 / 判失败）的条不重判，以人为准；给的是「拿不准」照常重判。这种跟着改标给的结论只在改标的回答仍是最新、
  且结论在它之后给出（按裁决 id）时算数；改标的回答一变（维持原标注、拿不准、换一段新标注），它就作废，仍在生效的改标照常重判。这与 Daemon 的
  `Queue._stands` 是同一条规则。task_success 拿不准的条，成败结论是卡片自己的问题，不会这样作废。
- `eef_check`：`consistent` / `inconsistent` / `unsure`；`integrity_check`：`intact` / `broken` / `unsure`。都当作模块在这条上的结果，不调模型、不用重跑。
- 没有执行规则的复核种类（`line` 不是 `task_verdict`、`reject_appeal`、`eef_check`、`integrity_check`）报参数错误（退出码 2）并写明是哪一种，不会跳过；
  对没有可复议拒绝的条目复议（有不可复议的发现、是人的结论、已弃用、根本没被拒）同样报参数错误（D42），一条也不应用。

**report**：`curation report --run-dir … --revision N [--format md,json] [--modules a,b] [--subtask-id ID] --json`

- 在 `revisions/r<NNNN>/` 写 `report.md`（中文，给人看）、`report.json`（`cli/report.schema.json`）、`perf.json` 和 `tables/*.parquet`，最后写 `commit.json`：
  这个版本用了各模块的哪些分片、哪些人工裁决。只有带 `commit.json` 的版本才算数；哪个版本生效由 Daemon 在上传核验之后切换（D25）。
- 总览的 `counts.skipped` 与完整性一块的 `integrity.skipped_episodes` 列出缺源文件未质检的条（D40），`report.md` 有「缺源文件未质检」一行和「未质检的条目」一节。
- 各模块的 `adjudication`：`pending` 只数计入待裁的问题，`appealable` 是这个模块拒掉、还能复议的条数；重复检测不拒、不问，它的 `adjudication` 是 null。
- 重复检测一节：`summary` 多 `groups`（重复组数）、`duplicated`（涉及条数）与 `group_sizes`，明细表 `duplicate_groups` 每条一行；`report.md` 这一节末行
  「重复组 N 组,涉及 M 条(只报告)」。
- token 用量来自 `usage.jsonl`（两本账：实际与分摊，`requests_unknown_usage` 是响应里没有用量的请求数，不估算）；时延来自 `details/vlm_latency.csv`，
  按调用类型排（`probe` 任务成败判定，`eef_review` EEF 复核，模块自己的类型排在后面，`curation/vlm_call_kinds.py`）。

**verify**：`curation verify --run-dir <本地运行目录> --output <交付目录下的 run_id 目录> [--visibility-timeout 60] --json`

- 关键文件 = 运行目录里的全部文件（排除 `logs/`、`inflight.json`、隐藏文件和临时文件）：结果版本的清单、报告与明细。
- 逐个检查（8 路并行回读）：存在（`missing`）、大小一致（`size_mismatch`）、开头不是全零（`zero_filled`）、能解析（`unparseable`：JSON / JSONL 整体解析，
  parquet 看首尾魔数并解析 footer，mp4 找 `moov`，JPEG / PNG 看魔数）。列举里有但读不到的文件在 `--visibility-timeout` 秒内反复重试，仍读不到记
  `not_visible_in_time`。交付目录本身读不了是退出码 3（`output_unreachable`）。
- mp4 和 parquet 只按范围读头尾。全部通过才写 `_COMPLETE`；没通过时如果交付目录里有旧的 `_COMPLETE`，会把它删掉。

**task**：`curation task create --file task.json [--wait]`、`list`、`get`、`wait`、`start|pause|resume|stop`、`retry [--modules a,b]`、`continue`、`report [--rev N]`、`adjudication`

- `--json` 时原样打印 Daemon 的响应，包括 `links`。`adjudication` 汇总待裁决条数和裁决页链接（裁决本身只能在网页上做）。
- 每个写请求都带 `Content-Type: application/json`（没有请求体也带）；可带 `--idempotency-key`，重试时用同一个 key，Daemon 不会重复执行。
- `wait` 每 `--poll-interval` 秒（默认 5）查一次，直到任务进入终态且没有运行中的子任务；`--timeout` 到了以退出码 8 结束。
- Daemon 连不上或回的不是 JSON：退出码 3（`daemon_unreachable`）；Daemon 回了错误（含 401、403、404、409、405、5xx）：退出码 7（`rejected`）。

### EEF–视频一致性与 `--param`

- `--param MODULE.KEY=VALUE`（`preflight`、`plan`、`check`，可重复）：任务级模块参数，值按该模块的 `param_schema` 转成数字 / 布尔 / 选项并校验；
  未知模块或参数是用法错误。例如 `--param eef_video_consistency.trajectory_json=PATH`。
- `eef_video_consistency`（设计 12、25）只给带置信度的意见、不判废（D81）：每条先由 CPU 测量第三视角相机（有夹爪参考时）和腕部相机的自运动，再请模型
  复核或对整段标记视频给意见，两个渠道按分项 × 相机取大合并，结论相反时出一条 `conflict`（review，裁决卡 `eef_check`），其余发现都是 info。
- 轨迹的来处按设计 25 §4.1 的顺序：`--param …trajectory_json=` 上传的文件；数据集自己能推出的（UMI 会话、手持夹爪的 LeRobot 导出或原始 mcap，
  标定取数据集声明的 `calibration.handheld`，没有就用内置的 DAS DEMO 标定）；按数据集声明生成（机械臂的位姿记录加相机标定）；数据集自带的
  `trajectory.json`。都没有时预检是 `needs_input: trajectory_missing` 或 `declaration_incomplete`。
- 轨迹与数据集记录的比对（设计 12 §8.7、设计 25 §6.1，只报告）：数据集声明的 `semantics.pose` / `semantics.joints` 说明记录在哪一列或哪个 topic、
  什么布局和单位、哪个机器人；有它就把轨迹与记录逐帧比，写进 `details.record`，报告的 EEF 一节多一行「轨迹与数据集记录(只报告,不参与判决)」
  和明细表 `eef_record`。声明里的记录列不在数据集里时，预检的 `subitems.record_consistency` 报 `record_columns_missing`。
- 两半（设计 23 §2.1、设计 25 F5.24b）：`check --modules eef_video_consistency --prep`（`vlm_prep`）读轨迹、测量、算自运动、比对记录、画带标记的视频、
  拼好请求，不问模型，请求包留在 `scratch/vlm/<episode>/eef_video_consistency/`；`check … --prepared`（与 task_success 同一调用）读回请求、发请求、
  合并、写结果行，结果落盘后删掉这一条的请求包，出错的留着等重试。两半写出的记录与一次 `check` 相同
  （`tests/cli/test_eef_check.py::test_the_two_halves_make_the_records_one_call_makes`）。不问模型时（`--no-vlm` 或 `use_vlm=false`）`--prep` 直接写结果行。
- 模型复核走共用的 VLM 客户端（`--retry` / `--hedge` 同样生效），调用类型 `eef_review`，用量记在这个模块名下，超时默认 120 秒
  （`--set checks.task_success.vlm.timeouts_s.eef_review=S`）。答复缓存在 `checks/eef_video_consistency/cache/`；每行记录带 `input_file_sha256`、
  `config_hash`、`seeds_sha256`、`template_sha256`、`review_config`，`--resume` 只跳过这些都没变的行。
- 实现在 `cli/eef_check.py`、`cli/eef_review.py`、`cli/modparams.py`，模块本身在 `extensions/eef_consistency/`（见[其 README](../extensions/eef_consistency/README.md)）。

### mcap 与 Lance 数据集（D44，F6.5）

读取器 `ingest/mcap_reader.py`、`ingest/lance_reader.py` 是 A 类代码，原样搬来（冻结点 `dev@eb637ba40`）；命令在外面包了一层 `cli/containers.py`，
判决全部出自这两个读取器与算法。合成数据上 mcap、Lance 与 LeRobot 的终判一致（`tools/parity/tests/test_containers_parity.py`）。

| | mcap | Lance |
|---|---|---|
| 认什么 | 数据集根目录下的 `*.mcap`，一个文件一条 episode（子目录里的不算）。文件名是 `episode_<N>.mcap` 的按 N 编号（混进来的其他 `.mcap` 不读，预检给警告），一个都不是就按文件名排序从 0 编 | lerobot-lance-convert（0.3.0 起）的布局：`frames.lance`、`videos.lance`、`meta.lance` 三张表加 `meta/`，`meta/info.json` 带 `storage_format: "lance"`（LeRobot v3.0 元数据）。三表齐但没有这个标记的是旧插件布局，预检报元数据无效；只有别的 Lance 表的是 `lancedb`，不支持 |
| 预检读什么 | 每个文件只读摘要区（通道、消息数、元数据记录），TOS 上是几次按范围读，不读消息。动作、状态、相机、任务文本、机器人型号按 topic 规则认（数据集声明的字段映射优先，其次站点的 `ingest.mcap_mapping`，UMI 的 `/robotN/vio/eef_pose` 自动认）。时间轴取动作 topic 的 `log_time`，`fps` 为 null。任务文本只在 `/task` topic 里的，预检数它有标注但读不到文字 | `meta/`，读法同 LeRobot v3（含 `validate_info`）；缺了 `meta/` 就读 `meta.lance` 里的镜像并给一条警告 |
| 快照记什么 | 全部 `*.mcap`（编号取决于目录里有哪些文件）；`meta_fingerprint` 覆盖全部 mcap 文件 | `meta/` 与三张表的全部对象（读的时候整表读）；`meta_fingerprint` 覆盖 `meta/`，没有 `meta/` 时覆盖 `meta.lance/` |
| EEF–视频一致性 | 可用（F5.13）：trajectory.json 的相机写 `media.uri=episode_<N>.mcap` 与 `media.topic`（图像 topic），模块把该 topic 的 JPEG / H.264 帧转成本地视频读，TOS 上按区间流式读 | 不支持：`unsupported`，原因码 `format_unsupported_by_module` |

- **数据集语义取整个任务的所选**：读取器用所选 episode 的前 100 条判定数据集语义（控制模式、单位等）。命令只读某一段的条目，所以读源数据的命令
  （`check`、`aggregate`）要带 `--selection <整个任务的所选>`（语法同 `--episodes`，Daemon 自动传；不带时取 `--episodes`）。LeRobot 数据集不受影响
  （它的语义样本一直是数据集的前 100 条）。
- **TOS 上的数据先拉到本地再读**：两个读取器只认本地目录。`tos://` 上的数据由 `SourceCache` 在本地留一份副本：mcap 先给每个文件放一个空占位（编号看文件名，
  占位不会被读），读到哪条才下载哪条；Lance 第一次读时整表下载。设了 `CURATION_SOURCE_CACHE` 就放在它下面（`<目录>/<格式>-<地址哈希>/<数据集名>/`，
  同一任务后面的命令复用，Daemon 在运行结束时删掉），没设就放在这条命令自己的临时目录里、命令结束就删。每个副本按列举时的大小和 ETag 核对，
  对不上以退出码 6 结束；只读源桶，凭证只从 `CURATION_INPUT_TOS_*` 环境变量读。
- **临时视频**：读取器转出来的视频写在 `$TMPDIR`，命令结束时清掉。Daemon 把 `TMPDIR` 指到任务的缓存目录下，不占容器的 `/tmp`。
- **`source_info.json`**：第一条读源数据的命令在运行目录里写下报告要用的信息（型号从哪来、时间轴从哪来、有没有任务文本；用到的机器人规格），
  `report` 据此写数据包体检（`report.json` 的 `integrity.container`、`report.md` 的「数据包」一节）。
- **站点开关**：`ingest.mcap_enabled` / `ingest.lance_enabled`（默认开；环境变量 `CURATION_MCAP_ENABLED` / `CURATION_LANCE_ENABLED` 优先于配置）。
  关掉后预检把全部模块标为不支持（原因码 `format_disabled`），读源数据的命令以参数错误结束。Helm 里写在 `pipelineConfigOverride.ingest` 下。
- **依赖**：`pylance`、`mcap`、`mcap-ros2-support`、`mcap-protobuf-support`，版本钉在 `backend/requirements.txt`；都是懒导入，不质检这两种格式时用不到。

## 运维命令

`ls`、`prune`、`fetch` 给在 Pod 里或终端前操作的人用，输出中文文本、没有 `--json`，`curation <命令> --help` 是中文说明。凭证和其他命令一样只走环境变量。

| 命令 | 用法 | 做什么 |
|---|---|---|
| `ls` | `curation ls <本地目录 \| tos://桶/前缀> [--region 地区]` | 列一层内容，先目录后文件，文件带大小 |
| `prune` | `curation prune <交付目录> [--keep-latest N] [--yes]` | 列出交付目录下的历次跑批（`<run_id>/`）：时间、最新结果版本的条数（总数、通过、拒绝、待补跑）、占用，`latest` 指的那次标出来。默认只列不删；加 `--keep-latest N` 看将删哪几次（留最新的 N 次），确认后再加 `--yes` 真删。`latest` 指的那次永远不删；只给 `--yes` 是输入错误（退出码 2） |
| `fetch` | `curation fetch --source ai-infra --ref <源站名> [--include 模式] [--into 数据集根] [--name 落地名] [--overwrite]` | 从内网 HuggingFace 缓存桶拉公开数据集进本站数据集根：先落本地暂存、逐文件校验，再顺序拷入；部分拉取（`--include`）的数据不会被当作完整数据集 |

## Daemon 的调用顺序

一次完整运行（设计 17 §3，计划 2.0）：两块同时跑、互不过滤，每一段都拿任务的全部所选条目：

```
preflight → plan → snapshot
CPU 块：check integrity → check numeric → check frame → check duplicate（逐条交接，各段交叠，D70、D87）
        并排的第二个起点：check --prep，EEF 的 CPU 半段（vlm_prep，注册表 5.3）
VLM 块：check task_success（及 EEF 的模型半段 --prepared，接在 vlm_prep 后面；没有任务标注的条不判成败，D72）
两块都结束 → aggregate --revision N → report --revision N → （同步运行目录到交付目录）→ verify
```

块内的逐条段由 Daemon 的流水线逐条交接（`check --pipeline-state … --pipeline-next …`，常驻 worker）：一条在本段有了记录（判完或出错）就交给下一段，
判废的发现、执行出错都不拦它。判决只在两块都结束后由 `aggregate` 按任务策略算一次。

例外是完整性拦截（D92，设计 28）：`check` 动手前先看完整性对这条的记录，按任务策略判废了就不读数据、不调模型，给每个所选模块写一条跳过记录
（`details.skipped = "integrity_rejected"`，算 `ok`、不算出错，`--resume` 不重做）；VLM 块与 `vlm_prep` 逐条等完整性有了结论才派发。
视频解不开（`InvalidDataError`、找不到 moov 或视频流）是文件坏了：视觉质量、同步、任务成败各报一条判废的 `decode_failed`（FILE-4），不进待补跑（D93）。
`--json` 每个模块多 `skipped_reasons`（按理由的条数），文字输出写「skipped: integrity_rejected 6」（D94）。

人工裁决后的重跑（新版本 N+1，旧版本原样保留）：

```
adjudicate-apply → check task_success --episodes <rerun_task_success>（写新分片）
→ aggregate --revision N+1 → report --revision N+1 → （同步运行目录）→ verify
```

裁决之后不重跑重复检测，它报的重复组不变。

重试（子任务）只补跑出错或缺记录的（模块 × 条目）：每一段只带要补的模块与条目（`check --modules <要补的> --resume`），然后 `aggregate`。

mcap / Lance 数据集的顺序相同，`check`、`aggregate` 多带 `--selection <任务的所选>`；数据在 TOS 上时，每条命令的环境里有 `CURATION_SOURCE_CACHE`
（任务的本地副本）和 `TMPDIR`，运行结束后 Daemon 删掉这个目录。

## 手动验证步骤

在仓库的 `backend/` 目录下执行。Python 用仓库根目录的虚拟环境，临时文件放在 `$TMPDIR`，不需要任何密钥：

```bash
cd backend
PY=../.venv/bin/python
C="$PY -m curation.cli"
D=${TMPDIR:-/tmp}/curation-cli-demo && mkdir -p "$D"
PYTHONPATH=../tools $PY -m parity make-fixture --out "$D/mini"      # 8 条 episode 的 LeRobot v2.1 数据集
```

另开一个终端，同样在 `backend/` 下启动一个本地假模型（OpenAI 兼容接口，答案只看请求内容）：

```bash
PYTHONPATH=../tools ../.venv/bin/python -c "
import time
from tests.cli.fakevlm_server import FakeVlmServer
with FakeVlmServer(port=8766) as s:
    print(s.url, flush=True); time.sleep(3600)"
```

回到原终端：`export CURATION_VLM_ENDPOINT=http://127.0.0.1:8766/v1 CURATION_VLM_MODEL=fake-vlm`。

1. 入口与帮助：`$C --help` 列出上表的原子命令，末尾一行列出运维命令 `ls, prune, fetch`；`$C --version` 输出 `curation <版本>`；
   `$C ls "$D/mini"` 列出 `data/`、`meta/`、`videos/` 和「共 3 个目录、0 个文件」。

2. 预检、计划、快照：

   ```bash
   R=$D/run; mkdir -p "$R"
   $C preflight --input "$D/mini" --vlm-backend ark --json > "$R/preflight.json"
   $C plan --preflight "$R/preflight.json" --episodes 0-7 --out "$R/plan.json" \
     --modules timestamp_check,kinematic_limits,motion_quality,visual_quality,video_action_sync,task_success,duplicate_check
   $C snapshot --input "$D/mini" --episodes 0-7 --out "$R/source_manifest.json"
   ```

   应看到：计划里重复检测一行是 `duplicate  duplicate_check <- selected x1`（并发恒为 1），VLM 段是 `task_success, camera_defects <- selected gates
   {'episode': 64, 'probe': 64}`；快照 `27 objects`（3 个元数据文件 + 8 条 × 3 个文件）。`preflight --json` 的输出可以用
   `$PY -c "import json,sys; from curation.contracts import schemas as s; print(s.errors('cli/preflight.schema.json', json.load(open(sys.argv[1]))))" "$R/preflight.json"`
   核对，应为空列表。

3. 按段跑完一遍（每条几秒）：

   ```bash
   S="--input $D/mini --run-dir $R --source-manifest $R/source_manifest.json"
   $C check --modules timestamp_check,kinematic_limits,motion_quality $S --episodes 0-7
   $C check --modules visual_quality,video_action_sync $S --episodes 0-7
   $C check --modules duplicate_check $S --episodes 0-7
   $C check --modules task_success $S --episodes 0-7
   $C aggregate --run-dir "$R" --revision 1 --episodes 0-7 --input "$D/mini"
   $C report --run-dir "$R" --revision 1
   ```

   应看到：
   - 数值段 `timestamp_check (part 0001): 8 episodes - ok 8, error 0; findings: fragment 1, gap 1`（2 时间戳跳变、5 残段，默认策略下判废）；
   - 重复检测 `findings: duplicate 2`（3 与 7 字节级相同，各得一条 SET-1 发现、互相写明，`checks/duplicate_check/groups.json` 的 `groups` 是 `[[3, 7]]`）；
   - task_success `ok 8, error 0; findings: failure 2, task_text_missing 2, uncertain 4`（1、5 一次请求就判失败；4、6 没有任务标注，不判成败、只报告），
     camera_defects 同时出了 8 条记录；
   - aggregate 的日志 `policy default: 5 kept, 3 rejected, 0 held`，结果 `passed 5, reject 3, held 0; 4 to review`：passed 是 0、3、4、6、7（3 与 7
     都通过，重复只报告），reject 是 1、2、5，review 里 0、3、7 问成败，1 是可复议的判废；`$R/revisions/r0001/policy.json` 的 `preset` 是 `default`。

   假模型的回答只看请求的文字、图片张数和像素尺寸，不看图片字节，所以这些数在 macOS 和 Linux 上一样（`tests/cli/test_fake_model.py`）。
   `cat "$R/revisions/r0001/report.md"` 是中文报告：「参与质检:8 条 = 通过 5 + 拒绝 3 + 待补跑 0」、判决策略、每个模块一行计数，「重复检测」一节末行是
   「重复组 1 组,涉及 2 条(只报告)」；`tail -3 "$R/usage.jsonl"` 是按模块记的 token 用量。没给 `--concurrency`，所以任何时刻只有一个模型请求在飞
   （`tests/cli/test_pipeline_chain.py` 在假模型那边量过）。

4. 交付与核验（本地目录充当交付目录）：

   ```bash
   rsync -a --exclude inflight.json "$R/" "$D/delivery/"      # Daemon 的同步：报告与清单
   $C verify --run-dir "$R" --output "$D/delivery" --visibility-timeout 0 --json
   ```

   应看到：`"failed": []`、`"complete_marker": true`，`$D/delivery/_COMPLETE` 出现。

5. 中断后续跑。先把假模型换成慢速版（在另一个终端 Ctrl-C，把启动命令里的 `FakeVlmServer(port=8766)` 改成 `FakeVlmServer(port=8766, delay_s=0.3)` 再启动），然后：

   ```bash
   cp -R "$R" "$D/run2"; rm -rf "$D/run2/checks/task_success" "$D/run2/checks/camera_defects"
   S2="--input $D/mini --run-dir $D/run2 --source-manifest $D/run2/source_manifest.json"
   $C check --modules task_success $S2 --episodes 0-7 --json > /dev/null 2>&1 & sleep 8; kill -9 $!
   cat "$D/run2/checks/task_success/inflight.json"            # 被杀时在手的 episode
   $C check --modules task_success $S2 --episodes 0-7 --resume --json | $PY -m json.tool | grep -A2 skipped
   ```

   `skipped_existing` 是被杀前已经做完的条数，`part` 是 `0002`；做完后比较两次运行的结果（去掉耗时字段）应完全相同：

   ```bash
   $PY -c "import json,sys; f=lambda p: {r['episode_index']: {k: v for k, v in r.items() if k not in ('elapsed_s', 'evidence')} for r in map(json.loads, open(p))}; print(f(sys.argv[1]) == f(sys.argv[2]))" "$R/checks/task_success/results.jsonl" "$D/run2/checks/task_success/results.jsonl"
   ```

   应输出 `True`。同样的命令改用 `kill -TERM`：进程做完在手的那条再退出，退出码 5，信封 `code` 为 `terminated`。

6. 错误的几种样子：

   ```bash
   $C check --modules task_success $S --episodes 0 --vlm-endpoint http://127.0.0.1:9/v1 --json; echo "exit=$?"
   cp -R "$D/mini" "$D/mini2" && $C snapshot --input "$D/mini2" --out "$D/m2.json" > /dev/null
   printf '\0' >> "$D/mini2/data/chunk-000/episode_000000.parquet"
   $C check --modules timestamp_check --input "$D/mini2" --run-dir "$D/run3" --source-manifest "$D/m2.json" --episodes 5-7 --json; echo "exit=$?"
   $C check --modules timestamp_check --input "$D/mini2" --run-dir "$D/run3b" --episodes 5-7 --json; echo "exit=$?"
   ```

   依次是：端点不通 → `module_failed`，`exit=4`；第 0 条的 parquet 变了，虽然只选了 5–7，但它在语义样本里 → `source_changed`，`details.key` 是那个 parquet，
   `exit=6`；不带快照时这个坏文件让语义判定失败 → `module_failed`「the dataset cannot be read」，`exit=4`。

7. 人工裁决与第二个结果版本：

   ```bash
   cat > "$D/decisions.json" <<'EOF'
   {"schema_version": "2.0", "decisions": [
    {"id": 1, "episode_index": 3, "line": "task_verdict", "decision": "failure", "new_label": null, "note": null, "decided_by": "me", "decided_at": 1790000000000},
    {"id": 2, "episode_index": 4, "line": "task_verdict", "decision": "unsure", "new_label": "wipe the table", "note": null, "decided_by": "me", "decided_at": 1790000000001}]}
   EOF
   $C adjudicate-apply --run-dir "$R" --decisions "$D/decisions.json"      # re-judge task_success: 4
   $C check --modules task_success $S --episodes 4
   $C aggregate --run-dir "$R" --revision 2 --episodes 0-7 --input "$D/mini"
   $C report --run-dir "$R" --revision 2
   ```

   应看到：`applied 2 decision(s) (0 already applied)` 和 `re-judge task_success: 4`；4 按新标注重判（`task_success (part 0002): 1 episodes`），
   假模型判失败；第二版 `passed 3, reject 5, held 0; 4 to review`：3 按人的结论拒，4 成了可复议的判废，7 仍通过、问成败，0 仍待判成败；
   `revisions/r0002/keep.txt` 是 0、6、7。没有再跑重复检测（`ls "$R/checks/duplicate_check/parts"` 仍只有 `0001.jsonl`）。再跑一次 `adjudicate-apply`
   显示 `applied 0 decision(s) (2 already applied)`；`revisions/r0001/` 原样未动。把 `schema_version` 改成 `1.0` 再应用，是参数错误（退出码 2）。

8. `curation task …`（用测试里的桩服务代替 Daemon）。另开一个终端，在 `backend/` 下启动桩：

   ```bash
   ../.venv/bin/python -c "
   import time
   from tests.cli.fakes import StubDaemon, make_task
   with StubDaemon(port=8765) as d:
       d.tasks['task_01'] = make_task('task_01', 'running', pending=3)
       print(d.url, flush=True); time.sleep(3600)"
   ```

   回到原终端：

   ```bash
   export CURATOR_URL=http://127.0.0.1:8765/curation CURATOR_USER=agent CURATOR_PASSWORD=s3cret-pw
   $C task get task_01                 # 状态、待裁决条数和三条链接
   $C task adjudication task_01 --json # pending 3，附裁决页链接
   $C task resume task_01 --json; echo "exit=$?"   # 409 task_state_conflict → exit=7，details.rest_error 是 REST 错误体
   $C task wait task_01 --poll-interval 1 --timeout 2 --json; echo "exit=$?"   # 任务没结束 → exit=8，details.task 是当时的任务
   CURATOR_PASSWORD=wrong $C task get task_01 --json; echo "exit=$?"   # 401 → exit=7
   CURATOR_URL=http://127.0.0.1:9/curation $C task get task_01 --json; echo "exit=$?"   # 连不上 → exit=3
   ```

9. 运维命令 `prune`。拿第 4 步的交付造一个有三次跑批的交付目录：

   ```bash
   O=$D/out; mkdir -p "$O"
   for r in 20261010-090000 20261011-090000 20261011-100000; do cp -R "$D/delivery" "$O/$r"; done
   printf 20261011-090000 > "$O/latest"
   $C prune "$O"
   $C prune "$O" --keep-latest 1
   $C prune "$O" --yes; echo "exit=$?"
   $C prune "$O" --keep-latest 1 --yes; ls "$O"
   ```

   应看到：第一条列出 3 次跑批（新的在前），每行是 `r0001：共 8 条，通过 5、拒绝 3、待补跑 0` 和占用，`20261011-090000` 那行带 `← latest`，末尾提示默认只列不删；
   第二条把 `20261010-090000` 标为「删除」（留最新 1 次，`latest` 指的那次也留），只预告、不删；第三条报「--yes 必须和 --keep-latest N 一起给」，`exit=2`；
   第四条删掉 `20261010-090000`，`ls` 剩另外两次和 `latest`。

10. mcap 与 Lance（D44）。同一份 8 条合成数据换成两种格式，按第 3、4 步的顺序跑（假模型照旧在另一个终端）：

    ```bash
    PYTHONPATH=../tools $PY -m parity make-fixture --format mcap --out "$D/mini_mcap"     # episode_0.mcap … episode_7.mcap（cdr 编码）
    PYTHONPATH=../tools $PY -m parity make-fixture --format lance --out "$D/mini_lance"   # frames / videos / meta 三张表 + meta/
    for F in mcap lance; do
      IN=$D/mini_$F; R=$D/run_$F; mkdir -p "$R"
      $C preflight --input "$IN" --vlm-backend ark --json > "$R/preflight.json"
      $C snapshot --input "$IN" --episodes 0-7 --out "$R/source_manifest.json"
      S="--input $IN --run-dir $R --source-manifest $R/source_manifest.json --selection 0-7"
      $C check --modules timestamp_check,kinematic_limits,motion_quality $S --episodes 0-7
      $C check --modules visual_quality,video_action_sync $S --episodes 0-7
      $C check --modules duplicate_check $S --episodes 0-7
      $C check --modules task_success $S --episodes 0-7
      $C aggregate --run-dir "$R" --revision 1 --episodes 0-7 --input "$IN" --selection 0-7
      $C report --run-dir "$R" --revision 1
      rsync -a --exclude inflight.json "$R/" "$D/delivery_$F/"
      $C verify --run-dir "$R" --output "$D/delivery_$F" --visibility-timeout 0 --json | grep -E '"failed"|complete_marker'
    done
    ```

    应看到：预检 `kind` 分别是 `mcap`（`version: null`，`fps: null`，detail 写着时间轴取自动作 topic 的 `log_time`）和 `lance`（`version: v3`），8 条、
    2 路相机、`robot_type: franka`、6 条有标注；EEF 模块在 mcap 上是 `needs_input`（给了 trajectory.json 就能用，F5.13），在 Lance 上是 `unsupported`
    （`format_unsupported_by_module`），其余可用。终判与第 3 步的 LeRobot 数据集相同：`passed 5, reject 3, held 0; 4 to review`，重复检测 `findings: duplicate 2`。
    （mcap 上第 2 条的画面尺寸不同，假模型对它答「失败」而不是「拿不准」；它本来就因时间戳跳变被拒，终判不变。）`report.md` 多一节「数据包(mcap)」/
    「数据包(lance)」：型号、时间轴、任务文本三项体检。两边 `verify` 都是 `"failed": []`、`"complete_marker": true`。`ls $TMPDIR` 里不留读取器转出的视频。
    TOS 上的读法（本地副本、按范围读摘要、源对象变化退出码 6、只读源桶）由 `tests/cli/test_containers.py` 在假 TOS 上核对。

11. 数据完整性（设计 14）。先看预检的三条警告，数据集是第 10 步两份夹具的副本：

    ```bash
    cp -r "$D/mini" "$D/bad" && cp -r "$D/mini_mcap" "$D/bad_mcap"
    : > "$D/bad/videos/chunk-000/observation.images.wrist/episode_000002.mp4"       # 0 字节
    $PY -c "import os,sys; p=sys.argv[1]; os.truncate(p, os.path.getsize(p)//2)" "$D/bad_mcap/episode_4.mcap"   # 录制中断
    $C preflight --input "$D/bad" --json | $PY -c "import json,sys; print(json.load(sys.stdin)['warnings'])"
    $C preflight --input "$D/bad_mcap" --json | $PY -c "import json,sys; print(json.load(sys.stdin)['warnings'])"
    ```

    应看到：LeRobot 一条 `1 file is empty or too small to be valid (1 episode: 2; videos/…/episode_000002.mp4 0 B)`；mcap 一条
    `1 episode (4) was cut off while recording (no mcap end marker); …`。两份的模块可用性与干净数据集相同。再对这两份跑数据完整性模块
    （`$C check --modules data_integrity --input "$D/bad" --run-dir "$D/run_bad" --episodes 0-7`，mcap 同理）：LeRobot 是 `findings: file_empty 1`
    （第 2 条，默认策略下判废）；mcap 是 `findings: cut_unreadable 1, stream_missing 1`（第 4 条录制中断且读不出数据，判废；第 6 条缺 `/task`，转人工）。
    更多损坏样本与裁决见[模块 README](../extensions/integrity/README.md)的手动验证。

12. 数据集自己的编号（F12.8）。从第 10 步的 mcap 夹具改名出一份编号不连续的数据集：

    ```bash
    mkdir -p "$D/numbered" && for p in 0:5 1:9 2:12 3:20; do cp "$D/mini_mcap/episode_${p%%:*}.mcap" "$D/numbered/episode_${p##*:}.mcap"; done
    $C preflight --input "$D/numbered" --json > "$D/numbered.pf.json" && $PY -c "import json,sys; print(json.load(open(sys.argv[1]))['dataset']['episode_indices'])" "$D/numbered.pf.json"
    $C plan --preflight "$D/numbered.pf.json" --modules timestamp_check --episodes 5,9 --json > /dev/null && echo plan ok
    $C plan --preflight "$D/numbered.pf.json" --modules timestamp_check --episodes 0 --json; echo "exit $?"
    ```

    应看到：`5,9,12,20`；`plan ok`；最后一条以退出码 2 结束，错误写 `--episodes: 1 index(es) not in the dataset's 4 episodes (5, 9, 12, 20) (first 0)`。
    编号是 0 … count-1 的数据集（如 `$D/mini`），预检里没有 `episode_indices`。

13. 自动化测试（CLI 约 9 分钟）：

    ```bash
    $PY -m pytest -q tests/cli tests/contracts tests/planner
    $PY -m curation.contracts check
    $PY -m pytest -q curation/tests --ignore=curation/tests/test_environment.py   # 内核单测
    (cd .. && PYTHONPATH=tools $PY -m pytest -q tools/parity/tests)               # 对账工具：黄金基线的录制与回放
    ```

    `tests/cli` 里：`test_pipeline_chain.py` 在夹具上按上面的顺序跑完整条链并逐个校验契约；`test_check_resume.py` 是 SIGTERM / SIGKILL 后续跑；
    `test_errors_and_policy.py` 是出错与拿不准的区分、默认不并发不重试、源数据变化；`test_aggregate.py` 是聚合与裁决的规则；`test_relabel.py` 是改标重判；
    `test_revision_flow.py` 是第 7 步的第二个版本；`test_fake_model.py` 钉住假模型的答案与图片字节无关。运维命令的用例在 `curation/tests/test_ops.py`。
