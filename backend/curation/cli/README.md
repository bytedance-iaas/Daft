# curation 命令行（v2，工作包 W3）

`curation` 是 Curator v2 的命令行入口，设计见 `docs/design/02-cli-contract.md`，契约见 `docs/contracts/`（C2 输出、C3 进度、C4 REST）。
一条命令只做一件事，命令之间通过运行目录里的文件交接；组合由 Daemon（或脚本）按固定顺序完成（见下文「Daemon 的调用顺序」）。

| 命令 | 作用 | `--json` 契约 |
|---|---|---|
| `curation preflight` | 只读 metadata，判格式、数 episode、给出每个模块「可跑 / 需补充 / 不支持」及原因（F2.7） | `cli/preflight.schema.json` |
| `curation plan` | 执行计划：分档、每档并发度与八把闸门、VLM 请求合并提议（纯计算，W6 的库） | `cli/plan.schema.json` |
| `curation snapshot` | 固化任务要读的源对象清单（键、大小、ETag 或修改时间），写 `source_manifest.json` | `cli/source-manifest.schema.json` |
| `curation check` | 跑**一档**的模块（数值档、帧档、VLM 档，或 `dedup`），每条 episode 一行结果 | `cli/check.schema.json` |
| `curation aggregate` | `funnel`：每条 keep / drop / held 与 `keep.txt`；`final`：passed / reject / held 三个清单和 review 视图 | `cli/aggregate.schema.json` |
| `curation adjudicate-apply` | 应用本任务的人工裁决（不调模型），列出接下来要重跑什么 | `cli/adjudicate-apply.schema.json` |
| `curation report` | 一个结果版本的 `report.md` / `report.json` / `perf.json` / 明细表，最后写 `commit.json` | `cli/report-output.schema.json` |
| `curation verify` | 从交付目录并行回读每个关键文件，全部通过才最后写 `_COMPLETE` | `cli/verify.schema.json` |
| `curation task …` | Daemon REST API 的薄客户端，给 Agent 和脚本用，输出带 `links` | `openapi.yaml` 里对应接口的响应，原样打印 |

v1 的子命令（`run`、`rejudge`、`review-page`、`prune`、`ls`、`fetch`、`backends`、`public` 和隐藏的 `reprofile`）原样转交给 `legacy.py`（即原来的 `curation/cli.py`，只改了 import 路径），用法和输出都不变。

## 代码结构

命令行（`curation/cli/`）：

| 文件 | 内容 |
|---|---|
| `app.py` | 参数解析、v1 子命令转交、`main()`（`curation.cli:main` 就是它） |
| `framework.py` | 输出纪律、C3 进度事件、错误信封与退出码、SIGTERM / SIGINT |
| `errors.py` | 退出码与对应的异常类 |
| `creds.py` | 凭证只从环境变量读；输入、输出两套访问密钥 |
| `storage.py` | 本地目录与 `tos://` 的统一读写（列举、按范围读、上传、删除、写 `_COMPLETE`） |
| `lerobot_meta.py` | 不读样本数据的 LeRobot 元数据读取：格式识别、episode 表、每条的文件键 |
| `containers.py` / `preflight_containers.py` | mcap 与 Lance（D44）：识别、按 v1 的规则给 mcap 文件编号、按范围读 mcap 的摘要区、读 Lance 的 `meta/`、TOS 数据的本地副本（`SourceCache`）、`source_info.json`；这两种格式的预检 |
| `runctx.py` | 流水线命令共用的部分：运行目录、`--input` / `--source-manifest`、VLM 参数与三个行为开关、闸门、配置、`VlmSession`（探活、传输策略、用量记账） |
| `preflight.py` / `plan.py` / `snapshot.py` / `check.py` / `aggregate.py` / `adjudicate.py` / `report_cmd.py` / `verify.py` / `task_client.py` | 各条命令 |
| `source_manifest.py` | `source_manifest.json` 的生成与校验（`--source-manifest`，对不上退出码 6） |
| `episodes.py` / `inputs.py` | `--episodes` 语法（含 `@文件`）、`--input` / `--source` |

编排壳（`curation/pipeline/`，v2 新增；算法仍是 v1 的 A 类代码，一行没改）：

| 文件 | 内容 |
|---|---|
| `records.py` | 结果行格式（`cli/result-record.schema.json`）、分片文件、`results.jsonl` 压实、`inflight.json`、原子写 |
| `check_stage.py` | 一次 `check` 调用：自建线程池、`--resume`、崩溃计数、VLM 档熔断 |
| `rows.py` | 逐条取样本行，与 v1 惰性扫描（`LeRobotDataSource`）同一个源对象、同一套字段；mcap / Lance 用 v1 的两个读取器（`ContainerRowSource`） |
| `incidents.py` | D33 的执行事故登记：模型调用、机位解码、崩溃，登记后原样抛出，算法看到的和以前一样 |
| `vlm_policy.py` | 传输策略：对冲开关、外层重试、文本调用一次一发、用量记账（W6 的两本账） |
| `tasktext.py` | 任务描述的来源：原始标注 / 自产 caption / 人工改标（改标的重判口径） |
| `skipped.py` | 缺源文件、照 v1 跳过的条（D40）：快照里的 `skipped_episodes` 与读到才发现的 `skipped_episodes.json` |
| `aggregate.py` / `adjudication.py` / `reporting.py` | 聚合判决、人工裁决、报告 |
| `funnel.py` / `run.py` | v1 的编排（B 类），只把闭包里的构建函数提到模块级；`curation run` 仍走它们 |
| `rejudge.py` | v1 的 rejudge（B 类）；改标重判的函数体提成 `rerun_task_success`，`check` 按 v1 口径重判时调的就是它（D39） |

报告（`curation/export/`）：报告正文 `report.py`、证据帧 `evidence.py`、同步曲线 `sync_plots.py`、明细 `detail_labels.py` /
`task_trace.py` / `timeline.py` / `episode_stats.py`、复核页 `review_page.py`，交付目录的安全写入 `safe_write.py`。
数据集写出器随 D69 下线（包名沿用，里面只剩出报告的那一半）。

## 全局约定

**全局参数**（02 篇 §2）：原子命令都接受 `--json`、`--log-level`、`--config`、`--set k=v`、`--region` / `--input-region` / `--output-region`；`curation task …` 只接受 `--json`、`--log-level`（连接参数另见下文）。参数都写在子命令之后，例如 `curation preflight --input … --json`。

**输出纪律**：

- 带 `--json` 时，stdout 只有一个 JSON 文档：成功时是命令结果，非零退出时是错误信封（`cli/error.schema.json`）。stderr 每行一个 C3 事件（`progress` / `log` / `usage` / `throttle`）。v1 库代码里的 `print` 不会混进 stdout，会被转成 `log` 事件。
- 不带 `--json` 时，stdout 是给人看的结果，日志和进度走 stderr；运行不到 1 秒的阶段不打进度行。

**退出码**（02 篇 §4，C2 1.1）：

| 码 | `error.code` | 含义 |
|---|---|---|
| 0 | — | 成功。**不等于每条都成功**：看 `--json` 里的逐状态计数和 `error_episodes`（`verify` 有坏文件时也是 0，看 `failed`） |
| 1 | `internal` | 命令自身的意外错误（bug），调用栈以 `error` 级日志写到 stderr |
| 2 | `usage` | 参数或配置错误；Daemon 调用时属于 bug |
| 3 | `input_unreachable` / `output_unreachable` / `daemon_unreachable` | 读不到输入数据集、交付目录，或（只有 `curation task`）Daemon 没有应答 |
| 4 | `module_failed` | 模块整体跑不了：VLM 端点不通、熔断、数据集读不了（语义样本坏了）、规格库里没有这个机器人型号、技能归纳要的文本调用失败 |
| 5 | `terminated` | 收到 SIGTERM：不再开始新的 episode，已在做的做完、落盘后退出 |
| 6 | `source_changed` | 源数据和 `--source-manifest` 对不上 |
| 7 | `rejected` | `curation task`：Daemon 回了错误（4xx / 5xx，含 405 `method_not_allowed`），REST 错误体在 `error.details.rest_error` |
| 8 | `wait_timeout` | `curation task … --wait` / `wait`：到了 `--timeout` 任务还没结束，当时的任务 JSON 在 `error.details.task` |
| 130 | `interrupted` | 收到 SIGINT，立即中止（在飞的请求放弃，已落盘的结果不丢） |

**三个行为开关，默认全关**（02 篇 §1 第 4 条，需求的硬要求）：

