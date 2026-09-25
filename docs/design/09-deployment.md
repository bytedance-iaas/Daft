# 09 镜像、部署与配置

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

## 2. Helm Chart：dataverse 里的质检台（D53）

质检台不单独出 Chart：它是 rerun 仓库 **`dataverse` Chart**（`deploy/helm/dataverse`，0.2.0 起）的一个组件，和 ReRun
（web viewer + catalog）一起装、共用一个 Secret、一个 APIG 网关和同一个域名。以后升级质检台，就是换 dataverse 的
`image.curator` 再 `helm upgrade`；本仓库只管镜像（§1）。自托管 vLLM 是 rerun 仓库里另一个独立的 `vllm` Chart，
和 dataverse 互不依赖：VLM 后端本来就是用户在质检台里添加的（08 篇），自托管的也只是其中一个 OpenAI 兼容地址。

dataverse 里属于质检台的对象（`<fullname>` 是 release 名，不含 `dataverse` 时补成 `<release>-dataverse`）：

```
templates/curation.yaml            # StatefulSet <fullname>-curation（单副本）+ 无头 Service（8080）
templates/configmap-curation.yaml  # <fullname>-curation-site：站点配置 site.yaml
templates/ingress-apig.yaml        # 同一域名的 /curation → 上面的 Service（不剥前缀）
```

两处和直觉不同的选择：

- **用 StatefulSet（replicas=1），不用 Deployment**。数据卷是 RWO 的块存储，SQLite 只能有一个进程写。
  Deployment 默认的滚动升级会先起新 Pod 再停旧 Pod：新 Pod 挂不上卷就卡死，挂上了就是双写。
  StatefulSet 先停后起，语义正好；将来「多实例分流」也是在它上面扩。
- **没有 schema 迁移的 Job**。迁移在 Daemon 启动时做（01 篇 §4.1）。pre-install 的 hook 跑的时候卷还不存在，
  pre-upgrade 的 hook 是另一个 Pod，挂不上正被占用的 RWO 卷 —— 这个 Job 从一开始就跑不起来。

### 2.1 值、写死的部分与环境变量

并入 dataverse 时做了精简（D53）：部署之间不会不同的东西写死在模板里，不再是值 —— 挂载前缀 `/curation`
（viewer 的「质检」按钮按同源的 `/curation` 拼链接）、端口 8080、三个探针（§2.2）、宽限期 120 秒与 preStop 5 秒（§2.3）、
运行用户 uid/gid 10001、不挂 ServiceAccount 令牌、关掉 Service 链接。质检台怎么跑（同时运行几个任务、每个任务开多少 worker）
是质检台自己的事，Chart 不设，用 Daemon 与 planner 的缺省（04 篇）。剩下的值：

```yaml
image:
  curator: <registry>/robot_curator:<提交号>   # 必填，完整引用；和 image.rerun 各走各的版本

curator:
  enabled: true
  publicBaseUrl: ""             # 生成给 Agent 的绝对链接用；为空则 links 给相对路径
  publicDatasets:               # HuggingFace 缓存桶；bucket 为空则新建页不出现这个数据来源
    bucket: ai-infra
    region: ""                  # 空 = tos.region
  siteConfig: {}                # 站点配置，原样写进 site.yaml（§3）；publicDatasets 并进来成为 public_datasets。
                                # 不写 concurrency：CPU 由质检台按容器配额自己分（D54），VLM 用 planner 缺省 64
  persistence:
    data:                       # /data：SQLite + 任务工作目录 + 库快照
      className: ebs-essd       # ⚠️ 必须是块存储（EBS）。NAS / TOS-FSX 这类网络盘会损坏 SQLite
      size: 100Gi
      existingClaim: ""         # 接管一块已有的盘（从独立 release 并入时用，不拷数据）
    scratch:                    # /scratch：导出时的视频临时文件、TOS 上 mcap / Lance 数据集的本地副本；可丢
      className: ebs-essd
      size: 500Gi
  resources:
    requests: {cpu: "16", memory: 128Gi}
    limits:   {cpu: "32", memory: 256Gi}   # limits.cpu 就是质检台的 CPU 配额：减 2 是全部任务共用的 CPU worker 数（D54）
  extraEnv: []                  # 其余 Daemon 设置，例如 CURATOR_WORK_RETENTION_DAYS、CURATOR_REASONING_EFFORT_TABLE（直接写 JSON）

vci:
  enabled: false                # 跑 VCI：Pod 注解，外加一个 root 初始化容器把两块盘交给 10001（VCI 不执行 fsGroup）
```

