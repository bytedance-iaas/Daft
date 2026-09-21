# Daemon 骨架（W4 / F2.2）

FastAPI + uvicorn，单副本。这一包只搭骨架：SQLite 仓储、鉴权、SSE、探针、静态资源与挂载前缀、启动对账，
以及只靠数据库和工作目录就能做完的接口。建任务、跑任务（W5）和密钥管理（W8）会接在这副骨架上。

设计依据：`docs/design/01`（数据模型与对账）、`03`（REST）、`08`（鉴权与主密钥）、`09`（部署与探针），
契约 C4 `docs/contracts/openapi.yaml`、C5 `daemon/repo/protocol.py`。

## 文件

| 文件 | 内容 |
|---|---|
| `__main__.py` | `python -m daemon`：读配置、校验主密钥、起 uvicorn；收到 SIGTERM 先结束 SSE 流再优雅退出 |
| `app.py` | `create_app(settings)`、`Runtime`（仓储、事件中心、鉴权、钩子）、就绪检查、维护线程 |
| `settings.py` / `masterkey.py` | 环境变量配置；主密钥缺失或格式不对就拒绝启动 |
| `instance.py` | 一个数据卷只跑一个 Daemon：启动时对 `curator.db.lock` 加排它锁，第二个进程等 10 秒拿不到锁就退出（退出码 2） |
| `repo/sqlite.py`、`repo/migrations.py` | C5 的 SQLite 实现：WAL、单写线程、显式事务、CAS、迁移（`PRAGMA user_version`） |
| `auth.py` | Basic 鉴权（搬自 v1 `ui/auth.py`）：htpasswd（bcrypt / apr1）优先，单用户环境变量兼容 |
| `events.py`、`routes/sse.py` | SSE 事件中心与 `GET {base}/events/tasks/{id}` |
| `transitions.py`、`timeline.py`、`reconcile.py` | 状态迁移（CAS + 审计事件 + SSE 一处做完）、执行时间线、启动对账 |
| `routes/api.py` | 已实现的 `{base}/api/v1` 接口 |
| `routes/static.py`、`deeplink.py` | 前端静态资源、SPA 回退、v1 旧深链 302（解析规则搬自 v1 `ui/runner.py`） |
| `errors.py`、`idempotency.py`、`pagination.py`、`logs.py`、`views.py`、`taskspec.py` | 统一错误体、幂等键、游标、任务日志、响应组装、任务配置校验 |
| `operations.py` | C4 全部 48 个操作的去向：已实现的，和留给 W3/W5/W8 的 |

## 已实现的接口

`/healthz`、`/readyz`（根路径和 `{base}` 下各一份，免鉴权）；`{base}/api/v1` 下：`GET /modules`、
`GET /tasks`（页码分页，带 `total`）、`GET/PATCH/DELETE /tasks/{id}`、`POST /tasks/{id}/restore`、
`POST /tasks/{id}/rebind-credentials`、`GET /tasks/{id}/subtasks|timeline|logs|usage`；`GET {base}/events/tasks/{id}`。