| 开关 | 不给时 | 给了时 |
|---|---|---|
| `--concurrency N` | CPU 档一次一条；VLM 档八把闸门全是 1，任何时刻只有一个模型请求在飞 | CPU 档同时 N 条；VLM 档按并行度 N 推导闸门（04 篇 §2.2，N=64 与 v1 出厂值逐项相等） |
| `--retry N` | 一次调用就是一次尝试，失败就记成这条的执行错误 | 超时、连接错误、5xx、429 隔 1 s / 2 s / 4 s 再来，最多 N 次（429 至少等 `Retry-After`） |
| `--hedge` | 一次调用就是一个 HTTP 请求 | 走 v1 的对冲补发（超时线上补发一枪、5xx 串行重发一次） |

视频判定请求的超时**随请求里的视频量走**（2026-10-08）：配置的 `timeouts_s.probe`（默认 60 秒）只是下限，实际超时 = max(下限, 每帧 0.5 秒 × 机位数 × 秒数 × 采样帧率)，封顶 600 秒；对冲线随之后移。DROID 三路 18 秒 5fps 的一条约 135 秒，anchor 那种几秒的片子仍是 60 秒。之前固定 60 秒，长视频几乎每条都在超时线上多补一枪、再超时再重发，三分之一的请求白打。

v1 的纯文本调用（技能归纳、标注审计、判废护栏的语义比对）自带 4 次尝试、闸门不低于 2；v2 命令下它也是一次一发、闸门按给定大小，`--retry 3` 就是 v1 的行为。Daemon 按任务参数传 `--hedge --retry 3 --plan-stage …`（与 v1 一致）；直接用命令行的人默认拿到的是「一次直来直去」的执行。

**VLM**：`--vlm-backend <名>`（站点配置 `vlm_backends` 里的预设）或 `--vlm-endpoint <URL> --vlm-model <名>`（缺省取 `$CURATION_VLM_ENDPOINT` / `$CURATION_VLM_MODEL`）；密钥只放在环境变量里，`--vlm-api-key-env <变量名>` 指明是哪个变量（缺省 `ARK_API_KEY`）。每条调模型的命令先 `GET /models` 探活一次，不通就退出码 4，不会逐条报错。
`check` 可加 `--vlm-reasoning-effort <档位>`：给了才在每个模型请求里带 `reasoning_effort`，值原样转交、命令行不校验；不给时请求与 v1 逐字节相同（v1 从不发这个字段）。
`check` 的 `--no-vlm`（设计 25 D84）：这次调用没有模型后端（任务没选模型时 Daemon 传）——带模型开关的模块（EEF，`use_vlm`）只跑 CPU 的那部分、
模型一路记 `no_vlm_backend`；所选模块里有要模型的（`task_success`）时是用法错误。不带它时照旧用 `--vlm-*` 或配置里的端点。

**凭证只走环境变量**，没有任何参数接收密钥（argv 在 `ps` 里全局可见）：

| 用途 | 环境变量 |
|---|---|
| 读输入数据集 | `CURATION_INPUT_TOS_ACCESS_KEY` / `CURATION_INPUT_TOS_SECRET_KEY`（可选 `CURATION_INPUT_TOS_SESSION_TOKEN`） |
| 写、读交付目录 | `CURATION_OUTPUT_TOS_ACCESS_KEY` / `CURATION_OUTPUT_TOS_SECRET_KEY`（可选 `CURATION_OUTPUT_TOS_SESSION_TOKEN`） |
| 上面某一组没设时的回落 | `TOS_ACCESS_KEY` / `TOS_SECRET_KEY`（v1 既有） |
| 模型 | `--vlm-api-key-env` 指定的变量（缺省 `ARK_API_KEY`） |
| `curation task …` | `CURATOR_URL`（含挂载前缀，如 `https://host/curation`，也可用 `--url`）、`CURATOR_USER` / `CURATOR_PASSWORD` |

一组变量只设了一半（只有 AK 或只有 SK）会直接报错，不会悄悄回落到另一组。输入永远不借用输出的密钥，反之亦然。地区与端点沿用 v1 规则：`--input-region` / `--output-region` > `--region` > `TOS_REGION` > `TOS_ENDPOINT` 里的地区 > `cn-beijing`。

## 运行目录

所有流水线命令都在一个本地运行目录（`--run-dir`）里读写，Daemon 边跑边把它同步到交付目录（00 篇 §4.2）：

```
<run-dir>/
  preflight.json  plan.json  source_manifest.json
  checks/<module>/parts/0001.jsonl …           每次 check 调用写一个新分片，逐条追加、逐行 fsync；高编号的分片优先
  checks/<module>/results.jsonl                压实后的当前结果（每条一行，按下标排序）
  checks/<module>/inflight.json                正在处理的 episode（进程被杀后留下，--resume 读它）
  checks/<module>/crashes.json                 每条 episode 害死进程的次数
  checks/video_action_sync/curves/ …           同步曲线（按 pipeline.sync_plots）
  checks/dedup/groups.json                     去重的遍历顺序、撞车组、剔除的重复对
  skipped_episodes.json                        不带快照时 check 读到才发现缺源文件、照 v1 跳过的条（D40）
  adjudication/{applied.jsonl, labels.json}    已应用的人工裁决、人工改标（改标带重判口径 relabel_rerun）
  human-decisions/*.csv                        本任务裁决的 v1 格式副本
  revisions/r0001/                             一个结果版本：verdicts.jsonl、keep.txt、passed/reject/held/review.json、
                                               label_audit.json、adjudications.json、report.md、report.json、perf.json、
                                               tables/*.parquet，最后是 commit.json（有它才算提交，之后不再改写）
  details/vlm_latency.csv  details/evidence/   每次模型请求的时延、取证图
  usage.jsonl                                  token 用量（C3 usage 行的落盘副本）
  source_info.json                             mcap / Lance（D44）：读取器给出的数据集信息与用到的机器人规格，report 据此写「数据包」一节
  logs/
```

## 各命令要点

**preflight**：`curation preflight --input <tos://… | 本地目录 | 公共数据集名> [--source tos|public|local] [--vlm-backend 名] [--embodiment-id 型号] [--modules a,b] [--source-manifest 文件] [--declaration 文件] --json`

- `--declaration`：数据集声明（C7 `dataset-declaration/1.0`，设计 25 §3）。EEF 条目的 `trajectory_source` 说轨迹从哪来：声明够了就是 `generate`；不给声明时预检按元数据
  起草一份，只用来说缺什么（`missing_declaration` 的 `missing`），从不拿它生成。`check` 同样收 `--declaration`（Daemon 传任务开跑时冻结的那份），EEF 据此逐条生成轨迹。

- 只列目录、只读 `meta/` 下的文件，不读 parquet 和视频（mcap 只读每个文件的摘要区，见下文「mcap 与 Lance 数据集」）。
- 认得 LeRobot v2/v3、mcap 与 Lance（lerobot-lance-convert 0.3.0 起的三表布局，D44）。其余（`.rrd`、别的 Lance 表、其他 LeRobot 版本、认不出的目录）所有模块都是 `unsupported`，原因写明检测到的格式（`format_unsupported`）。
- `info.json` 结构校验沿用 v1 的 `validate_info`，报错原文放进 `validation`（中文，照抄给用户）。
- 运动学极限：型号读到且在规格库里是 `available`；读到但不在规格库里是 `unsupported`，只跳过这一项，其余模块照常；读不到（缺失、空串或 `unknown`）是 `needs_input`，`input_hint.options` 列出规格库的 9 个型号。`--embodiment-id` 覆盖 `robot_type`，与 v1 相同。
- 两个 VLM 模块：没传 `--vlm-backend` 是 `needs_input`（`input_hint.field = "vlm"`）；没有任务标注不会让它们标灰，只在 `notes` 里提示这些条不判成败（D72）。
  EEF–视频一致性（注册表 5.2 起不再必须有模型）：没传 `--vlm-backend` 照常 `available`，`notes` 末尾一条 `vlm_backend_missing: …` 作提醒；
  `--param eef_video_consistency.use_vlm=false` 时不提醒。它的条目另有 `trajectory_source`、`cameras`（每路相机的安装方式、归属、能否画与原因）
  与 `applicable_params`（这个数据集上适用的参数：有第三视角相机才有夹爪参考，停用的参数不在内）。
