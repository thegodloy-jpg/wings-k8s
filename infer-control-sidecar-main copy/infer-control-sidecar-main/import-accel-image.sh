#!/bin/bash

# 导入 accel 镜像到 containerd 脚本
# 使用方法: bash import-accel-image.sh

set -e

echo "========================================="
echo "开始导入 wings-accel 镜像到 containerd"
echo "========================================="

# 导出镜像
echo "正在导出镜像..."
docker save wings-accel:latest -o /tmp/wings-accel.tar

# 导入到 containerd
echo "正在导入到 containerd..."
ctr -n k8s.io images import /tmp/wings-accel.tar

# 清理临时文件
rm -f /tmp/wings-accel.tar

# 验证镜像
echo "验证镜像是否导入成功..."
ctr -n k8s.io images ls | grep wings-accel

echo "========================================="
echo "镜像导入成功!"
echo "========================================="
