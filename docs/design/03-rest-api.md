# 03 REST API 契约

## 1. 总则

- 前缀 `{base}/api/v1`，JSON in / JSON out，UTF-8。`{base}` 是部署时的挂载前缀（默认空，现网 `/curation`），
  下文路径都省略它，见 `09-deployment.md` §2.4。
- **粒度粗**：一个端点完成一件用户视角的事，内部可以是多条 CLI 的组合。
  前端不负责编排，不存在「前端连续调三个接口才完成一件事」的设计。
- 写操作全部**异步**：立即返回资源和 `state`，进度靠 SSE 或轮询。
- 写接口只收 `application/json`，不开 CORS —— Basic 鉴权下浏览器会自动带凭证，这两条是防跨站请求的底线。
  没有请求体的写请求（`actions/*`、`continue` 等）也要带 `Content-Type: application/json`；跨站的写请求一律拒绝（C4 1.2）。
- 错误体统一：

```jsonc
{"error": {"code": "task_state_conflict", "message": "任务正在运行，请先停止再删除",
           "details": {"state": "running"}}}
```

`code` 是稳定的机器可读串，Agent 认它；`message` 是给人看的中文，UI 直接展示。

- 响应里的 `links` 是给 Agent 的可点链接（D22）：绝对 URL = `publicBaseUrl` + `{base}` + 前端路由；
  没配 `publicBaseUrl` 时给相对路径并带 `"absolute": false`。
- **ID 是不透明的字符串**（D45，C4 1.10.0）：新记录是「前缀-9 位小写字母」，前缀 task、sub、ds、pf、cred、vb、vm、upl
  （如 `task-kqzmrtbwe`、上传句柄 `upload:upl-kqzmrtbwe`）；之前建的记录保留原来的 `前缀_…`，两种都照常可用，
  调用方不要解析 ID。ID 不带时间，列表按创建时间排。

## 2. 端点总表

**TOS 访问密钥**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/credentials` | 列访问密钥（永不返回密钥本体） |
| POST | `/api/v1/credentials` | 新建，保存时只验身份（一次签名请求）；对具体存储桶的读写权限在任务开始前验（08 篇 §4） |
| PUT / DELETE | `/api/v1/credentials/{id}` | 更新（密钥字段留空 = 不改）/ 删除（规则见 01 篇 §2.1） |
| POST | `/api/v1/credentials/{id}/verify` | 重新校验 |

**VLM 后端与模型**（API Key 随后端一起提交，服务端代管，不经 `/credentials`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/vlm-backends` | 列后端及其模型 |
| POST | `/api/v1/vlm-backends` | 新建：名称 + 类型 + endpoint + API Key；保存后试着列一次模型，列不出来不算失败 |
| PUT / DELETE | `/api/v1/vlm-backends/{id}` | 更新（API Key 留空 = 不改）/ 删除（被非终态任务引用 → 409） |
| POST | `/api/v1/vlm-backends/{id}/verify` | 探活 |
| POST | `/api/v1/vlm-backends/{id}/refresh-models` | 重新列模型（`GET {endpoint}/models`；拉不出来就返回空，由用户手填） |
| POST | `/api/v1/vlm-backends/{id}/models` | 手动添加一个模型（Model ID 或推理接入点 ID），保存前用一次最小请求校验 |
| PATCH / DELETE | `/api/v1/vlm-backends/{id}/models/{model_id}` | 配置思考强度、并行度 / 移除 |