- `--modules` 只报告所选模块，没选的模块不追问。原因文案用英文，`validation` 里 v1 的报错保持中文原文。
- 列目录时发现缺文件的条写进 `warnings`：LeRobot v2 缺数据 parquet 或某个机位视频的条照 v1 跳过（见 snapshot）；v3 的不跳过，检查时读不了、记为出错。
- 编号不是 0 … count-1 时（从大数据集里取出、没有重新编号的子集；mcap 按文件名 `episode_<N>.mcap` 编号），`dataset.episode_indices` 写出全部编号，写法同 `--episodes`（如 `1,3,5`、`2604-2626`），`warnings` 里另有一句提示；编号正常的数据集没有这个字段（F12.8）。
- LeRobot 数据集另写给数据可视化看的三项（设计 18 §7，C2 只加可选字段）：`dataset.features`（info.json 的特征表，names 的几种写法摊平成一个列表）、`dataset.camera_info`（与 `cameras` 同序：编码、尺寸、fps，
  浏览器放不了的编码如 mpeg4 标 `needs_transcode`）、`dataset.segment_sources`（认出来的分段标注写法，认不出的写 `supported: false` 与「标注格式不支持」的原因）。只读 `meta/`，每张小表最多读开头 16 KiB。
- 另有三条完整性警告（D52，设计 14 §1），都不多读数据、不改变模块可用性：数据与视频文件为空或小到放不下该格式的固定字节
  （parquet 小于 12 字节、mcap 小于 45 字节、mp4 小于 512 字节）；mcap 录制中断（文件尾没有结束标识）；mcap 摘要区的 CRC
  不符（摘要区的字节读摘要时本来就取回了）。要读数据的检查归质检最前面的「数据完整性」模块。
- Git LFS 指针（2026-10-05）：认出格式后，清单里不到 1 KiB 的数据文件取前 5 个读开头；是 LFS 指针（没装 Git LFS 的 clone 传上来的）就判
  `metadata_invalid`，原因 `Git LFS pointer files instead of the data: <文件> … fetch the files (git lfs pull …, or hf download) and upload them again`。
  手动验证：`printf 'version https://git-lfs.github.com/spec/v1\noid sha256:%064d\nsize 2265\n' 0 > <数据集副本>/meta/tasks.parquet` 后预检，`format.detail` 里是这句。

**plan**：`curation plan --preflight pf.json --modules a,b,… [--episodes 表达式] [--unlabeled 表达式] [--vlm-parallelism N] [--backend-parallelism N] [--task-vlm-parallelism N] [--task-cpu-concurrency N] [--cpu-cores N] [--running-tasks N] [--site-config 文件] [--out plan.json] --json`

- 不读数据、不联网。并行度取各层上限里最小的那个（D31），`limits.*.bound_by` 写明卡在哪一层；`--running-tasks` 大于 1 时均分。
- 各档的闸门由并行度推导；`check` 用 `--plan-stage plan.json` 取自己那一档（整份计划按模块或档名挑，单独一档的 JSON 也行）。
- `--episodes` 不给就是数据集的全部条目；给了就要都在数据集里——按预检的 `episode_indices`，没有它时是 0 … count-1。不在的是用法错误（退出码 2），提示写明数据集有哪些编号（F12.8）。

**snapshot**：`curation snapshot --input … [--episodes 表达式] --out <运行目录>/source_manifest.json --json`

- 记录 `meta/` 下全部文件、所选 episode 的 parquet 与各机位视频，以及数据集**前 100 条**的 parquet：v1 不管选了哪些条，都用前 100 条的数值判定数据集语义（控制模式、单位等），它们一变，所选各条的判定就可能变。TOS 记 ETag，本地记 `mtime_ns`。
- `--episodes` 支持 `34`、`10-20`、`3,10-12` 和 `@文件`（每行一个表达式，`#` 之后是注释）；全部越界报参数错误，部分越界跑交集并警告。
- 之后任何读源数据的命令带上 `--source-manifest`，它要读的对象（元数据、语义样本、所选各条的文件）有一个变了就以退出码 6 结束，在读之前就停。快照时就缺的文件、现在仍缺，不算变化。
- mcap / Lance 记的对象不同（全部 mcap 文件；Lance 的 `meta/` 与三张表），见下文。
- 远端数据集的对象清单只列一次：`snapshot` 把清单存在 `--out` 旁边的 `.source_listing.json`（隐藏，不交付），带 `--source-manifest` 的命令读它、交给源文件守卫、喂给 v1 的读端（`dsfs.seed`），不再向 TOS 列（`cli/listing_cache.py`）。一小时过期；用之前 HEAD `meta/` 下的对象，变了就重新列；Daemon 续跑、重试前删掉它。
- LeRobot v2 的某条缺数据 parquet 或任一机位的视频（v1 的 `_v2_missing`）时，这条照 v1 跳过（D40）：记进 `skipped_episodes`（写明缺了哪些文件），带 `--source-manifest` 的命令都不读它；它不进任何清单、不计入总数，报告的完整性一块列出。v3 数据集不跳（v1 会去读），缺文件的条在检查时读不了，记为出错。

**没有任务标注的条目**（D72 / D73）：不补描述、不判成败，但画面照看。`check --modules task_success` 给它发一次只问画面缺陷的请求
（`CAMERAS_ONLY_PROMPT`：同样的机位、同样的三项缺陷定义，不问成败），写一条 `status: ok` 的记录：`details.skipped = "no_task_text"`，
`cameras` 里是每路相机的 `camera_check`（镜头画面缺陷模块由此出记录），发现只有 LABEL-2 的 `task_text_missing`（仅报告），TASK-4 / LABEL-4 记为无法评估（`no_task_text`）；
不转人工、不 held，`--resume` 不重做；请求失败按执行错误记，held 等重试。这条的去留由其余模块决定，通过时清单里 `task_text` 为 null。

**判废护栏**（D73 起没有）：模型判失败就是判废（可复议），不再多发请求去核对标注；每条恰好 1 次模型请求。

**check**：`curation check --modules <一档的模块> --input … --run-dir … --episodes 表达式 [--source-manifest …] [--part NNNN] [--resume] [--plan-stage …] [--survivors-out 文件] [--incremental] [--max-episodes N] [VLM 参数] [行为开关] --json`

- 一次只跑一档（D18）：`data_integrity`（完整性档，漏斗最前，设计 14）、`timestamp_check,kinematic_limits,motion_quality`（数值档）、`visual_quality,video_action_sync`（帧档，共用一次解码）、`task_success`（VLM 档，随附 `camera_defects`），或 `dedup`（对整个 keep 集合一次跑完）。混档是参数错误。
- `data_integrity`（D50、D51）：文件结构、整读（mcap CRC、零填充、parquet 数据页）、v1 的逐条结构校验，`--param data_integrity.decode_test=true` 时逐帧解码；坏了判废、可疑的留给人（`integrity_check`），存储读失败算出错。数据集级发现写 `checks/data_integrity/dataset.json`。详见 [模块 README](../extensions/integrity/README.md)。
- `camera_defects`（registry 1.14）：`task_success` 的随附模块，`--modules task_success` 自动带上它，不必列出也不能单独跑。
  判定请求的每路相机回答里多一个 `camera_check` 字段（花屏 / 抖动 / 镜头污染），没有额外的模型调用；记录只出
  `abstain`（`passed=score=null`），不影响判决。详见设计 13「逐机位画面缺陷」。
