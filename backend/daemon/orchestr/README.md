# 任务编排与 CLI 执行器（W5a）

把任务交给 CLI 按 episode 批次流水执行：排队、执行、暂停恢复停止、崩溃恢复、四种子任务、发布到交付目录，
以及启动前的检查、数据集登记与核对、浏览与 episode 列表。F2.3，以及 F2.2、F2.6、F2.7 中属于编排的部分。

设计依据：`docs/design/00-overview.md` §4、`01-data-model.md` §2–§3、`02-cli-and-files.md` §3–§4、`03-rest-api.md` §3、§12、
`04-execution.md` §2–§3、§7、`06-delivery-and-report.md` §1、§4、`09-deployment.md` §2；决策 D5、D9、D20、D24–D30、D35、D37、D39–D43，
P1、P14、P15、P17。契约：C2 `backend/curation/cli/README.md`（命令、退出码、调用顺序）、C3 进度协议、C4 `openapi.yaml`（1.7.0）、
C5 `daemon/repo/protocol.py`（状态机只经由 `daemon.transitions`）。

## 文件

| 文件 | 内容 |
|---|---|
| `../exec/runner.py` | 执行器：每条命令一个进程组（`start_new_session`），环境来自 W8 的 `cli_environment`，另加单线程的 `OMP_NUM_THREADS=1` 一类变量（D54，Daemon 自己的环境里设了别的值时以它为准），子进程 `oom_score_adj` +500；stdout 只收一份 `--json` 文档，stderr 逐行交给 C3；SIGTERM（暂停，90 秒后 SIGKILL）、SIGINT（停止，10 秒后 SIGKILL）；退出码按 02 篇 §4 映射 |
| `../exec/c3.py` | C3 行的解析与规整；不是 C3 的行（原生库的打印、Python 警告）变成 `warn` 日志，不丢也不打断 |
| `../exec/usage.py` | token 用量按「子任务 × 模块 × 调用种类 × 模型 × 账本」累加，约 5 秒写一次库，SSE 推累计值 |
| `config.py` | 编排的配置（环境变量，见下表） |
| `service.py` | `Orchestrator`：路由调用的入口；生命周期钩子（就绪后收拾孤儿进程、启动 worker 池和清理线程；停机时系统暂停）；动作、子任务、D37 重新预检、清理交付产物、执行计划、预检 |
| `scheduler.py` | 队列与 worker 池：`CURATOR_MAX_RUNNING_TASKS` 个槽（缺省 3），主流程与子任务共用，先进先出；重启后从库里重建队列 |
| `cpupool.py` | 全局 CPU 名额池（D54）：大小是核数 − 2，所有在跑任务的 CPU 档每条在途 episode 占一个名额，整档执行（重试等）按块拿；按公平份额轮流，先开跑的任务占满了后来的也能拿到自己那一份 |
| `runbase.py` | 所有运行共用的部分：意图（暂停 / 停止 / 停机）、日志、进度、按档调用 CLI（崩溃后带 `--resume` 重新拉起并点名在处理的 episode）、参数、结果版本、同步与核验、`latest` |
| `pipeline.py` / `episode_pipeline.py` / `stage_worker.py` | 主流程漏斗：numeric、frame、VLM 各用一个持久的 `multiprocessing` worker；按并发额度逐条交接、持续补位与 SQLite 续跑；外部 CLI 保留批次兼容路径 |
| `runs.py` | 主流程与四种子任务：`MainRun`、`ResumeRun`、`RetryRun`、`AdjudicationRun`、`ReexportRun`；建议性模块的 `advisory_<档>` 阶段（全部选中条目，任务参数里的上传句柄换成运行目录 `inputs/` 下的副本路径，F5.5） |
| `planning.py` | 第一次运行时调 W6 的 planner 生成 `plan.json`、`run.json` |
| `rules.py` | 纯函数：模块状态与终态规则（D35）、episode 选择、批次名、清单指纹与变化（D37）、读不到 W5b 的汇总时按清单兜底计数 |
| `start.py` | 启动前：三项检查（D30）、数据集指纹核对（D37）、固化输入并入队 |
| `datasets.py` | 数据集登记、核对（`curation snapshot` 全量清单，按内容寻址存在 `<data>/datasets/listings/`）、重新预检 |
| `browse.py` | `GET /datasets/browse`、`GET /datasets/episodes`（游标分页、相机的预签名地址） |
| `delivery.py` | 交付目录：TOS（输出密钥）或本地替身；批次名分配、增量同步、`latest`、每个交付目录一把发布锁 |
| `workdir.py` | 任务工作目录 `CURATOR_WORK_DIR/<task_id>/` 与编排自己的 `.orchestr/`（启动标记、各次运行的日志本、同步记录、清理 / 取回 / 清理交付产物的标记） |
| `janitor.py` / `backfill.py` | 终态 7 天后清理本地工作目录；子任务或读结果时从交付目录取回（大文件不取回） |
| `resources.py` | 帧档按内存准入；Daemon 自己的 `oom_score_adj` 尽力降到 −500（没有 CAP_SYS_RESOURCE 时只记一行日志） |
| `../uploads.py`、`../routes/uploads.py` | 模块参数的输入文件（F5.5）：`POST /uploads` 上传即校验、按属主存在 `<data>/uploads/`；任务只认 `upload:` 句柄，建任务时带文件做模块预检（`service.module_preflight`），开始时复制进 `inputs/`（`service.materialize_uploads`） |
| `../routes/runs.py`、`../routes/datasets_exec.py` | 路由 |

