# 01 数据模型、状态机与持久化

## 1. 实体全景

```
Owner(预留) ──┬── Credential ── VlmBackend(方舟/自定义) ── VlmModel
              │
              ├── Task ──┬── TaskModule (任务 × 模块，本期 8 行/任务)
              │          ├── Subtask ── SubtaskModule
              │          ├── TokenUsage (按模块/调用种类聚合)
              │          └── DeliveryState (交付数据集是否过期)
              │
              └── Adjudication (人工裁决记录，跨任务累积)
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
  max_concurrency INTEGER NOT NULL DEFAULT 32,   -- 该后端的在飞请求上限
  UNIQUE(owner_id, name)
);

CREATE TABLE vlm_model (
  id              TEXT PRIMARY KEY,       -- vm_<uuid7>
  backend_id      TEXT NOT NULL REFERENCES vlm_backend(id) ON DELETE CASCADE,
  model_name      TEXT NOT NULL,          -- ark 从 API 拉取；custom 手填
  thinking_effort TEXT,                   -- 'minimal'|'low'|'medium'|'high'|null
  max_concurrency INTEGER,                -- 覆盖后端级
  discovered      INTEGER NOT NULL DEFAULT 0,  -- 1=拉取得到 0=手填
  UNIQUE(backend_id, model_name)
);
```

方舟支持拉模型列表，自定义端点手填 —— 这是两类后端在 UI 上唯一的差别。

### 2.3 task — 任务

```sql
CREATE TABLE task (
  id             TEXT PRIMARY KEY,        -- task_<uuid7>
  owner_id       TEXT NOT NULL DEFAULT 'default',
  name           TEXT NOT NULL,
  state          TEXT NOT NULL,           -- 见 §3
  state_reason   TEXT,                    -- 失败/停止原因，给人看的一句话

  input_uri      TEXT NOT NULL,           -- tos://bucket/prefix | /mnt/...(experimental)
  input_region   TEXT,
  input_cred_id  TEXT REFERENCES credential(id),
  output_uri     TEXT NOT NULL,
  output_region  TEXT,
  output_cred_id TEXT REFERENCES credential(id),

  episode_selector TEXT NOT NULL,         -- JSON: {"mode":"all"|"explicit","ids":[...]}
  embodiment_id  TEXT,                    -- 预检读不到 robot_type 时用户补的
  vlm_model_id   TEXT REFERENCES vlm_model(id),
  params         TEXT NOT NULL,           -- JSON: 任务级参数（超时/重试/并发上限等）
  preflight      TEXT,                    -- JSON: 预检快照，任务创建时固化

  run_id         TEXT,                    -- 交付目录下的批次名（时间戳）
  progress       TEXT,                    -- JSON: {done, total, stage, eta_s}
  delivery_stale INTEGER NOT NULL DEFAULT 0,  -- 1 = 判决已变，交付数据集待重新导出
  started_at     INTEGER,
  finished_at    INTEGER
);
CREATE INDEX idx_task_list ON task(owner_id, created_at DESC);
```

