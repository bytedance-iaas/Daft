# 机器人数据 curation 管线镜像(串行版,2026-07-16)
# 说明:①管线本体纯 CPU(质检=numpy/scipy/光流/解码);GPU 只被 VLM 服务用,
#   云上 VLM=独立容器(vLLM 官方镜像),端点写在 YAML/--set,本镜像不含模型权重;
#   ②daft 用 pip 官方发行版,绝不从源码构建(可复现性纪律)。
#
# 基础镜像可换源(火山 CI 拉 Docker Hub 慢/不通时):
#   --build-arg BASE_IMAGE=hub-cache-cn-beijing.cr.volces.com/library/python:3.10-slim-bookworm
# ⚠️ 底座钉死 bookworm(2026-08-18):数据拉取通道用的 onion-ai-data 包只在
#   bookworm/bullseye/buster 的火山 extra-tools 源里有,trixie 源实测不存在
#   (dists/trixie 只有一个 irqbalance-ng)—— 裸 python:3.10-slim 会随上游漂到
#   trixie,装不上。
ARG BASE_IMAGE=python:3.10-slim-bookworm
FROM ${BASE_IMAGE}

# PyPI 源可换(2026-07-17 火山构建集群→官方 PyPI 实测 17kB/s+超时):
# 默认=火山**内网**镜像 ivolces(2026-07-27 换:公网 volces 对大 wheel 间歇 504,
# CI 第 12 次构建栽在 contourpy、pod 内曾栽在 gradio,同病;内网源两处实测稳定)。
# 火山体系外构建时换:--build-arg PIP_INDEX_URL=https://mirrors.volces.com/pypi/simple
# 或官方源 https://pypi.org/simple
ARG PIP_INDEX_URL=https://mirrors.ivolces.com/pypi/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PIP_DEFAULT_TIMEOUT=120

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
    && apt-get install -y -qq --no-install-recommends curl \
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

# (2026-07-29 U4)这里原本有一层"下载 ttyd 静态二进制"——**已整层删除**。
# 网页终端改成**内嵌式**:终端是质检台 UI 自己的一条 WebSocket 路由(/ws/term,
# forkpty 起 bash),前端 xterm.js 已 vendored 进 curation/ui/static/。换掉 ttyd 的
# 三条理由:
#   ① **单端口** —— 终端与 UI 同一个 7860,不再需要第二个 containerPort/port-forward;
#   ② **无外部二进制** —— 构建不再依赖 GitHub releases(那一层实测极不稳:SSL 秒断/
#      连上不吐字节/106s 才下完,靠 --max-time 兜着才没吊死构建),镜像也少一个供应链面;
#   ③ **鉴权同一套** —— ttyd 是独立进程、独立(无)鉴权;内嵌之后 Basic 中间件与
#      未来的 APIG 网关一次就把 UI 和终端一起护住(见 curation/ui/auth.py)。
# 开关:`curation ui --terminal`(或 CURATION_TERMINAL=1);不开则页签不渲染、
# /ws/term 路由根本不注册。

# 构建上下文=本目录(火山 CI 的 ContextPath 填 robot-curation;本地在包目录下 build)
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 质检台 UI 字体离线补丁(2026-07-27):gradio 前端在 index.html(preconnect)与
# Index-*.js(inject_fonts 硬编码)里请求 fonts.googleapis.com——与主题配置无关,
# 国内网络会把首屏挂起 15s+(实测)。把域名改写为相对路径 → 浏览器打回本服务,
# 毫秒级 404 完事,页面用系统字体(theme 已设),外观无损。金丝雀测试:
# tests/test_gradio_offline_fonts.py(装了 gradio 的环境里扫模板目录,残留即红)。
RUN T=$(python -c "import gradio,os;print(os.path.dirname(gradio.__file__))")/templates \
    && grep -rl 'fonts\.googleapis\.com\|fonts\.gstatic\.com' $T \
       | xargs -r sed -i 's|https://fonts.googleapis.com|/offline-fonts|g; s|https://fonts.gstatic.com|/offline-fonts|g' \
    && [ $(grep -rl 'fonts\.googleapis\.com' $T | wc -l) -eq 0 ]

COPY curation/ ./curation/
COPY pyproject.toml README.md* ./
RUN pip install --no-cache-dir --no-deps -e .

# 数据/输出经挂载进出(VKE 挂 TOS/PVC 到这两个路径)
VOLUME ["/data/input", "/data/output"]
ENTRYPOINT ["curation"]
CMD ["--help"]
# 用法: docker run -v 数据集:/data/input -v 结果:/data/output <镜像> \
#        run --input /data/input --output /data/output --lite --report-only
