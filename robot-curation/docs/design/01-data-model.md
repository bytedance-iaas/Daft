# 01 数据模型、状态机与持久化

## 1. 实体全景

```
Owner(预留) ──┬── Credential (TOS 访问密钥；VLM 后端的 API Key 也存这里，但由后端接口代管)
              ├── VlmBackend(方舟/自定义) ── VlmModel
              │
              ├── Task ──┬── TaskModule (任务 × 模块，本期 8 行/任务)
              │          ├── Subtask (重试 / 继续运行 / 执行裁决 / 重新导出)
              │          └── TokenUsage (按子任务/模块/调用种类聚合)
              │
              ├── Delivery (交付目录 ↔ 输入数据集 的绑定)
              ├── Adjudication (人工裁决记录，按交付目录跨任务累积)
              └── Event / PreflightCache / IdempotencyKey (审计、预检快照、幂等键)
```

一个 **Task = 一次质检 = 一份报告**。子任务不产生新报告，只覆盖父任务里某些模块的结果。

## 2. 表结构（SQLite，WAL 模式）

所有表带 `owner_id`（本期恒为 `default`，为 IAM 预留）、`created_at`、`updated_at`。
时间统一存 epoch 毫秒整数，避免时区问题。

### 2.1 credential — 密钥

```sql
CREATE TABLE credential (
  id           TEXT PRIMARY KEY,          -- cred_<uuid7>
  owner_id     TEXT NOT NULL DEFAULT 'default',
  name         TEXT NOT NULL,             -- 用户起的名字，任务里按名字引用
  kind         TEXT NOT NULL,             -- 'tos' | 'ark' | 'custom_vlm'
  payload_enc  BLOB NOT NULL,             -- AES-GCM 密文，明文是 JSON
  payload_meta TEXT NOT NULL,             -- 非敏感部分的明文 JSON，可直接展示
  last_verified_at INTEGER,               -- 最近一次连通性校验
  last_verify_error TEXT,
  UNIQUE(owner_id, name)
);
```

- `payload_enc` 里放 AK/SK、API Key。主密钥从 K8s Secret 经环境变量 `CURATOR_MASTER_KEY` 注入，
  不落盘。主密钥缺失时 Daemon **拒绝启动**（fail-closed，半配的加密比不加密更危险）。
- `payload_meta` 放能给人看的部分：TOS 的 region/endpoint、方舟的 base_url、自定义端点 URL。
  列表页只读这一列，永远不解密。
- `kind='ark' | 'custom_vlm'` 的行不经 `/credentials` 接口增删，由 `POST /vlm-backends` 连同后端一起创建、
  一起删除 —— 用户在界面上是「加一个方舟 API Key」一步，前端不该为此编排两次调用。
- **删除规则**：被运行中（非终态）任务引用 → 409；只被历史任务引用 → 二次确认后允许，
  任务上的外键置空（`ON DELETE SET NULL`），报告页提示「访问密钥已删除」，并允许给任务重新绑定一个。
- 详见 `08-secrets-and-auth.md`。

### 2.2 vlm_backend / vlm_model — VLM 后端与模型

```sql
CREATE TABLE vlm_backend (
  id            TEXT PRIMARY KEY,         -- vb_<uuid7>
  owner_id      TEXT NOT NULL DEFAULT 'default',
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL,            -- 'ark' | 'custom'
  credential_id TEXT REFERENCES credential(id),
  endpoint      TEXT NOT NULL,
  max_concurrency INTEGER NOT NULL DEFAULT 64,   -- 该后端的并行度 N，推导各闸门见 04 篇 §2.2
  UNIQUE(owner_id, name)
);

CREATE TABLE vlm_model (
  id              TEXT PRIMARY KEY,       -- vm_<uuid7>
  backend_id      TEXT NOT NULL REFERENCES vlm_backend(id) ON DELETE CASCADE,
  model_name      TEXT NOT NULL,          -- Model ID 或推理接入点 ID（ep-…）
  reasoning_effort TEXT,                  -- 方舟原生取值 none|minimal|low|medium|high|xhigh|max
                                          -- NULL = 请求里不带该字段，走模型默认
  max_concurrency INTEGER,                -- 覆盖后端级
  capabilities    TEXT,                   -- JSON：是否视觉模型、是否支持思考强度、有效档位
  source          TEXT NOT NULL DEFAULT 'manual',  -- 'listed' 接口列出 | 'builtin' 内置清单 | 'manual' 手填
  UNIQUE(backend_id, model_name)
);
```