## 接口

`POST /tasks`、`POST /tasks/batch`、`POST /tasks/{id}/actions/{start|pause|resume|stop}`、`POST /tasks/{id}/repreflight`、
`POST /tasks/{id}/retry`、`POST /tasks/{id}/continue`、`POST /tasks/{id}/reexport`、`POST /tasks/{id}/adjudication/apply`、
`POST /tasks/{id}/purge-artifacts`、`GET /tasks/{id}/plan`、`POST /preflight`、`GET /datasets/browse`、`GET /datasets/episodes`、
`POST /datasets`、`POST /datasets/{id}/recheck`、`POST /datasets/{id}/repreflight`、`POST /uploads`、`GET /uploads/{id}`。写接口都支持 `Idempotency-Key`，
每个响应在测试里按 C4 校验。

## 行为要点

- **建任务**：配置校验沿用 `daemon.taskspec`（与 PATCH 同一套）；模型的思考强度按 W8 的表校验。`start_now`（缺省）时依次做
  三项检查（422 `precheck_failed`）、D37 指纹核对（409 `source_changed`，`details` 是 `SourceChange`，登记的核对历史里多一条），
  然后固化 `run_id`（在交付目录里写 `<run_id>/run.json` 占位，同名加 `-2`）、预检结果、源文件清单、模型设置（不含密钥），
  `created → queued` 入队。不马上开始时只登记数据集。批量建任务先全部校验（`details.item` 指出第几项），再逐个建；
  逐个开始失败的写进 `warnings`。