原来独立 Chart 里的其余可调项都去掉了：镜像三段式与拉取凭证、副本数（本来只能是 1）、单用户 basic 登录、
Chart 自动生成的试用主密钥、只会报错的定时备份占位、自带的 Service 类型与 Ingress、日志格式与级别、时区、调度与安全上下文的
逃生口、本地挂载路径来源、同时运行的任务数、维护模式（恢复备份改为缩容到 0 再起临时 Pod，`deploy/README.md` 第 8 节）。这些要么由 dataverse 统一处理，要么 Daemon 的缺省就是现网的取值，确实要改时用 `extraEnv`。

dataverse 给质检台容器的环境变量如下，这张表就是 Chart 和 Daemon 之间的约定：改名或删掉其中一项设置，要同时改 dataverse 的模板；
`backend/tests/deploy/test_env_contract.py` 检查表里每一项都有代码在读、Daemon 能接受这些取值。

| 环境变量 | 取值 | 来自 |
|---|---|---|
| `CURATOR_BASE_PATH` | `/curation` | 写死（§2.4） |
| `CURATOR_PUBLIC_BASE_URL` | `https://<对外域名>` | `curator.publicBaseUrl`，为空则不设 |
| `CURATOR_DATA_DIR` | `/data` | 写死，数据盘的挂载点 |
| `CURATOR_SCRATCH_DIR` | `/scratch` | 写死，临时盘的挂载点 |
| `CURATION_EXPORT_SCRATCH` | `/scratch` | 写死，导出的视频临时文件 |
| `CURATOR_SOURCE_CACHE_DIR` | `/scratch/source-cache` | 写死，mcap / Lance 的本地副本（D44） |
| `CURATION_CONFIG` | `/etc/curator/site/site.yaml` | ConfigMap `<fullname>-curation-site` 挂成文件 |
| `TOS_ENDPOINT` | `https://tos-s3-<region>.ivolces.com` | 由 `tos.region` 推导的内网端点；Daemon 只从中取地域和内外网 |
| `CURATOR_AUTH_MODE` | `htpasswd` | `web.basicAuth.enabled`；关掉时是 `none`（Chart 不允许它和公网入口同时出现） |
| `CURATOR_HTPASSWD_FILE` | `/etc/curator/auth/htpasswd` | Secret 的 `web_htpasswd` 挂成文件，和 viewer 共用 |
| `CURATOR_MASTER_KEY` | Secret 的 `curator_master_key` | secretKeyRef |
| `CURATOR_MASTER_KEY_NEXT` | Secret 的 `curator_master_key_next` | secretKeyRef，可缺（只在轮换时有，08 篇 §2.1） |
| `CURATOR_MASTER_KEY_VERSION` | Secret 的 `curator_master_key_version` | secretKeyRef，可缺（没有 = 第 1 版） |

镜像里还固定了 `CURATOR_STATIC_DIR=/app/web`（网页控制台）和 `CURATOR_CONTRACTS_DIR=/app/docs/contracts`（契约文件），Chart 不改它们。
Daemon 的其余设置（端口、日志、时区、SSE 心跳、工作目录保留天数、同时运行的任务数）用 Daemon 的缺省：同时运行 3 个任务，
CPU worker 总数是容器 CPU 配额（`resources.limits.cpu`）减 2，由 Daemon 的全局 CPU 池在任务之间分（D54，04 篇 §2.1、§2.3）。
早于 D54 的 dataverse 写进 site.yaml 的 `concurrency.cpu` / `cpuMax` 会被忽略并在日志里告警，不影响启动；VCI 上按 limits 计费。