**数据集与预检**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/modules` | 模块注册表：id、中文名、所属档、参数 schema。前端的模块清单只从这里来 |
| GET | `/api/v1/overview` | 概览页一次取回：待处理事项、运行情况、近 7 天统计（D36，§12） |
| GET / POST | `/api/v1/datasets` | 已登记的数据集（页码分页，按名称搜索，按格式、指纹状态筛选）/ 登记：预检 + 取文件清单，记下两个指纹（D36，§12） |
| GET / PATCH / DELETE | `/api/v1/datasets/{id}` | 登记详情 / 改名称和备注 / 删除登记（不动 TOS；有非终态任务在用 → 409 `dataset_in_use`） |
| POST | `/api/v1/datasets/{id}/recheck` | 重新核对指纹，只比较、不改任何任务 |
| POST | `/api/v1/datasets/{id}/repreflight` | 重新预检，刷新预检结果和两个指纹 |
| GET | `/api/v1/datasets/browse` | 列私有 TOS 前缀下或 HuggingFace 缓存桶里的数据集，供登记时挑选。`source=tos`（需 uri + 访问密钥）或 `source=public`（匿名） |
| GET | `/api/v1/datasets/episodes` | 分页列 episode，供新建页预览勾选；给 `dataset_id`，或来源 + 地址，见 §10 |
| POST | `/api/v1/preflight` | 预检，同步返回，结果带 `preflight_id` |
| POST | `/api/v1/uploads` | 上传模块参数里的输入文件（注册表 1.5 的 `format: upload`，现为 EEF 的 trajectory.json 与观测种子），上传即校验、错误定位到样本 / 帧 / 字段，返回句柄 `upload:<id>` 与 sha256（12 篇 §11.1，F5.5） |
| GET | `/api/v1/uploads/{id}` | 读回上传件的元数据与校验摘要 |
| POST | `/api/v1/deliveries/probe` | 交付目录写探针：用指定的访问密钥真实写一个对象再删掉。新建页交付目录失焦时调 |

**任务**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/tasks` | 新建任务（= 需求里的 `run_modules()`） |
| POST | `/api/v1/tasks/batch` | 同一套配置、多个数据集，一次建 N 个任务（深链带多个数据集时用） |
| GET | `/api/v1/tasks` | 任务列表，页码分页；可按状态、名称、交付目录、数据集、所含模块筛选 |
| GET | `/api/v1/tasks/{id}` | 任务详情，见 §3.3 |
| PATCH | `/api/v1/tasks/{id}` | `created`（待启动）可改全部配置；启动之后只能改 `name` 和 `note`（D20）。带 `If-Match: <updated_at>`，两个窗口同时改时后到的返回 412 |
| DELETE | `/api/v1/tasks/{id}` | 删除平台里的任务记录，不动 TOS（`created` 或终态才允许），见 §8 |
| POST | `/api/v1/tasks/{id}/restore` | 恢复 30 天内删除的任务记录 |
| POST | `/api/v1/tasks/{id}/purge-artifacts` | 清理该任务在 TOS 上的交付产物，见 §8 |
| POST | `/api/v1/tasks/{id}/rebind-credentials` | 原访问密钥被删后，给历史任务重新指定一个，只影响报告读取与媒体签名 |
| POST | `/api/v1/tasks/{id}/actions/{action}` | `start` / `pause` / `resume` / `stop` |
| POST | `/api/v1/tasks/{id}/repreflight` | 开始时指纹对不上、用户确认之后调：重新预检，相容就直接开始（D37，§12） |
| POST | `/api/v1/tasks/{id}/retry` | 重试 → 建子任务，见 §3.2 |
| POST | `/api/v1/tasks/{id}/continue` | stopped / failed 的任务继续运行 → 建子任务 |
| POST | `/api/v1/tasks/{id}/reexport` | 重新导出交付数据集 → 建子任务 |
| GET | `/api/v1/tasks/{id}/subtasks` | 子任务列表 |
| GET | `/api/v1/tasks/{id}/timeline` | 执行时间线：主流程、子任务、暂停与恢复、结果版本（任务详情页用，W2 补） |
| GET | `/api/v1/tasks/{id}/plan` | planner 生成的执行计划，**只读** |
| GET | `/api/v1/tasks/{id}/logs` | 完整日志，按 stage 过滤，游标分页，见 §11 |
| GET | `/api/v1/tasks/{id}/usage` | Token 消耗（按模块 / 调用种类 / 模型 / 子任务聚合） |

