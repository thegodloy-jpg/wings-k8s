#!/bin/bash
# =============================================================================
#  build-accel-image.sh — 构建 wings-accel 加速包镜像
# =============================================================================
#
#  功能：将 wings-accel/ 目录构建为一个轻量级 initContainer 镜像。
#        该镜像在 K8s Pod 启动时，将加速文件拷贝到 accel-volume 共享卷，
#        供 engine 容器按需安装。
#
#  使用方法: bash build-accel-image.sh [TAG]
#    TAG 默认为 latest
#
#  产出镜像: wings-accel:<TAG>
# =============================================================================

set -euo pipefail

TAG="${1:-latest}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "========================================="
echo "开始构建 wings-accel 镜像"
echo "========================================="

cd "$SCRIPT_DIR/wings-accel"

if [ ! -f "Dockerfile" ]; then
    echo "错误: wings-accel/Dockerfile 不存在"
    exit 1
fi

echo "正在构建 wings-accel:${TAG} 镜像..."
docker build -t "wings-accel:${TAG}" .

echo "========================================="
echo "镜像构建成功!"
echo "镜像名称: wings-accel:${TAG}"
echo "========================================="