- **执行**：补描述后，numeric、frame、VLM 各启动一个持久的 `multiprocessing` worker，每条 episode 完成并提交 SQLite 后即可交给下一层；空出的执行槽立即补入已就绪条目。
  `POST /tasks` 或待启动任务的 `PATCH /tasks/{id}` 可传 `params.batch_size`（1–256 条/次派发）；不传时按并发度取 8–64 条，小数据集自动减小。
  此值不限制每层在途并发：并发由实际 plan 决定。层间等待队列按两次派发量或下游并发度取较大值，并计入上游在途条目的有界余量。
  numeric/frame 共用 CPU 总预算，逐条释放额度；预算为 1 时交替推进，也不持有整批锁。任务的 `limits.cpu_concurrency` 是上限，实际值见 plan 的 `value` 和 `bound_by`。
  plan 里 CPU 档的并发是这个任务最多用多少（核数 − 2 与任务上限取小）；派发一条 CPU episode 前还要从全局 CPU 名额池拿一个名额（帧档先过内存准入），
  做完归还，几个任务同时跑时在途的 CPU episode 总数不超过池的大小（D54，设计 04 §2.3）。数据完整性档只在开了逐帧解码时拿名额。
  自定义 `CURATOR_CLI` 仍按批次调用，以保留包装脚本的执行语义。
  实时进度的 `pipeline` 字段给出实际在途数、等待数和最近五次派发，UI 可同时观察三层重叠与新条目进入。
  耗时汇总按每条 episode 的处理时间计算，同层共享执行的模块只计一次；排队和 CPU 准入等待不计入。
  每档的运行区间只用于时间轴，任务总耗时仍是端到端墙钟时间。
  每条 episode 的模块结果和下一层位置写入 `.orchestr/episodes.sqlite3`，暂停或崩溃后按 episode 恢复。
  整体失败的模块门放行。漏斗完成后再做漏斗判决 → 去重 → 画像 → 终判 → 报告 → 导出 → 同步与核验。
  缺源文件被剔除的 episode（D40）不进计划、不进进度总数。
  每个检查档做完就把 `checks/` 传到交付目录（随产随传），但 `_COMPLETE` 只在最后的核验通过后才写。
- **模块与任务的状态**：模块状态看逐状态计数（有出错条目是 `completed_with_errors`，退出码 4 是 `failed`）；
  终态按 01 篇 §2.5 约束 3 与 D35：没有 `failed` 的模块、`held` 为空才是 `succeeded`。
- **结果版本**：一次运行写一个新版本 `revisions/rNNNN/`，`commit.json` 最后写；同步并核验通过之后才 CAS 切换 `result_rev`
  并记审计事件（D25），然后按 W5b 的 `refresh_summary` 刷新任务汇总（含 `pending_adjudication`、1.4 的 `skipped`），
  并判断交付是否过期（`export_fingerprint` 与当前版本的指纹比较）。同一交付目录的导出、同步、核验、`latest` 串行（每个目录一把锁）；
  `latest` 只在任务成功、已导出且不过期、核验通过时移动。
- **暂停 / 恢复 / 停止**：暂停发 SIGTERM，在途的 episode 做完后退出，任务 `paused`；恢复重新入队，从日志本接着做。
  停止发 SIGINT，10 秒不退就 SIGKILL，整个进程组都不留。有子任务在跑时，这三个动作作用在子任务上。非法迁移一律 409
  `task_state_conflict`，`details.state` 是当前状态。
- **停机与重启**：SIGTERM 时不再接新活，运行中的任务与子任务转为系统暂停（`pausing`，原因「Daemon 停机」），命令收到 SIGTERM；
  超过宽限期就 SIGKILL，任务仍是 `paused`（system），不会 `failed`。启动对账把系统暂停的改回 `queued`，worker 池自己接着跑；
  用户暂停的保持暂停，用户停止的不再运行。Daemon 被直接杀掉时留下的子进程，下次就绪后按 `.orchestr/proc.json` 收拾掉。
- **崩溃**：worker 异常退出（段错误、OOM）时重新拉起，并按 SQLite 与在途记录恢复该批次；
  同一条连续两次出现在崩溃现场，CLI 把它记为出错并跳过（P14）。没有在处理的 episode 却反复崩溃，任务 `failed`。
