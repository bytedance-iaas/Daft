# 部署：镜像与集群上的质检台

一个镜像、一个进程、一个端口：API Daemon（`curator-daemon`）同进程托管网页控制台，质检 CLI（`curation`）由它以子进程调用。
本仓库只管镜像；K8s 上的部署是 **rerun 仓库的 `dataverse` Chart**（`deploy/helm/dataverse`，D53）：ReRun 与质检台打成一个 Chart，
质检台是其中单副本的 StatefulSet `<release>-curation`，SQLite 和任务工作目录在块存储数据盘上。以后升级质检台，就是换
dataverse 的 `image.curator` 再 `helm upgrade`。自托管 vLLM 是 rerun 仓库里另一个独立的 Chart（`deploy/helm/vllm`），和 dataverse 互不依赖。
设计依据：`docs/design/09-deployment.md`（镜像、Chart 里质检台的部分、探针、优雅停机、挂载前缀、检查清单、备份），
`08-secrets-and-auth.md`（主密钥与账号），`00-overview.md` §3（部署拓扑）。

| 路径 | 内容 |
|---|---|
| `deploy/Dockerfile` | 多阶段镜像：Node 20 构建控制台，Python 3.10（bookworm）运行时 |
| `deploy/docker-entrypoint.sh` | 镜像入口：不带参数或只带选项时起 Daemon，其余原样执行 |
| 仓库根 `.dockerignore` | 构建上下文里挡掉 `node_modules`、`.venv`、`.git`、`.claude` 等 |
| `backend/tests/deploy/` | 不连集群、不用 docker 的检查（见第 10 节） |
| rerun 仓库 `deploy/helm/dataverse/` | 集群上的部署：值的说明见它的 `values.yaml` 与 README 的「The curation console」一节 |

下文的 `$NS` 是 dataverse 所在的命名空间，`$REL` 是它的 release 名，`$RERUN` 是 rerun 仓库的本地路径。质检台的对象都跟着
release 名走：StatefulSet `$REL-curation`，Pod `$REL-curation-0`，容器 `curation`，数据盘 `data-$REL-curation-0`。
release 名不含 `dataverse` 时 Chart 会补上（`<release>-dataverse-curation`），现网都含。

```bash
export NS=galbot REL=galbot-dataverse RERUN=~/ws/rerun      # 按实际填
export STS=$REL-curation POD=$REL-curation-0
```

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
| `SUPPLEMENT_PYPI_URL` | `https://mirrors.aliyun.com/pypi/simple/`（阿里云） | 火山源缺的版本从这里补：只装 `deploy/pip-extra-index.txt` 里列的几个及其依赖（现在是 uvicorn 0.53.0、cryptography 50.0.1，火山内网源只到 0.44.0、49.0.0；F6.5 又加了 mcap / Lance 读取要的 pylance 8.0.0、mcap 1.4.0，是按发布日期推测火山源还没有，下一次构建确认后删掉已有的那行），其余全走 `PIP_INDEX_URL`；火山源补齐后删掉对应那一行。传空则只用 `PIP_INDEX_URL` |
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
3. **两块盘都必须是块存储（EBS）**，dataverse 缺省 `ebs-essd`。SQLite 的 WAL 依赖共享内存和可靠的文件锁，放在 NAS 或 TOS-FSX 这类网络文件系统上会损坏；
   临时盘同样不能是 FSX：PyAV 收尾时要 seek 回文件头，FSX 拒绝随机写。块存储盘属于某个可用区，存储类最好是 `volumeBindingMode: WaitForFirstConsumer`。
4. **主密钥已写进 Secret 且已备份**（下一节）。丢了它，库里保存的访问密钥和 API Key 全部解不开，只能逐个重填。
5. **`curator.publicBaseUrl` 配好**，否则 Agent 拿到的链接是相对路径。域名只能在 APIG 控制台看到，装完再补也行。
6. **挂载前缀固定是 `/curation`**，网关分流不剥前缀，dataverse 的 Ingress 已按这个写好，不用配。
7. **和 rerun viewer 免二次登录**：dataverse 让两边共用 Secret 里的 `web_htpasswd`，realm 不改（Daemon 固定为 `Robot Data Curation`）。
8. **资源**：dataverse 缺省 `requests 16 核 / 128Gi`、`limits 32 核 / 256Gi`（`curator.resources`），照节点的 allocatable 调；
   跑在 VCI 上时按 limits 计费。
