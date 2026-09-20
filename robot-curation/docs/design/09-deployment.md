# 09 镜像、Helm Chart 与配置

## 1. 镜像

一个镜像，多阶段构建：前端产物 COPY 进后端镜像，由 Daemon 同进程 serve。

```dockerfile
# ── stage 1: 前端 ──
FROM node:20-slim AS web
WORKDIR /w
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build            # → /w/dist

# ── stage 2: 运行时 ──
ARG BASE_IMAGE=python:3.10-slim-bookworm
FROM ${BASE_IMAGE}
# ...依赖安装...
COPY --from=web /w/dist /app/web
ENTRYPOINT ["tini","--","curator-daemon"]
```

从 v1 Dockerfile **必须继承的四条经验**（都是踩过的坑，注释里有实锤）：

1. **底座钉死 `bookworm`**：裸 `python:3.10-slim` 会随上游漂到 trixie，
   火山 extra-tools 源在 trixie 下不存在，装不上依赖。
2. **PyPI 默认走火山内网镜像** `mirrors.ivolces.com`，公网构建用 `--build-arg PIP_INDEX_URL=...`
   换回官方源。官方 PyPI 在火山构建集群实测 17kB/s + 超时。
3. **apt 默认必须用公网可达的 `mirrors.volces.com`**：内网域在火山网络外解析不了，
   写死内网域会让公开 CI 构建直接失败。
4. **daft 用 pip 官方发行版，绝不从源码构建**（可复现性纪律）。
   这也是 baseline 可以删掉上游 daft 源码的前提。

另外：`curl` + `tini` 进镜像（pod 内探端点、信号转发），gradio 相关依赖**全部移除**。

## 2. Helm Chart

```
deploy/charts/curator/
├── Chart.yaml
├── values.yaml
├── templates/
│   ├── deployment.yaml        # 单副本
│   ├── service.yaml
│   ├── ingress.yaml           # 可选
│   ├── pvc.yaml               # SQLite + 帧缓存
│   ├── secret.yaml            # 主密钥（可由外部 Secret 接管）
│   ├── configmap.yaml         # 流水线 YAML 覆盖
│   ├── job-migrate.yaml       # schema 迁移，pre-install/pre-upgrade hook
│   └── NOTES.txt
└── README.md
```

### 2.1 values.yaml 骨架

```yaml
image:
  repository: <registry>/curator
  tag: ""                       # 默认用 Chart.appVersion
  pullPolicy: IfNotPresent

replicaCount: 1                 # ⚠️ 单副本。>1 目前不支持（SQLite 各副本独立）

auth:
  mode: htpasswd                # htpasswd | basic
  htpasswdSecret: curator-users # 外部 Secret，键名 htpasswd
  # basic 模式：
  # username / existingPasswordSecret

masterKey:
  existingSecret: ""            # 留空则 Chart 生成一个（仅适合试用）
  key: masterKey

persistence:
  data:                         # SQLite + 裁决副本
    size: 20Gi
    storageClass: ""
  cache:                        # 帧缓存，可用 emptyDir
    type: emptyDir              # emptyDir | pvc
    size: 200Gi

resources:
  requests: {cpu: "8",  memory: 32Gi}
  limits:   {cpu: "31", memory: 120Gi}    # 目标节点 32C128G，留出系统余量

concurrency:
  cpuCheck: 8
  decode: 8
  vlmEpisode: 32                # 见 04 篇

publicBaseUrl: ""               # 生成 Agent 可点链接用；为空则 links 返回空数组

pipelineConfigOverride: {}      # 挂成 ConfigMap，CURATION_CONFIG 指向它

ingress:
  enabled: false
  className: ""
  hosts: []
```

### 2.2 探针

```yaml
livenessProbe:   { httpGet: {path: /healthz, port: http}, periodSeconds: 10, failureThreshold: 6 }
readinessProbe:  { httpGet: {path: /readyz,  port: http}, periodSeconds: 5 }
startupProbe:    { httpGet: {path: /healthz, port: http}, failureThreshold: 60, periodSeconds: 5 }
```

- `/healthz`：进程活着（不查依赖）。
- `/readyz`：DB 可写 + 主密钥已加载 + 帧缓存目录可写。依赖不通就不接流量。
- **两个探针都豁免鉴权**（否则 401 会让 pod 反复重启，v1 注释里的教训）。
- `startupProbe` 给足 5 分钟：首次启动要跑 schema 迁移。

### 2.3 优雅停机

```yaml
terminationGracePeriodSeconds: 120
```

SIGTERM 后 Daemon：停止接新任务 → 给运行中的 CLI 子进程发 SIGTERM（它们会在
当前 episode 结束后落盘退出）→ 把任务置 `paused` 并持久化 → 退出。
超时未结束的子进程 SIGKILL，任务置 `failed` 并记明原因。

**不做**「长时间等任务跑完」：一个质检任务可能跑几小时，滚动升级不能等它。
`paused` + 可恢复是正确的语义。

## 3. 配置分层

```
出厂默认  backend/curation/pipeline/default.yaml
    ↓ 叠加
站点配置  ConfigMap → CURATION_CONFIG 指向的 site.yaml（只写与默认不同的部分）
    ↓ 叠加
任务参数  task.params（UI 上「高级」里的那些）
    ↓ 叠加
单次覆盖  --set k=v（调试用）
```

这一套是 v1 已有的设计（`pipeline/config.py`），原样保留并扩展到任务级。

## 4. 可观测性

| 面 | 做法 |
|---|---|
| 日志 | 结构化 JSON 到 stdout，字段：ts/level/task_id/subtask_id/module/msg。敏感字段脱敏 |
| 指标 | `/metrics`（Prometheus 格式）：任务数按状态、VLM 请求数/延迟分位、队列深度、token 累计 |
| 追踪 | 本期不接 APM；任务内的 stage 耗时已在性能剖析里 |
| 告警建议 | 队列深度持续 >N、VLM 429 率 >5%、磁盘使用 >85%、任务失败率突增 |

## 5. 部署前检查清单

1. TOS 桶存在、地区正确、AK/SK 有读写权限（凭证页会校验）。
2. 方舟 API Key 有效、目标模型已开通（后端页会拉模型列表）。
3. PVC 的 StorageClass 支持所需容量；**帧缓存所在卷不能是 FSX 挂载**
   （TOS FSX 拒绝随机写，PyAV 会 EINVAL —— v1 实锤）。
4. `CURATOR_MASTER_KEY` 已配置且已备份（丢了等于所有密钥要重填）。
5. `publicBaseUrl` 配好，否则 Agent 场景拿不到可点链接。
