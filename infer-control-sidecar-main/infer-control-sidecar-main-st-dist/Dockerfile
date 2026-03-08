# Wings-Infer 控制容器 Dockerfile（NV 分布式版本）
# 版本：dist-nv-zhanghui-20260303
# 对应 backend：backend-dist-nv-20260303
# 构建命令（在本目录执行）：
#   docker build -f Dockerfile -t wings-infer:dist-nv-zhanghui-20260303 .
FROM python:3.10-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y \
    curl \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# 依赖（含 ray[default] 用于分布式）
COPY backend-dist-nv-20260303/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# 复制 NV 分布式专用 app 代码
COPY backend-dist-nv-20260303/app ./app

RUN ls -la /app/app/ \
 && test -d /app/app/core    && echo "✓ core/ 目录存在" \
 && test -d /app/app/engines && echo "✓ engines/ 目录存在"

RUN mkdir -p /shared-volume

EXPOSE 18000 19000

ENV http_proxy=""
ENV https_proxy=""

CMD ["python", "-m", "app.main"]