- 思考强度存的是 **API 原生取值**，不另造枚举。方舟的 7 档会按模型映射到各自的有效档位
  （豆包系列有效档为 minimal / low / medium / high），UI 只展示有效档，映射表见 `08-secrets-and-auth.md` §4.1。
- **NULL 有明确语义：不传该字段**。v1 的请求体里从来没有思考参数，黄金对账必须用 NULL 跑，否则模型输入就变了。
- 生效顺序：任务级覆盖（`task.vlm_reasoning_effort`）> 模型配置 > NULL。
- 模型从哪来：先试 `GET {endpoint}/models`，不通则用内置清单，再不行手填，见 08 篇 §4。

### 2.3 task — 任务

```sql
CREATE TABLE task (
  id             TEXT PRIMARY KEY,        -- task_<uuid7>
  owner_id       TEXT NOT NULL DEFAULT 'default',
  name           TEXT NOT NULL,
  state          TEXT NOT NULL,           -- 见 §3
  state_reason   TEXT,                    -- 失败/停止原因，给人看的一句话

  pause_reason   TEXT,                    -- 'user' | 'system'，只在 pausing/paused 时有值

  input_source   TEXT NOT NULL,           -- 'tos' | 'public'(HuggingFace 缓存桶) | 'local'(experimental)
  input_uri      TEXT NOT NULL,           -- tos://bucket/prefix | 白名单根目录下的本地路径
  input_region   TEXT,
  input_cred_id  TEXT REFERENCES credential(id) ON DELETE SET NULL,  -- public 来源为空（匿名直读）
  output_uri     TEXT NOT NULL,
  output_region  TEXT,
  output_cred_id TEXT REFERENCES credential(id) ON DELETE SET NULL,
  delivery_key   TEXT NOT NULL REFERENCES delivery(delivery_key),    -- 见 §2.7

  episode_selector TEXT NOT NULL,         -- JSON: {"mode":"all"} | {"mode":"head","n":50}
                                          --     | {"mode":"explicit","expr":"3,10-12","indices":[3,10,11,12]}
  embodiment_id  TEXT,                    -- 预检读不到 robot_type 时用户补的
  vlm_model_id   TEXT REFERENCES vlm_model(id),
  vlm_reasoning_effort TEXT,              -- 任务级覆盖；NULL = 用模型配置
  params         TEXT NOT NULL,           -- JSON: 任务级参数，全集见 03 篇 §3.1（不含并发，D5）
  preflight      TEXT,                    -- JSON: 预检快照，任务启动时固化

  run_id         TEXT,                    -- 交付目录下的批次名（时间戳）
  progress       TEXT,                    -- JSON: {"stages":[{id,state,done,total,elapsed_s,eta_s}]}
  summary        TEXT,                    -- JSON: 报告概览快照（总数/通过/拒绝/待裁决/通过率），列表页直接用
  delivery_stale INTEGER NOT NULL DEFAULT 0,  -- 1 = 判决已变，交付数据集待重新导出
  started_at     INTEGER,
  finished_at    INTEGER
);
CREATE INDEX idx_task_list ON task(owner_id, created_at DESC);
```

- `preflight` 在**任务启动时固化**：数据集后来变了，任务的语义也不能变。
  「待启动」的任务还能改配置，所以固化发生在启动那一刻，不是创建那一刻。
- episode 以**整数下标**为准（与 v1 的 `3,10-12` 表达式一致）；`ep000034` 这种写法只是展示形式。
  表达式由后端解析和校验（负数、倒序区间、跨度超过 100 万都拒绝），前端不自己解析。