- **子任务**（同一任务串行，建时就入队）：
  - `retry`：缺省重跑所有出错条目和整体失败的模块；出错的 episode 从出错那一档重跑，整体失败的模块全量重跑；
    去重、画像的输入变了才重跑（画像走 `--incremental`）；新版本，终态按当前结果重算；不导出（交付过期）。
  - `resume`（「继续运行」）：已停止或失败的任务接着主流程的日志本往下做，不重做完成的档和条目。
  - `apply_adjudication`：导出 W5b 的 `Queue.executable()`，即尚未执行、仍然成立的裁决（追问的回答在打开它的判断变了之后作废，
    C4 1.5.1，作废的只记一行日志），`decisions.json` 顶层写 `relabel_rerun`（v1 / full，D39）；`curation adjudicate-apply` 之后
    用 W5b 的 `write_copies` 放回全部裁决的 CSV 副本，随后的发布把它们传到交付目录；改标的条目按新标注重跑任务成败判定，
    画像对新 `keep.txt` 增量同步，去重不重跑；新版本，不导出（D9）；执行完标记这些裁决已应用。
  - `reexport`：当前版本 `export --incremental`，再核验；交付不再过期，`latest` 可能移动。
- **清理与取回**（00 篇 §4.2）：任务到终态、最后一次运行结束 7 天后（`CURATOR_WORK_RETENTION_DAYS`），先把交付目录缺的传上去，
  再删掉工作目录里除 `.orchestr/` 之外的一切，导出临时目录也删。交付目录没接住的一律不删：上传失败（密钥删了、桶不通）或任务根本没有批次，
  目录留着，下一轮（每小时）再试；只有被清理过交付产物（D28）的任务不上传、直接删。
  之后来的子任务、W5b 读结果的接口（`ResultStore.backfill` 钩子）从交付目录取回：只取本地没有的文件，不覆盖本地的，
  交付数据集、审片片段、证据帧、同步曲线不取回。取回算一次活动，保留期从取回时重新计。
  数据库里已没有的任务（删除 30 天后被清掉的）的目录，旧于保留期就整个删除。
- **数据集**：登记、核对、重新预检都跑 CLI（`preflight`、`snapshot`），清单按内容寻址保存，核对只比清单（D37）；
  浏览：公共数据集读目录、本地目录列子目录、TOS 列公共前缀并读 `info.json` 提示；episode 列表读 v2 `episodes.jsonl` 或 v3 的 parquet，
  游标分页，TOS 上的相机视频给预签名地址（v3 带 `from_ts` / `to_ts`）。
- **mcap 与 Lance**（D44，C4 1.11）：登记、核对、浏览、建任务、D37 与 LeRobot 走同一条路，`Dataset.format` 是 `mcap` / `lance`。
  开始前的输入检查在没有 `meta/info.json` 时改读第一个 `.mcap` 文件（或 `meta.lance` 的一个版本文件）的开头几个字节。
  浏览时没有 `info.json` 的目录按文件认 `mcap` / `lance` / `rrd`；episode 列表对 mcap 按 v1 的规则从文件名编号，这一页每个文件读一次摘要区
  （几次按范围读）拿时长与元数据里的任务文本，任务文本只在 `/task` topic 里的给 `task_unread`；这两种格式都没有相机地址。
  运行时，读源数据的命令多带 `--selection <任务的所选>`（数据集语义取它的前 100 条，与 v1 一致），环境里多 `CURATION_SOURCE_CACHE`
  （TOS 上的数据先拉到 `CURATOR_SOURCE_CACHE_DIR/<task_id>/`，同一次运行的各条命令复用）和指向它下面 `tmp/` 的 `TMPDIR`（读取器转出的视频）。
  运行结束（完成、失败、暂停、停止都算）就删掉这个目录，下次运行重新拉；崩溃留下的由清理线程在任务没有运行时删掉。
  导出的数据集在 `export/mcap_curated/` 或 `export/lance_episodes/`，和 `lerobot_curated/` 一样由 CLI 自己上传、同步时跳过、不取回；
  Lance 导出的说明（原格式交付未做）写进任务日志。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `CURATOR_MAX_RUNNING_TASKS` | 3 | 同时运行的任务（主流程与子任务一起算，P1、D54） |