其余操作一个都没注册（访问返回 404 `not_found`），去向写在 `operations.py`，测试保证两张表合起来正好是 `openapi.yaml` 的全部操作。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `CURATOR_MASTER_KEY` | 必填 | 32 字节 base64（`openssl rand -base64 32`）。缺失或长度不对，进程以退出码 2 结束 |
| `CURATOR_MASTER_KEY_NEXT`、`CURATOR_MASTER_KEY_VERSION` | 空、1 | 主密钥轮换用（08 篇 §2.1） |
| `CURATOR_BASE_PATH`（旧名 `CURATION_UI_ROOT_PATH`） | 空 | 挂载前缀，现网 `/curation`；`curation`、`/curation/` 都会归一 |
| `CURATOR_DATA_DIR` | `/data` | 数据卷：`curator.db` 和任务工作目录 `runs/<task_id>/` |
| `CURATOR_DB_PATH`、`CURATOR_WORK_DIR` | 数据卷下 | 单独指定数据库文件、工作目录 |
| `CURATOR_SCRATCH_DIR` | 系统临时目录下 `curator-scratch` | 可丢的临时卷 |
| `CURATOR_STATIC_DIR` | `/app/web` 或仓库 `frontend/dist`（存在时） | 前端构建产物 |
| `CURATOR_PUBLIC_BASE_URL` | 空 | 给 Agent 的绝对链接；不配就给相对路径并带 `absolute: false` |
| `CURATOR_HTPASSWD_FILE`（旧名 `CURATION_UI_HTPASSWD_FILE`） | 空 | 多用户账号表，可与 rerun viewer 共用 |
| `CURATOR_AUTH_USER` / `CURATOR_AUTH_PASSWORD`（旧名 `CURATION_UI_USER` / `CURATION_UI_PASSWORD`） | 空 | 单用户模式，两个都配才生效 |
| `CURATOR_AUTH_MODE` | 自动 | `htpasswd` / `basic` / `none`。指定了模式但配置不全时拒绝所有请求，不会退回「不鉴权」 |
| `CURATOR_LOCAL_DATA_ROOT` | 空 | 开启 experimental 的本地路径数据来源，只认这个根目录之下 |
| `CURATOR_SSE_HEARTBEAT_S` | 15 | SSE 空闲时每隔多久发一行 `: ping` |
| `CURATOR_HOST` / `CURATOR_PORT` | `0.0.0.0` / 8080 | 监听地址 |
| `CURATOR_LOG_LEVEL` / `CURATOR_LOG_FORMAT` | `INFO` / `json` | 日志写到 stdout，密钥类字段一律打成 `***` |

三种鉴权都没配时不做鉴权，日志里会有一条警告，只适合本机调试。

## 手动验证步骤

以下命令在 `backend/` 下执行（依赖装在仓库根的 `.venv`，另需 `fastapi`、`uvicorn`，见交付报告的依赖清单）。

**准备**：选一个空目录当数据卷，造 23 条任务，其中 1 条停在「运行中」，模拟 Daemon 被杀掉时的样子。
W5 之前还没有建任务的接口，所以直接写仓储：

```bash
export CURATOR_DATA_DIR=$(mktemp -d)/data
PYTHONPATH=. ../.venv/bin/python - <<'EOF'
import os
from daemon.repo import protocol as P
from daemon.repo.sqlite import SqliteRepository
from curation.contracts import modules as registry

repo = SqliteRepository(os.path.join(os.environ["CURATOR_DATA_DIR"], "curator.db"))
rows = [P.TaskModule(task_id="", module_id=m, selected=True, availability="available")
        for m in registry.ids()]
for i in range(23):
    t = repo.create_task(P.TaskCreate(
        name=f"droid 抽检 {i:02d}", input_source="tos",
        input_uri="tos://bucket/datasets/droid_100", output_uri="tos://bucket/deliveries/droid",
        delivery_key="tos://bucket/deliveries/droid", episode_selector={"mode": "head", "n": 50},
        params={"export": True}, modules=rows))
repo.update_task_state(t.id, {"queued"}, "running", at=t.created_at)
repo.close()
print(t.id)
EOF
```

记下最后打印的任务 id，下文写作 `$T`（`export T=task_...`）。

1. **没有主密钥就拒绝启动**

   ```bash
   env -u CURATOR_MASTER_KEY ../.venv/bin/python -m daemon; echo "exit=$?"
   ```

   预期：`curator-daemon: 启动失败：没有配置 CURATOR_MASTER_KEY……`，`exit=2`。把 `CURATOR_MASTER_KEY` 设成 `dG9vIHNob3J0`（只有 9 字节）再试，同样退出码 2，报错里不回显这个值。

2. **启动**（另开一个终端，数据卷与上面相同）

   ```bash
   export CURATOR_MASTER_KEY=$(openssl rand -base64 32) CURATOR_BASE_PATH=/curation \
          CURATOR_AUTH_USER=demo CURATOR_AUTH_PASSWORD=demo-pass CURATOR_LOG_FORMAT=text
   ../.venv/bin/python -m daemon --host 127.0.0.1 --port 18080
   ```

3. **探针免鉴权，根路径和前缀下都在**

   ```bash
   curl -s localhost:18080/healthz; curl -s localhost:18080/curation/healthz
   curl -s localhost:18080/readyz;  curl -s localhost:18080/curation/readyz
   ```

   预期：`{"status":"ok"}`；`readyz` 的 `checks` 五项全是 `true`（其中 `reconciled` 表示启动对账已完成）。