9. **临时盘够大**：`curator.persistence.scratch`（缺省 500Gi）除了导出时的视频临时文件，还放 TOS 上 mcap / Lance 数据集的本地副本
   （`CURATOR_SOURCE_CACHE_DIR=/scratch/source-cache`，D44）：mcap 读到哪条下载哪条，Lance 整表下载，任务跑完就删。
   按要质检的最大数据集加上导出的余量来定。副本不放数据盘：数据盘写满会让 SQLite 写不进去。
10. **镜像能拉到**：`image.curator` 是完整引用，tag 是提交号（第 1 节）。

## 3. Secret 里质检台用到的键

dataverse 的全部密钥都在一个 Secret 里（`secrets.existingSecret`，现网叫 `dataverse-secrets`），**每个组件只挂自己要读的键**：
质检台只读下面这几个，viewer、catalog、原生会话都读不到主密钥。

| 键 | 必需 | 用途 |
|---|---|---|
| `web_htpasswd` | 是（`web.basicAuth.enabled` 时） | 登录账号表，和 viewer 共用：bcrypt（`$2y$`/`$2b$`/`$2a$`）和 apr1 两种哈希；挂成文件，`CURATOR_AUTH_MODE=htpasswd` |
| `curator_master_key` | 是 | 主密钥（`CURATOR_MASTER_KEY`，32 字节，base64） |
| `curator_master_key_next` | 否 | 轮换中的新主密钥（第 7 节），平时没有 |
| `curator_master_key_version` | 否 | 当前主密钥的版本号，没有 = 1；轮换收尾时和主密钥一起改 |

新装时生成主密钥并补进 Secret。经文件传值，密钥不进命令行参数和 shell 历史：

```bash
umask 077
openssl rand -base64 32 | tr -d '\n' > master.key
kubectl -n $NS patch secret dataverse-secrets --type merge \
  --patch-file <(jq -n --rawfile k master.key '{data: {curator_master_key: ($k | @base64)}}')
```

把 `master.key` 存进团队的密钥保管处，确认存好之后删掉本地文件（`rm -f master.key`）。
**只用 `patch` 改单个键**：这个 Secret 里还有 viewer 和 catalog 的全部密钥，用 `create … | replace` 整份覆盖会把没列出的键一起抹掉。

改了账号表要重启一次质检台才生效（Daemon 启动时读一次）：`kubectl -n $NS rollout restart statefulset/$STS`。

## 4. 安装

按 rerun 仓库的 `docs/release/02-deploy.md` 装 dataverse：`curator.enabled` 缺省就是 `true`，除了 `image.curator` 和 Secret 里的
`curator_master_key`，质检台不需要别的值。values 里和质检台有关的几项（只写和缺省不同的）：

```yaml
image:
  curator: iaas-us-cn-beijing.cr.volces.com/physicalai/robot_curator:<提交号>
curator:
  publicBaseUrl: https://<对外域名>
  siteConfig:                       # 站点配置 site.yaml，原样写入；只写和出厂默认不同的
    concurrency: {cpu: 8, cpuMax: 16, vlmParallelism: 64, vlmParallelismMax: 128}
    checks: {task_success: {vlm: {timeouts_s: {caption: 180}}}}
vci:
  enabled: true                     # 跑在 VCI 上：Pod 注解，和给数据盘改属主的初始化容器
```

验证：

```bash
kubectl -n $NS rollout status statefulset/$STS --timeout=10m
kubectl -n $NS get pvc data-$POD scratch-$POD                          # 都是 Bound
kubectl -n $NS exec $POD -c curation -- curl -s localhost:8080/healthz  # {"status":"ok"}
kubectl -n $NS exec $POD -c curation -- curl -s localhost:8080/curation/readyz   # checks 全是 true
kubectl -n $NS logs $POD -c curation | grep authentication              # authentication: htpasswd, N account(s)
```

浏览器打开 `https://<域名>/curation/`，用 viewer 的账号登录。