- `progress` 按档记录，对应 v1 任务卡上每个阶段各自的进度条、耗时和预计剩余。
- `summary` 让列表页和「已完成任务的报告概览」不必每次去 TOS 读报告。

### 2.4 task_module — 任务 × 模块

```sql
CREATE TABLE task_module (
  task_id    TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  module_id  TEXT NOT NULL,               -- 见 05 篇模块注册表
  selected   INTEGER NOT NULL,            -- 用户是否勾选
  availability TEXT NOT NULL,             -- 'available'|'unsupported'|'needs_input'
  unavailable_reason TEXT,                -- 标灰原因，UI 的「详细信息」弹窗读它
  state      TEXT NOT NULL,               -- 'pending'|'running'|'succeeded'|'failed'|'skipped'|'stale'
  error      TEXT,
  episodes_total INTEGER NOT NULL DEFAULT 0,   -- 该模块实际处理的条数（漏斗后）
  episodes_error INTEGER NOT NULL DEFAULT 0,   -- 其中出错的条数，「重试」默认只重跑它们
  params     TEXT,                        -- JSON，模块级参数覆盖
  started_at INTEGER, finished_at INTEGER,
  PRIMARY KEY (task_id, module_id)
);
```

几个状态的含义：

- `succeeded` 不代表每条都成功：单条 episode 出错不让模块失败，出错条数记在 `episodes_error`。
  只有「整个模块跑不起来」（VLM 端点完全不可达之类）才是 `failed`。
- **`stale`** 只表示一件事：**这个模块的输入集合变了，结果待同步**。去重和技能画像吃的是判决后的
  keep 集合（见 05 篇 §1），上游模块被重试、或人工裁决改了判决，它们就变 `stale`，
  由同一个子任务接着做增量同步，同步完回到 `succeeded`。
  「交付数据集待重新导出」是另一回事，只看 `task.delivery_stale`。

### 2.5 subtask — 子任务

```sql
CREATE TABLE subtask (
  id         TEXT PRIMARY KEY,            -- sub_<uuid7>
  task_id    TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,               -- 'retry' | 'resume' | 'apply_adjudication' | 'reexport'
  scope      TEXT NOT NULL,               -- JSON: {"modules":[...], "episodes":"errors"|"all"}，仅 retry 有意义
  state      TEXT NOT NULL,               -- 与 task 同一套状态机（没有 created）
  state_reason TEXT,
  progress   TEXT,
  started_at INTEGER, finished_at INTEGER
);
```

四种子任务覆盖了所有「在已有任务上再做一件事」的场景，共用一套状态机和一套 worker 池：

| kind | 做什么 | 允许的父任务状态 |
|---|---|---|
| `retry` | 重跑所选模块里出错的 episode（模块整体失败则全量），再同步下游 | succeeded / completed_with_errors |
| `resume` | 从断点接着跑主流程里没完成的部分（D20） | stopped / failed |
| `apply_adjudication` | 执行人工裁决 | succeeded / completed_with_errors |
| `reexport` | 重新导出交付数据集（增量）；任务创建时选了不导出的，也用它补做 | succeeded / completed_with_errors |

三条约束：

1. **同一任务的子任务串行**：它们写同一个工作目录。已有子任务未到终态时再建，返回 409。
2. **产物先写临时目录，成功才替换**：子任务失败或被停止，父任务原有的结果原样保留。
3. `resume` 成功后，父任务的 state 按主流程的结局改写为 succeeded / completed_with_errors
   —— 这是唯一一种会改父任务状态的子任务，因为它就是主流程的续篇。

### 2.6 token_usage — Token 消耗

```sql
CREATE TABLE token_usage (
  task_id     TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  subtask_id  TEXT NOT NULL DEFAULT '',   -- '' = 主流程
  module_id   TEXT NOT NULL,              -- 含 'autolabel'；合并请求按权重摊派，见 04 篇 §5
  call_kind   TEXT NOT NULL,              -- probe|endstate|arbitration|caption|llm|merged（前五个是 v1 的埋点标签）
  model_name  TEXT NOT NULL,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens  INTEGER NOT NULL DEFAULT 0,   -- usage.completion_tokens_details.reasoning_tokens
  cached_tokens     INTEGER NOT NULL DEFAULT 0,   -- usage.prompt_tokens_details.cached_tokens
  requests    INTEGER NOT NULL DEFAULT 0,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (task_id, subtask_id, module_id, call_kind, model_name)
);
```

