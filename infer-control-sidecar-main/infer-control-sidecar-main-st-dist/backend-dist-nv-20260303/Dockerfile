# Wings-Infer 控制容器 Dockerfile（NV 分布式版本）
# 版本：dist-nv-20260303
# 基于：backend-ascend-st-2603030944
# 变更：启用 ray 依赖，COPY 指向 backend-dist-nv-20260303/app/

FROM python:3.10-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV http_proxy=http://90.90.99.124:3128
ENV https_proxy=http://90.90.99.124:3128

RUN apt-get update && apt-get install -y \
    curl \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY backend-dist-nv-20260303/requirements.txt .

RUN pip config set global.index-url "https://artifactrepo.wux-g.tools.xfusion.com/artifactory/pypi-public/simple" \
 && pip config set global.trusted-host "artifactrepo.wux-g.tools.xfusion.com"

RUN pip install --no-cache-dir -r requirements.txt

# app/ 代码从 backend-dist-nv-20260303 目录复制
# 若该目录 app/ 不存在（开发初期），回退到 backend-ascend-st-2603030944/app/
COPY backend-dist-nv-20260303/app ./app

RUN ls -la /app/app/ \
 && test -d /app/app/core && echo "✓ core/ 目录存在" \
 && test -d /app/app/engines && echo "✓ engines/ 目录存在"

RUN mkdir -p /shared-volume

EXPOSE 18000 19000

ENV http_proxy=""
ENV https_proxy=""

CMD ["python", "-m", "app.main"]
