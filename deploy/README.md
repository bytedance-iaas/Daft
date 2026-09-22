# 部署：镜像与 Helm Chart（W11 / F4.1）

一个镜像、一个进程、一个端口：API Daemon（`curator-daemon`）同进程托管网页控制台，质检 CLI（`curation`）由它以子进程调用。
K8s 上用 `charts/curator`：单副本 StatefulSet，SQLite 和任务工作目录在块存储数据卷上。
设计依据：`docs/design/09-deployment.md`（镜像、Chart、探针、优雅停机、挂载前缀、检查清单、备份），
`08-secrets-and-auth.md`（主密钥与账号），`00-overview.md` §3（部署拓扑）。

| 路径 | 内容 |
|---|---|
| `deploy/Dockerfile` | 多阶段镜像：Node 20 构建控制台，Python 3.10（bookworm）运行时 |
| `deploy/docker-entrypoint.sh` | 镜像入口：不带参数或只带选项时起 Daemon，其余原样执行 |
| `deploy/charts/curator/` | Helm Chart，值与 Daemon 配置的对应见它的 [README](charts/curator/README.md) |
| 仓库根 `.dockerignore` | 构建上下文里挡掉 `node_modules`、`.venv`、`.git`、`.claude` 等 |
| `backend/tests/deploy/` | 不连集群、不用 docker 的检查（见文末） |

下文命令里的 `curator` 既是命名空间也是 release 名；换名字的话，Secret、Pod、PVC 的名字跟着变（`<release>-master-key`、`<release>-0`、`data-<release>-0`）。

## 1. 构建镜像

构建上下文是仓库根，Dockerfile 路径是 `deploy/Dockerfile`（火山 CI：ContextPath 填仓库根 `.`；从 v1 流水线复制来的
`robot-curation` 会让 buildkit 报 `lstat robot-curation: no such file or directory`）。火山 CP 流水线的镜像版本用
`${SCM_COMMIT_ID}`，即完整提交号，与 v1 相同。代码源触发靠 Webhook：推送后流水线没动静，先查 GitHub 仓库的
Webhook 是否指向触发器的 HookURL，没有就先在控制台手动运行。

火山网络之外（GitHub Actions、自己的电脑）：内网源一律不可达，用公网源并跳过 oniond 那一层：

```bash
docker build -f deploy/Dockerfile -t curator:dev \
  --build-arg NO_MIRROR=1 \
  --build-arg PIP_INDEX_URL=https://pypi.org/simple \
  --build-arg SUPPLEMENT_PYPI_URL= \
  --build-arg APT_MIRROR=http://deb.debian.org .
```

火山内网（火山 CI）：缺省值就是为它定的；拉 Docker Hub 或 npm 官方源慢时再换源：

```bash
docker build -f deploy/Dockerfile -t <镜像仓库>/curator:<版本> \
  --build-arg APT_MIRROR=http://mirrors.ivolces.com \
  --build-arg BASE_IMAGE=hub-cache-cn-beijing.cr.volces.com/library/python:3.10-slim-bookworm \
  --build-arg NODE_IMAGE=hub-cache-cn-beijing.cr.volces.com/library/node:20-bookworm-slim \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com .
docker push <镜像仓库>/curator:<版本>
```

| 构建参数 | 缺省 | 什么时候改 |
|---|---|---|
| `BASE_IMAGE` | `python:3.10-slim-bookworm` | 拉 Docker Hub 慢时换火山的缓存地址；**必须保持 bookworm**（trixie 上没有 oniond 的源） |
| `NODE_IMAGE` | `node:20-bookworm-slim` | 同上；只用于构建控制台，不进最终镜像 |
| `NPM_REGISTRY` | 空 = lock 文件里的 registry.npmjs.org | 官方源慢时，例如 `https://registry.npmmirror.com`（火山镜像站没有 npm 源） |
| `PIP_INDEX_URL` | `https://mirrors.ivolces.com/pypi/simple`（火山内网） | 火山网络之外必须改：`https://pypi.org/simple` 或 `https://mirrors.volces.com/pypi/simple` |
| `SUPPLEMENT_PYPI_URL` | `https://mirrors.aliyun.com/pypi/simple/`（阿里云） | 火山源缺的版本从这里补：只装 `deploy/pip-extra-index.txt` 里列的几个及其依赖（现在是 uvicorn 0.53.0、cryptography 50.0.1，火山内网源只到 0.44.0、49.0.0），其余全走 `PIP_INDEX_URL`；火山源补齐后删掉对应那一行。传空则只用 `PIP_INDEX_URL` |
| `APT_MIRROR` | `https://mirrors.volces.com`（公网可达） | 火山内网可改 `http://mirrors.ivolces.com`，更快；缺省值不要改成内网域名，公开 CI 会直接失败 |
| `NO_MIRROR` | 空 | 火山网络之外设为 `1`：跳过 oniond（`curation fetch` 不可用，其余照常） |