**一行是一个聚合桶，不是一次请求。** 一个任务有几万次 VLM 请求，逐次落库会把单写者队列打满。
Daemon 在内存里按主键累加 CLI 发来的 usage 事件，每 5 秒和每个 stage 结束时各刷一次（UPSERT 累加）。
逐请求的明细仍在交付目录的 `details/vlm_latency.csv` 里（v1 既有，本期加 token 列）。

思考强度是一等配置，思维链 token 要单列；`cached_tokens` 用来量前缀缓存的命中 ——
probe 的 8 次请求共享「题面 + 参考帧」前缀，这是后续调优要看的第一个数。
任务的 Token 总量累计所有尝试（重试花掉的也是花掉的），详情页可以按子任务展开。

⚠️ **这是全新能力**。现有 `adapters/vlm_client.py` 只统计延迟（`_LAT_ROWS`：标签/耗时/成败/
对冲补发），不解析 `usage`。搬运时要在客户端加 usage 采集，这是对 v1 代码为数不多的
允许改动之一（不碰判定逻辑，只加旁路统计）。

### 2.7 adjudication — 人工裁决

```sql
CREATE TABLE delivery (
  delivery_key TEXT PRIMARY KEY,          -- 归一化后的交付目录 URI
  owner_id     TEXT NOT NULL DEFAULT 'default',
  input_uri    TEXT NOT NULL,             -- 首个任务绑定的输入数据集
  created_at   INTEGER NOT NULL
);

CREATE TABLE adjudication (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id    TEXT NOT NULL DEFAULT 'default',
  delivery_key TEXT NOT NULL REFERENCES delivery(delivery_key),
  episode_index INTEGER NOT NULL,
  line        TEXT NOT NULL,              -- 'label'|'task_verdict'|'reject_appeal'
  decision    TEXT NOT NULL,
  new_label   TEXT,                       -- 仅 line='label' 且采纳改标时有值
  note        TEXT,
  decided_by  TEXT NOT NULL,              -- 登录账号名
  decided_at  INTEGER NOT NULL
);
CREATE INDEX idx_adj_lookup ON adjudication(delivery_key, line, episode_index, id);

CREATE TABLE adjudication_applied (      -- 哪条裁决在哪个任务的哪次执行里落实了
  adjudication_id INTEGER NOT NULL REFERENCES adjudication(id),
  task_id     TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  subtask_id  TEXT NOT NULL REFERENCES subtask(id),
  PRIMARY KEY (adjudication_id, task_id)
);
```

三条裁决线与 v1 一致（标注分歧 / 任务成败弃权 / 被拒复议），语义见
`06-delivery-and-report.md` §5。几条从 v1 继承的语义落在表结构上：

- **追加式写入，后写者胜**：改判不是 UPDATE，而是再追加一行；读取时同一
  （交付目录, 裁决线, episode）取最新一行。历史因此留得住，也和 v1 的 CSV 语义一致。
- **裁决跟着交付目录走，不跟着任务走**。同一个交付目录再跑一个任务，之前的裁决还在，
  但**不会自动生效** —— 对新任务而言它们是「沿用自此前的人工裁决 · 待应用」，要再点一次「执行裁决」。
  所以「是否已应用」必须按任务记录，这就是 `adjudication_applied` 的用途。
- **一个交付目录只绑定一个输入数据集**（`delivery.input_uri`）。不同数据集写进同一个交付目录，
  episode 下标会撞，历史裁决会张冠李戴；创建任务时发现不一致直接拒绝。

**双写纪律**：DB 是权威副本，同时导出一份 CSV 到交付目录 `human-decisions/`，
保证交付目录自包含、离开平台也能读懂。写 DB 成功即算成功，CSV 导出失败只告警。