**报告与裁决**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/tasks/{id}/report` | 报告（概览 + 各模块小节） |
| GET | `/api/v1/tasks/{id}/report/tables/{table}` | 明细表分页切片 |
| GET | `/api/v1/tasks/{id}/episodes/{index}` | 单条 episode 的全模块视图：判决卡、各模块读数、证据、视频位置 |
| GET | `/api/v1/tasks/{id}/perf` | 性能剖析 |
| GET | `/api/v1/tasks/{id}/adjudication` | 裁决队列，游标分页，`source` / `status` 过滤 |
| POST | `/api/v1/tasks/{id}/adjudication` | 提交裁决（只记录，不执行，可反复改） |
| POST | `/api/v1/tasks/{id}/adjudication/apply` | 执行裁决 → 建子任务；可选请求体 `{"relabel_rerun": "v1" \| "full"}`，缺省 `v1`（改标重判的口径，D39），记在子任务的 `scope.relabel_rerun` |

**其它**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/media/sign` | 换一个 TOS 预签名 URL（视频、证据帧） |
| GET | `/events/tasks/{id}` | SSE：进度、日志、状态变更 |
| GET | `/healthz`、`/readyz` | 探针，免鉴权，根路径与 `{base}` 下都可达 |

报告、明细表、单条 episode、性能剖析四个接口都接受 `?rev=N` 查看历史结果版本，缺省是当前版本。
全部接口的请求与响应结构以 `docs/contracts/openapi.yaml`（C4）为准，本篇是它的说明。

## 3. 新建任务：`POST /api/v1/tasks`

需求里的 `run_modules()` 就是它。**所有提交都走这一个标准接口**，调用方不传并发参数、不传计划；
Daemon 内部必经 planner，把能合并的 VLM 请求合并，再用最合适的并发度去跑（D5）。

```jsonc
// 请求
{
  "name": "droid 前 50 条质检",
  "note": "第一轮抽检",
  "input":  {"source": "tos", "uri": "tos://bucket/datasets/droid_lerobot",
             "region": "cn-beijing", "credential": "prod-tos"},   // source=public 时不需要 credential
  "output": {"uri": "tos://bucket/deliveries/droid-50", "region": "cn-beijing",
             "credential": "prod-tos"},
  "preflight_id": "pf-kqzmrtbwe",
  "episodes": {"mode": "head", "n": 50},          // all | head | explicit（"expr": "3,10-12"）
  "modules": ["timestamp_check", "motion_quality",
              {"id": "kinematic_limits"},
              {"id": "video_action_sync", "params": {"sync_plots": "all"}},
              "visual_quality", "task_success", "dedup", "skill_profile"],
  "embodiment_id": "franka",
  "vlm": {"backend": "ark-prod", "model": "doubao-seed-2-0-pro-260215",
          "reasoning_effort": null},              // null = 用模型上的配置；都为空则请求里不带该字段
  "params": {"vlm_retry": 3, "export": true, "start_now": true,
             "limits": {"cpu_concurrency": 4, "vlm_parallelism": 32}}   // 上限，可省略
}

// 响应 201
{"id": "task-kqzmrtbwe", "state": "queued", "created_at": 1758300000000,
 "warnings": [],
 "links": [{"rel": "task", "title": "Open task",
            "url": "https://<host>/curation/tasks/task-kqzmrtbwe"}]}
```

服务端行为：
1. 用 `preflight_id` 取回预检快照，校验所选模块的可用性与它一致；快照过期（30 分钟）或不存在
   → `preflight_expired`，要求重检。
2. `modules` 的每一项可以是 id 字符串，也可以是 `{id, params}`；`params` 按注册表里该模块的参数 schema 校验。
3. 写 DB。`start_now=true` → 走第 4 步；`start_now=false` → `created`（待启动），等 `actions/start` 时再走第 4 步。
4. **开始前的三项硬检查**（D30），任何一项不过都不允许开始，返回 `precheck_failed` 并逐项说明：
   - 输入：用输入密钥真实读一次（`meta/info.json`）；HuggingFace 缓存桶匿名读；
   - 输出：用输出密钥在交付目录下真实写一个探针对象，写完即删；
   - VLM（勾了 VLM 模块才查）：用所选后端和模型发一次最小请求。
   密钥在管理页上标着「未验证」或「验证失败」不妨碍保存和选用，但过不了这一步就跑不起来。
