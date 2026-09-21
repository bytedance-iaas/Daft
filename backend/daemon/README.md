# Daemon 骨架（W4 / F2.2）

FastAPI + uvicorn，单副本。这一包只搭骨架：SQLite 仓储、鉴权、SSE、探针、静态资源与挂载前缀、启动对账，
以及只靠数据库和工作目录就能做完的接口。建任务、跑任务（W5）和密钥管理（W8）会接在这副骨架上。
续作把 Daemon 对齐到契约 1.1 / 1.2：数据集登记（D36、D37）的仓储与只读接口、概览、任务列表的模块与数据集筛选，
以及 C4 1.2 的响应格式。

设计依据：`docs/design/01`（数据模型与对账）、`03`（REST）、`07` §4.3（概览）、`08`（鉴权与主密钥）、`09`（部署与探针），
契约 C4 `docs/contracts/openapi.yaml`（1.2.0）、C5 `daemon/repo/protocol.py`（1.2）。

## 文件

| 文件 | 内容 |
|---|---|
| `__main__.py` | `python -m daemon`：读配置、校验主密钥、起 uvicorn；收到 SIGTERM 先结束 SSE 流再优雅退出 |
| `app.py` | `create_app(settings)`、`Runtime`（仓储、事件中心、鉴权、钩子）、就绪检查、维护线程 |
| `settings.py` / `masterkey.py` | 环境变量配置；主密钥缺失或格式不对就拒绝启动 |
| `instance.py` | 一个数据卷只跑一个 Daemon：启动时对 `curator.db.lock` 加排它锁，第二个进程等 10 秒拿不到锁就退出（退出码 2） |
| `repo/sqlite.py`、`repo/migrations.py` | C5 的 SQLite 实现：WAL、单写线程、显式事务、CAS、迁移（`PRAGMA user_version`，现为第 2 步） |
| `repo/extras.py` | C5 之外的几条只读查询（按地址找登记、概览的统计口径），拟并入 C5 1.3 |
| `auth.py` | Basic 鉴权（搬自 v1 `ui/auth.py`）：htpasswd（bcrypt / apr1）优先，单用户环境变量兼容 |
| `events.py`、`routes/sse.py` | SSE 事件中心与 `GET {base}/events/tasks/{id}` |
| `transitions.py`、`timeline.py`、`reconcile.py` | 状态迁移（CAS + 审计事件 + SSE 一处做完）、执行时间线、启动对账 |
| `routes/api.py`、`routes/datasets.py`、`routes/overview.py` | 已实现的 `{base}/api/v1` 接口：任务、数据集登记、概览；未知路径统一由 `api.fallback` 兜底 |
| `overview.py` | 概览的各项数字怎么算（口径写在模块说明里） |
| `routes/static.py`、`deeplink.py` | 前端静态资源、SPA 回退、v1 旧深链 302（解析规则搬自 v1 `ui/runner.py`） |
| `errors.py`、`idempotency.py`、`pagination.py`、`logs.py`、`views.py`、`taskspec.py` | 统一错误体、幂等键、游标、任务日志、响应组装、任务配置校验 |
| `operations.py` | C4 全部 48 个操作的去向：已实现的，和留给 W3/W5/W8 的 |

## 已实现的接口

`/healthz`、`/readyz`（根路径和 `{base}` 下各一份，免鉴权）；`{base}/api/v1` 下：`GET /modules`、
`GET /tasks`（页码分页，带 `total`；可按 `state`、`q`、`delivery`、`module`、`dataset_id` 筛选）、`GET/PATCH/DELETE /tasks/{id}`、
`POST /tasks/{id}/restore`、`POST /tasks/{id}/rebind-credentials`、`GET /tasks/{id}/subtasks|timeline|logs|usage`、
`GET /datasets`、`GET/PATCH/DELETE /datasets/{id}`、`GET /overview`；`GET {base}/events/tasks/{id}`。

登记数据集（`POST /datasets`）、重新核对、重新预检都要跑 CLI，归 W5；这里只有读、改名改备注和删除登记。

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
| `CURATOR_TZ_OFFSET` | `+08:00` | 站点所在时区的 UTC 偏移（形如 `+08:00`、`-05:30`、`Z`），决定概览「近 7 天」每天从几点算起 |

htpasswd 和单用户都没配、也没指定 `CURATOR_AUTH_MODE` 时不做鉴权，日志里会有一条警告，只适合本机调试。