| `CURATOR_CLI` | 当前解释器 `-m curation.cli` | CLI 的命令行 |
| `CURATOR_SITE_CONFIG` | `$CURATION_CONFIG` | planner 读的站点配置（`concurrency` 里的 VLM 并行度、`vlm` 段），缺省就是 Chart 写的 site.yaml；旧文件里的 `concurrency.cpu` / `cpuMax` 忽略并告警（D54） |
| `CURATOR_CPU_CORES` | 容器的 CPU 配额（cgroup v2 / v1），没有配额时是本机核数 | 核数：减 2 是全局 CPU 名额池的大小，也是一个任务最多用的 CPU worker 数（P4、D54） |
| `CURATOR_MEMORY_ADMISSION` | 0.8 | 内存占用高于这个比例时帧档等待（0 = 不管） |
| `CURATOR_TERM_GRACE_S` / `CURATOR_INT_GRACE_S` | 90 / 10 | SIGTERM、SIGINT 之后多久 SIGKILL；Pod 的 `terminationGracePeriodSeconds` 要不小于 preStop + 前者 + 约 20 秒 |
| `CURATOR_VERIFY_VISIBILITY_S` | 60 | `curation verify --visibility-timeout` |
| `CURATOR_WORK_RETENTION_DAYS` | 7 | 终态之后多久清理本地工作目录（0 = 不清理） |
| `CURATOR_ORCHESTRATOR` | on | `off` 时 worker 池和清理线程都不启动（维护用） |
| `CURATOR_LOCAL_DELIVERY_ROOT` | 空 | **实验性，只用于调试和测试**：`tos://桶/前缀` 的交付写到本地 `<根>/桶/前缀` |
| `CURATOR_CODE_VERSION` | 空 | 写进 `run.json` 的代码版本（镜像构建时注入） |

## 手动验证步骤

在 `backend/` 下执行。用对账工具的 8 条合成数据集、本地交付替身，只选不调模型的六个模块：

```bash
export D=$(mktemp -d)
export CURATOR_DATA_DIR=$D/data CURATOR_LOCAL_DATA_ROOT=$D/inputs CURATOR_LOCAL_DELIVERY_ROOT=$D/tos \
       CURATOR_MASTER_KEY=$(openssl rand -base64 32) CURATOR_BASE_PATH=/curation \
       CURATOR_AUTH_USER=demo CURATOR_AUTH_PASSWORD=demo-pass CURATOR_LOG_FORMAT=text \
       CURATOR_VERIFY_VISIBILITY_S=0
PYTHONPATH=../tools ../.venv/bin/python -c "from parity.fixtures import make_mini_lerobot as m; m('$D/inputs/mini')"
../.venv/bin/python -m daemon --host 127.0.0.1 --port 18080 &
B=localhost:18080/curation/api/v1; c() { curl -s -u demo:demo-pass -H 'Content-Type: application/json' "$@"; }
# 输出密钥：本地替身不用它读写，但任务要引用一个（假的 AK/SK 校验结果是 failed，本地模式不看它）
c -X POST $B/credentials -d '{"name":"out-key","access_key_id":"AK","secret_access_key":"SK","region":"cn-beijing"}'
```

1. **预检**：`c -X POST $B/preflight -d "{\"input\":{\"source\":\"local\",\"uri\":\"$D/inputs/mini\"}}"` —— 给出 `preflight_id`，
   格式 LeRobot v2.1、8 条；两个 VLM 模块 `needs_input`（没选模型服务）。记下 `P=<preflight_id>`。
2. **建任务并开始**：

   ```bash
   c -X POST $B/tasks -d "{\"name\":\"mini\",\"input\":{\"source\":\"local\",\"uri\":\"$D/inputs/mini\"},
     \"output\":{\"uri\":\"tos://deliveries/mini\",\"credential\":\"out-key\"},\"preflight_id\":\"$P\",
     \"episodes\":{\"mode\":\"all\"},\"modules\":[\"timestamp_check\",\"kinematic_limits\",\"motion_quality\",
     \"visual_quality\",\"video_action_sync\",\"dedup\"],\"params\":{\"export\":true}}"
   ```

   201，`state` 是 `queued`。记下 `T=<id>`；`c $B/tasks/$T` 里的 `dataset_id` 说明数据集顺带登记了。
