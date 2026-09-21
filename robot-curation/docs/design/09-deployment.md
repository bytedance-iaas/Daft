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

另外：`curl` + `tini` 进镜像（pod 内探端点、信号转发）；`oniond` 继续装（`curation fetch` 靠它下载公开数据集）；
gradio 和 xterm 相关依赖**全部移除**。

## 2. Helm Chart

```
deploy/charts/curator/
├── Chart.yaml
├── values.yaml
├── templates/
│   ├── statefulset.yaml       # 单副本，volumeClaimTemplates 带出数据卷
│   ├── service.yaml
│   ├── ingress.yaml           # 可选
│   ├── secret.yaml            # 主密钥（可由外部 Secret 接管）
│   ├── configmap.yaml         # 站点配置：流水线 YAML 覆盖、公共数据集、模型能力表
│   ├── cronjob-backup.yaml    # 可选：触发库备份，见 §6
│   └── NOTES.txt
└── README.md
```

两处和直觉不同的选择：

- **用 StatefulSet（replicas=1），不用 Deployment**。数据卷是 RWO 的块存储，SQLite 只能有一个进程写。
  Deployment 默认的滚动升级会先起新 Pod 再停旧 Pod：新 Pod 挂不上卷就卡死，挂上了就是双写。
  StatefulSet 先停后起，语义正好；将来「多实例分流」也是在它上面扩。
- **没有 schema 迁移的 Job**。迁移在 Daemon 启动时做（01 篇 §4.1）。pre-install 的 hook 跑的时候卷还不存在，
  pre-upgrade 的 hook 是另一个 Pod，挂不上正被占用的 RWO 卷 —— 这个 Job 从一开始就跑不起来。

### 2.1 values.yaml 骨架

```yaml
image:
  repository: <registry>/curator
  tag: ""                       # 默认用 Chart.appVersion
  pullPolicy: IfNotPresent

replicaCount: 1                 # ⚠️ 单副本。>1 目前不支持（SQLite 各副本独立）

server:
  basePath: ""                  # 挂载前缀，现网为 /curation，见 §2.4
  publicBaseUrl: ""             # 生成给 Agent 的绝对链接用；为空则 links 给相对路径
  maxRunningTasks: 1            # 同时运行的任务数，其余排队，见 04 篇 §2.3

auth:
  mode: htpasswd                # htpasswd | basic
  htpasswdSecret: curator-users # 外部 Secret，键名 htpasswd；可与 rerun viewer 共用同一张表
  # basic 模式：
  # username / existingPasswordSecret

masterKey:
  existingSecret: ""            # 留空则 Chart 生成一个（仅适合试用）
  key: masterKey

persistence:
  data:                         # SQLite + 任务工作目录 + 裁决副本 + 库备份
    size: 100Gi
    storageClass: ""            # ⚠️ 必须是块存储（EBS）。NAS / TOS-FSX 这类网络盘会损坏 SQLite
  scratch:                      # 导出时的视频临时文件；可丢
    type: emptyDir              # emptyDir | pvc
    size: 200Gi

resources:
  requests: {cpu: "8",  memory: 32Gi}
  limits:   {cpu: "30", memory: 110Gi}    # 目标节点 32C128G；按节点 allocatable 留余量，别照抄规格

publicDatasets:                 # HuggingFace 缓存桶；不配则新建页不出现这个数据来源
  bucket: ""
  region: ""
  prefix: dataset
  manifest: dataset_files.json
  label: HuggingFace 缓存桶

localDataRoot: ""               # 留空 = 关闭「本地挂载路径」来源；配了则只允许这个根目录之下

concurrency:                    # 站点级默认，见 04 篇 §2
  cpu: 8
  vlmParallelism: 64

backup:
  enabled: false
  schedule: "0 3 * * *"
  tosUri: ""                    # 备份上传到哪；需要一个有写权限的访问密钥名
  credential: ""

pipelineConfigOverride: {}      # 挂成 ConfigMap，CURATION_CONFIG 指向它

ingress:
  enabled: false
  className: ""
  hosts: []
```

数据卷从 20Gi 调到 100Gi，是因为任务工作目录也放在这里（00 篇 §4.2）：每个任务的结果、证据帧、日志，
MB 到 GB 级，终态 7 天后清理。原设计里的 200Gi 帧缓存卷已经取消（D18，不再有帧缓存），
同样大小的空间改作导出时的视频临时区，用 emptyDir 即可，丢了只是重新导出。

### 2.2 探针

```yaml
livenessProbe:   { httpGet: {path: /healthz, port: http}, periodSeconds: 10, failureThreshold: 6 }
readinessProbe:  { httpGet: {path: /readyz,  port: http}, periodSeconds: 5 }
startupProbe:    { httpGet: {path: /healthz, port: http}, failureThreshold: 60, periodSeconds: 5 }
```