5. **核对数据集指纹**（D37）：重新取 meta 指纹，并用 `curation snapshot` 取全量文件清单，与数据集登记上记下的比
   （01 篇 §2.8）。不一致就不开始，返回 409 `source_changed`，`details` 写明变了什么：meta 有没有变、
   新增 / 删除 / 改动的文件数和前若干个键、上次预检的时间。前端弹框请用户确认，确认后调
   `POST /tasks/{id}/repreflight`（§12）。一致就沿用已有的预检结果，不受 30 分钟缓存期限的限制 ——
   数据没变，预检结论就还成立。
6. 固化：`preflight` 快照、`run_id`（时间戳，对应的目录已存在则换一个，绝不覆盖别人的批次）、
   第 5 步取到的源文件清单（`source_manifest.json`，D27）。
7. 入队。交付目录下已经有别的输入数据集跑出来的批次时，不拦，只在 `warnings` 里提醒一句。

`retry` / `continue` / `reexport` / 执行裁决 在建子任务之前同样走第 4 步里与它相关的检查。

### 3.1 `params` 全表

| 键 | 类型 | 默认 | 含义 | 对应 v1 |
|---|---|---|---|---|
| `start_now` | bool | true | false 则停在「待启动」 | — |
| `export` | bool | true | 是否导出交付数据集；false 时之后可用「导出」补做 | `--report-only` 取反 |
| `vlm_retry` | int 0–5 | 3 | VLM 调用的外层重试次数上限，间隔 1s / 2s / 4s | 新增 |
| `limits.cpu_concurrency` | int ≥1 | 不设 | CPU 档并发的上限 | 新增 |
| `limits.vlm_parallelism` | int ≥1 | 不设 | VLM 并行度 N 的上限 | v1 界面上的三个并发输入框 |
| `vlm_hedge` | bool | true | 超时对冲补发 | v1 默认开 |
| `vlm_timeouts_s` | object | probe/endstate/arbitration/caption 60，llm 120 | 各类调用的单次超时 | `vlm.timeouts_s` |
| `clips` | bool | false | 为裁决页预生成视频片段 | v1「一起生成」 |

**并发只能给上限，不能给计划**（D31）。实际取值 = min(用户给的上限, 模型 / 后端上配的并行度, 站点上限, planner 算出来的值)；
八把闸门怎么从 N 推出来、各档怎么排，仍然是 planner 的事（04 篇 §2）。超时和重试次数同理，是「最多这么多」。
CLI 的客户端命令（`curation task create`）和 UI 的「高级设置」提交的是同一组键。
模块自己的参数走 `modules[].params`，可用的键由 `GET /modules` 给出
（例：`video_action_sync.sync_plots = flagged | all | off`，取值沿用 v1 的 `pipeline.sync_plots`）。

### 3.2 重试、继续运行、重新导出

```jsonc
// POST /api/v1/tasks/{id}/retry
{"modules": ["task_success"]}      // 省略 = 所有有出错条目或整体失败的模块
```

- **只补跑出错的 episode，成功的结果一律保留**（D25）。640 条里 5 条超时，就只跑那 5 条。
  出错但已被别的模块确定拒绝的条目不在其中，它们聚合时就直接拒绝了（D35）。
  出错的条目当初没有进后面的档，所以补跑是从它出错的那一档接着往后跑，把该跑的都跑完。
  模块整体失败（从未产出结果）时，该模块全量跑。
- 补跑完成后，下游的去重和技能画像如果输入变了，会在同一个子任务里增量同步，不用再点一次。
- **新结果完整生成、上传并核验之后才替换旧的**：判决清单和报告作为一个结果版本整体存放，
  切换的只是库里的 `result_rev`（CAS）。补跑失败或被停止，用户看到的仍是上一版，不会出现清单是新的、报告是旧的。
- **补跑后任务的终态按当前结果重算**：出错的条目都救回来了，列表和报告上就从「错误」变为「已完成」；
  原先的失败和这次补跑留在任务详情的执行时间线里。
- `continue` 没有请求体：从断点接着跑主流程里没完成的部分。因 `source_changed` 失败的任务不能继续，只能复制为新任务。
- `reexport` 没有请求体：走增量导出；创建时 `export=false` 的任务也用它补做首次导出。
- 三者都建子任务。同一任务已有未结束的子任务时返回 409。