## 5. 升级

```bash
helm -n $NS upgrade $REL $RERUN/deploy/helm/dataverse -f <values 文件> --set image.curator=<仓库>/robot_curator:<新提交号>
kubectl -n $NS rollout status statefulset/$STS --timeout=10m
```

values 文件是 `helm -n $NS get values $REL -o yaml` 存下来的那份（加上要改的）。**不要用 `--reuse-values` 跨 Chart 版本升级**：
它连旧版 Chart 的缺省值一起沿用，新版加的值全都缺失；非要用参数，就用 `--reset-then-reuse-values`。
升级前先 dry-run，和线上清单比一比（`helm get manifest`），确认只动了想动的东西。

升级时发生的事（09 篇 §2.3）：StatefulSet 先停旧 Pod —— preStop 等 5 秒让 Service / 网关摘掉它，然后 SIGTERM；
Daemon 不再接新任务，让运行中的 CLI 子进程收尾当前 episode（最多 90 秒，超时的被杀掉，那一条恢复时重跑），
把任务置为系统暂停并退出；宽限期 120 秒兜底。新 Pod 挂上同一块数据盘，启动时迁移数据库、做状态对账，
系统暂停的任务自动重新排队续跑；用户自己暂停的保持暂停，用户停止的不恢复。
停机时间约等于 preStop + 收尾 + 新 Pod 启动，通常一两分钟。

验证：升级前有一个任务在运行，升级后看它的执行时间线（任务详情页，或 `GET /curation/api/v1/tasks/<任务 id>/timeline`），
依次有 `system_pause`（任务被系统暂停）和 `system_resume`。

几点注意：

- 两块盘由 `volumeClaimTemplates` 建出，建好以后不能再改：升级时别动 `curator.persistence.*.size` / `className`，否则 helm 报
  `updates to statefulset spec for fields other than ... are forbidden`。扩容直接改 PVC（存储类要允许扩容）：
  `kubectl -n $NS patch pvc data-$POD -p '{"spec":{"resources":{"requests":{"storage":"200Gi"}}}}'`。
- 改站点配置（`curator.siteConfig`、`curator.publicDatasets`）也会滚动重启 Pod，同样走系统暂停再续跑。
- 节点排水（drain）、Pod 被驱逐都等同一次重启；块存储盘会跟着 Pod 挂到同一可用区的新节点上。
- ReRun 的 Pod 和质检台互不影响：只改 `image.rerun` 时质检台不重启，反过来也一样。

## 6. 经 APIG 分流与 SSE

dataverse 的 web Ingress 把同一域名的 `/curation/**` 转给 Service `$REL-curation` 的 8080 端口，**不剥前缀**（09 篇 §2.4）：

- Daemon 把全部路由注册在前缀之下：`/curation/api/v1/**`、`/curation/events/**`、`/curation/` 的控制台（刷新任意页面不 404）。
- 网关的健康检查用 `GET /curation/healthz`（免鉴权）；`/curation/readyz` 在数据库不可写、工作目录不可写或启动对账没做完时返回 503。
- SSE（`/curation/events/tasks/{id}`）是长连接，Daemon 空闲时每 15 秒发一行心跳：网关的空闲超时要大于它，否则用
  `curator.extraEnv` 调小 `CURATOR_SSE_HEARTBEAT_S`。响应带 `X-Accel-Buffering: no`。
- v1 的旧深链 `/curation/?dataset=…`（ReRun 的「质检」按钮）由 Daemon 302 到新建任务页，参数原样带上。
- `/metrics` 目前没有；设计要求它将来只在集群内端口监听，不经 Ingress / APIG 暴露（P7）。

## 7. 主密钥轮换（08 篇 §2.1）

每一行密钥记着自己是用第几版主密钥加密的；轮换期间新旧两版同时可读，中途断了可以接着做。本期只提供命令，不做自动轮换。
下面 N 是当前版本（Secret 里没有 `curator_master_key_version` 时就是 1）。所有改动都用 `patch` 只动这几个键，内容经文件传，不打印。

```bash
umask 077
N=$(kubectl -n $NS get secret dataverse-secrets -o jsonpath='{.data.curator_master_key_version}' | base64 -d); N=${N:-1}
openssl rand -base64 32 | tr -d '\n' > master-next.key
```