验证镜像：

```bash
docker run --rm curator:dev --help                 # 只给选项 = 交给 curator-daemon，打印 Daemon 的帮助
docker run --rm curator:dev curation --help        # CLI 照常可用
docker run --rm curator:dev id                     # uid=10001(curator) gid=10001(curator)
CURATOR_MASTER_KEY=$(openssl rand -base64 32) docker run --rm -e CURATOR_MASTER_KEY curator:dev --check-config
```

最后一条用一把一次性的主密钥校验配置，预期日志 `configuration ok`、退出码 0；`-e CURATOR_MASTER_KEY` 不带值，密钥不会出现在命令行参数里。
不带主密钥时 Daemon 以退出码 2 拒绝启动（08 篇 §2）。

在本机直接跑（不上 K8s，只做调试）：

```bash
docker volume create curator-data
export CURATOR_MASTER_KEY=$(openssl rand -base64 32) CURATOR_AUTH_USER=demo
export CURATOR_AUTH_PASSWORD=$(python3 -c 'import getpass; print(getpass.getpass("登录密码："))')
docker run --rm -p 8080:8080 -v curator-data:/data -e CURATOR_BASE_PATH=/curation \
  -e CURATOR_MASTER_KEY -e CURATOR_AUTH_USER -e CURATOR_AUTH_PASSWORD curator:dev
# 浏览器打开 http://127.0.0.1:8080/curation/ ，账号 demo
```

主密钥决定库里的密钥能不能解开：同一个数据卷下次再跑要用同一把。

## 2. 部署前检查清单（09 篇 §5）

1. **TOS**：存储桶存在、地域正确、访问密钥有读写权限。装好后在「密钥与资源」页添加访问密钥时会校验身份；任务开始前的三项检查会实测输入能读、输出能写。
2. **方舟**：API Key 有效、目标模型已开通。添加 VLM 后端时会拉模型列表或发一次最小请求；任务开始前还会再实调一次。
3. **数据卷必须是块存储（EBS）**。SQLite 的 WAL 依赖共享内存和可靠的文件锁，放在 NAS 或 TOS-FSX 这类网络文件系统上会损坏。
   `kubectl get storageclass` 看准 provisioner 是块存储，把名字填进 `persistence.data.storageClass`（留空用集群默认的那个，先确认它是块存储）。
   块存储卷属于某个可用区，存储类最好是 `volumeBindingMode: WaitForFirstConsumer`。临时卷（`persistence.scratch`）同样不能是 FSX 挂载：PyAV 收尾时要 seek 回文件头，FSX 拒绝随机写。
4. **主密钥已配置且已备份**（下一节）。丢了它，库里保存的访问密钥和 API Key 全部解不开，只能逐个重填。
5. **`server.publicBaseUrl` 配好**，否则 Agent 拿到的链接是相对路径。
6. **经 APIG 分流且不剥前缀**：`server.basePath` 和分流路径一致（现网 `/curation`），见第 6 节。
7. **想和 rerun viewer 免二次登录**：同域、共用同一张 htpasswd、realm 不改（Daemon 固定为 `Robot Data Curation`）。
8. **节点规格**：缺省按 32 核 128G 节点给 `requests 8 核 / 32Gi`、`limits 30 核 / 110Gi`，照节点的 allocatable 调，别照抄规格。
9. **集群没有公网出口**：配 `server.tosEndpoint` 为部署地域的 TOS 内网端点（如 `tos-cn-beijing.ivolces.com`），否则 Daemon 和 CLI 都连不上 TOS。
10. **镜像能拉到**：`image.repository` / `image.tag` 填真实值；私有仓库按 VKE 的镜像仓库凭证配置（或自己建 `kubernetes.io/dockerconfigjson` 类型的 Secret），名字写进 `imagePullSecrets`。

