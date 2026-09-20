# 03 REST API 契约

## 1. 总则

- 前缀 `/api/v1`，JSON in / JSON out，UTF-8。
- **粒度粗**：一个端点完成一件用户视角的事，内部可以是多条 CLI 的组合。
  前端不负责编排，不存在「前端连续调三个接口才完成一件事」的设计。
- 写操作全部**异步**：立即返回资源和 `state`，进度靠 SSE 或轮询。
- 错误体统一：

```jsonc
{"error": {"code": "task_state_conflict", "message": "任务当前为 running，不能删除",
           "details": {"state": "running"}}}
```

`code` 是稳定的机器可读串，`message` 是给人看的中文（UI 直接展示）。

## 2. 端点总表

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/credentials` | 列密钥（永不返回密文/明文密钥） |
| POST | `/api/v1/credentials` | 新建密钥，创建时做一次连通性校验 |
| PUT/DELETE | `/api/v1/credentials/{id}` | 更新/删除 |
| POST | `/api/v1/credentials/{id}/verify` | 重新校验 |
| GET | `/api/v1/vlm-backends` | 列 VLM 后端及模型 |
| POST | `/api/v1/vlm-backends` | 新建（ark 会自动拉模型列表） |
| POST | `/api/v1/vlm-backends/{id}/refresh-models` | 重新拉模型列表 |
| GET | `/api/v1/datasets` | 列数据集（私有 TOS / HF 镜像） |
| POST | `/api/v1/preflight` | 预检，同步返回 |
| POST | `/api/v1/tasks` | 新建任务（= 需求里的 `run_modules()`） |
| GET | `/api/v1/tasks` | 任务列表，游标分页 |
| GET | `/api/v1/tasks/{id}` | 任务详情 |
| PATCH | `/api/v1/tasks/{id}` | 改名等元信息 |
| DELETE | `/api/v1/tasks/{id}` | 删除（终态才允许） |
| POST | `/api/v1/tasks/{id}/actions/{action}` | start/pause/resume/stop |
| POST | `/api/v1/tasks/{id}/retry` | 重试失败模块 → 建子任务 |
| POST | `/api/v1/tasks/{id}/reexport` | 重新导出交付数据集 → 建子任务 |
| GET | `/api/v1/tasks/{id}/report` | 报告（概览 + 各模块小节） |
| GET | `/api/v1/tasks/{id}/report/tables/{table}` | 明细表分页切片 |
| GET | `/api/v1/tasks/{id}/perf` | 性能剖析 |
| GET | `/api/v1/tasks/{id}/usage` | Token 消耗（按模块/模型聚合） |
| GET | `/api/v1/tasks/{id}/adjudication` | 待裁决队列 |
| POST | `/api/v1/tasks/{id}/adjudication` | 提交裁决（只记录，不执行） |
| POST | `/api/v1/tasks/{id}/adjudication/apply` | 执行裁决 → 建子任务 |
| GET | `/api/v1/tasks/{id}/subtasks` | 子任务列表 |
| GET | `/api/v1/media/sign` | 换一个 TOS 预签名 URL（视频/证据帧） |
| GET | `/events/tasks/{id}` | SSE：进度、日志、状态变更 |
| GET | `/healthz` `/readyz` | 探针 |

## 3. 新建任务：`POST /api/v1/tasks`

需求里的 `run_modules()` 就是它。**只有标准模式**，调用方不传并发参数；
Daemon 内部必经 planner 做合并与并发优化（D5）。

```jsonc
// 请求
{
  "name": "umi_640 全量质检",
  "input":  {"uri": "tos://bucket/datasets/umi_640_notask", "region": "cn-beijing",
             "credential": "prod-tos"},
  "output": {"uri": "tos://bucket/deliveries/umi-640", "region": "cn-beijing",
             "credential": "prod-tos"},
  "episodes": {"mode": "explicit", "ids": ["000000", "000001", "000002"]},
  "modules": ["timestamp_check", "kinematic_limits", "motion_quality",
              "visual_quality", "video_action_sync", "task_success",
              "dedup", "skill_profile"],
  "embodiment_id": null,
  "vlm": {"backend": "ark-prod", "model": "doubao-seed-2-0-pro-260215",
          "thinking_effort": "medium"},
  "params": {"vlm_retry": 3, "vlm_timeout_s": 120, "start_now": true}
}

