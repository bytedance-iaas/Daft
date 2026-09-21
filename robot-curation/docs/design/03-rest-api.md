# 03 REST API 契约

## 1. 总则

- 前缀 `{base}/api/v1`，JSON in / JSON out，UTF-8。`{base}` 是部署时的挂载前缀（默认空，现网 `/curation`），
  下文路径都省略它，见 `09-deployment.md` §2.4。
- **粒度粗**：一个端点完成一件用户视角的事，内部可以是多条 CLI 的组合。
  前端不负责编排，不存在「前端连续调三个接口才完成一件事」的设计。
- 写操作全部**异步**：立即返回资源和 `state`，进度靠 SSE 或轮询。
- 写接口只收 `application/json`，不开 CORS —— Basic 鉴权下浏览器会自动带凭证，这两条是防跨站请求的底线。
- 错误体统一：

```jsonc
{"error": {"code": "task_state_conflict", "message": "任务正在运行，请先停止再删除",
           "details": {"state": "running"}}}
```

`code` 是稳定的机器可读串，Agent 认它；`message` 是给人看的中文，UI 直接展示。

- 响应里的 `links` 是给 Agent 的可点链接（D22）：绝对 URL = `publicBaseUrl` + `{base}` + 前端路由；
  没配 `publicBaseUrl` 时给相对路径并带 `"absolute": false`。

## 2. 端点总表

**TOS 访问密钥**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/credentials` | 列访问密钥（永不返回密钥本体） |
| POST | `/api/v1/credentials` | 新建，保存时做一次连通性校验（含写探针） |
| PUT / DELETE | `/api/v1/credentials/{id}` | 更新（密钥字段留空 = 不改）/ 删除（规则见 01 篇 §2.1） |
| POST | `/api/v1/credentials/{id}/verify` | 重新校验 |

**VLM 后端与模型**（API Key 随后端一起提交，服务端代管，不经 `/credentials`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/vlm-backends` | 列后端及其模型 |
| POST | `/api/v1/vlm-backends` | 新建：名称 + 类型 + endpoint + API Key；保存后自动列一次模型 |
| PUT / DELETE | `/api/v1/vlm-backends/{id}` | 更新（API Key 留空 = 不改）/ 删除（被非终态任务引用 → 409） |
| POST | `/api/v1/vlm-backends/{id}/verify` | 探活 |
| POST | `/api/v1/vlm-backends/{id}/refresh-models` | 重新列模型（先 `/models`，不通则内置清单） |
| POST | `/api/v1/vlm-backends/{id}/models` | 手动添加一个模型（Model ID 或推理接入点 ID），保存前用一次最小请求校验 |
| PATCH / DELETE | `/api/v1/vlm-backends/{id}/models/{model_id}` | 配置思考强度、并行度 / 移除 |

**数据集与预检**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/modules` | 模块注册表：id、中文名、所属档、参数 schema。前端的模块清单只从这里来 |
| GET | `/api/v1/datasets` | 列数据集。`source=tos`（需 uri + 访问密钥）或 `source=public`（HuggingFace 缓存桶，匿名） |
| GET | `/api/v1/datasets/episodes` | 分页列 episode，供新建页预览勾选，见 §10 |
| POST | `/api/v1/preflight` | 预检，同步返回，结果带 `preflight_id` |

**任务**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/tasks` | 新建任务（= 需求里的 `run_modules()`） |
| POST | `/api/v1/tasks/batch` | 同一套配置、多个数据集，一次建 N 个任务（深链带多个数据集时用） |
| GET | `/api/v1/tasks` | 任务列表，页码分页 |
| GET | `/api/v1/tasks/{id}` | 任务详情，见 §3.3 |
| PATCH | `/api/v1/tasks/{id}` | `created` 状态可改全部配置；其余状态只能改名和重新绑定访问密钥 |
| DELETE | `/api/v1/tasks/{id}` | 删除（`created` 或终态才允许），见 §8 |
| POST | `/api/v1/tasks/{id}/actions/{action}` | `start` / `pause` / `resume` / `stop` |
| POST | `/api/v1/tasks/{id}/retry` | 重试 → 建子任务，见 §3.2 |
| POST | `/api/v1/tasks/{id}/continue` | stopped / failed 的任务继续运行 → 建子任务 |
| POST | `/api/v1/tasks/{id}/reexport` | 重新导出交付数据集 → 建子任务 |
| GET | `/api/v1/tasks/{id}/subtasks` | 子任务列表 |
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
| POST | `/api/v1/tasks/{id}/adjudication/apply` | 执行裁决 → 建子任务 |

