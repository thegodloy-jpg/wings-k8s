#!/bin/bash

# 构建 accel 镜像脚本
# 使用方法: bash build-accel-image.sh

set -e

echo "========================================="
echo "开始构建 wings-accel 镜像"
echo "========================================="

# 进入 wings-accel 目录
cd "$(dirname "$0")/wings-accel"

# 检查 Dockerfile 是否存在
if [ ! -f "Dockerfile" ]; then
    echo "错误: Dockerfile 不存在"
    exit 1
fi

# 构建镜像
echo "正在构建 wings-accel:latest 镜像..."
docker build -t wings-accel:latest .

# 检查构建是否成功
if [ $? -eq 0 ]; then
    echo "========================================="
    echo "镜像构建成功!"
    echo "镜像名称: wings-accel:latest"
    echo "========================================="
else
    echo "========================================="
    echo "镜像构建失败!"
    echo "========================================="
    exit 1
fi