// 响应 201
{"id": "task_01HX...", "state": "queued", "created_at": 1758300000000,
 "links": [{"rel": "self", "url": "/tasks/task_01HX..."}]}
```

服务端行为：
1. 校验凭证可用、模块可用性与预检快照一致（不一致返回 `preflight_mismatch`，要求前端重检）。
2. 固化 `preflight` 快照进任务。
3. 生成 `run_id`（时间戳），写 DB，入队。
4. `start_now=false` 时停在 `queued` 但不派发，等 `actions/start`。

## 4. 任务列表：游标分页

```
GET /api/v1/tasks?limit=20&cursor=<opaque>&state=running&q=umi
```

```jsonc
{"items": [...], "next_cursor": "eyJ0cyI6MTc1ODMwfQ", "has_more": true}
```

- 游标是 `(created_at, id)` 的 base64，**不用 OFFSET**。
- 列表行里带 `progress` 和 `module_summary`（各状态模块数），够渲染列表页，不用再逐个查详情。

## 5. SSE：`GET /events/tasks/{id}`

```
event: state
data: {"state":"running","at":1758300001000}

event: progress
data: {"stage":"check:task_success","done":128,"total":640,"eta_s":420}

event: log
data: {"level":"warn","msg":"episode 000018 too short (0.5s)"}

event: usage
data: {"prompt_tokens":182000,"completion_tokens":6400,"requests":1024}

event: done
data: {"state":"completed_with_errors","failed_modules":["video_action_sync"]}
```

- 事件源是 CLI 子进程 stderr 的 JSON Lines（见 `02-cli-contract.md` §5），
  Daemon 做聚合与限流（progress 最多 2 条/秒，log 最多 20 条/秒，超出丢弃中间态）。
- **断线重连**：客户端带 `Last-Event-ID`，Daemon 重放最近 200 条。再早的从
  `GET /api/v1/tasks/{id}` 的快照恢复 —— SSE 是加速通道，不是唯一真相源。
- 前端必须能在 SSE 完全不可用时靠轮询工作（每 5s 拉一次详情），这是容错底线。

## 6. 报告与大表分页

报告概览一次返回（KB 级）。明细表可能是几十万行的 CSV，走切片接口：

```
GET /api/v1/tasks/{id}/report/tables/visual_quality?cursor=...&limit=100&sort=score&order=asc
```

Daemon 不把 CSV 读进内存，转调 `curation table slice`，由 CLI 用 pyarrow 做投影+切片。
排序字段限白名单，避免对大文件做任意排序。

## 7. 媒体访问：预签名 URL

```
GET /api/v1/media/sign?task=task_01HX...&path=details/clips/000034.mp4&ttl=1800
→ {"url": "https://bucket.tos-cn-beijing.volces.com/...&X-Tos-Signature=...", "expires_at": ...}
```

- 浏览器直连 TOS 取视频和证据帧，Daemon 不做流量中转（D：用户本来就有该桶访问权限）。
- TTL 默认 30 分钟，`path` 必须落在该任务的交付目录前缀内（防越权签名）。
- 前端在 403/过期时自动重签一次再重试，不弹错。

## 8. 幂等与并发

- 所有 `POST .../actions/*` 支持 `Idempotency-Key` 头；相同 key 24 小时内返回首次结果。
- 状态相关的写操作走 Repository 的 CAS，冲突返回 409 + 当前状态，前端刷新即可。
- 删除任务只允许终态；运行中删除返回 409 并提示先停止。

## 9. 鉴权

本期单实例单租户，沿用 HTTP Basic / htpasswd（搬 `ui/auth.py` 的中间件，含
`/healthz` 豁免与常数时间比较两条纪律）。所有 API 走同一个 `AuthProvider` 接口：

```python
class AuthProvider(Protocol):
    def authenticate(self, scope) -> Principal | None: ...   # 返回 owner_id
```

火山 IAM 将来只是这个接口的另一个实现。详见 `08-secrets-and-auth.md`。