## 3. 创建 Secret

Chart 不接受任何明文密钥，只引用已有的 Secret。下面的命令都经文件传值，密钥不进命令行参数和 shell 历史。

```bash
kubectl create namespace curator
umask 077                                  # 下面生成的文件只有自己能读
```

**主密钥**（`CURATOR_MASTER_KEY`，32 字节，base64）：

```bash
openssl rand -base64 32 | tr -d '\n' > master.key
kubectl -n curator create secret generic curator-master-key --from-file=masterKey=master.key
```

把 `master.key` 存进团队的密钥保管处，确认存好之后删掉本地文件（`rm -f master.key`）。
values 里写 `masterKey.existingSecret: curator-master-key`。同一个 Secret 里还会用到两个可选的键：`masterKeyNext`（轮换中的新主密钥）、`masterKeyVersion`（当前主密钥的版本号，没有 = 1），见第 7 节。

> 不配 `masterKey.existingSecret` 时 Chart 会自己生成一个（Secret `<release>-master-key`，卸载时不删），只适合试用；
> 装完 NOTES 会提示怎么把它导出来备份。

**登录账号表**（htpasswd，推荐）：bcrypt（`$2y$`/`$2b$`/`$2a$`）和 apr1 两种哈希，可以和 rerun viewer 的 nginx 共用同一张表。

```bash
htpasswd -cB curator-users.htpasswd alice        # 交互输入密码；再加账号去掉 -c
# 没有 htpasswd 命令时：printf 'alice:%s\n' "$(openssl passwd -apr1)" >> curator-users.htpasswd
kubectl -n curator create secret generic curator-users --from-file=htpasswd=curator-users.htpasswd
```

values 缺省就是 `auth.mode: htpasswd`、`auth.htpasswdSecret: curator-users`，Secret 不存在时 Pod 起不来（停在 ContainerCreating）。
改账号表之后要重启一次才生效（Daemon 启动时读一次）：

```bash
kubectl -n curator create secret generic curator-users --from-file=htpasswd=curator-users.htpasswd \
  --dry-run=client -o yaml | kubectl -n curator replace -f -
kubectl -n curator rollout restart statefulset/curator
```

**单用户模式**（兼容存量，二选一）：

```bash
python3 -c 'import getpass; print(getpass.getpass("登录密码："), end="")' > login.password
kubectl -n curator create secret generic curator-login --from-file=password=login.password
rm -f login.password
```

values：`auth.mode: basic`、`auth.username: <登录名>`、`auth.existingPasswordSecret: curator-login`。两个都要配，只配一个 Chart 直接报错。

## 4. 安装

`curator-values.yaml`（只写和缺省不同的部分）：

```yaml
image:
  repository: <镜像仓库>/curator
  tag: "<版本>"
server:
  basePath: /curation
  publicBaseUrl: https://<对外域名>
  tosEndpoint: tos-cn-beijing.ivolces.com      # 按部署地域
masterKey:
  existingSecret: curator-master-key
persistence:
  data:
    storageClass: <块存储的存储类>
```

```bash
helm install curator deploy/charts/curator -n curator -f curator-values.yaml
kubectl -n curator rollout status statefulset/curator --timeout=10m
kubectl -n curator get pod curator-0 -o wide
kubectl -n curator get pvc data-curator-0        # Bound，容量 100Gi
```

验证（另开一个终端做端口转发）：

```bash
kubectl -n curator port-forward svc/curator 8080:8080
curl -s localhost:8080/healthz                                        # {"status":"ok"}
curl -s localhost:8080/curation/readyz | python3 -m json.tool          # checks 五项全是 true
curl -si localhost:8080/curation/api/v1/tasks | head -3                # 401，Basic realm="Robot Data Curation"
curl -s -u alice localhost:8080/curation/api/v1/tasks                  # 按提示输入密码；返回任务列表 JSON
kubectl -n curator logs curator-0 | head -5                            # 每行一个 JSON；含 authentication: htpasswd, N account(s)
```

浏览器打开 http://127.0.0.1:8080/curation/ ，用账号表里的账号登录。

## 5. 升级

```bash
helm upgrade curator deploy/charts/curator -n curator -f curator-values.yaml --set image.tag=<新版本>
kubectl -n curator rollout status statefulset/curator --timeout=10m
```

