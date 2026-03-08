#!/bin/bash

# 调试 accel 功能脚本
# 使用方法: bash debug-accel.sh

set -e

echo "========================================="
echo "调试 accel 功能"
echo "========================================="

# 获取 pod 名称
POD_NAME=$(kubectl get pods -l app=wings-infer -o jsonpath='{.items[0].metadata.name}')

if [ -z "$POD_NAME" ]; then
    echo "错误: 找不到 wings-infer pod"
    exit 1
fi

echo "Pod 名称: $POD_NAME"
echo ""

# 检查 accel-volume 是否存在
echo "========================================="
echo "检查 accel-volume 内容:"
kubectl exec $POD_NAME -c vllm-engine -- ls -la /accel-volume

echo ""
echo "========================================="
echo "检查 install.sh 是否存在:"
kubectl exec $POD_NAME -c vllm-engine -- cat /accel-volume/install.sh

echo ""
echo "========================================="
echo "检查 wings_engine_patch 目录:"
kubectl exec $POD_NAME -c vllm-engine -- ls -la /accel-volume/wings_engine_patch

echo ""
echo "========================================="
echo "检查 Python 包是否已安装:"
kubectl exec $POD_NAME -c vllm-engine -- pip list | grep -E "wings_engine_patch|wrapt"

echo ""
echo "========================================="
echo "vllm-engine 容器完整日志:"
kubectl logs $POD_NAME -c vllm-engine

echo ""
echo "========================================="
echo "调试完成!"
echo "========================================="