### 3.3 任务详情的响应

```jsonc
{
  "id": "task-kqzmrtbwe", "name": "droid 前 50 条质检", "note": "第一轮抽检",
  "state": "completed_with_errors", "state_reason": null, "pause_reason": null,
  "result_rev": 1, "source": {"objects": 204, "bytes": 1520331122, "digest": "sha256:…"},
  "input": {...}, "output": {...}, "episodes": {...}, "vlm": {...}, "params": {...},
  "progress": {"stages": [
    {"id": "autolabel", "state": "succeeded", "done": 22, "total": 22, "elapsed_s": 46},
    {"id": "numeric",   "state": "succeeded", "done": 50, "total": 50, "elapsed_s": 1},
    {"id": "frame",     "state": "succeeded", "done": 49, "total": 49, "elapsed_s": 144},
    {"id": "vlm",       "state": "succeeded", "done": 49, "total": 49, "elapsed_s": 408}
  ]},
  "modules": [
    {"id": "task_success", "name": "任务成败判定", "state": "succeeded",
     "episodes_total": 49, "episodes_error": 2, "elapsed_s": 408}
  ],
  "summary": {"total": 50, "passed": 41, "rejected": 7, "held": 2, "review": 10, "pass_rate": 0.82},
                                    // 有缺源文件被剔除的条目时另带 "skipped": N（D40），不计入 total
  "usage": {"prompt_tokens": 1820000, "completion_tokens": 64000,
            "reasoning_tokens": 41000, "cached_tokens": 903000, "requests": 742},
  "pending_adjudication": 10, "delivery_stale": false,
  "active_subtask": null,
  "links": [
    {"rel": "report", "title": "Open QA report", "url": "https://<host>/curation/tasks/task-kqzmrtbwe/report"},
    {"rel": "adjudication", "title": "10 episodes need human judgement",
     "url": "https://<host>/curation/tasks/task-kqzmrtbwe/adjudication?status=pending"}
  ]
}
```

## 4. 任务列表：页码分页

```
GET /api/v1/tasks?page=1&page_size=20&state=running&q=droid
```

```jsonc
{"items": [...], "page": 1, "page_size": 20, "total": 137}
```

- 页码 + 每页条数 + 总数，与火山控制台的表格一致（D21）。`page_size` 取 10 / 20 / 50 / 100。
- 按创建时间倒序。列表行里带 `progress`、`summary`、`pending_adjudication` 和各状态的模块数，
  够渲染列表页和「已完成任务的报告概览」，不用再逐个查详情。
- 往下翻的内容（裁决队列、日志、episode 列表、报告明细表）用游标：`?cursor=<opaque>&limit=50`
  → `{"items": [...], "next_cursor": "...", "has_more": true}`。

## 5. SSE：`GET /events/tasks/{id}`

```
event: state
data: {"state":"running","at":1758300001000}

event: progress
data: {"stage":"vlm","done":28,"total":49,"elapsed_s":230,"eta_s":170}

event: log
data: {"stage":"vlm","level":"warn","msg":"episode 18 too short (0.5s)"}

event: usage
data: {"prompt_tokens":182000,"completion_tokens":6400,"reasoning_tokens":4100,"cached_tokens":90000,"requests":124}

event: done
data: {"state":"completed_with_errors","failed_modules":["task_success"]}
```

- 事件源是 CLI 子进程 stderr 的 JSON Lines（见 `02-cli-contract.md` §5），
  Daemon 做聚合与限流（progress 最多 2 条/秒，log 最多 20 条/秒，超出丢弃中间态）。
- **每条事件都带 `id: <epoch>-<seq>`**（上面的示例省略了）。`epoch` 是 Daemon 的启动序号，`seq` 在一次启动内单调递增。
- **断线重连**：客户端带 `Last-Event-ID`。`epoch` 相同且 `seq` 还在缓冲区（最近 200 条）里 → 从下一条接着发；
  `epoch` 变了（Daemon 重启过）或 `seq` 已被挤出缓冲区 → 先发一条 `event: reset`，
  客户端丢掉本地的增量状态，从 `GET /api/v1/tasks/{id}` 拉一次快照再继续听。