升级时发生的事（09 篇 §2.3）：StatefulSet 先停旧 Pod —— preStop 等 5 秒让 Service / 网关摘掉它，然后 SIGTERM；
Daemon 不再接新任务，让运行中的 CLI 子进程收尾当前 episode（最多 90 秒，超时的被杀掉，那一条恢复时重跑），
把任务置为系统暂停并退出；宽限期 120 秒兜底。新 Pod 挂上同一个数据卷，启动时迁移数据库、做状态对账，
系统暂停的任务自动重新排队续跑；用户自己暂停的保持暂停，用户停止的不恢复。
停机时间约等于 preStop + 收尾 + 新 Pod 启动，通常一两分钟。

验证：升级前有一个任务在运行，升级后看它的执行时间线，依次有 `system_pause`（任务被系统暂停）和 `system_resume`：

```bash
curl -s -u alice localhost:8080/curation/api/v1/tasks/<任务 id>/timeline | python3 -m json.tool
```

几点注意：

- 数据卷由 `volumeClaimTemplates` 建出，建好以后不能再改：升级时别动 `persistence.data.size` / `storageClass`，否则 helm 报
  `updates to statefulset spec for fields other than ... are forbidden`。扩容直接改 PVC（存储类要允许扩容）：
  `kubectl -n curator patch pvc data-curator-0 -p '{"spec":{"resources":{"requests":{"storage":"200Gi"}}}}'`。
- 改站点配置（`concurrency`、`vlm`、`publicDatasets`、`pipelineConfigOverride`、`reasoningEffortTable`）也会滚动重启 Pod，同样走系统暂停再续跑。
- 节点排水（drain）、Pod 被驱逐都等同一次重启；块存储卷会跟着 Pod 挂到同一可用区的新节点上。

## 6. 经 APIG 分流（现网）与 Ingress

现网的 APIG 把 `/curation/**` 分流到本服务，**不剥前缀**（09 篇 §2.4）：

- 上游指向 Service `curator` 的 8080 端口（VKE 服务），路径原样转发、不改写；`server.basePath` 必须是 `/curation`。
  Daemon 把全部路由注册在前缀之下：`/curation/api/v1/**`、`/curation/events/**`、`/curation/` 的控制台（刷新任意页面不 404）。
- 网关的健康检查用 `GET /curation/healthz`（免鉴权）；`/curation/readyz` 在数据库不可写、工作目录不可写或启动对账没做完时返回 503。
- SSE（`/curation/events/tasks/{id}`）是长连接，Daemon 空闲时每 15 秒发一行心跳：网关的空闲超时要大于它，否则调小 `server.sseHeartbeatSeconds`。
  响应带 `X-Accel-Buffering: no`；网关若会缓冲响应体，要对这条路径关掉。
- v1 的旧深链 `/curation/?dataset=…` 由 Daemon 302 到新建任务页，参数原样带上。
- 不经 APIG 时可以用 Ingress：`ingress.enabled: true`、`ingress.hosts: [{host: <域名>}]`，路径就是 `server.basePath`（前缀匹配，不改写）。
- VKE 的 APIG 也可以直接接 Ingress：`ingress.className` 填 APIG 实例绑定的类名，`ingress.annotations` 带上
  `ingress.vke.volcengine.com/apig-instance-name` 与 `ingress.vke.volcengine.com/loadbalancer-id`（照同一实例上已有的
  Ingress 抄）。这个 APIG 实例的监听命名空间（`apiginstances` 资源的 `spec.ingress.watchNamespaces`）必须包含部署所在的
  命名空间，否则 Ingress 不会被接管，`kubectl get ingress` 的 ADDRESS 一直为空。实例是共享配置，找它的维护者加。
  Ingress 的后端要按端口号引用 Service（Chart 已这样写）：按端口名引用时，APIG 控制器建上游报
  `MissingParameter.UpstreamSpec.K8SService.Port`，同步失败（`kubectl describe ingress` 的事件里能看到）。
- `/metrics` 目前没有；设计要求它将来只在集群内端口监听，不经 Ingress / APIG 暴露（P7）。

## 7. 主密钥轮换（08 篇 §2.1）

每一行密钥记着自己是用第几版主密钥加密的；轮换期间新旧两版同时可读，中途断了可以接着做。本期只提供命令，不做自动轮换。
下面 N 是当前版本（Secret 里没有 `masterKeyVersion` 时就是 1）。