**其它**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/media/sign` | 换一个 TOS 预签名 URL（视频、证据帧） |
| GET | `/events/tasks/{id}` | SSE：进度、日志、状态变更 |
| GET | `/healthz` `/readyz` | 探针，免鉴权，根路径与 `{base}` 下都可达 |

## 3. 新建任务：`POST /api/v1/tasks`

需求里的 `run_modules()` 就是它。**所有提交都走这一个标准接口**，调用方不传并发参数、不传计划；
Daemon 内部必经 planner，把能合并的 VLM 请求合并，再用最合适的并发度去跑（D5）。

```jsonc
// 请求
{
  "name": "droid 前 50 条质检",
  "input":  {"source": "tos", "uri": "tos://bucket/datasets/droid_lerobot",
             "region": "cn-beijing", "credential": "prod-tos"},   // source=public 时不需要 credential
  "output": {"uri": "tos://bucket/deliveries/droid-50", "region": "cn-beijing",
             "credential": "prod-tos"},
  "preflight_id": "pf_01HX...",
  "episodes": {"mode": "head", "n": 50},          // all | head | explicit（"expr": "3,10-12"）
  "modules": ["timestamp_check", "motion_quality",
              {"id": "kinematic_limits"},
              {"id": "video_action_sync", "params": {"sync_plots": "all"}},
              "visual_quality", "task_success", "dedup", "skill_profile"],
  "embodiment_id": "franka",
  "vlm": {"backend": "ark-prod", "model": "doubao-seed-2-0-pro-260215",
          "reasoning_effort": null},              // null = 用模型上的配置；都为空则请求里不带该字段
  "params": {"vlm_retry": 3, "export": true, "start_now": true}
}

// 响应 201
{"id": "task_01HX...", "state": "queued", "created_at": 1758300000000,
 "links": [{"rel": "task", "title": "Open task",
            "url": "https://<host>/curation/tasks/task_01HX..."}]}
```

服务端行为：
1. 用 `preflight_id` 取回预检快照，校验所选模块的可用性与它一致；快照过期（30 分钟）或不存在
   → `preflight_expired`，要求重检。
2. 校验访问密钥可用、交付目录可写；校验交付目录绑定的输入数据集与本次一致（01 篇 §2.7）。
3. `modules` 的每一项可以是 id 字符串，也可以是 `{id, params}`；`params` 按注册表里该模块的参数 schema 校验。
4. 写 DB。`start_now=true` → `queued`；`start_now=false` → `created`（待启动），等 `actions/start`。
5. 启动那一刻固化 `preflight` 快照、生成 `run_id`（时间戳）。

### 3.1 `params` 全表

| 键 | 类型 | 默认 | 含义 | 对应 v1 |
|---|---|---|---|---|
| `start_now` | bool | true | false 则停在「待启动」 | — |
| `export` | bool | true | 是否导出交付数据集；false 时之后可用「导出」补做 | `--report-only` 取反 |
| `vlm_retry` | int 0–5 | 3 | VLM 调用的外层重试次数，间隔 1s / 2s / 4s | 新增 |
| `vlm_hedge` | bool | true | 超时对冲补发 | v1 默认开 |
| `vlm_timeouts_s` | object | probe/endstate/arbitration/caption 60，llm 120 | 各类调用的单次超时 | `vlm.timeouts_s` |
| `clips` | bool | false | 为裁决页预生成视频片段 | v1「一起生成」 |

没有任何并发类的键。模块自己的参数走 `modules[].params`，可用的键由 `GET /modules` 给出
（例：`video_action_sync.sync_plots = flagged | all | none`）。

### 3.2 重试、继续运行、重新导出

```jsonc
// POST /api/v1/tasks/{id}/retry
{"modules": ["task_success"],      // 省略 = 所有出过错的模块
 "episodes": "errors"}             // errors（默认）| all
