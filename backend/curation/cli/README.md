# curation 命令行（v2，工作包 W3）

`curation` 是 Curator v2 的命令行入口，设计见 `docs/design/02-cli-contract.md`，契约见 `docs/contracts/`（C2 输出、C3 进度、C4 REST）。
一条命令只做一件事，命令之间通过运行目录里的文件交接；组合由 Daemon（或脚本）按固定顺序完成（见下文「Daemon 的调用顺序」）。

| 命令 | 作用 | `--json` 契约 |
|---|---|---|
| `curation preflight` | 只读 metadata，判格式、数 episode、给出每个模块「可跑 / 需补充 / 不支持」及原因（F2.7） | `cli/preflight.schema.json` |
| `curation plan` | 执行计划：分档、每档并发度与八把闸门、VLM 请求合并提议（纯计算，W6 的库） | `cli/plan.schema.json` |
| `curation snapshot` | 固化任务要读的源对象清单（键、大小、ETag 或修改时间），写 `source_manifest.json` | `cli/source-manifest.schema.json` |
| `curation autolabel` | 给没有任务标注的 episode 补一句描述（`autolabel/captions.jsonl`） | `cli/autolabel.schema.json` |
| `curation check` | 跑**一档**的模块（数值档、帧档、VLM 档，或 `dedup`、`skill_profile` 之一），每条 episode 一行结果 | `cli/check.schema.json` |
| `curation aggregate` | `funnel`：每条 keep / drop / held 与 `keep.txt`；`final`：passed / reject / held 三个清单和 review 视图 | `cli/aggregate.schema.json` |
| `curation adjudicate-apply` | 应用本任务的人工裁决（不调模型、不导出），列出接下来要重跑什么 | `cli/adjudicate-apply.schema.json` |
| `curation report` | 一个结果版本的 `report.md` / `report.json` / `perf.json` / 明细表，最后写 `commit.json` | `cli/report-output.schema.json` |
| `curation export` | 把一个结果版本的 passed 导出成交付数据集（LeRobot 可增量；mcap / Lance 照 v1 交付，见「mcap 与 Lance 数据集」），并把变化同步到交付目录 | `cli/export.schema.json` |
| `curation verify` | 从交付目录逐个回读关键文件，全部通过才最后写 `_COMPLETE` | `cli/verify.schema.json` |
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
| `preflight.py` / `plan.py` / `snapshot.py` / `autolabel.py` / `check.py` / `aggregate.py` / `adjudicate.py` / `report_cmd.py` / `export_cmd.py` / `verify.py` / `task_client.py` | 各条命令 |
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
| `dataset_stages.py` | `autolabel`、`dedup`、`skill_profile`（含 `--incremental` 的重新归档） |
| `aggregate.py` / `adjudication.py` / `reporting.py` | 聚合判决、人工裁决、报告 |
| `funnel.py` / `run.py` | v1 的编排（B 类），只把闭包里的构建函数提到模块级、给技能画像留了逐条 caption 的钩子；`curation run` 仍走它们 |
| `rejudge.py` | v1 的 rejudge（B 类）；改标重判的函数体提成 `rerun_task_success`，`check` 按 v1 口径重判时调的就是它（D39） |

交付（`curation/export/`）：LeRobot 的全量与增量导出见 [INCREMENTAL.md](../export/INCREMENTAL.md)；mcap / Lance 的交付在 `export/containers.py`（D44）。

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

v1 的纯文本调用（技能归纳、标注审计、判废护栏的语义比对）自带 4 次尝试、闸门不低于 2；v2 命令下它也是一次一发、闸门按给定大小，`--retry 3` 就是 v1 的行为。Daemon 按任务参数传 `--hedge --retry 3 --plan-stage …`（与 v1 一致）；直接用命令行的人默认拿到的是「一次直来直去」的执行。

