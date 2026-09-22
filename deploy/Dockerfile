# Curator v2 镜像（W11 / F4.1，2026-09-21；设计见 docs/design/09-deployment.md §1）
# 一个镜像、一个进程、一个端口：API Daemon（curator-daemon，默认入口）同进程托管网页控制台的
# 构建产物；质检 CLI（curation）由 Daemon 以子进程调用，也可以单独跑。
#
# 沿用 v1 镜像（机器人数据 curation 管线镜像，串行版，2026-07-16）的两条说明:
# 说明:①管线本体纯 CPU(质检=numpy/scipy/光流/解码);GPU 只被 VLM 服务用,
#   云上 VLM=独立容器(vLLM 官方镜像),端点写在 YAML/--set,本镜像不含模型权重;
#   ②daft 用 pip 官方发行版,绝不从源码构建(可复现性纪律)。
#
# 构建上下文=仓库根目录(F1.2 起):docker build -f deploy/Dockerfile .
# (火山 CI 的 ContextPath 从 robot-curation 改为仓库根,Dockerfile 路径填 deploy/Dockerfile)
# 上下文里哪些东西进得来由仓库根的 .dockerignore 管（node_modules、.venv、.git 都挡在外面）。
#   公网构建（GitHub Actions 等，火山内网源不可达）：
#     --build-arg NO_MIRROR=1 --build-arg PIP_INDEX_URL=https://pypi.org/simple \
#     --build-arg APT_MIRROR=http://deb.debian.org
#   火山内网构建：缺省值即可；想更快再加 --build-arg APT_MIRROR=http://mirrors.ivolces.com
# 全部构建参数的汇总见 deploy/README.md「构建镜像」。
#
# 基础镜像可换源(火山 CI 拉 Docker Hub 慢/不通时):
#   --build-arg BASE_IMAGE=hub-cache-cn-beijing.cr.volces.com/library/python:3.10-slim-bookworm
#   --build-arg NODE_IMAGE=hub-cache-cn-beijing.cr.volces.com/library/node:20-bookworm-slim
# ⚠️ 底座钉死 bookworm(2026-08-18):数据拉取通道用的 onion-ai-data 包只在
#   bookworm/bullseye/buster 的火山 extra-tools 源里有,trixie 源实测不存在
#   (dists/trixie 只有一个 irqbalance-ng)—— 裸 python:3.10-slim 会随上游漂到
#   trixie,装不上。
# 两个底座的 ARG 都写在第一个 FROM 之前：多阶段构建里只有这里声明的 ARG 能用在 FROM 行上。
# 前端阶段只跑 npm、不装系统包，钉 bookworm 只为可复现。
ARG BASE_IMAGE=python:3.10-slim-bookworm
ARG NODE_IMAGE=node:20-bookworm-slim

# ── 阶段 1：网页控制台，frontend/ → /src/frontend/dist ─────────────────────────
FROM ${NODE_IMAGE} AS web
# npm 源可换：缺省用 package-lock.json 里记的官方源 registry.npmjs.org。
# 火山构建集群拉官方源慢时传 --build-arg NPM_REGISTRY=https://registry.npmmirror.com
# （火山镜像站没有 npm 源：mirrors.volces.com/npm/ 实测 301 到镜像站首页，2026-09-21）。
# 换源不影响 lock 里的完整性校验（同一个 tarball），npm 只把 registry.npmjs.org 这个主机名换掉。
ARG NPM_REGISTRY=
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
# 依赖版本全部钉死，只用 npm ci（frontend/README.md）；--no-audit 免得经镜像源查漏洞库时失败
RUN npm ci --no-audit --no-fund ${NPM_REGISTRY:+--registry="$NPM_REGISTRY"}
COPY frontend/ ./
# 模拟数据 src/mocks/world.ts 直接 import ../../../docs/contracts/modules.json。生产构建不含
# 模拟代码，但打包器解析依赖图时可能仍要找到这个文件（没有实测不带它能否构建），带上最稳。
COPY docs/contracts/ /src/docs/contracts/
RUN npm run build