- **事件不是账本**。`usage` 和 `progress` 发的都是**累计值**，不是增量，重放或重复收到都不会多记；
  真正的数在库里，SSE 只是让界面动得快一点。
- 被限流丢掉的日志不会真的丢：完整日志在 `/logs`（§11）。
- 前端必须能在 SSE 完全不可用时靠轮询工作（每 5s 拉一次详情），这是容错底线。

## 6. 报告与大表分页

报告概览一次返回（KB 级）。明细表可能有几十万行，走切片接口：

```
GET /api/v1/tasks/{id}/report/tables/visual_quality?cursor=...&limit=100&sort=score&order=asc
```

- 明细表在生成报告时就写成 **Parquet**（按 episode 下标排序，带行组统计），放在结果版本目录里。
  Daemon 直接用 pyarrow 读需要的行组和列，不为翻一页去拉起一个 CLI 进程，也不把整张表读进内存。
- 排序字段限白名单（注册表里逐表声明）；非默认排序在 Daemon 内对单列做一次排序并缓存该版本的行序，
  几十万行是亚秒级。
- 游标里带着 `result_rev`。翻页翻到一半结果版本换了（补跑、裁决之后），返回 `result_changed`，
  前端回到第一页，不会把两个版本的行拼在一起。

报告按模块组织，但看一条具体的 episode 时需要横着看所有模块：
`GET /tasks/{id}/episodes/{index}` 返回这一条的判决卡、各模块读数、证据帧和各机位视频位置，
供报告页的逐条下钻抽屉使用（v1「轨迹」页的对应物）。

## 7. 媒体访问：预签名 URL

```
GET /api/v1/media/sign?task=task-kqzmrtbwe&scope=delivery&path=details/clips/ep000034.mp4&ttl=1800
→ {"url": "https://bucket.tos-cn-beijing.volces.com/...&X-Tos-Signature=...", "expires_at": ...}
```

- 浏览器直连 TOS 取视频和证据帧，Daemon 不做流量中转（D16）。
- `scope` 两种，各用各的访问密钥，各有各的前缀校验：
  - `delivery`：任务的交付目录（输出密钥）—— 证据帧、同步曲线、裁决片段、交付数据集里的视频；
  - `input`：任务的输入数据集（输入密钥；HuggingFace 缓存桶不签名，直接给匿名地址）——
    被拒的条目只在源数据集里有画面，不放开这一路，它们的视频就没法看。
- 视频来源的先后顺序沿用 v1：预生成的片段 → 交付数据集 → 源数据集。
  v3 源是多条拼接的 mp4，返回里带 `from_ts` / `to_ts`，前端用 `#t=from,to` 片段播放。
- 必须用**公网端点**签名。Pod 里用的内网端点对浏览器是死链（v1 注释里的实锤）。
- TTL 默认 30 分钟。前端在 403/过期时自动重签一次再重试，不弹错。

## 8. 幂等、并发与删除

- 所有写接口都支持 `Idempotency-Key` 头（C4 1.2 起，改名、删除、恢复、重新绑定密钥也在内）；
  相同 key 24 小时内返回首次结果。Agent 超时重试时最容易重复建任务，所以创建接口必须支持。
- 状态相关的写操作走 Repository 的 CAS，冲突返回 409 + 当前状态，前端刷新即可。
- **删除和清理是两个动作**（D28）：
  - `DELETE /tasks/{id}` 只处理平台里的记录，**不碰 TOS**。只允许 `created` 或终态；运行中返回 409 并提示先停止。
    实现为软删除：从列表里消失，30 天内可 `restore`，之后连同库里的裁决、Token 记录一起清除。
    留这个窗口是因为产物虽然还在 TOS，但平台没有「把已有目录导入为任务」的入口，记录一删，报告就再也打不开了。
  - `POST /tasks/{id}/purge-artifacts` 清理该任务的批次目录 `<交付目录>/<run_id>/`。请求体必须回传完整路径
    （`{"confirm_path": "tos://…/<run_id>/"}`），与服务端算出来的一致才执行；界面上先展示路径和体积，再二次确认。
    只清这一个批次目录：同一交付目录下别的任务的批次不动；`latest` 若正指向它则一并移除。