## 手动验证步骤

以下命令在 `backend/` 下执行。依赖装在仓库根的 `.venv`：Daemon 需要 `fastapi`、`uvicorn`，测试另需 `httpx2`
（版本见 `backend/requirements.txt`、`requirements-dev.txt`）。

**准备**：选一个空目录当数据卷，登记两个数据集（其中 `umi_640` 的指纹有变化），在 `droid_100` 上造 23 条任务：
单号的只勾时间戳检查，第 0 条已经跑完（有错误、有 4 条待裁决、用了一些 Token），最后 1 条停在「运行中」，
模拟 Daemon 被杀掉时的样子。W5 之前还没有登记数据集和建任务的接口，所以直接写仓储：

```bash
export CURATOR_DATA_DIR=$(mktemp -d)/data
PYTHONPATH=. ../.venv/bin/python - <<'EOF'
import os
from daemon.repo import protocol as P
from daemon.repo.sqlite import SqliteRepository
from daemon.util import now_ms
from curation.contracts import modules as registry

repo = SqliteRepository(os.path.join(os.environ["CURATOR_DATA_DIR"], "curator.db"))
now = now_ms()

def preflight(version, episodes):
    return {"schema_version": "1.0", "validation": [], "modules": [], "warnings": [],
            "format": {"kind": "lerobot", "version": version, "supported": True, "detail": version},
            "dataset": {"episode_count": episodes, "cameras": ["wrist"], "fps": 15.0,
                        "robot_type": "franka", "total_frames": episodes * 250,
                        "labels": {"with_task": episodes, "without_task": 0}, "profile": None},
            "meta_fingerprint": "sha256:" + "a" * 64}

def register(name, version, episodes):
    ds, _ = repo.register_dataset(P.Dataset(
        id="", name=name, source="tos", uri=f"tos://bucket/datasets/{name}", region="cn-beijing",
        preflight=preflight(version, episodes), meta_fingerprint="sha256:" + "a" * 64,
        source_fingerprint={"objects": 204, "bytes": 1520331122, "digest": "sha256:" + "b" * 64},
        preflighted_at=now))
    return ds

ds, umi = register("droid_100", "v2", 100), register("umi_640", "v3", 640)
repo.record_dataset_check(P.DatasetCheck(
    dataset_id=umi.id, at=now, trigger="recheck", result="changed",
    change={"meta_changed": False, "added": 12, "removed": 0, "modified": 1, "preflighted_at": now,
            "sample_keys": ["data/chunk-000/episode_000640.parquet"]}))
for i in range(23):
    rows = [P.TaskModule(task_id="", module_id=m, availability="available",
                         selected=i % 2 == 0 or m == "timestamp_check") for m in registry.ids()]
    t = repo.create_task(P.TaskCreate(
        name=f"droid 抽检 {i:02d}", input_source="tos", input_region="cn-beijing",
        input_uri="tos://bucket/datasets/droid_100", output_uri="tos://bucket/deliveries/droid",
        delivery_key="tos://bucket/deliveries/droid", episode_selector={"mode": "head", "n": 50},
        params={"export": True}, modules=rows, dataset_id=ds.id))
    if i == 0:
        repo.update_task_state(t.id, {"queued"}, "running", at=now)
        repo.update_task_state(t.id, {"running"}, "completed_with_errors", at=now)
        repo.set_task_summary(t.id, {"total": 50, "passed": 41, "rejected": 7, "held": 2,
                                     "review": 10, "pass_rate": 0.82, "pending_adjudication": 4})
        repo.switch_result_rev(t.id, 0, 1)
        repo.add_usage([P.UsageDelta(task_id=t.id, ledger="actual", module_id="task_success",
                                     call_kind="probe", model_name="doubao", prompt_tokens=182000,
                                     completion_tokens=6400, requests=124)], at=now)
repo.update_task_state(t.id, {"queued"}, "running", at=t.created_at)
repo.close()
print(f"export T={t.id} D={ds.id} U={umi.id}")
EOF
```

把最后打印的那行复制下来执行一遍，下文用 `$T`（停在运行中的任务）、`$D`（`droid_100`）、`$U`（`umi_640`）。

1. **没有主密钥就拒绝启动**

   ```bash
   env -u CURATOR_MASTER_KEY ../.venv/bin/python -m daemon; echo "exit=$?"
   ```

   预期：`curator-daemon: 启动失败：没有配置 CURATOR_MASTER_KEY……`，`exit=2`。把 `CURATOR_MASTER_KEY` 设成 `dG9vIHNob3J0`（只有 9 字节）再试，同样退出码 2，报错里不回显这个值。