# ── 阶段 2：运行时 ───────────────────────────────────────────────────────────
FROM ${BASE_IMAGE}

# PyPI 源可换(2026-07-17 火山构建集群→官方 PyPI 实测 17kB/s+超时):
# 默认=火山**内网**镜像 ivolces(2026-07-27 换:公网 volces 对大 wheel 间歇 504,
# CI 第 12 次构建栽在 contourpy、pod 内曾栽在 gradio,同病;内网源两处实测稳定)。
# 火山体系外构建时换:--build-arg PIP_INDEX_URL=https://mirrors.volces.com/pypi/simple
# 或官方源 https://pypi.org/simple
ARG PIP_INDEX_URL=https://mirrors.ivolces.com/pypi/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PIP_DEFAULT_TIMEOUT=120
# 补充源(2026-09-22):火山源缺新版本 —— 火山 CP 构建实测内网源上 uvicorn 只到 0.44.0,v2 锁的是
# 0.53.0。按以往经验 apt / pip 用火山源,uvicorn 用阿里云:缺的几个列在 deploy/pip-extra-index.txt,
# 先从补充源不带依赖单独装,其余照旧全走火山源。⚠️ 别把补充源做成 pip 的 --extra-index-url:
# 两个源都有的包 pip 会随手挑一个,实测大半从阿里云下,构建集群出公网只有约 300 kB/s(内网源 300 MB/s)。
# 只想用一个源(比如 GitHub CI 走官方 PyPI):--build-arg PIP_EXTRA_INDEX_URL=
ARG PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# 运维基础件:curl(pod 内探查 OpenAI 端点/健康检查用;slim 镜像默认没有,
# 以前靠 pod 里临时 apt 装,一重建就丢——2026-07-26 进镜像固化)。
# apt 走火山镜像:deb.debian.org 实测慢到卡死(pod 内 apt install curl 十几分钟
# 没动静,换火山源秒装)。bookworm 的源声明在
# /etc/apt/sources.list.d/debian.sources(deb822 格式),不是 sources.list,
# sed 直接改该文件里的 URIs。
# ⚠️ 缺省必须用**公网可达**的 mirrors.volces.com:本文件要推公开仓,而内网变体
#    mirrors.ivolces.com 在火山网络外**解析不了**(Mac 上实测 000),且这一层
#    **不受 NO_MIRROR 闸门保护** —— 改成内网域等于让公开 CI 构建直接失败。
#    在火山内网构建想更快(实测 0.014s vs 0.33s),显式传:
#      --build-arg APT_MIRROR=http://mirrors.ivolces.com
ARG APT_MIRROR=https://mirrors.volces.com
RUN sed -i "s|http://deb.debian.org|${APT_MIRROR}|g; s|https://deb.debian.org|${APT_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