- 每条做完立刻追加到 `checks/<module>/parts/<part>.jsonl`（整行写入并 fsync）；`--part` 不给时取比现有最大编号大一的号。结束时重写 `results.jsonl`。
- `--survivors-out` 写出进入下一档的条（本档硬门没拦下、也没出错的），Daemon 用它当下一档的 `--episodes @文件`，与 v1 的漏斗一致。
- **出错与弃权严格分开**（D33）：模型正常答了「看不清 / 拿不准」是 `abstain`（或 `unclear`），不是错；一次模型调用最终失败、一路机位解码失败、样本读不了、进程两次死在这条上，这条就是 `verdict = error`，`error.incidents` 写明步骤、调用类型、机位、原因和尝试次数——即使 v1 的兜底逻辑仍给出了结论（结论照旧留在 `passed` / `score` / `details` 里备查）。单条出错不影响其他条，命令仍以 0 退出；`--json` 给逐状态计数、`error_episodes` 和输入集合的指纹 `input_digest`。
- 改了标的条（`adjudication/labels.json`）按新标注重判，口径按裁决时记下的 `relabel_rerun`（D39）：`v1`（缺省）就是 v1 的 rejudge——多视角联合打分加逐机位末态复核，参数相同，不跑任务类型识别、机位提示、判废护栏和取证仲裁，发出的请求与 v1 逐字节相同（对账工具的 `dump-v1 -- rejudge` 与 `run-v2 --from` 逐位核对）；`full` 是首轮的完整流程。记录的 `details` 写明 `task_desc`（新标注）、`task_desc_source`（`人工改标`）和 `relabel_rerun`。
- 不带 `--source-manifest` 时，读到才发现缺源文件的条（规则同 snapshot，只对 v2）不写结果行，列在 `--json` 的 `skipped_missing_source` 里并记进 `skipped_episodes.json`，不计入总数；其它读失败照旧记为出错（聚合时 held）。
- `--resume`：跳过已有非错误结果的条。SIGTERM 时做完在手的条再退出（退出码 5）；SIGKILL 后 `inflight.json` 留着当时在手的条，下次 `--resume` 把它们的崩溃次数加一，重跑；同一条两次出现在死掉的进程手里就记为 `error`（步骤 `crash`）并跳过（P14）。中断后续跑的结果与一次跑完逐字段相同（耗时字段除外）。
- VLM 档熔断（P15）：开头连续 20 条都因为基础设施原因出错（连不上、超时、5xx、限流），整个模块以退出码 4 结束，不再烧配额。
- **dedup 是逐条的**（D70）：一条进来算 action 哈希（便宜），撞车才读视频算内容指纹（只读撞上的那两条）；
  记录的 `details.action_hash`（撞车的还有 `fingerprint`）让 `--resume` 和重试能把状态读回来。
  这一段结束时把每组定下来：留下标最小的那条，说法不对的补一条记录（只有真撞车才发生），然后写 `groups.json`。
- 技能画像已下线（D68）。下面这段关于它的说明仅对 v1 的 `curation run` 还成立：dedup 判定为字节级重复的条不进画像（由人捞回的条除外，见 aggregate）。`--incremental` 保留已有的分类体系，只动变化的部分（v1 的 `_sync_profile`）：不在 `--episodes` 里的条移出画像（弃用、人工判失败、改标重判仍失败），改标的条按新标注重新归类，由人带回交付的条（复议捞回、对拒绝条目人工判成功）不调模型打 caption、直接按文本归类（人工改标，否则原始标注，否则 autolabel 的 caption，都没有就留「未归类」），其余新加入的条先打 caption 再归类。分类体系要的文本调用最终失败，模块以退出码 4 结束。

**aggregate**：`curation aggregate --run-dir … --phase funnel|final [--revision N] [--modules a,b] [--episodes 表达式] [--input …] --json`

- 纯计算、秒级、每次全量重算。模块取 `--modules`，否则取 `plan.json`；episode 取 `--episodes`，否则取有结果的全部。
- 判决按策略（设计 17 §4，D58）：模块只报发现，任务的策略（`run.json` 的 `policy`，开始时冻结；没有时是 `default`）给每条发现定级 blocking / review / info（`report_only` 只留数据完整性的 blocking，其余都是 info，策略版本 2）。有 blocking 发现即 drop，每条 blocking 发现都是理由，对它执行出错的模块写进原因「另有…执行出错，不影响结论」（D35）；否则所需模块出错或没有记录即 held（「待补跑」）；否则 keep，review 级发现进 review（弃权不是错）。`default` 就是今天的硬门，只是软分不再拒（P18）；`report_only` 一律只报告，不拒也不问。换策略只要重跑 aggregate。
- `funnel`：逐条段的模块 → 每条 keep / drop / held（`verdicts.jsonl`，2.0 行：`blocking`、`review`、`info_count`、`error_modules`、`reason`）和 `keep.txt`。两块的运行（计划 2.0，F12.4）里每个所选模块都判过每一条，Daemon 不再单独跑这一步（`final` 也写这两份）；手工跑漏斗计划的旧运行目录照旧可用。不给 `--revision` 时写到 `<run-dir>/funnel/`。
- `verdicts.jsonl` 始终是检查本身的漏斗判决；`keep.txt`（dedup 的输入）还要跟着已应用的人工裁决走：弃用的、人工判失败的移出，复议捞回的、对拒绝条目人工判成功的加入（`counts` 里的 `decided_in` / `decided_out`）。没有裁决时两者一致。
- **dedup 只在第一个结果版本跑一次**，人工裁决之后不再跑：它报的重复组保持不变，每组留哪条由 aggregate 在人工决定之后选——组内第一条没因别的原因被拒的（设计 17 §4.5：被人判失败的原件去掉了，它的副本顶上），其余成员按副本拒；由人带回交付的条从不去重。
- `final --revision N`：再叠加 dedup 和已应用的人工裁决，写 passed / reject / held（三者不相交、合起来是全部）、review 视图和这一版用的策略（`policy.json`）。三份清单的每条都带 `findings`（F12.5）：这一条的全部发现与它在这一版的级别——人的结论是它自己的 blocking 发现（带它的一句话），复议恢复的可复议发现降为 info，已了结的 review 发现不再列出，去重组留下的那条没有重复发现；每条发现用 `index` 指向模块记录里的位置，控制台的 Episode 明细与按级别 / 检测项筛选读它。人工裁决按 v1 的优先级：「弃用」压过一切（包括 held）；人工判了任务成败就以人为准，改标后不再重判（改标时顺手给的成败结论同样采信，C1 1.3；改标的回答一变它就作废，见 adjudicate-apply）；人的结论落在它回答的发现上（判失败、数据确有问题、EEF 不一致是人工的 blocking 发现，不可复议）；复议按发现（D42）：这条的 blocking 发现全部可复议（注册表细码的 `appealable`：任务失败、字节级重复、EEF 不一致）且都不是人的结论才受理，别的发现是终判；恢复把这些发现降为只报告——去重的复议恢复后回到 passed（技能画像给它归档，它在 task_success 上的弃权从下一版起进 review），另有模块对它执行出错的恢复后 held（P11）；「拿不准」只记录，这条留在队列里。改了标还没按新标注重判的条 held。
- review 视图按 v1 的队列，复核种类取自注册表的目录（D42、D43）：成败弃权（`task_verdict`）只问在 passed 里、task_success 弃权、人还没下结论的条；EEF 与画面核对（`eef_check`，registry 1.9）只问在 passed 里、EEF 模块转人工、人还没下结论的条；标注分歧（`label`）只问 passed 里的条；被拒的条没有这两种问题，拒绝可复议时给一项 `reject_appeal`（去重的写明 `duplicate_of`）；held 的条什么都不问。每一项都写明注册表的 `line`。
- 缺源文件被跳过的条（D40）不进任何清单、不计入总数（`counts.skipped`）。某个模块整体失败（退出码 4，没写出结果）时，它本该判的每一条都 held，一条都不交付，等重试成功（D41）。
- `--input` 给了才在 passed 里写出交付用的任务描述（原始标注要从数据集的元数据里读）。已有 `commit.json` 的版本拒绝改写。
- 运行目录里是 C2 1.0 的记录（升级前建的任务）时拒绝聚合，退出码 2：旧任务只读（D59）。

**adjudicate-apply**：`curation adjudicate-apply --run-dir … --decisions decisions.json --json`

- `decisions.json` 只含本任务尚未应用的裁决（D32，`cli/decisions.schema.json`）；按 id 去重，重复应用不改变任何东西。写 `adjudication/applied.jsonl`、`adjudication/labels.json` 和 `human-decisions/*.csv`（v1 的列与用词）。
- `--json` 的 `rerun_task_success` 是要按新标注重判的条（改了标、且没有人工判定成败的）。
- `decisions.json` 顶层的 `relabel_rerun`（`v1` 缺省 / `full`，D39）随每条改标记进 `applied.jsonl` 和 `labels.json`，`--json` 原样带回；之后重试这些条也按记下的口径判。
- 改标的同时给了成败结论（判成功 / 判失败）的条不重判，以人为准（注册表 1.3 的后续问题，v1 的 `human_concluded`）；给的是「拿不准」照常重判。
- 这种跟着改标给的结论（task_success 没有弃权的条上的成败结论）只在改标的回答仍是最新、且结论在它之后给出（按裁决 id）时算数；改标的回答一变（维持原标注、拿不准、换一段新标注），它就作废：不起作用，仍在生效的改标照常重判（`rerun_task_success` 列出），与 Daemon 的 `Queue._stands` 是同一条规则。task_success 弃权的条，成败结论是卡片自己的问题，不会这样作废。
- `eef_check`（registry 1.9）：`consistent` / `inconsistent` / `unsure`，当作 EEF 模块在这条上的结果，不调模型、不用重跑；
  副本在 `human-decisions/eef_checks.csv`。