4. **鉴权**

   ```bash
   curl -si localhost:18080/curation/api/v1/tasks | head -5
   curl -s -u demo:demo-pass 'localhost:18080/curation/api/v1/tasks?page=3&page_size=10' \
     | python3 -c 'import json,sys; b=json.load(sys.stdin); print(b["total"], b["page"], len(b["items"]))'
   ```

   预期：第一条是 `401`，带 `www-authenticate: Basic realm="Robot Data Curation"`，响应体是统一错误体
   `{"error":{"code":"unauthorized",...}}`；第二条输出 `23 3 3`（第 3 页只剩 3 条，总数 23）。

5. **启动对账**：造数据时停在「运行中」的那条，已被改成系统暂停再自动排队

   ```bash
   curl -s -u demo:demo-pass localhost:18080/curation/api/v1/tasks/$T | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])'
   curl -s -u demo:demo-pass localhost:18080/curation/api/v1/tasks/$T/timeline
   ```

   预期：`queued`；时间线依次是 `created`、`started`、`system_pause`（「任务被系统暂停：Daemon 重启时任务还在运行，将自动恢复」）、`system_resume`。

6. **SSE 与 Last-Event-ID 重放**

   ```bash
   curl -sN --max-time 2 -u demo:demo-pass localhost:18080/curation/events/tasks/$T
   curl -sN --max-time 2 -u demo:demo-pass -H 'Last-Event-ID: 1-0' localhost:18080/curation/events/tasks/$T
   curl -sN --max-time 2 -u demo:demo-pass -H 'Last-Event-ID: 999-0' localhost:18080/curation/events/tasks/$T
   ```

   预期：第一条先是 `retry: 3000`，再是一条 `state`（当前状态快照，`id: <epoch>-<seq>`），之后空闲时每 15 秒一行 `: ping`。
   新库第一次启动时 epoch 是 1，所以第二条带着 `1-0` 重连，会把对账产生的三条事件（`pausing`、`paused`、`queued`）按顺序补发。
   第三条的 epoch 对不上，先收到 `event: reset`，再是状态快照。重启 Daemon 后 epoch 会加 1。

7. **旧深链 302**

   ```bash
   curl -si -u demo:demo-pass 'localhost:18080/curation/?dataset=tos://bucket/datasets/droid_100&region=cn-beijing' | grep -iE '^(HTTP|location)'
   ```

   预期：`302`，`location: /curation/tasks/new?dataset=tos://bucket/datasets/droid_100&region=cn-beijing`，查询串原样保留。

8. **任务日志：最新的在前，游标往前翻**

   ```bash
   D=$CURATOR_DATA_DIR/runs/$T/logs; mkdir -p $D
   printf '%s\n' '{"ts":1,"kind":"log","level":"info","msg":"start"}' \
     '{"ts":2,"kind":"progress","stage":"check:x","done":1,"total":50}' \
     '{"ts":3,"kind":"log","level":"warn","msg":"episode 18 too short","episode_index":18}' \
     '{"ts":4,"kind":"log","level":"info","msg":"done"}' >> $D/numeric.jsonl
   curl -s -u demo:demo-pass "localhost:18080/curation/api/v1/tasks/$T/logs?limit=2"
   ```

   预期：返回 `done`、`episode 18 too short` 两行，`has_more: true`；带上返回的 `next_cursor` 再请求一次，得到 `start`，`has_more: false`。
   `progress` 行不算日志；加 `&level=warn` 只剩警告及以上。

9. **改名与 If-Match**

   ```bash
   ETAG=$(curl -si -u demo:demo-pass localhost:18080/curation/api/v1/tasks/$T | grep -i '^etag' | cut -d' ' -f2 | tr -d '\r')
   curl -s -u demo:demo-pass -X PATCH -H 'Content-Type: application/json' -H "If-Match: $ETAG" \
     -d '{"name":"改个名字"}' localhost:18080/curation/api/v1/tasks/$T | head -c 120; echo
   curl -s -u demo:demo-pass -X PATCH -H 'Content-Type: application/json' -H "If-Match: $ETAG" \
     -d '{"name":"再改一次"}' localhost:18080/curation/api/v1/tasks/$T
   ```

   预期：第一次成功；第二次用的还是旧的 `If-Match`，返回 412 `precondition_failed`。