1. **新主密钥写进 `curator_master_key_next`，版本号写明 N，重启一次**，让 Daemon 同时认得新旧两版（从这时起新写入的密钥用新版加密）：

   ```bash
   kubectl -n $NS patch secret dataverse-secrets --type merge --patch-file <(jq -n --rawfile k master-next.key --arg v "$N" \
     '{data: {curator_master_key_next: ($k | @base64), curator_master_key_version: ($v | @base64)}}')
   kubectl -n $NS rollout restart statefulset/$STS
   kubectl -n $NS rollout status statefulset/$STS
   ```

2. **在 Pod 里执行轮换**：

   ```bash
   kubectl -n $NS exec $POD -c curation -- curator-rotate-master-key
   ```

   预期输出 `{"target_version": N+1, "rotated": …, "remaining": 0, "failed": [], "complete": true}`，退出码 0。
   中途断了就再执行一次；退出码 1 表示有几行用两把主密钥都解不开（打印了 id），先在「密钥与资源」页重新填写这几条再执行。

3. **新主密钥挪到 `curator_master_key`，版本改为 N+1，去掉 `curator_master_key_next`，再重启**。三处在同一个 patch 里，一次生效，
   不会出现「新密钥配旧版本号」的中间状态（merge patch 里的 `null` 就是删掉这个键）：

   ```bash
   kubectl -n $NS patch secret dataverse-secrets --type merge --patch-file <(jq -n --rawfile k master-next.key --arg v "$((N+1))" \
     '{data: {curator_master_key: ($k | @base64), curator_master_key_version: ($v | @base64), curator_master_key_next: null}}')
   kubectl -n $NS rollout restart statefulset/$STS
   ```

4. **确认**：在「密钥与资源」页对一条访问密钥点「验证」，能通过说明解密正常。新主密钥存进保管处，旧的确认无误后销毁，删掉本地的文件。

## 8. 备份与恢复（09 篇 §6）

平台自己丢不起的只有两样：**SQLite 库**（任务历史、密钥密文、裁决记录）和**主密钥**。TOS 上的交付物本来就在对象存储里。
主密钥由运维另行保管（第 3 节）；库备份里的密钥是密文，没有主密钥也解不开。

设计里的定时备份（CronJob 调 Daemon 的内部接口，`VACUUM INTO` 出快照再传到 TOS、保留 14 份）要等 Daemon 提供这个接口，
Chart 里也就没有这一项。在那之前手动备份：

```bash
# 在线做一份一致性快照（Daemon 照常运行；VACUUM INTO 读的是一个事务内的状态）
kubectl -n $NS exec $POD -c curation -- python -c "
import os, sqlite3, time
os.makedirs('/data/backups', exist_ok=True)
dst = '/data/backups/curator-%s.db' % time.strftime('%Y%m%d-%H%M%S')
con = sqlite3.connect('/data/curator.db', timeout=30)
con.execute('VACUUM INTO ?', (dst,))
con.close()
print(dst)"
# 拷出来，另存到集群之外（例如传到自己的 TOS 桶）
kubectl -n $NS cp -c curation $POD:/data/backups/curator-<时间>.db ./curator-<时间>.db
```

快照留在数据盘上会占空间，旧的记得删（`kubectl -n $NS exec $POD -c curation -- rm /data/backups/curator-<时间>.db`）。

**恢复**：停 Daemon → 把快照放回数据盘 → 起 Daemon。StatefulSet 缩到 0 个副本，数据盘空出来，再照它的 Pod 模板起一个
只挂数据盘、只跑 `sleep` 的临时 Pod 来换库（镜像、运行用户、VCI 注解都沿用；不带标签，网关不会把流量转给它）。
**恢复期间别跑 `helm upgrade`**：它会把副本数改回 1。