- 没有执行规则的复核种类（`line` 不是 `label`、`task_verdict`、`reject_appeal`、`eef_check`）报参数错误（退出码 2）并写明是哪一种，不会跳过；对没有可复议拒绝的条目复议（有不可复议的发现、是人的结论、已弃用、根本没被拒）同样报参数错误（D42），一条也不应用。

**report**：`curation report --run-dir … --revision N [--format md,json] [--modules a,b] [--subtask-id ID] --json`

- 在 `revisions/r<NNNN>/` 写 `report.md`（中文，给人看）、`report.json`（`cli/report.schema.json`）、`perf.json` 和 `tables/*.parquet`，最后写 `commit.json`：这个版本用了各模块的哪些分片、哪些人工裁决。只有带 `commit.json` 的版本才算数；哪个版本生效由 Daemon 在上传核验之后切换（D25）。
- 总览的 `counts.skipped` 与完整性一块的 `integrity.skipped_episodes`（缺了哪些文件）列出缺源文件未质检的条（D40），`report.md` 有「缺源文件未质检」一行和「未质检的条目」一节。
- 各模块的 `adjudication`：`pending` 只数注册表里计入待裁的问题（成败弃权、标注分歧），`appealable` 是这个模块拒掉、还能复议的条数（只有拒绝可复议的模块才有）；去重的是 `{"pending": 0, "appealable": N}`（D43）。
- 报告里的 token 用量来自 `usage.jsonl`（两本账：实际与分摊，`requests_unknown_usage` 是响应里没有用量的请求数，不估算）；时延来自 `details/vlm_latency.csv`。

**verify**：`curation verify --run-dir <本地运行目录> --output <交付目录下的 run_id 目录> [--visibility-timeout 60] --json`

- 关键文件 = 运行目录里的全部文件（排除 `logs/`、`inflight.json`、隐藏文件和临时文件）：结果版本的清单、报告与明细。
- 逐个检查（8 路并行回读，文件小、省的是往返）：存在（`missing`）、大小一致（`size_mismatch`）、开头不是全零（`zero_filled`）、能解析（`unparseable`：JSON / JSONL 整体解析，parquet 看首尾魔数并解析 footer，mp4 找 `moov`，JPEG / PNG 看魔数）。列举里有但读不到的文件在 `--visibility-timeout` 秒内反复重试，仍读不到记 `not_visible_in_time`。交付目录本身读不了是退出码 3（`output_unreachable`）。
- mp4 和 parquet 只按范围读头尾，不下载整个文件。全部通过才最后写 `_COMPLETE`；没通过时如果交付目录里有旧的 `_COMPLETE`，会把它删掉。

**task**：`curation task create --file task.json [--wait]`、`list`、`get`、`wait`、`start|pause|resume|stop`、`retry [--modules a,b]`、`continue`、`report [--rev N]`、`adjudication`

- `--json` 时原样打印 Daemon 的响应，包括 `links`。`adjudication` 汇总待裁决条数和裁决页链接（裁决本身只能在网页上做）。
- 每个写请求都带 `Content-Type: application/json`（没有请求体也带，C4 1.2）；可带 `--idempotency-key`，重试时用同一个 key，Daemon 不会重复执行。
- `wait` 每 `--poll-interval` 秒（默认 5）查一次，直到任务进入终态且没有运行中的子任务；`--timeout` 到了以退出码 8 结束，当时的任务 JSON 在 `error.details.task`。
- Daemon 连不上或回的不是 JSON：退出码 3（`daemon_unreachable`）；Daemon 回了错误（含 401、403、404、409、405 `method_not_allowed`、5xx）：退出码 7（`rejected`），REST 错误体原样放在 `error.details.rest_error`。

### EEF–视频一致性与 `--param`（registry 1.4 起；D49 后参与判决）

- `--param MODULE.KEY=VALUE`（`preflight`、`check`，可重复）：任务级模块参数，值按该模块的 `param_schema` 转成数字 / 布尔 / 选项并校验；
  未知模块或参数是用法错误。第一个用它的是 EEF–视频一致性：`--param eef_video_consistency.trajectory_json=PATH`。
- `eef_video_consistency`（D49，registry 1.8）：vlm 档的一票否决模块，和 `task_success` 同一个 `check` 调用、同一个逐条循环
  （`StageRun` 调 `cli/eef_check.py` 的 `EefJudge`；流水线模式下一样逐条交接），只跑前面没被判废的条目。每条先 CPU 测量，再请
  模型复核（VLM 参数与其他 VLM 模块相同：`--vlm-backend` / `--vlm-endpoint` / `--vlm-model` / `--retry` / `--hedge`；调用种类
  `eef_review`，用量记在这个模块名下；超时默认 120 秒，`--set checks.task_success.vlm.timeouts_s.eef_review=S`），然后按设计 12
  附录 C.9 给出 `passed = true`（判过）、`false`（判废，理由在 `details.reason`）或 `null`（转人工）。没给文件时 `preflight` 报
  `needs_input: trajectory_missing`；没给 VLM 后端不再是 `needs_input`（注册表 5.2，设计 25 D84）：`available` 带一条提醒，
  运行时没有模型（`--no-vlm`）或 `use_vlm=false` 就只用 CPU 的测量。只有它、没有 `task_success` 时
  不读 v1 的数据行，媒体自己读（远端数据集按需分段读到临时目录，调用结束删掉）。`aggregate` 在调用边界把它作为一票否决项加进
  v1 的判决配置（`verdict.py` 不变），不选它时配置与之前完全相同。每行记录带 `input_file_sha256`、`config_hash`、`seeds_sha256`、
  `template_sha256`、`review_config`（窗口数、帧数、模型、prompt / Schema 版本、预处理），`--resume` 只跳过这些都没变的行，
  `input_digest` 也包含它们。模型答复缓存在 `checks/eef_video_consistency/cache/`；窗口没拿到合格答复不是执行出错（CPU 可疑的分项
  因此没有模型意见，转人工）；VLM 探活不过是整个调用失败（模块失败）。转人工的条把每个复核窗口的标记图都写进证据
  （窗口的 `evidence`），裁决卡片按窗口展示；判过的条只留模型反驳或与 CPU 冲突的窗口的图。
  实现在 `cli/eef_check.py`、`cli/eef_review.py`、`cli/modparams.py`，模块本身在 `extensions/eef_consistency/`（见其 README）。
- 转人工的条（registry 1.9 的复核种类 `eef_check`，review 项 `kind: eef_consistency`，F5.11）：留在 passed、进 review，计入待裁；
  人判「一致」（`consistent`）当作这个模块判过，「不一致」（`inconsistent`）当作它判废（reject 的理由是
  `人工裁决判为 EEF 与视频不一致`，`kind: human`），「拿不准」只记一笔、照旧待裁；和人工的成败结论同时存在时两条都算。
  只被这个模块判废的条可以复议（`reject_appeal`，`source_module` 是它），恢复后回到 passed；和别的硬门一起判废的是终判。
- `--param eef_video_consistency.record_mapping=PATH`（registry 1.13，设计 12 §8.7，F5.15，可选）：`eef-mapping/1.1` 的 `record`
  块（JSON 或 YAML），写明数据集自己的末端位姿列 / topic、关节角列 / topic 与机器人型号（DEMO 内置 `franka_panda` / `franka_fr3`），
  列名、布局、单位、坐标系都由人写，不猜。给了就把上传的三维轨迹与数据集的记录逐帧比，写进 `details.record`（每个来源的对齐方式、
  两组残差、恒定差与声明的比较、时间差、分段、残差曲线；两个来源都有时另附数据集内部的互比）与 `details.summary.record_consistency`；
  **只报告，不参与判决**，有没有这个参数每条的 `passed` 都一样，没有夹爪参考、只给模型意见时也照样比。映射不合格是用法错误；
  预检的 `subitems.record_consistency` 报 `record_mapping_missing` / `record_mapping_invalid` / `robot_model_unknown` /
  `record_columns_missing`（本地数据集按 `meta/info.json` 或第一个 mcap 的摘要核对列与 topic），模块级可用性不看它。远端 LeRobot
  只多读 `meta/` 与这一条的 data 文件；mcap 的记录 topic 与视频在同一个 episode 文件里，一起按区间流式读。`config_hash` 含映射的
  sha256，换了映射 `--resume` 会重做。有标定的相机上，可疑来源的叠加图写在 `checks/eef_video_consistency/evidence/<ep>/record/`，
  路径另记在 `details.record.evidence`。报告的 EEF 一节多一行「轨迹与数据集记录(只报告,不参与判决)」，另有明细表 `eef_record`。
  映射里位姿的 `frame_id` 可以写 null（不知道是哪个点：恒定差只报告），`reference_frame` 可以写 `"@upload"`（与上传轨迹同一基座）。
