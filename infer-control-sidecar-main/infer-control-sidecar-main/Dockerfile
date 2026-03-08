# Wings-Infer 控制容器 Dockerfile
FROM python:3.10-slim
#FROM ubuntu:22.04

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV http_proxy=http://90.90.99.124:3128
ENV https_proxy=http://90.90.99.124:3128
# 安装系统依赖
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY backend/requirements.txt .

RUN pip config set global.index-url "https://artifactrepo.wux-g.tools.xfusion.com/artifactory/pypi-public/simple"

RUN pip config set global.trusted-host "artifactrepo.wux-g.tools.xfusion.com"

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制完整的应用代码目录树（包括 core/ 等新增模块）
COPY backend/app ./app

# 验证核心模块已复制
RUN ls -la /app/app/ && test -d /app/app/core && echo "✓ core/ 目录存在" || echo "✗ core/ 目录缺失！"

# 创建共享卷目录
RUN mkdir -p /shared-volume

# 暴露端口
EXPOSE 9000
ENV http_proxy=""
ENV https_proxy=""

# 启动命令
CMD ["python", "-m", "app.main"]