```bash
# 数据盘的 PVC 名：接管过旧盘的实例不是 data-$POD（galbot 是 data-curator-v2-0），从 Pod 上读
CLAIM=$(kubectl -n $NS get pod $POD -o jsonpath='{.spec.volumes[?(@.name=="data")].persistentVolumeClaim.claimName}')
# 1. 停 Daemon（SIGTERM：运行中的任务被系统暂停），等 Pod 删掉、盘卸下来
kubectl -n $NS scale statefulset/$STS --replicas=0
kubectl -n $NS wait --for=delete pod/$POD --timeout=5m
# 2. 临时 Pod：同一份模板，只跑 sleep，资源压到 2 核 8Gi，临时盘换成 emptyDir
kubectl -n $NS get statefulset $STS -o json | jq --arg claim "$CLAIM" '{
  apiVersion: "v1", kind: "Pod",
  metadata: {name: "curator-restore", annotations: .spec.template.metadata.annotations},
  spec: (.spec.template.spec
    | .restartPolicy = "Never"
    | .containers |= map(.args = ["sleep", "infinity"]
        | .resources = {requests: {cpu: "2", memory: "8Gi"}, limits: {cpu: "2", memory: "8Gi"}}
        | del(.startupProbe, .livenessProbe, .readinessProbe, .lifecycle))
    | .volumes = ((.volumes // []) | map(select(.name != "data")))
        + [{name: "data", persistentVolumeClaim: {claimName: $claim}}, {name: "scratch", emptyDir: {}}])}' \
  | kubectl -n $NS apply -f -
kubectl -n $NS wait --for=condition=Ready pod/curator-restore --timeout=10m
# 3. 换库：旧库连同它的 -wal / -shm 一起挪走（留着旧的 WAL 会被套到恢复的库上），再换上快照
kubectl -n $NS cp -c curation ./curator-<时间>.db curator-restore:/data/restore.db
kubectl -n $NS exec curator-restore -c curation -- sh -c \
  'cd /data && d=replaced/$(date +%Y%m%d-%H%M%S) && mkdir -p $d && mv curator.db* $d/ && mv restore.db curator.db'
# 4. 删掉临时 Pod，起回 Daemon
kubectl -n $NS delete pod curator-restore --wait
kubectl -n $NS scale statefulset/$STS --replicas=1
kubectl -n $NS rollout status statefulset/$STS
```

Daemon 起来后照常迁移和对账：快照那一刻在跑的任务被置为系统暂停并自动续跑（检查点在数据盘的 `runs/` 下）。
确认无误后删掉 `/data/replaced/`。Daemon 起不来、要离线查看数据库时，同样做第 1、2 步，看完做第 4 步。

## 9. 排障

| 现象 | 原因与处理 |
|---|---|
| `CrashLoopBackOff`，日志 `curator-daemon: 启动失败：…`，退出码 2 | 配置不对：主密钥不是 32 字节 base64、`extraEnv` 里某项写法不对等，日志里写明了哪一项 |
| `CreateContainerConfigError`，事件提到 `curator_master_key` | Secret 里没有主密钥（第 3 节）。已经在用的实例**别新生成**，找回原来那把，否则库里的密钥全解不开 |
| 停在 `ContainerCreating`，事件 `couldn't find key web_htpasswd` | 开着 `web.basicAuth` 但 Secret 里没有账号表 |
| PVC 一直 `Pending` | 存储类不存在或不是块存储；`WaitForFirstConsumer` 的类在 Pod 调度之前本来就是 Pending |
| 所有请求都是 401，密码没错 | 账号表里没有可用的行（只认 bcrypt 和 apr1），日志有 `htpasswd configured but has no usable account`；改好 Secret 后重启 |
| Pod 不 Ready，`/readyz` 503 | 看响应里的 `checks`：`db_writable`（盘满或写不进）、`workdir_writable`、`scratch_writable`、`reconciled`（启动对账还没做完，会自动重试）。VCI 上写不进多半是盘的属主不对：确认 dataverse 的 `vci.enabled=true`，它会加改属主的初始化容器 |
| autolabel（caption）整批 `status: error`、`cause: timeout`，重试 4 次都超时 | 模型答得比分类型超时慢。出厂默认 caption / probe / 端态 / 仲裁 60 秒、纯文本 120 秒（`adapters/vlm_client.py` 的 `DEFAULT_TIMEOUTS_S`，A 类不改）。先换个快的模型试；确实要等，就用站点配置抬高（见下） |
| `helm upgrade` 报 `nil pointer evaluating …` | 用了 `--reuse-values` 跨 Chart 版本升级，模板读不到新版加的值（第 5 节） |