- `preflight` 的 EEF 条目带 `drafts.record_mapping`（F5.16，D-E17；不给 `record_mapping` 也有）：按 LeRobot `info.json` 的分量名与
  `robot_type` 起草的映射（`document`）、推断的地方（`assumptions`）、没起草的部分与原因（`not_drafted`）。控制台据此让人确认后作为
  上传件提交；命令行要用就把 `document` 存成文件传给 `--param …record_mapping=`。mcap / Lance 不起草。

### mcap 与 Lance 数据集（D44，F6.5）

v1 在 `dev` 的 PR #155 里接入了这两种格式：读取器 `ingest/mcap_reader.py`、`ingest/lance_reader.py` 是 A 类代码，原样搬来（冻结点随之前移到 `dev@eb637ba40`；交付用的写出器随 D69 删掉了）；v2 的命令在外面包了一层 `cli/containers.py`，判决仍全部出自 v1 的读取器与算法（合成数据上 v1 对 v2 逐位对账，`tools/parity/tests/test_containers_parity.py`）。

| | mcap | Lance |
|---|---|---|
| 认什么 | 数据集根目录下的 `*.mcap`，一个文件一条 episode（子目录里的不算）。编号照 v1：文件名是 `episode_<N>.mcap` 的按 N（混进来的其他 `.mcap` 不读，预检给警告），一个都不是就按文件名排序从 0 编 | lerobot-lance-convert（0.3.0 起）的布局：`frames.lance`、`videos.lance`、`meta.lance` 三张表加 `meta/`，`meta/info.json` 带 `storage_format: "lance"`（LeRobot v3.0 元数据）。三表齐但没有这个标记的是旧插件布局，预检报元数据无效（v1 的原话）；只有别的 Lance 表的是 `lancedb`，不支持 |
| 预检读什么 | 每个文件只读摘要区（通道、消息数、元数据记录），TOS 上是几次按范围读，不读消息。动作、状态、相机、任务文本、机器人型号按 v1 的 topic 规则认（站点的 `ingest.mcap_mapping` 优先，UMI 的 `/robotN/vio/eef_pose` 自动认）。时间轴取动作 topic 的 `log_time`，没有要配的帧率，`fps` 为 null。任务文本只在 `/task` topic 里的，预检数它有标注但读不到文字（真正的文字质检时读） | `meta/`，读法同 LeRobot v3（含 v1 的 `validate_info`）；缺了 `meta/` 就读 `meta.lance` 里的镜像并给一条警告 |
| 快照记什么 | 全部 `*.mcap`（v1 按目录里有哪些文件来编号，每个文件又是一条 episode 的数据）；`meta_fingerprint` 覆盖全部 mcap 文件 | `meta/` 与三张表的全部对象（读的时候整表读）；`meta_fingerprint` 覆盖 `meta/`，没有 `meta/` 时覆盖 `meta.lance/` |
| 交付 | 不再写交付数据集（D69）：平台只交付质检报告与结果清单，两种格式都一样 | 同左 |
| EEF–视频一致性 | 可用（F5.13）：trajectory.json 的相机写 `media.uri=episode_<N>.mcap` 与 `media.topic`（图像 topic），模块把该 topic 的 JPEG / H.264 帧转成本地视频读，TOS 上按区间流式读（`streams.objects`） | 不支持：`unsupported`，原因码 `format_unsupported_by_module` |

- **数据集语义取整个任务的所选**：v1 用所选 episode 的前 100 条判定数据集语义（控制模式、单位等）。v2 的命令只读某一档的幸存者，所以读源数据的命令（`check`、`aggregate --phase final`）要带 `--selection <整个任务的所选>`（语法同 `--episodes`，Daemon 自动传；不带时取 `--episodes`），判定取它的前 100 条，与 v1 一致。LeRobot 数据集不受影响（它的语义样本一直是数据集的前 100 条）。
- **TOS 上的数据先拉到本地再读**：v1 的两个读取器只认本地目录。`tos://` 上的数据由 `SourceCache` 在本地留一份副本：mcap 先给每个文件放一个空占位（v1 按目录里的文件名编号，占位不会被读），读到哪条才下载哪条；Lance 第一次读时整表下载（读取器要整表）。设了 `CURATION_SOURCE_CACHE` 就放在它下面（`<目录>/<格式>-<地址哈希>/<数据集名>/`，同一任务后面的命令复用，Daemon 在运行结束时删掉），没设就放在这条命令自己的临时目录里、命令结束就删。每个副本按列举时的大小和 ETag 核对，对不上以退出码 6 结束（`source_changed`）；带 `--source-manifest` 时照常先核对快照。只读源桶，从不往源桶写；凭证只从 `CURATION_INPUT_TOS_*` 环境变量读。
- **临时视频**：读取器转出来的视频（mcap 里 JPEG / H.264 消息转成的 mp4、Lance 里取出的视频）写在 `$TMPDIR`，命令结束时清掉。Daemon 把 `TMPDIR` 指到任务的缓存目录下，不占容器的 `/tmp`。
- **`source_info.json`**：第一条读源数据的命令在运行目录里写下 v1 报告要用的信息（读取器的 `mcap_dataset_info` / `lance_dataset_info`：型号从哪来、时间轴从哪来、有没有任务文本；用到的机器人规格），`report` 据此写出 v1 的数据包体检（`report.json` 的 `integrity.container`、`report.md` 的「数据包」一节）。
- **站点开关**：`ingest.mcap_enabled` / `ingest.lance_enabled`（默认开；v1 的环境变量 `CURATION_MCAP_ENABLED` / `CURATION_LANCE_ENABLED` 优先于配置）。关掉后预检把全部模块标为不支持（原因码 `format_disabled`），读源数据的命令以参数错误（退出码 2）结束。Helm 里写在 `pipelineConfigOverride.ingest` 下。
- **依赖**：`pylance`（Lance）、`mcap`、`mcap-ros2-support`（cdr 消息）、`mcap-protobuf-support`（protobuf 消息），版本钉在 `backend/requirements.txt`；都是懒导入，不质检这两种格式时用不到。

## Daemon 的调用顺序

一次完整运行（设计 17 §3，计划 2.0）：两块同时跑、互不过滤，每一段都拿任务的全部所选条目（`--episodes` 是所选，不再是上一档的幸存者）：

```
preflight → plan → snapshot
CPU 块：check 数据完整性 → check 数值档 → check 帧档 → check dedup（逐条交接，和前面的段交叠，D70）
        并排的第二个起点：check --prep EEF 的 CPU 半段（vlm_prep，注册表 5.3）
VLM 块：check task_success（及 EEF 的模型半段，--prepared，接在 vlm_prep 后面；没有任务标注的条目不判，D72）
两块都结束 → aggregate --phase final --revision N → report --revision N → （同步运行目录到交付目录）→ verify
```

EEF 分两半（设计 23 §2.1、设计 25 F5.24b）：`check --modules eef_video_consistency --prep` 读轨迹、测量、算自运动、比对记录、画带标记的
视频、拼好请求，不问模型，每条 episode 的请求与部分记录留在 `scratch/vlm/<episode>/eef_video_consistency/`（`requests.json`、`prep.json`
与图、视频文件），这一档不写这个模块的结果行；`--resume` 跳过已经留好、而且用同样的输入做出来的。`check … --prepared`（与 task_success
同一调用）读回请求、发请求、合并、写结果行，结果落盘后删掉这一条的请求包，出错的留着等重试。两半与一次 `check` 写出的记录相同
（`tests/cli/test_eef_check.py::test_the_two_halves_make_the_records_one_call_makes`）。不问模型时（`--no-vlm` 或
`use_vlm=false`）`--prep` 直接写结果行，计划里也没有它的模型半段；`curation plan` 加 `--no-vlm` / `--param` 得到同样的计划。
`--prep` 用的模型设置（`--vlm-*`）只用来拼请求与缓存键，不调用；给了没有 CPU 半段的模块是用法错误。

