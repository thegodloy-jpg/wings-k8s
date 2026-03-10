# =============================================================================
# Wings-Infer 统一控制容器 Dockerfile
# 支持引擎: vllm, vllm_ascend, sglang, mindie
# 支持模式: 单机 / 分布式
# 构建命令:
#   docker build -t wings-infer:latest .
#
# 构建产物:
#   - /app/app/*: Sidecar 后端 Python 代码
#   - /app/wings_start.sh: 兼容 wings 接口的启动脚本
#   - /shared-volume: 启动脚本写入目录
# 端口:
#   - 17000: 引擎推理端口
#   - 18000: Proxy 代理
#   - 19000: Health 健康检查
# 启动方式:
#   - CMD 默认使用 wings_start.sh，与 wings 原始容器完全兼容
#   - 也可通过覆盖 CMD 直接运行 python -m app.main
# =============================================================================
FROM python:3.10-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y \
    curl \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY wings_start.sh ./wings_start.sh
RUN sed -i 's/\r//' ./wings_start.sh && chmod +x ./wings_start.sh

RUN ls -la /app/app/ \
 && test -d /app/app/core    && echo "core/ OK" \
 && test -d /app/app/engines && echo "engines/ OK" \
 && test -d /app/app/proxy   && echo "proxy/ OK" \
 && test -d /app/app/utils   && echo "utils/ OK" \
 && test -f /app/wings_start.sh && echo "wings_start.sh OK"

RUN mkdir -p /shared-volume /var/log/wings

EXPOSE 17000 18000 19000

ENV http_proxy=""
ENV https_proxy=""
ENV APP_WORKDIR="/app"

# 显式声明引擎和模型参数，构建即确定运行配置。
# ENTRYPOINT 中的参数不可被 docker run / K8s args 覆盖，只能追加。
ENTRYPOINT ["bash", "/app/wings_start.sh", \
            "--engine", "vllm", \
            "--model-name", "DeepSeek-R1-Distill-Qwen-1.5B", \
            "--model-path", "/models/DeepSeek-R1-Distill-Qwen-1.5B", \
            "--device-count", "1", \
            "--trust-remote-code"]
CMD []