`preflight` 在**创建任务时固化**：数据集后来变了，任务的语义也不能变。

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
  params     TEXT,                        -- JSON，模块级参数覆盖
  started_at INTEGER, finished_at INTEGER,
  PRIMARY KEY (task_id, module_id)
);
```

**`stale`** 是为子任务准备的：某模块被子任务重跑后，依赖它的聚合产物（判决、交付数据集）
需要重算，这个状态位驱动 UI 上的「需重新导出」提示。

### 2.5 subtask — 子任务

```sql
CREATE TABLE subtask (
  id         TEXT PRIMARY KEY,            -- sub_<uuid7>
  task_id    TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,               -- 'retry_modules' | 'apply_adjudication' | 'reexport'
  modules    TEXT NOT NULL,               -- JSON 数组，kind=retry_modules 时有意义
  state      TEXT NOT NULL,               -- 与 task 同一套状态机
  state_reason TEXT,
  progress   TEXT,
  started_at INTEGER, finished_at INTEGER
);
```

三种子任务覆盖了所有「在已完成任务上再做一件事」的场景：重试失败模块、
执行人工裁决、重新导出交付数据集。它们共用一套状态机和一套 worker 池。

### 2.6 token_usage — Token 消耗

```sql
CREATE TABLE token_usage (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  subtask_id  TEXT REFERENCES subtask(id) ON DELETE CASCADE,
  module_id   TEXT,                       -- 合并请求按权重摊派，见 04 篇 §5
  call_kind   TEXT NOT NULL,              -- probe|endstate|caption|llm|merged
  model_name  TEXT NOT NULL,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  requests    INTEGER NOT NULL DEFAULT 0,
  recorded_at INTEGER NOT NULL
);
```

⚠️ **这是全新能力**。现有 `adapters/vlm_client.py` 只统计延迟（`_LAT_ROWS`：标签/耗时/成败/
对冲补发），不解析 `usage`。搬运时要在客户端加 usage 采集，这是对 v1 代码为数不多的
允许改动之一（不碰判定逻辑，只加旁路统计）。

### 2.7 adjudication — 人工裁决

```sql
CREATE TABLE adjudication (
  id          TEXT PRIMARY KEY,
  owner_id    TEXT NOT NULL DEFAULT 'default',
  delivery_key TEXT NOT NULL,             -- 交付目录标识，跨任务累积
  episode_id  TEXT NOT NULL,
  line        TEXT NOT NULL,              -- 'label'|'task_verdict'|'reject_appeal'
  decision    TEXT NOT NULL,
  note        TEXT,
  decided_by  TEXT NOT NULL,
  decided_at  INTEGER NOT NULL,
  applied_in_subtask TEXT REFERENCES subtask(id)   -- NULL = 尚未执行
);
```

三条裁决线与 v1 一致（标注分歧 / 任务成败弃权 / 被拒复议），语义见
`06-delivery-and-report.md` §5。

**双写纪律**：DB 是权威副本，同时导出一份 CSV 到交付目录 `human-decisions/`，
保证交付目录自包含、离开平台也能读懂。写 DB 成功即算成功，CSV 导出失败只告警。

## 3. 状态机

任务和子任务共用：

```
                    ┌──────────────────────────────┐
                    ▼                              │
  queued ──▶ running ──▶ pausing ──▶ paused ───────┘ (resume)
     │          │
     │          ├──▶ stopping ──▶ stopped        (终态)
     │          ├──▶ succeeded                    (终态，全部模块 succeeded)
     │          ├──▶ completed_with_errors        (终态，部分模块 failed)
     │          └──▶ failed                       (终态，任务级失败：预检失败/凭证失效/进程崩溃)
     └──▶ stopped                                 (排队中被取消)
```

几个必须说清楚的语义：

- **`completed_with_errors` 不是 `failed`**。需求明确要求「任务级别的失败不能影响整个流水线」：
  一个模块挂了，其余模块的结果照常聚合、照常出报告，报告里那一项显示「错误」并提供重试入口。
  只有让整个任务无法继续的原因（数据集读不了、凭证失效、进程被 OOM kill）才是 `failed`。
- **`paused` 是 episode 级断点**：停止派发新 episode，在飞的跑完正常落盘；恢复时从
  未处理的 episode 继续。不做帧级/请求级中断续传（D13）。
- **终态任务仍可产生子任务**，子任务有自己的状态机，父任务状态不回退。
  父任务在子任务运行期间展示「有子任务运行中」的附加标记，而不是把 state 改回 running。

### 3.1 合法迁移表

| 从 | 到 | 触发 |
|---|---|---|
| queued | running | worker 取到任务 |
| queued | stopped | 用户取消 |
| running | pausing → paused | 用户暂停（在飞 episode 跑完才进 paused） |
| paused | queued | 用户恢复（重新排队，不抢占） |
| running | stopping → stopped | 用户停止 |
| running | succeeded / completed_with_errors / failed | 自然结束 |

非法迁移一律拒绝并返回 409，不做「尽力而为」的猜测。

## 4. Repository 接口

所有持久化访问收在这一层，**业务代码不出现 SQL**。将来换火山 RDS 只重写实现。

```python
class Repository(Protocol):
    # 事务边界由调用方显式声明，实现内部不偷偷开事务
    def transaction(self) -> ContextManager[None]: ...

    # 任务
    def create_task(self, t: TaskCreate) -> Task: ...
    def get_task(self, task_id: str) -> Task | None: ...
    def list_tasks(self, owner: str, *, cursor: str | None,
                   limit: int, state: str | None) -> Page[Task]: ...
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
2. **分页一律游标式**（`cursor` = 上一页末行的 `(created_at, id)` 编码），不用 OFFSET。
   任务列表要支持 Lazy Loading，OFFSET 在插入频繁时会漏行重行。

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

**Daemon 不缓存产物**：报告页要的数据从 TOS 读，读不动的（大 CSV 明细）由 CLI 做服务端切片，
见 `03-rest-api.md` §6。