块内的逐条段由 Daemon 的流水线逐条交接（`check --pipeline-state … --pipeline-next …`，常驻 worker）：一条在本段有了记录（判完或出错）
就交给下一段，判废的发现、执行出错都不拦它。D70 起块里没有全量步骤了。没有 `aggregate --phase funnel`：判决只在两块都结束后由 `final` 按任务策略算一次。

人工裁决后的重跑（新版本 N+1，旧版本原样保留）：

```
adjudicate-apply → check task_success --episodes <rerun_task_success>（写新分片）
→ aggregate --phase final --revision N+1
→ report --revision N+1 → （同步运行目录）→ verify
```

裁决之后**不再跑 dedup**：它报的重复组不变，`final` 在人工决定之后选每组留哪条（原件被人判失败时副本顶上），由人带回的条不做去重。

重试（子任务）只补跑出错或缺记录的（模块 × 条目）：每一段只带要补的模块与条目（`check --modules <要补的> --resume`），dedup 和别的段一样只补出错或缺记录的条目，然后 `final`。

mcap / Lance 数据集的顺序相同，`check`、`aggregate --phase final` 多带 `--selection <任务的所选>`；数据在 TOS 上时，每条命令的环境里有 `CURATION_SOURCE_CACHE`（任务的本地副本）和 `TMPDIR`，运行结束后 Daemon 删掉这个目录。

## 手动验证步骤

在仓库的 `backend/` 目录下执行。Python 用仓库根目录的虚拟环境，临时文件放在 `$TMPDIR`，不需要任何密钥：

```bash
cd backend
PY=../.venv/bin/python
C="$PY -m curation.cli"
D=${TMPDIR:-/tmp}/curation-cli-demo && rm -rf "$D" && mkdir -p "$D"
PYTHONPATH=../tools $PY -m parity make-fixture --out "$D/mini"      # 8 条 episode 的 LeRobot v2.1 数据集
```

另开一个终端，同样在 `backend/` 下启动一个本地假模型（OpenAI 兼容接口，答案只取决于请求内容）：

```bash
PYTHONPATH=../tools ../.venv/bin/python -c "
import time
from tests.cli.fakevlm_server import FakeVlmServer
with FakeVlmServer(port=8766) as s:
    print(s.url, flush=True); time.sleep(3600)"
```

回到原终端：`export CURATION_VLM_ENDPOINT=http://127.0.0.1:8766/v1 CURATION_VLM_MODEL=fake-vlm`。

1. 入口与帮助：`$C --help` 列出上表的 11 条命令，末尾列出 v1 命令；`$C --version` 输出 `curation <版本>`；`$C ls "$D/mini"` 仍是 v1 的中文输出。

2. 预检、计划、快照：

   ```bash
   R=$D/run; mkdir -p "$R"
   $C preflight --input "$D/mini" --vlm-backend ark --json > "$R/preflight.json"
   $C plan --preflight "$R/preflight.json" --episodes 0-7 --out "$R/plan.json" \
     --modules timestamp_check,kinematic_limits,motion_quality,visual_quality,video_action_sync,task_success,dedup
   $C snapshot --input "$D/mini" --episodes 0-7 --out "$R/source_manifest.json"
   ```

   应看到：计划的 VLM 档闸门是 `episode 32, probe 64, endstate 64, arbitration 32, guard_caption 32`（N=64，与 v1 出厂值相同）；快照 `27 objects`（3 个元数据文件 + 8 条 × 3 个文件）。
   `preflight --json` 的输出可以用 `$PY -c "import json,sys; from curation.contracts import schemas as s; print(s.errors('cli/preflight.schema.json', json.load(open(sys.argv[1]))))" "$R/preflight.json"` 核对，应为空列表。

3. 按 Daemon 的顺序跑完一遍（每条约几秒）：

   ```bash
   S="--input $D/mini --run-dir $R --source-manifest $R/source_manifest.json"
   $C check --modules timestamp_check,kinematic_limits,motion_quality $S --episodes 0-7 --survivors-out "$R/stages/numeric.txt"
   $C check --modules visual_quality,video_action_sync $S --episodes "@$R/stages/numeric.txt" --survivors-out "$R/stages/frame.txt"
   $C check --modules task_success $S --episodes "@$R/stages/frame.txt"
   $C aggregate --run-dir "$R" --phase funnel --revision 1 --episodes 0-7
   $C check --modules dedup $S --episodes "@$R/revisions/r0001/keep.txt" --survivors-out "$R/stages/dedup.txt"
   $C aggregate --run-dir "$R" --phase final --revision 1 --episodes 0-7 --input "$D/mini"
   $C report --run-dir "$R" --revision 1
   ```

   应看到：数值档 `timestamp_check (part 0001): 8 episodes - ok 8, error 0; findings: gap 1, fragment 1`（2 时间戳跳变、5 残段，默认策略下 blocking，停在这一档）；task_success 6 条 `ok 6, error 0`，发现 `uncertain 3`（0、3 和 3 的字节级副本 7，进复核）、`failure 1`（1：一次判决就判失败，D71）与 `task_text_missing 2`（4、6 没有任务标注，不判成败、只报告，D72）；dedup 报 `duplicate 1`（7 与 3 重复）；final 为 `passed 4, reject 4, held 0, review 4`（0 和 3 问成败，1 是可复议的判废，7 是可复议的去重拒绝；4、6 靠其余检查通过），日志有 `policy default: 4 kept, 1 rejected, 0 held`；`$R/revisions/r0001/policy.json` 是 `{"preset": "default", ...}`。
   假模型的回答只看请求的文字、图片张数和像素尺寸，不看图片字节，所以这些数在 macOS 和 Linux 上一样（`tests/cli/test_fake_model.py`）。
   `cat "$R/revisions/r0001/report.md"` 是中文报告，含「通过 2」、判决策略、按细码的拒绝原因，每个模块一行「评估 N 条;检出:…」；`tail -3 "$R/usage.jsonl"` 是按模块记的 token 用量。
   没给 `--concurrency`，所以整个过程中任何时刻只有一个模型请求在飞（`tests/cli/test_pipeline_chain.py` 在假模型那边量过）。

4. 交付与核验（本地目录充当交付目录）：

   ```bash
   rsync -a --exclude inflight.json "$R/" "$D/delivery/"      # Daemon 的同步：报告与清单
   $C verify --run-dir "$R" --output "$D/delivery" --visibility-timeout 0 --json
   ```

   应看到：`verify` 输出 `"failed": []`、`"complete_marker": true`，`$D/delivery/_COMPLETE` 出现。

5. 中断后续跑。先把假模型换成慢速版（在另一个终端 Ctrl-C，把启动命令里的 `FakeVlmServer(port=8766)` 改成 `FakeVlmServer(port=8766, delay_s=0.3)` 再启动），然后：

   ```bash
   cp -R "$R" "$D/run2"; rm -rf "$D/run2/checks/task_success"
   S2="--input $D/mini --run-dir $D/run2 --source-manifest $D/run2/source_manifest.json"
   $C check --modules task_success $S2 --episodes "@$D/run2/stages/frame.txt" --json > /dev/null 2>&1 & sleep 8; kill -9 $!
   cat "$D/run2/checks/task_success/inflight.json"            # 被杀时在手的 episode
   $C check --modules task_success $S2 --episodes "@$D/run2/stages/frame.txt" --resume --json | $PY -m json.tool | grep -A2 skipped
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
   $C check --modules timestamp_check --input "$D/mini2" --run-dir "$D/run3" --episodes 5-7 --json; echo "exit=$?"
   ```

   依次是：端点不通 → `module_failed`，`exit=4`；第 0 条的 parquet 变了，虽然只选了 5–7，但它在语义样本里 → `source_changed`，`details.key` 是那个 parquet，`exit=6`；不带快照时这个坏文件让语义判定失败 → `module_failed`「the dataset cannot be read」，`exit=4`。