## 9. 鉴权

本期单实例单租户，沿用 HTTP Basic / htpasswd（搬 `ui/auth.py` 的中间件，含
探针豁免与常数时间比较两条纪律）。所有 API 走同一个 `AuthProvider` 接口：

```python
class AuthProvider(Protocol):
    def authenticate(self, scope) -> Principal | None: ...   # 返回 owner_id
```

火山 IAM 将来只是这个接口的另一个实现。详见 `08-secrets-and-auth.md`。

## 10. episode 列表与预览

需求要求新建任务时能「在预览图里勾选」episode。v1 没有这个能力（只有文本框），这是新增的。

```
GET /api/v1/datasets/episodes?source=tos&uri=tos://…&region=…&credential=prod-tos&cursor=…&limit=48
```

```jsonc
{"items": [
   {"index": 34, "length_s": 21.4, "task": "put the cup in the sink", "task_source": "原始标注",
    "cameras": [{"name": "exterior_1", "url": "https://…签名…", "from_ts": 0.0, "to_ts": 21.4}]}
 ],
 "next_cursor": "...", "has_more": true}
```

**服务端不解码、不生成缩略图。** 前端用 `<video preload="metadata" src="…#t=0.1">` 让浏览器自己取首帧，
配合视口懒加载，一屏只加载看得见的那几十个。v2 源每条一个 mp4；v3 源用 `from_ts` 定位。
列表只读 metadata（`episodes.jsonl` / 任务表），秒级返回。

## 11. 任务日志

```
GET /api/v1/tasks/{id}/logs?stage=vlm&subtask=&level=warn&cursor=…&limit=200
```

每个 stage 的 CLI 进程把完整的 stderr 事件流落到工作目录 `logs/<stage>.jsonl`，stage 结束后上传到交付目录。
这个接口先读本地，本地已清理就读 TOS。SSE 负责「正在发生什么」，这里负责「当时发生了什么」。

## 12. 数据集登记、概览与开始前的指纹核对（D36、D37）

2026-09-21 静态稿评审新增，已写进 C4 1.1.0（端点见 §2 总表，结构以 `openapi.yaml` 为准）。

- **登记**（`POST /datasets`）：Daemon 调 `curation preflight` 和 `curation snapshot`，存下预检结果、meta 指纹、
  全量文件清单及其指纹（01 篇 §2.8）。同一个来源 + 地址 + 地域再登记一次，返回已有的那条（200），不重复建。
  新建任务时直接填地址的，预检通过后同样走这一步，任务上记 `dataset_id`。
- **重新核对**（`/recheck`）只比较、不改任何任务：有变化时 `check_state` 置为 `changed`，
  返回 `SourceChange`（meta 有没有变、新增 / 删除 / 改动的文件数、前若干个键）。**重新预检**（`/repreflight`）
  刷新预检结果和两个指纹，`check_state` 回到 `ok`。
- **开始任务**（`actions/start`，或 `POST /tasks` 带 `start_now`）先核对，见 §3 第 5 步。对不上返回 409
  `source_changed`，`error.details` 是一个 `SourceChange`；前端弹框请用户确认后调 `POST /tasks/{id}/repreflight`：
  重新预检并判断与任务配置是否相容（所选模块仍可用、自选的 episode 仍在范围内、需要补充的输入都有）。
  相容就直接开始，不用再点一次；不相容则任务留在待启动，`incompatibilities` 逐项说明，前端带用户回编辑页。
- **概览**（`GET /overview`）一次返回：待处理事项（错误的任务、待裁决、交付待导出、有变化的数据集、
  验证失败的密钥与后端）、运行情况、近 7 天统计（Token 只算实际调用账）。
- 任务列表的 `module` 参数是逗号分隔的模块 id，只看**勾选了其中每一个**的任务；列表条目带所选模块的 id
  （质检模块列显示预设名要用，07 篇 §4.1）。请求里的 `input` 可以只给 `dataset_id`，与给全来源和地址等价。