- `/healthz`：进程活着（不查依赖）。
- `/readyz`：DB 可写 + 主密钥已加载 + 工作目录与临时卷可写 + 启动对账已完成。依赖不通就不接流量。
- **两个探针都豁免鉴权**（否则 401 会让 pod 反复重启，v1 注释里的教训）。
  探针走根路径；挂载前缀下的同名路径也在，给经 APIG 进来的外部探测用。
- `startupProbe` 给足 5 分钟：启动时要跑 schema 迁移和任务状态对账（01 篇 §3.2）。

### 2.3 优雅停机

```yaml
terminationGracePeriodSeconds: 120
```

SIGTERM 后 Daemon：停止接新任务 → 给运行中的 CLI 子进程发 SIGTERM（它们会在
当前 episode 结束后落盘退出）→ 把任务置 `paused`（`pause_reason=system`）并持久化 → 退出。
90 秒还没退出的子进程 SIGKILL，**任务同样置 `paused(system)`，不是 `failed`**：
结果是逐条落盘的，被杀掉的只是手上那一条，恢复时 `--resume` 会把它重跑。
一个跑了五小时的任务，不该因为一次升级时有一条没来得及收尾就算失败。

新 Pod 起来后，系统暂停的任务**自动恢复**入队（01 篇 §3.2）；用户自己暂停的不动。

**不做**「长时间等任务跑完」：一个质检任务可能跑几小时，滚动升级不能等它。
`paused` + 可恢复是正确的语义。

### 2.4 挂载前缀

现网的 APIG 把 `/curation/**` 分流到本服务，而且**不剥前缀**（v1 `ui/app.py` 注释里写明了这一点：
这和「网关剥前缀、后端设 ASGI root_path」的场景正好相反）。所以做法和 v1 一致：

- `server.basePath` 经环境变量 `CURATOR_BASE_PATH` 传给 Daemon（兼容旧名 `CURATION_UI_ROOT_PATH`）；
  写成 `curation`、`/curation/` 都归一化为 `/curation`。
- 全部路由**实际注册在前缀之下**：`{base}/api/v1/**`、`{base}/events/**`、`{base}/` 静态资源与 SPA 回退。
  不是设置 ASGI 的 `root_path`。
- `/healthz`、`/readyz` 在根路径和 `{base}` 下各有一份，都免鉴权。
- `{base}/?dataset=…` 是 v1 的旧深链入口，302 到 `{base}/tasks/new?…`，参数原样带上。
- 前端怎么适配见 07 篇 §2.3。

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
| 指标 | `/metrics`（Prometheus 格式）：任务数按状态、VLM 请求数/延迟分位、队列深度、token 累计、降并发事件数。免鉴权，但只在集群内端口监听，不经 Ingress / APIG 暴露 |
| 追踪 | 本期不接 APM；任务内的 stage 耗时已在性能剖析里 |
| 告警建议 | 队列深度持续 >N、VLM 429 率 >5%、磁盘使用 >85%、任务失败率突增 |

## 5. 部署前检查清单

1. TOS 存储桶存在、地域正确、访问密钥有读写权限（密钥管理页会校验）。
2. 方舟 API Key 有效、目标模型已开通（添加模型时会用一次最小请求确认）。
3. **数据卷必须是块存储（EBS）**。SQLite 的 WAL 依赖共享内存和可靠的文件锁，放在 NAS 或 TOS-FSX
   这类网络文件系统上会损坏。临时卷同样不能是 FSX 挂载：TOS FSX 拒绝随机写，PyAV 收尾时要 seek 回文件头，
   会 EINVAL（v1 实锤）。
4. `CURATOR_MASTER_KEY` 已配置且已备份（丢了等于所有密钥要重填）。
5. `server.publicBaseUrl` 配好，否则 Agent 场景拿到的是相对路径。
6. 经 APIG 分流且不剥前缀的，`server.basePath` 要和分流路径一致（现网 `/curation`）。
7. 想和 rerun viewer 免二次登录：同域、共用同一张 htpasswd、realm 不要改。

## 6. 备份与恢复

平台自己的状态只有两样东西丢不起，TOS 上的交付物不在其列（它们本来就在对象存储里）：

| 东西 | 丢了会怎样 | 怎么备 |
|---|---|---|
| SQLite 库 | 任务历史、密钥（密文）、裁决记录全没了。裁决在交付目录里还有一份 CSV 副本，其余没有 | `backup.enabled` 后每天一次：Daemon 内执行 `VACUUM INTO` 出一份一致性快照到数据卷，再上传到 `backup.tosUri`，保留最近 14 份。CronJob 只负责调 Daemon 的内部接口触发，不自己碰卷 |
| 主密钥 | 库里的密钥全部解不开，只能逐个重填 | 部署时由运维另行保管；Chart 不替用户备份它 |

恢复：停 Pod → 把快照放回数据卷 → 起 Pod（启动对账会把当时在跑的任务置为系统暂停并恢复）。步骤写进 README。