7. 人工裁决与第二个结果版本：

   ```bash
   cat > "$D/decisions.json" <<'EOF'
   {"schema_version": "1.0", "decisions": [
    {"id": 1, "episode_index": 3, "line": "task_verdict", "decision": "failure", "new_label": null, "note": null, "decided_by": "me", "decided_at": 1790000000000},
    {"id": 2, "episode_index": 4, "line": "task_verdict", "decision": "unsure", "new_label": "wipe the table", "note": null, "decided_by": "me", "decided_at": 1790000000001}]}
   EOF
   $C adjudicate-apply --run-dir "$R" --decisions "$D/decisions.json"      # re-judge task_success: 4 (relabel_rerun v1)
   $C check --modules task_success $S --episodes 4
   $C aggregate --run-dir "$R" --phase funnel --revision 2 --episodes 0-7
   $C aggregate --run-dir "$R" --phase final --revision 2 --episodes 0-7 --input "$D/mini"
   $C report --run-dir "$R" --revision 2
   ```

   应看到：第二版的漏斗行末尾是 `keep.txt after the human decisions: 0 in, 1 out`（3 被人工判失败，移出 `keep.txt`）；没有再跑 dedup（`ls "$R/checks/dedup/parts"` 仍只有 `0001.jsonl`），它报的重复组 {3, 7} 不变，3 被人判失败后由副本 7 顶上（设计 17 §4.5）；第二版 `passed 5, reject 3, review 3`（0 仍待判成败；4 按 v1 的两层重判后弃权，重新问成败；7 进了交付，问成败）；再跑一次 `adjudicate-apply` 显示 `applied 0 decision(s) (2 already applied)`；`revisions/r0001/` 原样未动。

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

10. mcap 与 Lance（D44）。同一份 8 条合成数据换成两种格式，按第 3、4 步的顺序跑（假模型照旧在另一个终端）：

    ```bash
    PYTHONPATH=../tools $PY -m parity make-fixture --format mcap --out "$D/mini_mcap"     # episode_0.mcap … episode_7.mcap（cdr 编码）
    PYTHONPATH=../tools $PY -m parity make-fixture --format lance --out "$D/mini_lance"   # frames / videos / meta 三张表 + meta/
    for F in mcap lance; do
      IN=$D/mini_$F; R=$D/run_$F; mkdir -p "$R/stages"
      $C preflight --input "$IN" --vlm-backend ark --json > "$R/preflight.json"
      $C snapshot --input "$IN" --episodes 0-7 --out "$R/source_manifest.json"
      S="--input $IN --run-dir $R --source-manifest $R/source_manifest.json --selection 0-7"
      $C check --modules timestamp_check,kinematic_limits,motion_quality $S --episodes 0-7 --survivors-out "$R/stages/numeric.txt"
      $C check --modules visual_quality,video_action_sync $S --episodes "@$R/stages/numeric.txt" --survivors-out "$R/stages/frame.txt"
      $C check --modules task_success $S --episodes "@$R/stages/frame.txt"
      $C aggregate --run-dir "$R" --phase funnel --revision 1 --episodes 0-7
      $C check --modules dedup $S --episodes "@$R/revisions/r0001/keep.txt" --survivors-out "$R/stages/dedup.txt"
      $C aggregate --run-dir "$R" --phase final --revision 1 --episodes 0-7 --input "$IN" --selection 0-7
      $C report --run-dir "$R" --revision 1
      rsync -a --exclude inflight.json "$R/" "$D/delivery_$F/"
      $C verify --run-dir "$R" --output "$D/delivery_$F" --visibility-timeout 0 --json | grep -E '"failed"|complete_marker'
    done
    ```

    应看到：预检 `kind` 分别是 `mcap`（`version: null`，`fps: null`，detail 写着时间轴取自动作 topic 的 `log_time`）和 `lance`（`version: v3`），8 条、2 路相机、`robot_type: franka`、6 条有标注；EEF 模块在 mcap 上是 `needs_input: trajectory_missing`（给了 trajectory.json 就能用，F5.13），在 Lance 上是 `unsupported`（`format_unsupported_by_module`），其余可用。
    判决与第 3 步的 LeRobot 数据集完全相同：数值档拦下 2、5，task_success 4 fail 4 abstain，dedup 剔除 7，final 为 `passed 2, reject 6, held 0; 6 to review`。
    `report.md` 多一节「数据包(mcap)」/「数据包(lance)」：型号、时间轴、任务文本三项体检（D69 起没有交付数据集这一项）。
    两边 `verify` 都是 `"failed": []`、`"complete_marker": true`。`ls $TMPDIR` 里不留读取器转出的视频。
    TOS 上的读法（本地副本、按范围读摘要、源对象变化退出码 6、只读源桶）由 `tests/cli/test_containers.py` 在假 TOS 上核对；有自己的桶时，把两个目录传上去，
    设好 `CURATION_INPUT_TOS_ACCESS_KEY` / `CURATION_INPUT_TOS_SECRET_KEY` 与 `CURATION_SOURCE_CACHE=$D/cache`，把上面的 `IN` 换成 `tos://…` 再跑一遍，判决应不变，`$D/cache` 里是 mcap 读过的那几条文件和 Lance 的整表。

11. 自动化测试（约 4 分钟）：

   ```bash
   $PY -m pytest -q tests/cli tests/contracts tests/planner
   $PY -m curation.contracts check
   $PY -m pytest -q curation/tests --ignore=curation/tests/test_environment.py   # v1 单测，1373 passed
   (cd .. && PYTHONPATH=tools $PY -m pytest -q tools/parity/tests)               # 对账工具，含 v1 对 v2 的逐位对账
   ```

   `tests/cli` 里：`test_pipeline_chain.py` 在夹具上按上面的顺序跑完整条链并逐个校验契约；`test_check_resume.py` 是 SIGTERM / SIGKILL 后续跑；`test_errors_and_policy.py` 是出错与弃权的区分、默认不并发不重试、源数据变化；`test_aggregate.py` 是聚合与裁决的规则；`test_revision_flow.py` 是第 7 步的第二个版本与增量导出；`test_fake_model.py` 钉住假模型的答案与图片字节无关（换一种 JPEG 质量重编码，判决不变）。

12. 数据完整性（设计 14）。先看预检的三条警告，数据集是第 10 步的两份夹具的副本：

    ```bash
    cp -r "$D/mini" "$D/bad" && cp -r "$D/mini_mcap" "$D/bad_mcap"
    : > "$D/bad/videos/chunk-000/observation.images.wrist/episode_000002.mp4"       # 0 字节
    $PY -c "import os,sys; p=sys.argv[1]; os.truncate(p, os.path.getsize(p)//2)" "$D/bad_mcap/episode_4.mcap"   # 录制中断
    $C preflight --input "$D/bad" --json | $PY -c "import json,sys; print(json.load(sys.stdin)['warnings'])"
    $C preflight --input "$D/bad_mcap" --json | $PY -c "import json,sys; print(json.load(sys.stdin)['warnings'])"
    ```

    应看到：LeRobot 一条 `1 file is empty or too small to be valid (1 episode: 2; videos/…/episode_000002.mp4 0 B)`；mcap 一条
    `1 episode (4) was cut off while recording (no mcap end marker); …`，原来那条「no mcap summary section」不再重复它。
    两份的模块可用性与干净数据集相同。控制台的「预检提示」与报告的「数据包完整性」把它们显示为中文。
    再对这两份跑数据完整性模块（`$C check --modules data_integrity --input "$D/bad" --run-dir "$D/run_bad" --episodes 0-7`，mcap 同理）：
    LeRobot 是 `findings: duplicate_content 2, file_empty 1`——第 2 条 `file_empty`（默认策略下判废），3 与 7 是夹具故意放的字节级副本（转人工）；
    mcap 是 `findings: cut_unreadable 1, duplicate_content 2, stream_missing 1`——第 4 条「录制中断，且读不出数据」（判废），第 6 条缺 `/task`（转人工）。
    更多损坏样本与裁决见[模块 README](../extensions/integrity/README.md)的手动验证。

13. 数据集自己的编号（F12.8）。从第 10 步的 mcap 夹具改名出一份编号不连续的数据集，再从 LeRobot 夹具删出一份「保留源编号的子集」：

    ```bash
    mkdir -p "$D/numbered" && for p in 0:5 1:9 2:12 3:20; do cp "$D/mini_mcap/episode_${p%%:*}.mcap" "$D/numbered/episode_${p##*:}.mcap"; done
    $C preflight --input "$D/numbered" --json > "$D/numbered.pf.json" && $PY -c "import json,sys; print(json.load(open(sys.argv[1]))['dataset']['episode_indices'])" "$D/numbered.pf.json"
    $C plan --preflight "$D/numbered.pf.json" --modules timestamp_check --episodes 5,9 --json > /dev/null && echo plan ok
    $C plan --preflight "$D/numbered.pf.json" --modules timestamp_check --episodes 0 --json; echo "exit $?"
    ```

    应看到：`5,9,12,20`；`plan ok`；最后一条以退出码 2 结束，错误写 `--episodes: 1 index(es) not in the dataset's 4 episodes (5, 9, 12, 20) (first 0)`。
    编号是 0 … count-1 的数据集（如 `$D/mini`），预检里没有 `episode_indices`。