2. **启动**（同一个终端，放到后台；它的日志会和下面命令的输出混在一起）

   ```bash
   export CURATOR_MASTER_KEY=$(openssl rand -base64 32) CURATOR_BASE_PATH=/curation \
          CURATOR_AUTH_USER=demo CURATOR_AUTH_PASSWORD=demo-pass CURATOR_LOG_FORMAT=text
   ../.venv/bin/python -m daemon --host 127.0.0.1 --port 18080 &
   ```

   在另一个终端起第二个 Daemon（同样的 `CURATOR_DATA_DIR`）会等 10 秒后以退出码 2 结束：一个数据卷只允许一个 Daemon。

3. **探针免鉴权，根路径和前缀下都在**

   ```bash
   curl -s localhost:18080/healthz; curl -s localhost:18080/curation/healthz
   curl -s localhost:18080/readyz;  curl -s localhost:18080/curation/readyz
   ```

   预期：`{"status":"ok"}`；`readyz` 的 `checks` 五项全是 `true`（其中 `reconciled` 表示启动对账已完成）。

   下文的 `curl` 都要带账号，先定义一个简写：`c() { curl -s -u demo:demo-pass "$@"; }`。

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
   curl -s -u demo:demo-pass "localhost:18080/curation/api/v1/tasks/$T/logs?stage=numeric&limit=2"
   curl -s -u demo:demo-pass "localhost:18080/curation/api/v1/tasks/$T/logs?stage=system"
   ```

   预期：第一条返回 `done`、`episode 18 too short` 两行，`has_more: true`；带上返回的 `next_cursor`（`&cursor=...`）再请求一次，
   得到 `start`，`has_more: false`。`progress` 行不算日志；加 `&level=warn` 只剩警告及以上。
   第二条是启动对账写下的两行系统日志（系统暂停、自动恢复）。不带 `stage` 时两个文件按时间合在一起，最新的在前。

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
    kill %1; wait; ../.venv/bin/python -m daemon --host 127.0.0.1 --port 18080 &   # 带着新变量重启
    sleep 2; curl -s -u demo:demo-pass localhost:18080/curation/tasks/$T/report
    ```

    预期：任意前端路由都返回 `index.html`，`<head>` 后面注入了 `<base href="/curation/">` 和 `window.__CURATOR_BASE__="/curation"`；
    `/curation/assets/不存在.js` 返回 404 错误体，不会返回 `index.html`。

11. **数据集列表与详情**

    ```bash
    c 'localhost:18080/curation/api/v1/datasets?page_size=10' | python3 -m json.tool | head -40
    c 'localhost:18080/curation/api/v1/datasets?check_state=changed&format=lerobot_v3' \
      | python3 -c 'import json,sys; b=json.load(sys.stdin); print(b["total"], [x["name"] for x in b["items"]])'
    c localhost:18080/curation/api/v1/datasets/$D | python3 -m json.tool | head -30
    c localhost:18080/curation/api/v1/datasets/browse
    ```

    预期：列表 `total` 是 2，新登记的在前；`droid_100` 的 `format` 是 `lerobot_v2`、`episode_count` 100，`last_task` 是最新建的那条任务。
    第二条输出 `1 ['umi_640']`。详情里有 `preflight`、`listing`（`objects` 204）、`checks`（最新的在前）、`tasks`（最多 20 条，最新的在前），
    `links` 暂时是空数组（契约的 `Link.rel` 还没有数据集这一种）。最后一条是 404，`message` 是「这个接口不存在（或还没有实现）」：
    `browse` 归 W3，不会被当成数据集 id。

12. **任务列表按模块、数据集筛选**

    ```bash
    for q in 'module=task_success' 'module=timestamp_check,task_success' "dataset_id=$D" 'module=nope'; do
      c "localhost:18080/curation/api/v1/tasks?$q" | python3 -c 'import json,sys; b=json.load(sys.stdin); print(b.get("total", b))'
    done
    ```

    预期：`12`、`12`（要求每一个都勾了）、`23`，最后一条是 400 `validation_failed`，列出可选的模块。列表条目带 `modules`（所选模块，按注册表顺序）和 `dataset_id`。

