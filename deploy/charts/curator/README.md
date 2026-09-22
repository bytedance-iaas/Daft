# curator Chart

Curator v2 的 Helm Chart：单副本 StatefulSet，API Daemon 同进程托管网页控制台，SQLite 和任务工作目录在块存储数据卷上。
设计见 `docs/design/09-deployment.md` §2；构建、安装、升级、建 Secret、主密钥轮换、备份恢复、部署前检查清单都在 [`deploy/README.md`](../../README.md)。

## 装出来的东西

| 资源 | 名字（release 名为 `curator` 时） | 说明 |
|---|---|---|
| StatefulSet | `curator` | 固定 1 个副本；先停旧 Pod 再起新 Pod，数据卷上永远只有一个 Daemon |
| PVC | `data-curator-0` | 数据卷（`volumeClaimTemplates`），`helm uninstall` 不会删它 |
| Service | `curator`、`curator-headless` | 前者接流量（端口名 `http`），后者只给 StatefulSet 做 Pod 域名 |
| ConfigMap | `curator-site` | 站点配置 `site.yaml`（`CURATION_CONFIG` 指向它），可选的思考强度映射表 |
| Secret | `curator-master-key` | 只在没有配 `masterKey.existingSecret` 时生成（试用）；带 `helm.sh/resource-policy: keep`，卸载也不删 |
| Ingress | `curator` | 可选（`ingress.enabled`） |

登录账号表（htpasswd）和生产用的主密钥都是**外部 Secret**，Chart 只引用、不生成，也没有任何值可以把密钥明文写进 values。

## 值与 Daemon 配置的对应

| 值 | 默认 | 变成 | 说明 |
|---|---|---|---|
| `image.repository` / `tag` / `digest` | 示例值 / `appVersion` / 空 | 镜像 | 仓库地址必改 |
| `imagePullSecrets` | `[]` | Pod | 私有仓库的拉取凭证 |
| `replicaCount` | `1` | StatefulSet | 只能是 1，别的值直接报错 |
| `server.basePath` | 空 | `CURATOR_BASE_PATH` | 现网 `/curation`；`curation`、`/curation/` 都归一为 `/curation`；也是 Ingress 的路径 |
| `server.publicBaseUrl` | 空 | `CURATOR_PUBLIC_BASE_URL` | 给 Agent 的绝对链接 |
| `server.port` | `8080` | `CURATOR_PORT`、容器端口 | |
| `server.tzOffset` | `+08:00` | `CURATOR_TZ_OFFSET` | 概览按它切分每一天 |
| `server.sseHeartbeatSeconds` | 空（15） | `CURATOR_SSE_HEARTBEAT_S` | 网关空闲超时比 15 秒短时调小 |
| `server.tosEndpoint` | 空 | `TOS_ENDPOINT` | 同地域走内网端点 |
| `server.maxRunningTasks` | `1` | —— | Daemon 还不读，只能是 1 |
| `logging.format` / `level` | `json` / `INFO` | `CURATOR_LOG_FORMAT` / `CURATOR_LOG_LEVEL` | |
| `auth.mode` | `htpasswd` | `CURATOR_AUTH_MODE` | `htpasswd` 或 `basic`，配置不全时 Daemon 拒绝所有请求 |
| `auth.htpasswdSecret` / `htpasswdKey` | `curator-users` / `htpasswd` | 挂成文件，`CURATOR_HTPASSWD_FILE=/etc/curator/auth/htpasswd` | 可与 rerun viewer 共用 |
| `auth.username` / `existingPasswordSecret` / `passwordKey` | 空 / 空 / `password` | `CURATOR_AUTH_USER` / `CURATOR_AUTH_PASSWORD`（secretKeyRef） | basic 模式，两个都要配 |
| `masterKey.existingSecret` | 空（Chart 生成） | —— | 生产环境必配 |
| `masterKey.key` / `nextKey` / `versionKey` | `masterKey` / `masterKeyNext` / `masterKeyVersion` | `CURATOR_MASTER_KEY` / `_NEXT` / `_VERSION`（secretKeyRef，后两个可缺） | 轮换见 deploy/README.md |
| `persistence.data.*` | 100Gi、集群默认存储类、`/data` | PVC、`CURATOR_DATA_DIR` | 必须是块存储（EBS）；`existingClaim` 改用已有 PVC |
| `persistence.scratch.*` | emptyDir 200Gi、`/scratch` | `CURATOR_SCRATCH_DIR`、`CURATION_EXPORT_SCRATCH` | `type: pvc` 改用块存储卷；不能是 FSX |
| `publicDatasets.*` | 桶为空（关闭） | `site.yaml` 的 `public_datasets` | HuggingFace 缓存桶 |
| `localDataRoot` | 空（关闭） | `CURATOR_LOCAL_DATA_ROOT` | 目录用 `extraVolumes` 挂进来 |
| `concurrency.*`、`vlm.*` | 04 篇 §2 的默认 | `site.yaml` 的 `concurrency`、`vlm` | planner 的站点默认与上限、合并开关、闸门覆盖 |
| `reasoningEffortTable` | `[]` | `reasoning-effort.json`，`CURATOR_REASONING_EFFORT_TABLE` | 思考强度映射表的站点覆盖 |
| `pipelineConfigOverride` | `{}` | `site.yaml` | 流水线站点配置；和上面几段同名的键以上面为准 |
| `terminationGracePeriodSeconds` / `preStopSleepSeconds` | `120` / `5` | Pod | 宽限期至少 preStop + 100 秒，否则报错 |
| `probes.*` | 09 篇 §2.2 | 三个探针 | 路径固定：`/healthz`、`/readyz` |
| `podSecurityContext` / `securityContext` | uid/gid 10001、不提权、去掉全部 capability | Pod / 容器 | 与镜像里的用户一致 |
| `maintenance.enabled` | `false` | 容器只跑 `sleep infinity` | 恢复备份等离线操作 |
| `backup.*` | 关闭 | —— | Daemon 还没有备份接口，开了报错 |
| `ingress.*`、`service.*` | 关闭、ClusterIP 8080 | Ingress、Service | Ingress 按前缀匹配 `server.basePath`，不改写路径 |
| `extraEnv`、`extraVolumes`、`extraVolumeMounts`、`nodeSelector`、`tolerations`、`affinity`、`podAnnotations`、`podLabels`、`priorityClassName`、`serviceAccountName` | 空 | Pod | 常规的逃生口 |

镜像里还固定了 `CURATOR_STATIC_DIR=/app/web`（网页控制台）和 `CURATOR_CONTRACTS_DIR=/app/docs/contracts`（契约文件），Chart 不改它们。

Pod 关掉了 Service 链接（`enableServiceLinks: false`）：否则名叫 `curator` 的 Service 会注入 `CURATOR_PORT=tcp://…`，正好撞上 Daemon 自己的端口配置，进程起不来。

## 不连集群的检查

```bash
helm lint deploy/charts/curator
helm template curator deploy/charts/curator -n curator --set server.basePath=/curation
.venv/bin/python -m pytest -q backend/tests/deploy       # 渲染出来的清单与 Daemon 的配置逐项对照
```