分类型超时按站点配置覆盖（09 篇 §3 的深合并，D5 只允许调上限，不改算法），写进 dataverse 的 values：

```yaml
curator:
  siteConfig:
    checks:
      task_success:
        vlm:
          timeouts_s:
            caption: 180        # 出厂 60；probe / endstate / arbitration 同理，llm 出厂 120
```

`helm upgrade` 之后 ConfigMap 就变了，Pod 随之滚动，新起的任务按新值跑。

## 10. 不连集群的检查

```bash
.venv/bin/python -m pytest -q backend/tests/deploy                       # 约 1 秒
helm lint $RERUN/deploy/helm/dataverse --set image.rerun=r/rerun:t --set image.curator=r/robot_curator:t \
  --set secrets.existingSecret=s --set apig.existingId=x --set apig.ingressClassName=c
DRY_RUN=1 HELM_REGISTRY_HOST=example HELM_REGISTRY_NAMESPACE=ci bash $RERUN/deploy/publish_charts.sh   # 三个 Chart 都 lint、渲染、打包
```

`backend/tests/deploy`：`test_dockerfile.py` 读 Dockerfile 本身（两个阶段、v1 的几条经验、控制台和契约文件的位置、入口、非 root 用户、
`.dockerignore` 放行了 COPY 要的全部文件）；`test_env_contract.py` 对 09 篇 §2.1 那张环境变量表：表里每一项都有代码在读，
Daemon 能接受表里的写法 —— dataverse 的模板照这张表写，改名、删设置时这里先红。

镜像本身（`docker build`、`docker run`）要在装了 docker 的机器上按第 1 节做；VKE 上的安装、升级时的系统暂停与续跑、经 APIG 的访问要在集群上按第 4–6 节做。

## 11. 还没有的（Daemon 侧）

- `/metrics`（P7，只在集群内端口监听）：Daemon 还没有，Chart 也就没有指标端口。
- 定时库备份：缺 Daemon 的内部备份接口，先按第 8 节手动做。

## 12. galbot：独立的 curator-v2 并入 galbot-dataverse（D53）

D48（2026-09-23）起，galbot 的 v2 是单独装的 release `curator-v2`（本仓库原来的 `deploy/charts/curator`），和
`galbot-dataverse`（`curator.enabled=false`）挂在同一个 APIG、同一个域名的 `/curation`。D53 把它并回 dataverse：
**数据盘直接接管、不拷数据**，主密钥复制进 `dataverse-secrets`。

2026-09-25 在集群上核对过的事实（名字照此写，变了以集群为准）：

| 项 | 值 |
|---|---|
| curator-v2 | StatefulSet `curator-v2`、Ingress `curator-v2`；数据盘 `data-curator-v2-0`、临时盘 `scratch-curator-v2-0`（都是 `ebs-essd`）；主密钥 Secret `curator-v2-master-key`，只有 `masterKey` 一个键（第 1 版） |
| galbot-dataverse | Chart `dataverse-0.1.7`，`curator.enabled=false`、`vci.enabled=true`、`apig.create=true`；`dataverse-secrets` 有 `tos_access_key`、`tos_secret_key`、`server_token_secret`、`web_htpasswd`、`ark_api_key`（最后一个 0.2.0 不再读） |
| 新名字 | StatefulSet 与 Service `galbot-dataverse-curation`，Pod `galbot-dataverse-curation-0`；数据盘沿用 `data-curator-v2-0`，临时盘新建 `scratch-galbot-dataverse-curation-0`（500Gi，原来是 200Gi） |

顺带的变化：0.2.0 让 viewer 和 catalog 只挂自己要的键，`rerun-cloud-0` 会跟着重启一次（上面四个键都在，挂载不会失败）。

密钥只经管道在 kubectl 之间传，不打印、不落盘。

**1. 准备**

- 确认没有任务在跑（在跑就等它跑完，或和需求方约时间）；按第 8 节做一份库快照，拷到集群之外。
- 存下两个 release 现在的值：`helm -n galbot get values galbot-dataverse -o yaml > galbot-dataverse-values.yaml`，
  `helm -n galbot get values curator-v2 -o yaml > curator-v2-values.yaml`（都不含密钥本体）。