### 2.8 其余小表

```sql
CREATE TABLE event (                      -- 审计，保留 90 天，见 08 篇 §7
  id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, resource TEXT NOT NULL, detail TEXT, at INTEGER NOT NULL
);
CREATE TABLE preflight_cache (            -- POST /preflight 的结果，30 分钟过期
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, request_hash TEXT NOT NULL,
  result TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE idempotency_key (            -- 24 小时过期，见 03 篇 §8
  key TEXT NOT NULL, owner_id TEXT NOT NULL, route TEXT NOT NULL,
  response TEXT NOT NULL, created_at INTEGER NOT NULL,
  PRIMARY KEY (owner_id, route, key)
);
```

## 3. 状态机

任务和子任务共用：

```
  created ──(start)──▶ queued ◀──────────────(resume)──────────────┐
     │                   │                                          │
     │                   ▼                                          │
     │                running ──▶ pausing ──▶ paused ───────────────┘
     │                   │           │           │
     │                   │           └───────────┴──▶ stopping ──▶ stopped   (终态)
     │                   ├──▶ stopping ──▶ stopped                           (终态)
     │                   ├──▶ succeeded                 (终态，全部模块 succeeded)
     │                   ├──▶ completed_with_errors     (终态，部分模块 failed)
     │                   └──▶ failed                    (终态，任务级失败：预检失败/凭证失效/进程崩溃)
     └──(delete)         queued ──▶ stopped             (排队中被取消)
```

几个必须说清楚的语义：

- **`created`（待启动）只有任务有，子任务没有**（D20）。`start_now=false` 创建的任务停在这里，
  这时可以改全部配置（`PATCH` 任意字段），也可以直接删除；一旦启动，配置锁定，只能改名。
  它和 `queued`（已启动、在等 worker）是两回事，不能共用一个状态。
- **`completed_with_errors` 不是 `failed`**。需求明确要求「任务级别的失败不能影响整个流水线」：
  一个模块挂了，其余模块的结果照常聚合、照常出报告，报告里那一项显示「错误」并提供重试入口。
  只有让整个任务无法继续的原因（数据集读不了、凭证失效、进程被 OOM kill）才是 `failed`。
- **`paused` 是 episode 级断点**：停止派发新 episode，在飞的跑完正常落盘；恢复时从
  未处理的 episode 继续。不做帧级/请求级中断续传（D13）。
- **暂停分两种**（`pause_reason`）：`user` 是用户点的，只有用户能恢复；`system` 是 Daemon 自己暂停的
  （滚动升级、优雅停机、崩溃后对账），Daemon 启动后**自动恢复**。不区分的话，一次升级之后
  用户会发现任务全停着没人管。
- **终态任务仍可产生子任务**，子任务有自己的状态机，父任务状态不回退（`resume` 例外，见 §2.5）。
  父任务在子任务运行期间展示「有子任务运行中」的附加标记，而不是把 state 改回 running。
- **`stopped` / `failed` 不是死路**：可以「继续运行」（`resume` 子任务，复用已完成的模块 × episode 结果），
  也可以「复制为新任务」从头来。

### 3.1 合法迁移表

| 从 | 到 | 触发 |
|---|---|---|
| created | queued | 用户启动 |
| queued | running | worker 取到任务 |
| queued | stopped | 用户取消 |
| running | pausing → paused | 用户暂停 / 系统暂停（在飞 episode 跑完才进 paused） |
| paused | queued | 用户恢复，或系统暂停的在 Daemon 启动后自动恢复（重新排队，不抢占） |
| running / pausing / paused | stopping → stopped | 用户停止 |
| running | succeeded / completed_with_errors / failed | 自然结束 |

非法迁移一律拒绝并返回 409，不做「尽力而为」的猜测。

### 3.2 Daemon 启动时的状态对账

Daemon 可能是被 OOM 或节点故障直接杀掉的，库里会留下「看着在跑、其实没人在跑」的任务。
启动时、接受请求之前做一次对账：