```bash
umask 077
kubectl -n curator get secret curator-master-key -o jsonpath='{.data.masterKey}' | base64 -d > master.key
N=$(kubectl -n curator get secret curator-master-key -o jsonpath='{.data.masterKeyVersion}' | base64 -d); N=${N:-1}
openssl rand -base64 32 | tr -d '\n' > master-next.key
```

1. **新主密钥写进 `masterKeyNext`，重启一次**，让 Daemon 同时认得新旧两版（从这时起新写入的密钥用新版加密）：

   ```bash
   kubectl -n curator create secret generic curator-master-key --from-file=masterKey=master.key \
     --from-file=masterKeyNext=master-next.key --from-literal=masterKeyVersion=$N \
     --dry-run=client -o yaml | kubectl -n curator replace -f -
   kubectl -n curator rollout restart statefulset/curator
   kubectl -n curator rollout status statefulset/curator
   ```

2. **在 Pod 里执行轮换**：

   ```bash
   kubectl -n curator exec curator-0 -- curator-rotate-master-key
   ```

   预期输出 `{"target_version": N+1, "rotated": …, "remaining": 0, "failed": [], "complete": true}`，退出码 0。
   中途断了就再执行一次；退出码 1 表示有几行用两把主密钥都解不开（打印了 id），先在「密钥与资源」页重新填写这几条再执行。

3. **新主密钥挪到 `masterKey`，版本改为 N+1，去掉 `masterKeyNext`，再重启**：

   ```bash
   kubectl -n curator create secret generic curator-master-key --from-file=masterKey=master-next.key \
     --from-literal=masterKeyVersion=$((N+1)) \
     --dry-run=client -o yaml | kubectl -n curator replace -f -
   kubectl -n curator rollout restart statefulset/curator
   ```

4. **确认**：在「密钥与资源」页对一条访问密钥点「验证」，能通过说明解密正常。新主密钥存进保管处，旧的确认无误后销毁，删掉本地的两个文件。

用 `replace` 而不是 `apply`：整份替换，`masterKeyNext` 一定会被去掉。版本号和主密钥放在同一个 Secret 里，一次修改同时生效，
不会出现「新密钥配旧版本号」的中间状态。Chart 生成的试用 Secret 也可以这样轮换（升级时 Chart 保留 Secret 里已有的全部键）。

## 8. 备份与恢复（09 篇 §6）

平台自己丢不起的只有两样：**SQLite 库**（任务历史、密钥密文、裁决记录）和**主密钥**。TOS 上的交付物本来就在对象存储里。
主密钥由运维另行保管（第 3 节），Chart 不替你备份它；库备份里的密钥是密文，没有主密钥也解不开。

设计里的定时备份（`backup.enabled`：CronJob 调 Daemon 的内部接口，`VACUUM INTO` 出快照再传到 TOS、保留 14 份）要等 Daemon 提供这个接口，
现在打开会被 Chart 拒绝。在那之前手动备份：

```bash
# 在线做一份一致性快照（Daemon 照常运行；VACUUM INTO 读的是一个事务内的状态）
kubectl -n curator exec curator-0 -- python -c "
import os, sqlite3, time
os.makedirs('/data/backups', exist_ok=True)
dst = '/data/backups/curator-%s.db' % time.strftime('%Y%m%d-%H%M%S')
con = sqlite3.connect('/data/curator.db', timeout=30)
con.execute('VACUUM INTO ?', (dst,))
con.close()
print(dst)"
# 拷出来，另存到集群之外（例如传到自己的 TOS 桶）
kubectl -n curator cp curator-0:/data/backups/curator-<时间>.db ./curator-<时间>.db
```

快照留在数据卷上会占空间，旧的记得删（`kubectl -n curator exec curator-0 -- rm /data/backups/curator-<时间>.db`）。

**恢复**：停 Daemon → 把快照放回数据卷 → 起 Daemon。用 Chart 的维护模式做，Pod 里只跑 `sleep`，数据卷照挂、环境变量照旧：

