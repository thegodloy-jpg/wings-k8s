#!/bin/bash
# inject-dns.sh — 在 k3s-in-Docker 环境中为分布式 pod 注入跨 pod DNS 解析
# 用法: 在 k3s server 容器内执行: bash /tmp/inject-dns.sh
# 原理: CoreDNS 在 k3s-in-Docker 中不可用，通过 kubectl 获取 pod IP 后
#       写入 peers_hosts 文件到各 pod 的 /shared-volume/
set -euo pipefail

NS="${1:-inference}"
STS="${2:-infer}"
REPLICAS="${3:-2}"

echo "[inject-dns] Namespace: $NS, StatefulSet: $STS, Replicas: $REPLICAS"

# 等待所有 pod 就绪
echo "[inject-dns] Waiting for all pods to be running..."
for i in $(seq 0 $((REPLICAS - 1))); do
  POD="${STS}-${i}"
  while true; do
    PHASE=$(kubectl get pod "$POD" -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    if [ "$PHASE" = "Running" ]; then
      break
    fi
    echo "[inject-dns] Waiting for $POD (phase=$PHASE)..."
    sleep 3
  done
done

# 收集所有 pod IP
HOSTS_CONTENT=""
for i in $(seq 0 $((REPLICAS - 1))); do
  POD="${STS}-${i}"
  POD_IP=$(kubectl get pod "$POD" -n "$NS" -o jsonpath='{.status.podIP}')
  FQDN="${POD}.infer-hl.${NS}.svc.cluster.local"
  SHORT="${POD}.infer-hl"
  LINE="${POD_IP} ${FQDN} ${SHORT} ${POD}"
  echo "[inject-dns] $LINE"
  HOSTS_CONTENT="${HOSTS_CONTENT}${LINE}\n"
done

# 注入到每个 pod 的两个容器的 shared-volume
for i in $(seq 0 $((REPLICAS - 1))); do
  POD="${STS}-${i}"
  # 写入 engine 容器的 shared-volume（engine 启动脚本会读取此文件）
  kubectl exec "$POD" -c engine -n "$NS" -- sh -c "printf '${HOSTS_CONTENT}' > /shared-volume/peers_hosts"
  echo "[inject-dns] Injected peers_hosts into $POD/engine"
done

echo "[inject-dns] Done! All pods should now resolve each other."