| 库里的状态 | 改成 | 说明 |
|---|---|---|
| running / pausing（`pause_reason` 为空或 system） | paused（system）→ 随即自动恢复入队 | 结果是逐条落盘的，被打断的那条恢复时重跑 |
| pausing（user） | paused（user） | 用户本来就要暂停 |
| stopping | stopped | 用户本来就要停 |
| queued | 保持 | 重新进内存队列 |

子任务同理。对账动作写入 `event` 表，事后能回答「这个任务为什么中途停过」。

## 4. Repository 接口

所有持久化访问收在这一层，**业务代码不出现 SQL**。将来换火山 RDS 只重写实现。

```python
class Repository(Protocol):
    # 事务边界由调用方显式声明，实现内部不偷偷开事务
    def transaction(self) -> ContextManager[None]: ...

    # 任务
    def create_task(self, t: TaskCreate) -> Task: ...
    def get_task(self, task_id: str) -> Task | None: ...
    def list_tasks(self, owner: str, *, page: int, page_size: int,
                   state: str | None, q: str | None) -> PagedResult[Task]: ...   # 带 total
    def update_task_state(self, task_id: str, frm: set[str], to: str,
                          reason: str | None = None) -> bool: ...   # CAS，返回是否成功
    # 模块
    def upsert_task_modules(self, task_id: str, rows: list[TaskModule]) -> None: ...
    def mark_modules_stale(self, task_id: str, modules: list[str]) -> None: ...
    # 子任务 / 裁决 / 凭证 / token …… 同构
```

两条纪律：

1. **状态迁移一律用 CAS**（`update_task_state(frm={...}, to=...)`），不做「先读后写」。
   单副本下也要这么写 —— worker 线程和 API 线程是并发的，竞态在单进程里一样会发生。
2. **两种分页各管一摊**（D21）。任务列表用页码分页（`page` / `page_size`，返回 `total`），
   与火山控制台的表格一致；任务量级是千到万，OFFSET 没有性能问题，偶尔的漏行重行在控制台表格里可以接受。
   裁决队列、日志、episode 列表这些「往下翻」的内容用游标（上一页末行的排序键编码），不用 OFFSET。

### 4.1 SQLite 的并发纪律

- `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=5000`。
- **单写者**：所有写操作经由一个串行化的写队列（一个专用线程），读可并发。
  SQLite 的写锁是库级的，让并发写去撞锁再重试，不如一开始就串行。
- 连接不跨线程复用（`check_same_thread=True` 保持默认）。
- 每次启动跑一次 schema 迁移（版本号存 `PRAGMA user_version`），迁移脚本只增不改。

## 5. 与文件/对象存储的分工

DB 存**索引和状态**，TOS 存**产物**。判断标准：这条数据交付给客户时要不要跟着走？

| 数据 | 存哪 | 为什么 |
|---|---|---|
| 任务配置、状态、进度、Token 统计 | DB | 平台自身的运行状态，不属于交付物 |
| 模块判决结果 `checks/<module>/results.jsonl` | TOS（交付目录内） | 报告的原始依据，要跟着交付走 |
| 判决清单 passed/reject/review | TOS | 同上 |
| 交付数据集 `lerobot_curated/` | TOS | 交付物本体 |
| 报告 md/json、性能剖析 | TOS | 交付物 |
| 人工裁决记录 | DB（权威）+ TOS CSV（副本） | 平台要查，交付也要自包含 |
| 证据帧、裁决视频片段、同步曲线 | TOS | 体积大，前端用预签名 URL 直连 |
| 各 stage 的完整日志 | TOS（`logs/`），运行期间在工作目录 | 排查失败要看全量日志，SSE 只是实时通道 |
| 任务工作目录 `runs/<task_id>/` | 数据卷（本地） | CLI 的工作区，随产随传，终态 7 天后清理，见 00 篇 §4.2 |

**Daemon 不缓存产物**：报告页要的数据从 TOS 读，读不动的（大 CSV 明细）由 CLI 做服务端切片，
见 `03-rest-api.md` §6。