```

- `errors`：只重跑这些模块里 `verdict=error`、或因调用失败而弃权的 episode。
  640 条里 5 条超时，就只跑那 5 条。模块整体失败（从未产出结果）时自动按 `all` 处理。
- 重试完成后，下游的去重和技能画像如果输入变了，会在同一个子任务里增量同步，不用再点一次。
- `continue` 没有请求体：从断点接着跑主流程里没完成的部分。
- `reexport` 没有请求体：走增量导出；创建时 `export=false` 的任务也用它补做首次导出。
- 三者都建子任务。同一任务已有未结束的子任务时返回 409。

### 3.3 任务详情的响应

```jsonc
{
  "id": "task_01HX...", "name": "droid 前 50 条质检",
  "state": "completed_with_errors", "state_reason": null, "pause_reason": null,
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
  "summary": {"total": 50, "passed": 43, "rejected": 7, "review": 10, "pass_rate": 0.86},
  "usage": {"prompt_tokens": 1820000, "completion_tokens": 64000,
            "reasoning_tokens": 41000, "cached_tokens": 903000, "requests": 742},
  "pending_adjudication": 10, "delivery_stale": false,
  "active_subtask": null,
  "links": [
    {"rel": "report", "title": "Open QA report", "url": "https://<host>/curation/tasks/task_01HX.../report"},
    {"rel": "adjudication", "title": "10 episodes need human judgement",
     "url": "https://<host>/curation/tasks/task_01HX.../adjudication?status=pending"}
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
- **断线重连**：客户端带 `Last-Event-ID`，Daemon 重放最近 200 条。再早的从
  `GET /api/v1/tasks/{id}` 的快照恢复 —— SSE 是加速通道，不是唯一真相源。
- 被限流丢掉的日志不会真的丢：完整日志在 `/logs`（§11）。
- 前端必须能在 SSE 完全不可用时靠轮询工作（每 5s 拉一次详情），这是容错底线。

## 6. 报告与大表分页

报告概览一次返回（KB 级）。明细表可能是几十万行的 CSV，走切片接口：

```
GET /api/v1/tasks/{id}/report/tables/visual_quality?cursor=...&limit=100&sort=score&order=asc
```

Daemon 不把 CSV 读进内存，转调 `curation table slice`，由 CLI 用 pyarrow 做投影+切片。
排序字段限白名单，避免对大文件做任意排序。

报告按模块组织，但看一条具体的 episode 时需要横着看所有模块：
`GET /tasks/{id}/episodes/{index}` 返回这一条的判决卡、各模块读数、证据帧和各机位视频位置，
供报告页的逐条下钻抽屉使用（v1「轨迹」页的对应物）。

## 7. 媒体访问：预签名 URL

```
GET /api/v1/media/sign?task=task_01HX...&scope=delivery&path=details/clips/ep000034.mp4&ttl=1800
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

- `POST /tasks`、`/tasks/batch`、所有 `actions/*` 与建子任务的接口都支持 `Idempotency-Key` 头；
  相同 key 24 小时内返回首次结果。Agent 超时重试时最容易重复建任务，所以创建接口必须支持。
- 状态相关的写操作走 Repository 的 CAS，冲突返回 409 + 当前状态，前端刷新即可。
- 删除任务只允许 `created` 或终态；运行中删除返回 409 并提示先停止。
- **删除只删平台里的记录，TOS 上的产物保留**。要连产物一起清，显式带 `?purge_artifacts=true`，
  界面上二次确认；即便如此也不动交付目录根上的 `human-decisions/`（它属于交付目录，不属于某个任务）。

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
