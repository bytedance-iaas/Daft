# 机器人数据 curation 管线镜像(串行版,2026-07-16)
# 说明:①管线本体纯 CPU(质检=numpy/scipy/光流/解码);GPU 只被 VLM 服务用,
#   云上 VLM=独立容器(vLLM 官方镜像),端点写在 YAML/--set,本镜像不含模型权重;
#   ②daft 用 pip 官方发行版,绝不从源码构建(可复现性纪律)。
#
# 基础镜像可换源(火山 CI 拉 Docker Hub 慢/不通时):
#   --build-arg BASE_IMAGE=hub-cache-cn-beijing.cr.volces.com/library/python:3.10-slim
ARG BASE_IMAGE=python:3.10-slim
FROM ${BASE_IMAGE}

# PyPI 源可换(2026-07-17 火山构建集群→官方 PyPI 实测 17kB/s+超时):
# 默认=火山公共镜像(国内构建零参数直接快);要回官方源:
#   --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://mirrors.volces.com/pypi/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PIP_DEFAULT_TIMEOUT=120

# 构建上下文=本目录(火山 CI 的 ContextPath 填 robot-curation;本地在包目录下 build)
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY curation/ ./curation/
COPY pyproject.toml README.md* ./
RUN pip install --no-cache-dir --no-deps -e .

# 数据/输出经挂载进出(VKE 挂 TOS/PVC 到这两个路径)
VOLUME ["/data/input", "/data/output"]
ENTRYPOINT ["curation"]
CMD ["--help"]
# 用法: docker run -v 数据集:/data/input -v 结果:/data/output <镜像> \
#        run --input /data/input --output /data/output --lite --report-only
