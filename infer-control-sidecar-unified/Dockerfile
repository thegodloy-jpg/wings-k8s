# =============================================================================
# Wings-Infer 统一控制容器 Dockerfile
# 支持引擎: vllm, vllm_ascend, sglang, mindie
# 支持模式: 单机 / 分布式
# 构建命令:
#   docker build -t wings-infer:latest .
#
# 构建产物:
#   - /app/app/*: Sidecar 后端 Python 代码
#   - /shared-volume: 启动脚本写入目录
# 端口:
#   - 17000: 引擎推理端口
#   - 18000: Proxy 代理
#   - 19000: Health 健康检查
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

RUN ls -la /app/app/ \
 && test -d /app/app/core    && echo "core/ OK" \
 && test -d /app/app/engines && echo "engines/ OK" \
 && test -d /app/app/proxy   && echo "proxy/ OK" \
 && test -d /app/app/utils   && echo "utils/ OK"

RUN mkdir -p /shared-volume

EXPOSE 17000 18000 19000

ENV http_proxy=""
ENV https_proxy=""

CMD ["python", "-m", "app.main"]