# 数据拉取通道(curation fetch,2026-08-18):从火山 extra-tools 源装 onion-ai-data,
# 它提供 oniond —— 内网 HF 镜像缓存桶(ai-infra)的下载器;依赖 aria2-byted/jq/wget
# 由 apt 自动解决,不需要手动解包 deb(aria2c_byted 二进制在 bookworm 上实测能跑)。
# NO_MIRROR=1 时整层跳过(照抄 RLinf 的闸门):内网源只在火山网络可达,公开 CI
# 构建 --build-arg NO_MIRROR=1 要能过;跳过后镜像照常能跑,只是 fetch 子命令
# 会因缺下载器而失败。gnupg 只为导入源签名(slim 没自带 gpg)。
ARG NO_MIRROR=
RUN if [ "$NO_MIRROR" != "1" ]; then \
      apt-get update -qq \
      && apt-get install -y -qq --no-install-recommends gnupg \
      && curl -fsSL http://mirrors.ivolces.com/extra-tools/debian/GPG-KEY-system \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/volc-extra-tools.gpg \
      && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/trusted.gpg.d/volc-extra-tools.gpg] http://mirrors.ivolces.com/extra-tools/debian bookworm main" \
        > /etc/apt/sources.list.d/volctools.list \
      && apt-get update -qq \
      && apt-get install -y -qq --no-install-recommends onion-ai-data \
      && rm -rf /var/lib/apt/lists/*; \
    fi

# 运行用户（2026-09-21）：非 root，uid/gid 10001，与 Chart 的 podSecurityContext 一致。
# Daemon 和 CLI 只写数据卷 /data 与临时卷 /scratch；两个目录先建好并归它所有，
# docker run 挂具名卷时初始属主随之正确（K8s 上由 fsGroup 保证）。
# 不写 VOLUME：v1 的 /data/input、/data/output 已经没有了（数据经 TOS SDK 读写），
# 数据库和任务工作目录在 /data，部署时显式挂卷（-v curator-data:/data / Chart 的 PVC）。
RUN groupadd --gid 10001 curator \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/curator \
       --shell /usr/sbin/nologin curator \
    && mkdir -p /data /scratch \
    && chown curator:curator /data /scratch

WORKDIR /app
COPY backend/requirements.txt deploy/pip-extra-index.txt ./
RUN if [ -n "$PIP_EXTRA_INDEX_URL" ]; then \
        pip install --no-cache-dir --no-deps --index-url "$PIP_EXTRA_INDEX_URL" -r pip-extra-index.txt; \
    fi \
    && pip install --no-cache-dir -r requirements.txt

# 后端两个包：curation（质检内核与 CLI）、daemon（API Daemon），入口见 pyproject.toml 的
# [project.scripts]：curation、curator-daemon、curator-rotate-master-key。
# 可编辑安装同 v1：default.yaml 等包内数据文件就在源码目录里，不必逐个声明 package-data。
# 源码目录属 root，运行用户写不了 __pycache__，所以构建时先编译好，免得每个 CLI 子进程启动时重新编译。
COPY backend/pyproject.toml ./
COPY backend/curation/ ./curation/
COPY backend/daemon/ ./daemon/
RUN pip install --no-cache-dir --no-deps -e . \
    && python -m compileall -q curation daemon

# 契约文件：Daemon 运行时按 docs/contracts/ 校验请求体与 CLI 输出（curation.contracts.schemas）。
# 放在 /app/docs/contracts，从包目录往上找也找得到；再用 CURATOR_CONTRACTS_DIR 写明。
COPY docs/contracts/ ./docs/contracts/
# 网页控制台：Daemon 在 {base}/ 下托管，并往 index.html 里注入挂载前缀（frontend/README.md）
COPY --from=web /src/frontend/dist/ ./web/
COPY deploy/docker-entrypoint.sh /usr/local/bin/curator-entrypoint
RUN chmod 0755 /usr/local/bin/curator-entrypoint
ENV CURATOR_STATIC_DIR=/app/web \
    CURATOR_CONTRACTS_DIR=/app/docs/contracts \
    PYTHONUNBUFFERED=1

USER 10001:10001
EXPOSE 8080
# tini 当 PID 1(issue #141):UI 进程曾直接做 1 号进程,过继来的孤儿无人收尸;
# tini 负责收尸与转发信号,`curation` 退到子进程。
# v2：缺省起 Daemon（CMD）；curator-entrypoint 把「只给了选项」补成 curator-daemon <选项>，
# 其余原样执行，所以 docker run <镜像> curation --help 照常可用。
# 部署侧若用 command: 覆盖入口，要自己写成 ["tini","--","curator-daemon",...]，否则这一层不生效
# （Chart 只写 args，不覆盖入口）。
ENTRYPOINT ["tini", "--", "curator-entrypoint"]
CMD ["curator-daemon"]
# 用法: docker run -p 8080:8080 -v curator-data:/data -e CURATOR_MASTER_KEY=... <镜像>
#       docker run --rm <镜像> curation --help
# 主密钥、登录账号怎么给见 deploy/README.md；K8s 上用 deploy/charts/curator。