10. **前端静态资源与 SPA 回退**（没有构建好的前端时，用一个假的 `index.html` 代替）

    ```bash
    export CURATOR_STATIC_DIR=$(mktemp -d); mkdir -p $CURATOR_STATIC_DIR/assets
    echo '<html><head></head><body>curator</body></html>' > $CURATOR_STATIC_DIR/index.html
    # 带上 CURATOR_STATIC_DIR 重启第 2 步的 Daemon，然后：
    curl -s -u demo:demo-pass localhost:18080/curation/tasks/$T/report
    ```

    预期：任意前端路由都返回 `index.html`，`<head>` 后面注入了 `<base href="/curation/">` 和 `window.__CURATOR_BASE__="/curation"`；
    `/curation/assets/不存在.js` 返回 404 错误体，不会返回 `index.html`。

11. **优雅停机**：在 Daemon 的终端按 Ctrl+C（或 `kill -TERM`），日志最后是 `Application shutdown complete.`；打开着的 SSE 连接会被主动结束。

## 自动化测试

```bash
../.venv/bin/python -m pytest -q tests/daemon tests/contracts   # 约 40 秒
../.venv/bin/python -m curation.contracts check                 # 无输出、退出码 0
```

`tests/daemon/test_repo_conformance.py` 是仓储的一致性测试套件，只用 C5 协议，
按 `tests/daemon/repo_impls.py` 列出的实现逐个跑。将来接火山 RDS，把新实现加进去（或设环境变量
`CURATOR_EXTRA_REPO_FACTORIES=包.模块:工厂函数`），原样跑过这套测试即可。

## 给后续工作包

- **W5（编排）**：
  - 状态变更一律走 `transitions.change_task_state` / `change_subtask_state`：CAS、审计事件、SSE 一次做完，时间线也从这些事件来。
    结果版本切换成功后调 `transitions.record_revision`。子任务结束、父任务终态重算后，发一条 `hub.publish_done`（或让 `change_task_state` 发）。
  - 进度、日志、用量推给 `runtime.hub.publish_progress / publish_log / publish_usage`（线程安全、不阻塞；进度和用量发累计值）。
  - 日志文件按 `logs.TaskLogs` 的布局写：主流程 `runs/<task_id>/logs/<stage>.jsonl`，子任务 `runs/<task_id>/logs/<subtask_id>/<stage>.jsonl`，
    每行一个 C3 对象；Daemon 自己的系统日志用 stage `system`。
  - 生命周期钩子：`runtime.on_ready`（启动对账之后，启动 worker 池）、`on_stopping`（收到 SIGTERM 立即调用，开始把任务置为系统暂停）、
    `on_shutdown`（lifespan 结束时，等暂停收尾）。
  - 写接口用 `routes.common.read_json_body`（只收 JSON、拒绝跨站写）和 `runtime.idempotency.run`（`Idempotency-Key`）。
  - `POST /tasks` 复用 `taskspec.resolve_config`，和 PATCH 的校验保持一致；`created` 任务把预检结果存在 `task.preflight`。
  - 任务列表的「待裁决」徽标读 `summary.pending_adjudication`：提交裁决后请更新这个数。
  - 裁决队列等内存里排好序的列表，可以用 `pagination.keyset_page` 做游标分页，`scope` 里带上结果版本。
  - 接上一个接口，就把它从 `operations.PENDING` 挪到 `IMPLEMENTED`，测试会检查路由和表是否一致。
- **W8（密钥）**：主密钥在 `runtime.master_key`（`key`、`version`、`next_key`）；进程启动后已从 `os.environ` 删掉，CLI 子进程不会继承。
  仓储里凭证与模型服务的方法都已实现并有一致性测试。
- **W10（前端）**：路由基址取 `window.__CURATOR_BASE__`；`index.html` 里已注入 `<base href="{base}/">`，Vite 用 `base: './'` 即可。
  SSE 收到 `done` 后请关闭 `EventSource`；收到 `reset` 就丢掉本地增量状态，重新拉一次任务详情。