13. **概览**

    ```bash
    c localhost:18080/curation/api/v1/overview | python3 -m json.tool
    ```

    预期：`todo` 里 `error_tasks` 1、`adjudication` 为 `{"tasks": 1, "episodes": 4}`、`delivery_pending` 1（有结果但从没导出过）、
    `datasets_changed` 1；`running` 里 `queued` 22（对账后运行中的那条也回到了排队）；
    `recent` 的 `tasks_finished` 1、`episodes_checked` 50、`pass_rate` 0.82，`tokens_per_day` 列出 7 天、最早的在前，
    今天是 188400（只算实际调用账的 `prompt + completion`）；`datasets` 为 `{"total": 2, "changed": 1}`。
    日期按 `CURATOR_TZ_OFFSET`（默认北京时间）切分。

14. **改名、备注与删除登记**

    ```bash
    c -X PATCH -H 'Content-Type: application/json' -d '{"name":"DROID 抽检集","note":"第二批"}' \
      localhost:18080/curation/api/v1/datasets/$D | head -c 160; echo
    c -X DELETE -H 'Content-Type: application/json' localhost:18080/curation/api/v1/datasets/$D; echo
    c -o /dev/null -w '%{http_code}\n' -X DELETE -H 'Content-Type: application/json' localhost:18080/curation/api/v1/datasets/$U
    ```

    预期：改名成功，返回整条详情；删 `droid_100` 返回 409 `dataset_in_use`，「还有 22 个未结束的任务在用这个数据集……」，
    `details.tasks` 列出其中最新的 20 条；`umi_640` 没有任务在用，删除返回 `204`，TOS 上的数据不受影响。
    不带 `Content-Type: application/json` 的写请求一律 400。

15. **优雅停机**：`kill %1`（SIGTERM），Daemon 日志最后是 `Application shutdown complete.`；打开着的 SSE 连接会被主动结束，
    不会拖住停机。重启之后 SSE 的 epoch 加 1，旧的 `Last-Event-ID` 会收到 `reset`。

## 自动化测试

```bash
../.venv/bin/python -m pytest -q tests/daemon tests/contracts   # 约 1 分钟
../.venv/bin/python -m curation.contracts check                 # 无输出、退出码 0
```

`tests/daemon/test_repo_conformance.py` 是仓储的一致性测试套件，只用 C5 协议，
按 `tests/daemon/repo_impls.py` 列出的实现逐个跑；`test_repo_extras.py` 同样地测 `repo/extras.py` 里的查询。
将来接火山 RDS，把新实现加进去（或设环境变量 `CURATOR_EXTRA_REPO_FACTORIES=包.模块:工厂函数`），原样跑过这两套测试即可。
`test_repo_sqlite.py` 里有从第 1 步迁移上来的测试。

## 给后续工作包

