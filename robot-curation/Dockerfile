# 机器人数据 curation 管线镜像(串行版,2026-07-16)
# 说明:①管线本体纯 CPU(质检=numpy/scipy/光流/解码);GPU 只被 VLM 服务用,
#   云上 VLM=独立容器(vLLM 官方镜像),端点写在 YAML/--set,本镜像不含模型权重;
#   ②daft 用 pip 官方发行版,绝不从源码构建(可复现性纪律)。
#
# 基础镜像可换源(火山 CI 拉 Docker Hub 慢/不通时):
#   --build-arg BASE_IMAGE=hub-cache-cn-beijing.cr.volces.com/library/python:3.10-slim
ARG BASE_IMAGE=python:3.10-slim
FROM ${BASE_IMAGE}

# 构建上下文兼容(2026-07-17 火山 CI 上下文=仓库根,代码在 robot-curation/ 子目录):
#   火山 fork 根上下文:默认值即可;本地/子目录上下文:--build-arg APP_DIR=.
ARG APP_DIR=robot-curation

WORKDIR /app
COPY ${APP_DIR}/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ${APP_DIR}/curation/ ./curation/
COPY ${APP_DIR}/pyproject.toml ${APP_DIR}/README.md* ./
RUN pip install --no-cache-dir --no-deps -e .

# 数据/输出经挂载进出(VKE 挂 TOS/PVC 到这两个路径)
VOLUME ["/data/input", "/data/output"]
ENTRYPOINT ["curation"]
CMD ["--help"]
# 用法: docker run -v 数据集:/data/input -v 结果:/data/output <镜像> \
#        run --input /data/input --output /data/output --lite --report-only