```bash
helm upgrade curator deploy/charts/curator -n curator -f curator-values.yaml --set maintenance.enabled=true
kubectl -n curator rollout status statefulset/curator
kubectl -n curator cp ./curator-<时间>.db curator-0:/data/restore.db
# 旧库连同它的 -wal / -shm 一起挪走（留着旧的 WAL 会被套到恢复的库上），再换上快照
kubectl -n curator exec curator-0 -- sh -c \
  'cd /data && d=replaced/$(date +%Y%m%d-%H%M%S) && mkdir -p $d && mv curator.db* $d/ && mv restore.db curator.db'
helm upgrade curator deploy/charts/curator -n curator -f curator-values.yaml --set maintenance.enabled=false
kubectl -n curator rollout status statefulset/curator
```

Daemon 起来后照常迁移和对账：快照那一刻在跑的任务被置为系统暂停并自动续跑（检查点在数据卷的 `runs/` 下）。
确认无误后删掉 `/data/replaced/`。维护模式也可以用来离线查看数据库、在 Daemon 起不来时排查。

## 9. 排障

| 现象 | 原因与处理 |
|---|---|
| `CrashLoopBackOff`，日志 `curator-daemon: 启动失败：…`，退出码 2 | 配置不对：主密钥缺失或不是 32 字节 base64、`server.tzOffset` 写法不对等，日志里写明了哪一项 |
| `CreateContainerConfigError` | 引用的 Secret 或键不存在（`curator-master-key` 的 `masterKey`、单用户模式的密码） |
| 停在 `ContainerCreating`，事件 `secret "curator-users" not found` | 账号表 Secret 没建，见第 3 节 |
| PVC 一直 `Pending` | 存储类不存在或不是块存储；`WaitForFirstConsumer` 的类在 Pod 调度之前本来就是 Pending |
| 所有请求都是 401，密码没错 | 账号表里没有可用的行（只认 bcrypt 和 apr1），日志有 `htpasswd configured but has no usable account`；改好 Secret 后重启 |
| Pod 不 Ready，`/readyz` 503 | 看响应里的 `checks`：`db_writable`（卷满或写不进）、`workdir_writable`、`scratch_writable`、`reconciled`（启动对账还没做完，会自动重试） |
| `helm install/upgrade` 直接报错 | Chart 拒绝了 Daemon 做不到的配置：`replicaCount` 不是 1、`server.maxRunningTasks` 不是 1、`backup.enabled`、宽限期短于 preStop + 100 秒、`auth` 配了一半等，报错里写明了哪一项 |

## 10. 不连集群的检查

```bash
helm lint deploy/charts/curator
helm template curator deploy/charts/curator -n curator -f curator-values.yaml
.venv/bin/python -m pytest -q backend/tests/deploy        # 约 10 秒；没有 helm 时 Chart 部分跳过
```

`backend/tests/deploy` 把渲染出来的清单和 Daemon 的实际行为逐项对照：单副本、数据卷挂在 Daemon 放数据库的目录、
探针走的是免鉴权路径、宽限期与 preStop、挂载前缀和 Daemon 的归一化一致、每个环境变量都有代码在读、密钥只经 secretKeyRef、
站点配置能被流水线和 planner 读进去；另有一条用渲染出的环境变量和挂载真的起一个 Daemon，探针免登录、接口要登录、SIGTERM 按时退出。
`test_dockerfile.py` 读 Dockerfile 本身：两个阶段、v1 的几条经验、控制台和契约文件的位置、入口、非 root 用户、`.dockerignore` 放行了 COPY 要的全部文件。

镜像本身（`docker build`、`docker run`）要在装了 docker 的机器上按第 1 节做；VKE 上的安装、升级时的系统暂停与续跑、经 APIG 的访问要在集群上按第 4–6 节做。

## 11. 还没有的（Daemon 侧）

- `/metrics`（P7，只在集群内端口监听）：Daemon 还没有，Chart 也就没有指标端口。
- 定时库备份（`backup.*`）：缺 Daemon 的内部备份接口，先按第 8 节手动做。
- `server.maxRunningTasks` 大于 1：Daemon 还不读这一项。
- 任务编排（W5）合入之前，镜像里的 Daemon 还不会真正跑任务：SIGTERM 时没有子进程要收尾；
  升级后「系统暂停 → 自动续跑」目前只靠启动对账（运行中 → 系统暂停 → 重新排队）这一半。
- `concurrency`、`vlm` 两段写进了站点配置 `site.yaml`（`CURATION_CONFIG` 指向它），`curation plan --site-config $CURATION_CONFIG` 现在就能读；Daemon 的 planner 由 W5 接入后读同一份。