- 在 `galbot-dataverse-values.yaml` 里改成（其余照旧；`vllm:` 那段删掉）：

  ```yaml
  image:
    curator: iaas-us-cn-beijing.cr.volces.com/physicalai/robot_curator:<curator-v2 现在的 image.tag>
  curator:
    enabled: true
    persistence:
      data: {existingClaim: data-curator-v2-0}
    siteConfig:
      concurrency: {cpu: 8, cpuMax: 16, vlmParallelism: 64, vlmParallelismMax: 128}
      checks: {task_success: {vlm: {timeouts_s: {caption: 180}}}}
  ```

  `publicDatasets`（`ai-infra`，地域跟 `tos.region`）、`resources`（16 / 128Gi，上限 32 / 256Gi）、VCI 的注解和改属主的初始化容器
  都是 dataverse 的缺省或由 `vci.enabled` 带出，和 curator-v2 现在的值一致，不用写。
- dry-run 看一眼：`helm -n galbot upgrade galbot-dataverse $RERUN/deploy/helm/dataverse -f galbot-dataverse-values.yaml --dry-run=server`，
  应该出现 `galbot-dataverse-curation` 的 StatefulSet（数据卷指向 `data-curator-v2-0`）、Service、ConfigMap 和 web Ingress 的 `/curation`，
  `rerun-cloud` 只有两个 Secret 卷变了。

**2. 主密钥复制进 dataverse-secrets**

```bash
kubectl -n galbot patch secret dataverse-secrets --type merge --patch-file <(
  kubectl -n galbot get secret curator-v2-master-key -o json | jq '{data: {curator_master_key: .data.masterKey}}')
```

核对（两边 SHA-256 相同，只打印哈希）：

```bash
for s in "curator-v2-master-key masterKey" "dataverse-secrets curator_master_key"; do set -- $s
  kubectl -n galbot get secret $1 -o jsonpath="{.data.$2}" | base64 -d | shasum -a 256; done
```

**3. 卸掉独立的 release**

```bash
helm -n galbot uninstall curator-v2        # SIGTERM：运行中的任务置系统暂停；两块盘和主密钥 Secret 都不删
kubectl -n galbot get pvc data-curator-v2-0 # 仍是 Bound（没有 Pod 挂着）
```

从这一步到下一步做完，`/curation` 不可用。

**4. 升级 dataverse，打开 curator**

```bash
helm -n galbot upgrade galbot-dataverse $RERUN/deploy/helm/dataverse -f galbot-dataverse-values.yaml
kubectl -n galbot rollout status statefulset/galbot-dataverse-curation --timeout=10m
kubectl -n galbot rollout status statefulset/rerun-cloud --timeout=10m
```

**5. 验收**

- `https://<域名>/curation/healthz` 200；`/curation/` 用 dataverse 的账号登录；viewer（`/`）和 catalog 照常。
- 迁移前的任务、数据集、访问密钥、VLM 后端与模型都在；在「密钥与资源」页对一条访问密钥点「验证」能通过（主密钥对上了）；
  之前被系统暂停的任务自动续跑。
- ReRun 里点数据集的「质检」按钮，跳到新建任务页并预填数据集；数据集列表点「可视化」，ReRun 打开该数据集。

**6. 清理**（验收通过、需求方确认之后）：`kubectl -n galbot delete pvc scratch-curator-v2-0`、
`kubectl -n galbot delete secret curator-v2-master-key`。

**回滚**：`helm -n galbot upgrade galbot-dataverse $RERUN/deploy/helm/dataverse -f galbot-dataverse-values.yaml --set curator.enabled=false`
→ 从本仓库历史里取出原来的 Chart（`git worktree add /tmp/curator-chart <D53 之前的提交>`），
`helm -n galbot install curator-v2 /tmp/curator-chart/deploy/charts/curator -f curator-v2-values.yaml`：它的数据盘模板名字就是
`data-curator-v2-0`，直接挂回原来的盘；主密钥 Secret 在第 6 步之前都还在。