3. **看它跑**：`curl -N -u demo:demo-pass localhost:18080/curation/events/tasks/$T` 能看到 `state`、`progress`（numeric → frame →
   verdict → dedup → final → report → export → verify）、`log`、最后的 `done`；`c $B/tasks/$T/logs?limit=20` 是各档的日志；
   `c $B/tasks/$T/plan` 是执行计划。十几秒后 `c $B/tasks/$T` 是 `succeeded`，`result_rev` 1，`summary.total` 8。
4. **交付**：`ls $D/tos/deliveries/mini/*/` 有 `_COMPLETE`、`revisions/r0001/commit.json`、`export/`；`cat $D/tos/deliveries/mini/latest`
   是这个任务的 `run_id`。
5. **非法迁移**：`c -X POST $B/tasks/$T/actions/pause` 是 409 `task_state_conflict`，`details.state` 是 `succeeded`。
6. **重新导出**：`c -X POST $B/tasks/$T/reexport` 是 202 与一个 `reexport` 子任务；`c $B/tasks/$T/subtasks` 里它很快 `succeeded`。
7. **启动前的指纹核对（D37）**：用同一个 `preflight_id` 再建一个 `params: {"start_now": false}` 的任务（记为 `T2`，`created`），
   然后 `touch $D/inputs/mini/meta/episodes.jsonl`，`c -X POST $B/tasks/$T2/actions/start` 是 409 `source_changed`，
   `details` 里 `meta_changed: true`、`modified: 1`；`c -X POST $B/tasks/$T2/repreflight` 返回 `compatible: true`，任务进入 `queued`。
8. **浏览与 episode 列表**：`c "$B/datasets/browse?source=local&uri=$D/inputs"` 列出 `mini`；
   `c "$B/datasets/episodes?source=local&uri=$D/inputs/mini&limit=3"` 三条，`has_more: true`，带 `next_cursor` 接着翻。
9. **清理与取回**：停掉 Daemon，加上 `CURATOR_WORK_RETENTION_DAYS=0.0001`（约 9 秒）重启，一分钟后 `ls -a $D/data/runs/$T`
   只剩 `.orchestr`，Daemon 日志里有 `janitor: task … cleaned`；`c $B/tasks/$T/report` 照样 200，工作目录从交付目录取回了，
   但大文件不取回：`ls $D/data/runs/$T/export` 只有两份清单，没有 `lerobot_curated/`。
   **注意**：保留期对数据目录里所有已结束的任务都生效（交付目录接得住的就会被清理），别拿存着别的数据的目录做这一步。
10. **停机**：再建一个任务，趁它在跑 `kill -TERM` Daemon：日志里 `shutdown: 1 running job(s) asked to pause`，
    任务停在 `paused`（`pause_reason: system`，原因「Daemon 停机」）；重启 Daemon（去掉上一步的保留期）后启动对账把它放回队列，
    它自己跑完。