**VLM**：`--vlm-backend <名>`（站点配置 `vlm_backends` 里的预设）或 `--vlm-endpoint <URL> --vlm-model <名>`（缺省取 `$CURATION_VLM_ENDPOINT` / `$CURATION_VLM_MODEL`）；密钥只放在环境变量里，`--vlm-api-key-env <变量名>` 指明是哪个变量（缺省 `ARK_API_KEY`）。每条调模型的命令先 `GET /models` 探活一次，不通就退出码 4，不会逐条报错。
`check` 与 `autolabel` 可加 `--vlm-reasoning-effort <档位>`：给了才在每个模型请求里带 `reasoning_effort`，值原样转交、命令行不校验；不给时请求与 v1 逐字节相同（v1 从不发这个字段）。

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
  autolabel/captions.jsonl                     每条无标注 episode 一行（ok / unclear / error）
  checks/<module>/parts/0001.jsonl …           每次 check 调用写一个新分片，逐条追加、逐行 fsync；高编号的分片优先
  checks/<module>/results.jsonl                压实后的当前结果（每条一行，按下标排序）
  checks/<module>/inflight.json                正在处理的 episode（进程被杀后留下，--resume 读它）
  checks/<module>/crashes.json                 每条 episode 害死进程的次数
  checks/video_action_sync/curves/ …           同步曲线（按 pipeline.sync_plots）
  checks/dedup/groups.json                     去重的遍历顺序、撞车组、剔除的重复对
  checks/skill_profile/{profile.json, assignments.jsonl, captions.jsonl, label_audit.json}
  skipped_episodes.json                        不带快照时 check 读到才发现缺源文件、照 v1 跳过的条（D40）
  adjudication/{applied.jsonl, labels.json}    已应用的人工裁决、人工改标（改标带重判口径 relabel_rerun）
  human-decisions/*.csv                        本任务裁决的 v1 格式副本
  revisions/r0001/                             一个结果版本：verdicts.jsonl、keep.txt、passed/reject/held/review.json、
                                               label_audit.json、adjudications.json、report.md、report.json、perf.json、
                                               tables/*.parquet，最后是 commit.json（有它才算提交，之后不再改写）
  details/vlm_latency.csv  details/evidence/   每次模型请求的时延、取证图
  usage.jsonl                                  token 用量（C3 usage 行的落盘副本）
  source_info.json                             mcap / Lance（D44）：读取器给出的数据集信息与用到的机器人规格，report 据此写「数据包」一节
  export/{manifest.json, manifest.detail.json, lerobot_curated/}   mcap 源是 mcap_curated/，Lance 源是 lance_episodes/
  logs/
```

## 各命令要点

**preflight**：`curation preflight --input <tos://… | 本地目录 | 公共数据集名> [--source tos|public|local] [--vlm-backend 名] [--embodiment-id 型号] [--modules a,b] [--source-manifest 文件] --json`

- 只列目录、只读 `meta/` 下的文件，不读 parquet 和视频（mcap 只读每个文件的摘要区，见下文「mcap 与 Lance 数据集」）。
- 认得 LeRobot v2/v3、mcap 与 Lance（lerobot-lance-convert 0.3.0 起的三表布局，D44）。其余（`.rrd`、别的 Lance 表、其他 LeRobot 版本、认不出的目录）所有模块都是 `unsupported`，原因写明检测到的格式（`format_unsupported`）。
- `info.json` 结构校验沿用 v1 的 `validate_info`，报错原文放进 `validation`（中文，照抄给用户）。
- 运动学极限：型号读到且在规格库里是 `available`；读到但不在规格库里是 `unsupported`，只跳过这一项，其余模块照常；读不到（缺失、空串或 `unknown`）是 `needs_input`，`input_hint.options` 列出规格库的 9 个型号。`--embodiment-id` 覆盖 `robot_type`，与 v1 相同。
- 两个 VLM 模块：没传 `--vlm-backend` 是 `needs_input`（`input_hint.field = "vlm"`）；没有任务标注不会让它们标灰，只在 `notes` 里提示会先补描述。
- `--modules` 只报告所选模块，没选的模块不追问。原因文案用英文，`validation` 里 v1 的报错保持中文原文。
- 列目录时发现缺文件的条写进 `warnings`：LeRobot v2 缺数据 parquet 或某个机位视频的条照 v1 跳过（见 snapshot）；v3 的不跳过，检查时读不了、记为出错。

**plan**：`curation plan --preflight pf.json --modules a,b,… [--episodes 表达式] [--unlabeled 表达式] [--vlm-parallelism N] [--backend-parallelism N] [--task-vlm-parallelism N] [--task-cpu-concurrency N] [--cpu-cores N] [--running-tasks N] [--site-config 文件] [--out plan.json] --json`

- 不读数据、不联网。并行度取各层上限里最小的那个（D31），`limits.*.bound_by` 写明卡在哪一层；`--running-tasks` 大于 1 时均分。
- 各档的闸门由并行度推导；`check` / `autolabel` 用 `--plan-stage plan.json` 取自己那一档（整份计划按模块或档名挑，单独一档的 JSON 也行）。

**snapshot**：`curation snapshot --input … [--episodes 表达式] --out <运行目录>/source_manifest.json --json`

- 记录 `meta/` 下全部文件、所选 episode 的 parquet 与各机位视频，以及数据集**前 100 条**的 parquet：v1 不管选了哪些条，都用前 100 条的数值判定数据集语义（控制模式、单位等），它们一变，所选各条的判定就可能变。TOS 记 ETag，本地记 `mtime_ns`。
- `--episodes` 支持 `34`、`10-20`、`3,10-12` 和 `@文件`（每行一个表达式，`#` 之后是注释）；全部越界报参数错误，部分越界跑交集并警告。
- 之后任何读源数据的命令带上 `--source-manifest`，它要读的对象（元数据、语义样本、所选各条的文件）有一个变了就以退出码 6 结束，在读之前就停。快照时就缺的文件、现在仍缺，不算变化。
- mcap / Lance 记的对象不同（全部 mcap 文件；Lance 的 `meta/` 与三张表），见下文。
- LeRobot v2 的某条缺数据 parquet 或任一机位的视频（v1 的 `_v2_missing`）时，这条照 v1 跳过（D40）：记进 `skipped_episodes`（写明缺了哪些文件），带 `--source-manifest` 的命令都不读它；它不进任何清单、不计入总数，报告的完整性一块列出。v3 数据集不跳（v1 会去读），缺文件的条在检查时读不了，记为出错。

**autolabel**：`curation autolabel --input … --run-dir … --episodes 表达式 [--source-manifest …] [--resume] [--plan-stage …] [VLM 参数] [行为开关] --json`

- 只处理所选里没有任务标注的条；每条一行写进 `autolabel/captions.jsonl`：`ok`（有描述）、`unclear`（模型看不清，正常结果）、`error`（调用失败或解码失败，带 `incidents`）。
- `--resume` 跳过已经 `ok` / `unclear` 的条。出错的条在下游是执行错误：它的 task_success 记为 `error`（事故步骤 `autolabel`），聚合时 held，重跑 autolabel 后再判。

**check**：`curation check --modules <一档的模块> --input … --run-dir … --episodes 表达式 [--source-manifest …] [--part NNNN] [--resume] [--plan-stage …] [--survivors-out 文件] [--incremental] [--max-episodes N] [VLM 参数] [行为开关] --json`

- 一次只跑一档（D18）：`timestamp_check,kinematic_limits,motion_quality`（数值档）、`visual_quality,video_action_sync`（帧档，共用一次解码）、`task_success`（VLM 档），或 `dedup`、`skill_profile` 之一（对整个 keep 集合一次跑完）。混档是参数错误。
- 每条做完立刻追加到 `checks/<module>/parts/<part>.jsonl`（整行写入并 fsync）；`--part` 不给时取比现有最大编号大一的号。结束时重写 `results.jsonl`。
- `--survivors-out` 写出进入下一档的条（本档硬门没拦下、也没出错的），Daemon 用它当下一档的 `--episodes @文件`，与 v1 的漏斗一致。
- **出错与弃权严格分开**（D33）：模型正常答了「看不清 / 拿不准」是 `abstain`（或 `unclear`），不是错；一次模型调用最终失败、一路机位解码失败、样本读不了、进程两次死在这条上，这条就是 `verdict = error`，`error.incidents` 写明步骤、调用类型、机位、原因和尝试次数——即使 v1 的兜底逻辑仍给出了结论（结论照旧留在 `passed` / `score` / `details` 里备查）。单条出错不影响其他条，命令仍以 0 退出；`--json` 给逐状态计数、`error_episodes` 和输入集合的指纹 `input_digest`。
- 改了标的条（`adjudication/labels.json`）按新标注重判，口径按裁决时记下的 `relabel_rerun`（D39）：`v1`（缺省）就是 v1 的 rejudge——多视角联合打分加逐机位末态复核，参数相同，不跑任务类型识别、机位提示、判废护栏和取证仲裁，发出的请求与 v1 逐字节相同（对账工具的 `dump-v1 -- rejudge` 与 `run-v2 --from` 逐位核对）；`full` 是首轮的完整流程。记录的 `details` 写明 `task_desc`（新标注）、`task_desc_source`（`人工改标`）和 `relabel_rerun`。
- 不带 `--source-manifest` 时，读到才发现缺源文件的条（规则同 snapshot，只对 v2）不写结果行，列在 `--json` 的 `skipped_missing_source` 里并记进 `skipped_episodes.json`，不计入总数；其它读失败照旧记为出错（聚合时 held）。
- `--resume`：跳过已有非错误结果的条。SIGTERM 时做完在手的条再退出（退出码 5）；SIGKILL 后 `inflight.json` 留着当时在手的条，下次 `--resume` 把它们的崩溃次数加一，重跑；同一条两次出现在死掉的进程手里就记为 `error`（步骤 `crash`）并跳过（P14）。中断后续跑的结果与一次跑完逐字段相同（耗时字段除外）。
- VLM 档熔断（P15）：开头连续 20 条都因为基础设施原因出错（连不上、超时、5xx、限流），整个模块以退出码 4 结束，不再烧配额。
- 技能画像：dedup 判定为字节级重复的条不进画像（由人捞回的条除外，见 aggregate）。`--incremental` 保留已有的分类体系，只动变化的部分（v1 的 `_sync_profile`）：不在 `--episodes` 里的条移出画像（弃用、人工判失败、改标重判仍失败），改标的条按新标注重新归类，由人带回交付的条（复议捞回、对拒绝条目人工判成功）不调模型打 caption、直接按文本归类（人工改标，否则原始标注，否则 autolabel 的 caption，都没有就留「未归类」），其余新加入的条先打 caption 再归类。分类体系要的文本调用最终失败，模块以退出码 4 结束。

**aggregate**：`curation aggregate --run-dir … --phase funnel|final [--revision N] [--modules a,b] [--episodes 表达式] [--input …] --json`

- 纯计算、秒级、每次全量重算。模块取 `--modules`，否则取 `plan.json`；episode 取 `--episodes`，否则取有结果的全部。
- `funnel`：六项漏斗检查 → 每条 keep / drop / held（`verdicts.jsonl`）和 `keep.txt`。硬门拦下的条不看后面的档；某档有模块出错时停在这一档：如果正常判完的模块已经判它不合格（硬门失败，或各软分都有、加权低于阈值），照样 drop，出错的模块写进原因「另有…执行出错，不影响结论」（D35）；否则 held（「待补跑」）。弃权不是错，照常 keep 并进 review。不给 `--revision` 时写到 `<run-dir>/funnel/`。
- `verdicts.jsonl` 始终是检查本身的漏斗判决；`keep.txt`（dedup 与技能画像的输入）还要跟着已应用的人工裁决走：弃用的、人工判失败的移出，复议捞回的、对拒绝条目人工判成功的加入（`counts` 里的 `decided_in` / `decided_out`）。没有裁决时两者一致。
- **dedup 只在第一个结果版本跑一次**，人工裁决之后不再跑：它的结论保持不变（被人判失败的那条原件去掉了，它的副本仍按副本拒绝），由人带回交付的条从不去重（v1 的 rejudge 同样如此）。
- `final --revision N`：再叠加 dedup、skill_profile 和已应用的人工裁决，写 passed / reject / held（三者不相交、合起来是全部）和 review 视图。人工裁决按 v1 的优先级：「弃用」压过一切（包括 held）；人工判了任务成败就以人为准，改标后不再重判（改标时顺手给的成败结论同样采信，C1 1.3；改标的回答一变它就作废，见 adjudicate-apply）；复议只对注册表标为可复议的拒绝有效（`curation.contracts.modules.appealable`：只被 task_success 拒掉的、被 dedup 判为重复的），物理与结构的硬门和软分是终判；恢复只推翻被复议那个模块的结论——去重的复议恢复后回到 passed（技能画像给它归档，它在 task_success 上的弃权从下一版起进 review），另有模块对它执行出错的恢复后 held（P11）；「拿不准」只记录，这条留在队列里。改了标还没按新标注重判的条 held。
- review 视图按 v1 的队列，复核种类取自注册表的目录（D42、D43）：成败弃权（`task_verdict`）只问在 passed 里、task_success 弃权、人还没下结论的条；EEF 与画面核对（`eef_check`，registry 1.9）只问在 passed 里、EEF 模块转人工、人还没下结论的条；标注分歧（`label`）只问 passed 里的条；被拒的条没有这两种问题，拒绝可复议时给一项 `reject_appeal`（去重的写明 `duplicate_of`）；held 的条什么都不问。每一项都写明注册表的 `line`。
- 缺源文件被跳过的条（D40）不进任何清单、不计入总数（`counts.skipped`）。技能画像整个模块失败（退出码 4，没写出结果）时，它本该归档的每一条都 held（「技能画像执行出错」），一条都不交付，等重试成功（D41）。
- `--input` 给了才在 passed 里写出交付用的任务描述（原始标注要从数据集的元数据里读）。已有 `commit.json` 的版本拒绝改写。

**adjudicate-apply**：`curation adjudicate-apply --run-dir … --decisions decisions.json --json`

- `decisions.json` 只含本任务尚未应用的裁决（D32，`cli/decisions.schema.json`）；按 id 去重，重复应用不改变任何东西。写 `adjudication/applied.jsonl`、`adjudication/labels.json` 和 `human-decisions/*.csv`（v1 的列与用词）。
- `--json` 的 `rerun_task_success` 是要按新标注重判的条（改了标、且没有人工判定成败的），`profile_resync` 是技能画像要重新归档的条。
- `decisions.json` 顶层的 `relabel_rerun`（`v1` 缺省 / `full`，D39）随每条改标记进 `applied.jsonl` 和 `labels.json`，`--json` 原样带回；之后重试这些条也按记下的口径判。
- 改标的同时给了成败结论（判成功 / 判失败）的条不重判，以人为准（注册表 1.3 的后续问题，v1 的 `human_concluded`）；给的是「拿不准」照常重判。
- 这种跟着改标给的结论（task_success 没有弃权的条上的成败结论）只在改标的回答仍是最新、且结论在它之后给出（按裁决 id）时算数；改标的回答一变（维持原标注、拿不准、换一段新标注），它就作废：不起作用，仍在生效的改标照常重判（`rerun_task_success` 列出），与 Daemon 的 `Queue._stands` 是同一条规则。task_success 弃权的条，成败结论是卡片自己的问题，不会这样作废。
- `eef_check`（registry 1.9）：`consistent` / `inconsistent` / `unsure`，当作 EEF 模块在这条上的结果，不调模型、不用重跑；
  副本在 `human-decisions/eef_checks.csv`。
- 没有执行规则的复核种类（`line` 不是 `label`、`task_verdict`、`reject_appeal`、`eef_check`）报参数错误（退出码 2）并写明是哪一种，不会跳过；对没有可复议拒绝的条目复议（被自己的硬门或软分拒掉、已弃用、根本没被拒）同样报参数错误（D42），一条也不应用。

**report**：`curation report --run-dir … --revision N [--format md,json] [--modules a,b] [--subtask-id ID] --json`

- 在 `revisions/r<NNNN>/` 写 `report.md`（中文，给人看）、`report.json`（`cli/report.schema.json`）、`perf.json` 和 `tables/*.parquet`，最后写 `commit.json`：这个版本用了各模块的哪些分片、哪些人工裁决。只有带 `commit.json` 的版本才算数；哪个版本生效由 Daemon 在上传核验之后切换（D25）。
- 总览的 `counts.skipped` 与完整性一块的 `integrity.skipped_episodes`（缺了哪些文件）列出缺源文件未质检的条（D40），`report.md` 有「缺源文件未质检」一行和「未质检的条目」一节。
- 各模块的 `adjudication`：`pending` 只数注册表里计入待裁的问题（成败弃权、标注分歧），`appealable` 是这个模块拒掉、还能复议的条数（只有拒绝可复议的模块才有）；去重的是 `{"pending": 0, "appealable": N}`（D43）。
- 报告里的 token 用量来自 `usage.jsonl`（两本账：实际与分摊，`requests_unknown_usage` 是响应里没有用量的请求数，不估算）；时延来自 `details/vlm_latency.csv`。

**export**：`curation export --run-dir … --input … [--source-manifest …] [--revision N] [--output <交付目录>] [--incremental] [--concurrency N] --json`

- 导出版本（默认最新提交的那个）的 passed 到 `export/lerobot_curated/`：待人工确认的条照 v1 的保守口径一起交付，待补跑的（held）不交付。任务描述按来源写（原始标注、自产 caption、人工改标），`export/manifest.json` 的每条带 `task`，并列出全部文件及大小（`files`）。
- `--incremental` 在上一次导出的基础上只动变化的部分（W7）：`diff` 给出 keep / relabel / renumber / add / drop 的条数；信不过上一次导出时退回全量，原因写在 `full_reason`。
- `--output` 时同步到交付目录：先删 `_COMPLETE`，上传新增和变化的文件、删掉不再交付的文件，最后写 `export/manifest.detail.json` 和 `export/manifest.json`；之后由 `curation verify` 回读并写 `_COMPLETE`。
- 源数据和 `source_manifest.json`（不给 `--source-manifest` 时默认用运行目录里的）对不上时退出码 6。

**verify**：`curation verify --run-dir <本地运行目录> --output <交付目录下的 run_id 目录> [--visibility-timeout 60] --json`

- 关键文件 = 运行目录里的全部文件（排除 `logs/`、`inflight.json`、隐藏文件和临时文件）加上 `export/manifest.json` 的 `files` 里列出的数据集文件（`export/lerobot_curated/` 下，按清单里的大小核对）。
- 逐个检查：存在（`missing`）、大小一致（`size_mismatch`）、开头不是全零（`zero_filled`）、能解析（`unparseable`：JSON / JSONL 整体解析，parquet 看首尾魔数并解析 footer，mp4 找 `moov`，JPEG / PNG 看魔数）。列举里有但读不到的文件在 `--visibility-timeout` 秒内反复重试，仍读不到记 `not_visible_in_time`。交付目录本身读不了是退出码 3（`output_unreachable`）。
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
  `needs_input: trajectory_missing`，给了文件没给 VLM 后端时报 `needs_input: vlm_backend_missing`。只有它、没有 `task_success` 时
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

### mcap 与 Lance 数据集（D44，F6.5）

v1 在 `dev` 的 PR #155 里接入了这两种格式：读取器 `ingest/mcap_reader.py`、`ingest/lance_reader.py` 与交付用的 `export/mcap_writer.py` 是 A 类代码，原样搬来（冻结点随之前移到 `dev@eb637ba40`）；v2 的命令在外面包了一层 `cli/containers.py`，判决仍全部出自 v1 的读取器与算法（合成数据上 v1 对 v2 逐位对账，`tools/parity/tests/test_containers_parity.py`）。

| | mcap | Lance |
|---|---|---|
| 认什么 | 数据集根目录下的 `*.mcap`，一个文件一条 episode（子目录里的不算）。编号照 v1：文件名是 `episode_<N>.mcap` 的按 N（混进来的其他 `.mcap` 不读，预检给警告），一个都不是就按文件名排序从 0 编 | lerobot-lance-convert（0.3.0 起）的布局：`frames.lance`、`videos.lance`、`meta.lance` 三张表加 `meta/`，`meta/info.json` 带 `storage_format: "lance"`（LeRobot v3.0 元数据）。三表齐但没有这个标记的是旧插件布局，预检报元数据无效（v1 的原话）；只有别的 Lance 表的是 `lancedb`，不支持 |
| 预检读什么 | 每个文件只读摘要区（通道、消息数、元数据记录），TOS 上是几次按范围读，不读消息。动作、状态、相机、任务文本、机器人型号按 v1 的 topic 规则认（站点的 `ingest.mcap_mapping` 优先，UMI 的 `/robotN/vio/eef_pose` 自动认）。时间轴取动作 topic 的 `log_time`，没有要配的帧率，`fps` 为 null。任务文本只在 `/task` topic 里的，预检数它有标注但读不到文字（真正的文字质检时读） | `meta/`，读法同 LeRobot v3（含 v1 的 `validate_info`）；缺了 `meta/` 就读 `meta.lance` 里的镜像并给一条警告 |
| 快照记什么 | 全部 `*.mcap`（v1 按目录里有哪些文件来编号，每个文件又是一条 episode 的数据）；`meta_fingerprint` 覆盖全部 mcap 文件 | `meta/` 与三张表的全部对象（读的时候整表读）；`meta_fingerprint` 覆盖 `meta/`，没有 `meta/` 时覆盖 `meta.lance/` |
| 交付 | `export/mcap_curated/`：v1 的 `export_mcap_curated` 原样。passed 各条的 `.mcap` 逐字节拷贝（源文件不是 `episode_<N>.mcap` 命名的改成这个名字），`index.json` 列每条的任务文本与来源；自产描述与人工改标只写进 `index.json`，文件本体不动 | `export/lance_episodes/`：Lance 原格式交付本版本未做，交的是 v1 的 `episodes_parquet/`（passed 各条的轨迹级数值，任务文本写进 `instruction` / `instruction_source`）和 `videos/`（视频指针改写到交付位置）。`index.json`、导出结果的 `note`、报告的「数据包」一节都写明这一点 |
| 增量导出 | 没有：`--incremental` 退回全量，`full_reason` 写明原因；内容没变的文件不重新上传 | 同左（daft 每次给 parquet 分片起新名字，这一个文件每次都换） |
| EEF–视频一致性 | 可用（F5.13）：trajectory.json 的相机写 `media.uri=episode_<N>.mcap` 与 `media.topic`（图像 topic），模块把该 topic 的 JPEG / H.264 帧转成本地视频读，TOS 上从源缓存读 | 不支持：`unsupported`，原因码 `format_unsupported_by_module` |

- **数据集语义取整个任务的所选**：v1 用所选 episode 的前 100 条判定数据集语义（控制模式、单位等）。v2 的命令只读某一档的幸存者，所以读源数据的命令（`autolabel`、`check`、`aggregate --phase final`）要带 `--selection <整个任务的所选>`（语法同 `--episodes`，Daemon 自动传；不带时取 `--episodes`），判定取它的前 100 条，与 v1 一致。LeRobot 数据集不受影响（它的语义样本一直是数据集的前 100 条）。
- **TOS 上的数据先拉到本地再读**：v1 的两个读取器只认本地目录。`tos://` 上的数据由 `SourceCache` 在本地留一份副本：mcap 先给每个文件放一个空占位（v1 按目录里的文件名编号，占位不会被读），读到哪条才下载哪条；Lance 第一次读时整表下载（读取器要整表）。设了 `CURATION_SOURCE_CACHE` 就放在它下面（`<目录>/<格式>-<地址哈希>/<数据集名>/`，同一任务后面的命令复用，Daemon 在运行结束时删掉），没设就放在这条命令自己的临时目录里、命令结束就删。每个副本按列举时的大小和 ETag 核对，对不上以退出码 6 结束（`source_changed`）；带 `--source-manifest` 时照常先核对快照。只读源桶，从不往源桶写；凭证只从 `CURATION_INPUT_TOS_*` 环境变量读。
- **临时视频**：读取器转出来的视频（mcap 里 JPEG / H.264 消息转成的 mp4、Lance 里取出的视频）写在 `$TMPDIR`，命令结束时清掉。Daemon 把 `TMPDIR` 指到任务的缓存目录下，不占容器的 `/tmp`。
- **`source_info.json`**：第一条读源数据的命令在运行目录里写下 v1 报告要用的信息（读取器的 `mcap_dataset_info` / `lance_dataset_info`：型号从哪来、时间轴从哪来、有没有任务文本；用到的机器人规格），`report` 据此写出 v1 的数据包体检（`report.json` 的 `integrity.container`、`report.md` 的「数据包」一节）。
- **站点开关**：`ingest.mcap_enabled` / `ingest.lance_enabled`（默认开；v1 的环境变量 `CURATION_MCAP_ENABLED` / `CURATION_LANCE_ENABLED` 优先于配置）。关掉后预检把全部模块标为不支持（原因码 `format_disabled`），读源数据的命令以参数错误（退出码 2）结束。Helm 里写在 `pipelineConfigOverride.ingest` 下。
- **依赖**：`pylance`（Lance）、`mcap`、`mcap-ros2-support`（cdr 消息）、`mcap-protobuf-support`（protobuf 消息），版本钉在 `backend/requirements.txt`；都是懒导入，不质检这两种格式时用不到。

## Daemon 的调用顺序

一次完整运行（00 篇 §4，每档读上一档的幸存者）：

```
preflight → plan → snapshot → autolabel
→ check 数值档 --survivors-out numeric.txt → check 帧档 --episodes @numeric.txt --survivors-out frame.txt
→ check task_success --episodes @frame.txt → aggregate --phase funnel --revision N
→ check dedup --episodes @revisions/rNNNN/keep.txt --survivors-out dedup.txt
→ check skill_profile --episodes @dedup.txt → aggregate --phase final --revision N
→ report --revision N → export --revision N --output … → （同步运行目录）→ verify
```

人工裁决后的重跑（新版本 N+1，旧版本原样保留）：

```
adjudicate-apply → check task_success --episodes <rerun_task_success>（写新分片）
→ aggregate --phase funnel --revision N+1
→ check skill_profile --incremental --episodes @revisions/rN+1/keep.txt
→ aggregate --phase final --revision N+1
→ report --revision N+1 → export --revision N+1 --incremental --output … → verify
```

裁决之后**不再跑 dedup**：第一次的去重结论保持不变，技能画像自己跳过其中的副本，`final` 对由人带回的条不做去重（与 v1 相同）。`keep.txt` 已经按裁决增减过，直接交给技能画像。

mcap / Lance 数据集的顺序相同，`autolabel`、`check`、`aggregate --phase final` 多带 `--selection <任务的所选>`；数据在 TOS 上时，每条命令的环境里有 `CURATION_SOURCE_CACHE`（任务的本地副本）和 `TMPDIR`，运行结束后 Daemon 删掉这个目录。

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
     --modules timestamp_check,kinematic_limits,motion_quality,visual_quality,video_action_sync,task_success,dedup,skill_profile
   $C snapshot --input "$D/mini" --episodes 0-7 --out "$R/source_manifest.json"
   ```

   应看到：计划的 VLM 档闸门是 `episode 32, probe 64, endstate 64, arbitration 32, guard_caption 32`（N=64，与 v1 出厂值相同）；快照 `27 objects`（3 个元数据文件 + 8 条 × 3 个文件）。
   `preflight --json` 的输出可以用 `$PY -c "import json,sys; from curation.contracts import schemas as s; print(s.errors('cli/preflight.schema.json', json.load(open(sys.argv[1]))))" "$R/preflight.json"` 核对，应为空列表。

3. 按 Daemon 的顺序跑完一遍（每条约几秒）：

   ```bash
   S="--input $D/mini --run-dir $R --source-manifest $R/source_manifest.json"
   $C autolabel $S --episodes 0-7
   $C check --modules timestamp_check,kinematic_limits,motion_quality $S --episodes 0-7 --survivors-out "$R/stages/numeric.txt"
   $C check --modules visual_quality,video_action_sync $S --episodes "@$R/stages/numeric.txt" --survivors-out "$R/stages/frame.txt"
   $C check --modules task_success $S --episodes "@$R/stages/frame.txt"
   $C aggregate --run-dir "$R" --phase funnel --revision 1 --episodes 0-7
   $C check --modules dedup $S --episodes "@$R/revisions/r0001/keep.txt" --survivors-out "$R/stages/dedup.txt"
   $C check --modules skill_profile $S --episodes "@$R/stages/dedup.txt"
   $C aggregate --run-dir "$R" --phase final --revision 1 --episodes 0-7 --input "$D/mini"
   $C report --run-dir "$R" --revision 1
   ```

   应看到：autolabel 给 2 条（4、6）补了描述；数值档 8 条里 2 条（2 时间戳跳变、5 残段）被硬门拦下；task_success 6 条里 3 条 pass（1、4、6，都是仲裁救回的）、3 条（0、3 和 3 的字节级副本 7）abstain，`error 0`；dedup 剔除 7（与 3 重复）；final 为 `passed 5, reject 3, held 0, review 3`（0 和 3 问成败，7 是可复议的去重拒绝）。
   假模型的回答只看请求的文字、图片张数和像素尺寸，不看图片字节，所以这些数在 macOS 和 Linux 上一样（`tests/cli/test_fake_model.py`）。
   `cat "$R/revisions/r0001/report.md"` 是中文报告，含「通过 5」和各项检查的拦截数；`tail -3 "$R/usage.jsonl"` 是按模块记的 token 用量。
   没给 `--concurrency`，所以整个过程中任何时刻只有一个模型请求在飞（`tests/cli/test_pipeline_chain.py` 在假模型那边量过）。

4. 导出与交付核验（本地目录充当交付目录）：

   ```bash
   $C export --run-dir "$R" --input "$D/mini" --output "$D/delivery"
   rsync -a --exclude export/lerobot_curated --exclude inflight.json "$R/" "$D/delivery/"   # Daemon 的同步
   $C verify --run-dir "$R" --output "$D/delivery" --visibility-timeout 0 --json
   ```

   应看到：导出 5 条（`incremental: false`，`add 5`），`verify` 输出 `"failed": []`、`"complete_marker": true`，`$D/delivery/_COMPLETE` 出现。

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
    {"id": 2, "episode_index": 4, "line": "label", "decision": "custom_label", "new_label": "wipe the table", "note": null, "decided_by": "me", "decided_at": 1790000000001}]}
   EOF
   $C adjudicate-apply --run-dir "$R" --decisions "$D/decisions.json"      # re-judge task_success: 4 (relabel_rerun v1)
   $C check --modules task_success $S --episodes 4
   $C aggregate --run-dir "$R" --phase funnel --revision 2 --episodes 0-7
   $C check --modules skill_profile $S --episodes "@$R/revisions/r0002/keep.txt" --incremental
   $C aggregate --run-dir "$R" --phase final --revision 2 --episodes 0-7 --input "$D/mini"
   $C report --run-dir "$R" --revision 2
   $C export --run-dir "$R" --input "$D/mini" --output "$D/delivery" --revision 2 --incremental
   ```

   应看到：第二版的漏斗行末尾是 `keep.txt after the human decisions: 0 in, 1 out`（3 被人工判失败，移出 `keep.txt`）；没有再跑 dedup（`ls "$R/checks/dedup/parts"` 仍只有 `0001.jsonl`），7 仍按 3 的副本拒绝；技能画像这次只归 4 条（3 移出，7 是副本不归）；第二版 `passed 4, reject 4, review 3`（0 仍待判成败；4 按 v1 的两层重判后弃权，重新问成败；7 是可复议的去重拒绝）；再跑一次 `adjudicate-apply` 显示 `applied 0 decision(s) (2 already applied)`；导出是增量的：`diff` 为 `keep 2, renumber 2, drop 1`，没有复制任何视频；`revisions/r0001/` 原样未动。

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
      $C autolabel $S --episodes 0-7
      $C check --modules timestamp_check,kinematic_limits,motion_quality $S --episodes 0-7 --survivors-out "$R/stages/numeric.txt"
      $C check --modules visual_quality,video_action_sync $S --episodes "@$R/stages/numeric.txt" --survivors-out "$R/stages/frame.txt"
      $C check --modules task_success $S --episodes "@$R/stages/frame.txt"
      $C aggregate --run-dir "$R" --phase funnel --revision 1 --episodes 0-7
      $C check --modules dedup $S --episodes "@$R/revisions/r0001/keep.txt" --survivors-out "$R/stages/dedup.txt"
      $C check --modules skill_profile $S --episodes "@$R/stages/dedup.txt"
      $C aggregate --run-dir "$R" --phase final --revision 1 --episodes 0-7 --input "$IN" --selection 0-7
      $C report --run-dir "$R" --revision 1
      $C export --run-dir "$R" --input "$IN" --output "$D/delivery_$F"
      rsync -a --exclude export/mcap_curated --exclude export/lance_episodes --exclude inflight.json "$R/" "$D/delivery_$F/"
      $C verify --run-dir "$R" --output "$D/delivery_$F" --visibility-timeout 0 --json | grep -E '"failed"|complete_marker'
    done
    ```

    应看到：预检 `kind` 分别是 `mcap`（`version: null`，`fps: null`，detail 写着时间轴取自动作 topic 的 `log_time`）和 `lance`（`version: v3`），8 条、2 路相机、`robot_type: franka`、6 条有标注；EEF 模块在 mcap 上是 `needs_input: trajectory_missing`（给了 trajectory.json 就能用，F5.13），在 Lance 上是 `unsupported`（`format_unsupported_by_module`），其余可用。
    判决与第 3 步的 LeRobot 数据集完全相同：数值档拦下 2、5，task_success 3 pass 3 abstain，dedup 剔除 7，final 为 `passed 5, reject 3, held 0; 3 to review`。
    `report.md` 多一节「数据包(mcap)」/「数据包(lance)」：mcap 写着交付 `mcap_curated/`（5 个 .mcap，原格式逐字节）和型号、时间轴、任务文本三项体检；Lance 写着「lance 原格式交付本版本未做」。
    导出：mcap 是 `export/mcap_curated/` 下 `episode_0/1/3/4/6.mcap` 与 `index.json`（`cmp "$D/mini_mcap/episode_0.mcap" "$D/delivery_mcap/export/mcap_curated/episode_0.mcap"` 无输出），日志写「改标 2 条记入 index.json,文件本体不动」（4、6 是自产描述）；
    Lance 是 `export/lance_episodes/` 下 `episodes_parquet/`、`videos/` 与 `index.json`，导出结果带 `note`。两边 `verify` 都是 `"failed": []`、`"complete_marker": true`。`ls $TMPDIR` 里不留读取器转出的视频。
    TOS 上的读法（本地副本、按范围读摘要、源对象变化退出码 6、只读源桶）由 `tests/cli/test_containers.py` 在假 TOS 上核对；有自己的桶时，把两个目录传上去，
    设好 `CURATION_INPUT_TOS_ACCESS_KEY` / `CURATION_INPUT_TOS_SECRET_KEY` 与 `CURATION_SOURCE_CACHE=$D/cache`，把上面的 `IN` 换成 `tos://…` 再跑一遍，判决应不变，`$D/cache` 里是 mcap 读过的那几条文件和 Lance 的整表。

11. 自动化测试（约 4 分钟）：

   ```bash
   $PY -m pytest -q tests/cli tests/contracts tests/planner tests/export
   $PY -m curation.contracts check
   $PY -m pytest -q curation/tests --ignore=curation/tests/test_environment.py   # v1 单测，1407 passed
   (cd .. && PYTHONPATH=tools $PY -m pytest -q tools/parity/tests)               # 对账工具，含 v1 对 v2 的逐位对账
   ```

   `tests/cli` 里：`test_pipeline_chain.py` 在夹具上按上面的顺序跑完整条链并逐个校验契约；`test_check_resume.py` 是 SIGTERM / SIGKILL 后续跑；`test_errors_and_policy.py` 是出错与弃权的区分、默认不并发不重试、源数据变化；`test_aggregate.py` 是聚合与裁决的规则；`test_revision_flow.py` 是第 7 步的第二个版本与增量导出；`test_fake_model.py` 钉住假模型的答案与图片字节无关（换一种 JPEG 质量重编码，判决不变）。