dataverse 的全部密钥在一个 Secret 里，但**每个组件只挂自己要读的键**：质检台只拿 `web_htpasswd` 和 `curator_master_key*`，
viewer、catalog 和原生会话（面向用户的桌面）都读不到主密钥。

**内存上不要指望子进程隔离**。CLI 子进程和 Daemon 在同一个容器、同一个 cgroup 里，容器内存触顶时
内核挑谁杀不由我们定。三件事降低 Daemon 被误杀的概率：启动子进程时把它的 `oom_score_adj` 调到 +500、
Daemon 自己 −500；帧档按 RSS 准入（04 篇 §7）；`limits.memory` 给足。真被整个带走了，StatefulSet 把 Pod 拉起来，
任务从检查点自动续跑。

数据卷从 20Gi 调到 100Gi，是因为任务工作目录也放在这里（00 篇 §4.2）：每个任务的结果、证据帧、日志，
MB 到 GB 级，终态 7 天后清理。原设计里的 200Gi 帧缓存卷已经取消（D18，不再有帧缓存），
同样大小的空间改作临时盘：导出时的视频临时文件，以及 TOS 上 mcap / Lance 数据集的本地副本（D44）。
dataverse 里它也是块存储盘，不用 emptyDir：VCI 的系统盘固定 40 GiB，而且不认 `ephemeral-storage` 上限。

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

- 前缀经环境变量 `CURATOR_BASE_PATH` 传给 Daemon（兼容旧名 `CURATION_UI_ROOT_PATH`），dataverse 写死为 `/curation`（§2.1）；
  Daemon 把 `curation`、`/curation/` 都归一化为 `/curation`（本机调试时自己设）。
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
2. 方舟 API Key 有效、目标模型已开通（添加模型时会用一次最小请求确认；任务开始前还会再查一次）。
3. **数据卷必须是块存储（EBS）**。SQLite 的 WAL 依赖共享内存和可靠的文件锁，放在 NAS 或 TOS-FSX
   这类网络文件系统上会损坏。临时卷同样不能是 FSX 挂载：TOS FSX 拒绝随机写，PyAV 收尾时要 seek 回文件头，
   会 EINVAL（v1 实锤）。
4. dataverse Secret 里的 `curator_master_key` 已配置且已备份（丢了等于所有密钥要重填）。
5. `curator.publicBaseUrl` 配好，否则 Agent 场景拿到的是相对路径。
6. 前缀固定为 `/curation`，dataverse 的 Ingress 已按不剥前缀写好，不用配。
7. 和 rerun viewer 免二次登录：dataverse 让两边同域、共用 Secret 里的 `web_htpasswd`；realm 不要改。

## 6. 备份与恢复

平台自己的状态只有两样东西丢不起，TOS 上的交付物不在其列（它们本来就在对象存储里）：

| 东西 | 丢了会怎样 | 怎么备 |
|---|---|---|
| SQLite 库 | 任务历史、密钥（密文）、裁决记录全没了。裁决在交付目录里还有一份 CSV 副本，其余没有 | 设计：每天一次，Daemon 内执行 `VACUUM INTO` 出一份一致性快照到数据卷，再上传到 TOS，保留最近 14 份；CronJob 只负责调 Daemon 的内部接口触发，不自己碰卷。Daemon 还没有这个接口，dataverse 也就没有这一项，现在按 `deploy/README.md` 第 8 节手动做 |
| 主密钥 | 库里的密钥全部解不开，只能逐个重填 | 部署时由运维另行保管；Chart 不替用户备份它 |

恢复：停 Daemon（StatefulSet 缩到 0）→ 把快照放回数据卷 → 起 Daemon（启动对账会把当时在跑的任务置为系统暂停并恢复）。
步骤见 `deploy/README.md` 第 8 节。