- **W5（编排）**：
  - 状态变更一律走 `transitions.change_task_state` / `change_subtask_state`（带上任务的 `owner`）：CAS、审计事件、SSE 一次做完，
    时间线也从这些事件来；SSE 事件以审计事件的 id 作版本，多个线程同时改同一个任务也不会把旧状态发在新状态后面。
    结果版本切换成功后调 `transitions.record_revision`，子任务产出的版本再用 `repo.set_subtask_result_rev` 记到子任务上。
    子任务的终态那一步默认会发带 `subtask_id` 的 `done`，之后已结束任务的 SSE 流就会收尾：所以子任务结束时，
    先按当前结果重算父任务终态（D25，含 C5 1.2 新增的 `stopped / failed → succeeded / completed_with_errors`，只对任务），再结束子任务。
    任务从 `stopped` / `failed` 因 resume 结束时 `finished_at` 取新的结束时间，`succeeded` 与 `completed_with_errors` 之间重算时保留原值。
    子任务的暂停原因现在是子任务上的一列（C5 1.2），`change_subtask_state(pause_reason=...)` 会写进去；启动对账对子任务和任务用同一张表。
  - 进度、日志、用量推给 `runtime.hub.publish_progress / publish_log / publish_usage`（线程安全、不阻塞；进度和用量发累计值，
    发 `state` / `done` 之前会先把积压的进度和用量发出去）。
  - 日志文件按 `logs.TaskLogs` 的布局写：主流程 `runs/<task_id>/logs/<stage>.jsonl`，子任务 `runs/<task_id>/logs/<subtask_id>/<stage>.jsonl`，
    每行一个 C3 对象；Daemon 自己的系统日志用 stage `system`。
  - 生命周期钩子：`runtime.on_ready`（启动对账之后，启动 worker 池）、`on_stopping`（收到 SIGTERM 立即调用，开始把任务置为系统暂停）、
    `on_shutdown`（lifespan 结束时，等暂停收尾）。
  - 写接口用 `routes.common.read_json_body`（只收 JSON、拒绝跨站写）和 `runtime.idempotency.run`（`Idempotency-Key`）。
    事务里每次仓储调用都有保存点，方法中途失败不留半截数据；磁盘写满这类让 SQLite 整体回滚的错误，块内后续调用会直接报错。
  - `POST /tasks` 复用 `taskspec.resolve_config`，和 PATCH 的校验保持一致；`created` 任务把预检结果存在 `task.preflight`。
    `input` 给 `{dataset_id}` 时由 `taskspec.resolve_input` 换成登记的来源、地址、地域和访问密钥，并记下 `dataset_id`；
    给完整地址时按地址找已有登记（`repo.find_dataset`），找不到就是 `None`，登记新地址（预检加 `curation snapshot`）是 W5 的事。
  - 数据集登记：`repo.register_dataset` 按（来源、地址、地域）取或建，地址先用 `taskspec.normalize_tos_uri` 归一，空地域等同于没有；
    `source_fingerprint` 存 `{objects, bytes, digest}`（`listing` 也认 C2 `source-manifest` 的 `count`）。每次核对都 `record_dataset_check`
    （`add` / `recheck` / `task_start` / `repreflight`），它会同时更新 `check_state` 和 `checked_at`；重新预检用
    `update_dataset(preflight=, meta_fingerprint=, source_fingerprint=, preflighted_at=, manifest_path=)` 一次换掉基线，`check_state` 回到 `ok`，
    `repreflight` 那条核对记录不会再把它改回 `changed`。已登记的地址再登记一次时，若原来的访问密钥已被删除，请用 `update_dataset(credential_id=...)` 换上新的。
  - 概览读 `summary` 里的 `total`、`passed`、`pending_adjudication`，Token 按 `add_usage` 被调用的时刻计入某一天：用量请照常每 5 秒汇总一次。
  - 任务列表的「待裁决」徽标读 `summary.pending_adjudication`：提交裁决后请更新这个数。
  - 裁决队列等内存里排好序的列表，可以用 `pagination.keyset_page` 做游标分页，`scope` 里带上结果版本。
  - 接上一个接口，就把它从 `operations.PENDING` 挪到 `IMPLEMENTED`，测试会检查路由和表是否一致。
    新路由加在 `app.py` 那组 `include_router` 里、`api.fallback` 之前；`/datasets/{id}` 只匹配 `ds_` 开头的 id，
    W3 的 `/datasets/browse`、`/datasets/episodes` 放在哪个路由表里都不会被它挡住。
- **W8（密钥）**：主密钥在 `runtime.master_key`（`key`、`version`、`next_key`）；进程启动后已从 `os.environ` 删掉，CLI 子进程不会继承。
  仓储里凭证与模型服务的方法都已实现并有一致性测试。访问密钥删掉后，任务和数据集的响应里 `credential` 为 `null`（C4 1.2），
  数据集上的引用会被置空；概览的「验证失败」只数 TOS 访问密钥和 VLM 后端，VLM 的 API Key 跟着后端算。
- **W10（前端）与 W3（`curation task …` 客户端）**：写请求（POST / PUT / PATCH / DELETE）一律带
  `Content-Type: application/json`，没有请求体也要带，否则 400；浏览器的跨站写请求（`Sec-Fetch-Site` 不是 `same-origin`）会被拒。
  路由基址取 `window.__CURATOR_BASE__`；`index.html` 里已注入 `<base href="{base}/">`，Vite 用 `base: './'` 即可。
  SSE 收到 `done` 后请关闭 `EventSource`（断线重连也只会再收到一次快照和 `done`）；收到 `reset` 就丢掉本地增量状态，重新拉一次任务详情。
  `state`、`done` 总带 `subtask_id`（任务本身为 `null`）和 `reason`（状态原因，可为 `null`）。
  日志接口按时间倒序分页：第一页是最新的，`next_cursor` 往更早翻；`subtask` 不传是全部，传空串只看主流程，传 id 只看那个子任务。
  `Task.vlm` 现在带 `snapshot`（启动时固化的有效配置，未启动时为 `null`）。
