#!/bin/bash

# 部署 wings-infer 并启用加速功能
# 使用方法: bash deploy-with-accel.sh

set -e

echo "========================================="
echo "开始部署 wings-infer (启用加速)"
echo "========================================="

# 检查 kubectl 是否可用
if ! command -v kubectl &> /dev/null; then
    echo "错误: kubectl 不可用"
    exit 1
fi

# 应用部署配置
echo "正在应用部署配置..."
kubectl apply -f k8s/deployment.yaml

# 等待部署完成
echo "等待部署完成..."
kubectl rollout status deployment/wings-infer

# 查看部署状态
echo "========================================="
echo "部署状态:"
kubectl get pods -l app=wings-infer

echo "========================================="
echo "查看日志:"
echo "wings-infer 容器日志:"
kubectl logs -l app=wings-infer -c wings-infer --tail=20

echo "========================================="
echo "vllm-engine 容器日志:"
kubectl logs -l app=wings-infer -c vllm-engine --tail=20

echo "========================================="
echo "部署完成!"
echo "========================================="