11. **mcap 与 Lance（D44）**：把同一份数据做成两种格式，放在本地数据根下：

    ```bash
    PYTHONPATH=../tools ../.venv/bin/python -m parity make-fixture --format mcap --out $D/inputs/mini_mcap
    PYTHONPATH=../tools ../.venv/bin/python -m parity make-fixture --format lance --out $D/inputs/mini_lance
    c "$B/datasets/browse?source=local&uri=$D/inputs"                         # mini_lance: lance 8、mini_mcap: mcap 8
    c "$B/datasets/episodes?source=local&uri=$D/inputs/mini_mcap&limit=3"     # cameras 为空、length_s 约 5 秒、task_unread: true
    ```

    然后按第 1、2 步各建一个任务（`uri` 换成 `$D/inputs/mini_mcap` / `$D/inputs/mini_lance`，交付目录换成
    `tos://deliveries/mini_mcap` / `tos://deliveries/mini_lance`，模块同第 2 步）。预检的 `format` 分别是
    `{"kind": "mcap", "version": null, …}` 与 `{"kind": "lance", "version": "v3", …}`。十几秒后两个任务都是 `succeeded`，
    `summary` 为 `total 8, passed 5, rejected 3, held 0`，和 LeRobot 版本相同；`ls $D/tos/deliveries/mini_mcap/*/export/mcap_curated/`
    是 `episode_0/1/3/4/6.mcap` 与 `index.json`，`ls $D/tos/deliveries/mini_lance/*/export/lance_episodes/` 是 `episodes_parquet`、`index.json`、`videos`；
    Lance 任务的日志（`c "$B/tasks/$T/logs?stage=export"`）里有「lance 原格式交付本版本未做」。`c "$B/datasets?format=mcap"` 只列出 `mini_mcap`。
    跑完之后 `$D/data/source-cache/` 下没有任务目录（本地数据不用拉副本，读取器的临时视频目录随运行删掉；TOS 上的数据拉到这里，同样随运行删掉）。

12. **CPU 名额池与同时运行的任务数（D54）**：停掉 Daemon，写一个早于 D54 的站点配置，按 4 核重启（池里 2 个名额）：

    ```bash
    printf 'concurrency: {cpu: 8, cpuMax: 16}\n' > $D/site.yaml
    CURATOR_CPU_CORES=4 CURATOR_SITE_CONFIG=$D/site.yaml ../.venv/bin/python -m daemon --host 127.0.0.1 --port 18080 &
    ```

    Daemon 照常就绪，日志里有 `site config …: concurrency.cpu, concurrency.cpuMax ignored` 的告警和
    `CPU pool: 2 worker slot(s) shared by up to 3 running task(s)`。按第 1、2 步连着建 4 个任务（同一个 `preflight_id` 即可），
    趁它们在跑 `c "$B/tasks?state=running"` 是 3 个，第 4 个是 `queued`；`c $B/tasks/$T/plan` 的 `limits.cpu_concurrency` 是
    `{"value": 2, "bound_by": "planner"}`，每个任务的日志（`c "$B/tasks/$T/logs?stage=system"`）里有「CPU 档每条占全局 CPU 池的一个名额（共 2 个，
    与同时运行的任务共用）」。几个任务 SSE 里 numeric、frame 两层的 `pipeline.inflight` 加起来任何时候不超过 2；四个最后都 `succeeded`。

## 自动化测试

```bash
../.venv/bin/python -m pytest -q tests/orchestr -m "not slow"    # 约 1.5 分钟：执行器、规则、交付目录、worker 池、接口与校验
../.venv/bin/python -m pytest -q tests/orchestr                  # 全部，含真跑 CLI 的端到端（slow）
```

`test_cpupool.py` 是全局 CPU 名额池本身（计数、公平份额、按块拿、并发下不超额），`test_episode_dispatch.py` 末尾几条是流水线经名额池派发
（别的任务占着的名额不用、崩溃 / 整体失败 / 停止后全部归还），`test_e2e_more.py::test_three_tasks_share_one_cpu_pool` 真跑 4 个任务。

`test_containers.py` 是 mcap / Lance（D44）：登记、核对与重新预检、浏览提示、episode 列表，慢测试里两种格式各真跑一个任务到交付，
另有一个是 D37 在 mcap 文件变了时拦下开始。

端到端测试用对账工具的合成数据集、CLI 测试的假模型服务、本地交付替身；密钥按 W8 的方式真实封存（假 TOS），
启动前的检查、D37、固化都按生产路径走。`faultycli.py` 往 CLI 注入崩溃、出错条目、整体失败；
`test_e2e_recovery.py` 起真的 Daemon 进程再 SIGKILL / SIGTERM 它。
