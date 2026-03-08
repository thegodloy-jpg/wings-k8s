#!/usr/bin/env bash
set -euo pipefail

NAME="k3s-verify"
PORT="16443"
IMAGE="rancher/k3s:v1.30.6-k3s1"
WD="/home/zhanghui/docker-k8s-verify"

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  docker rm -f "$NAME" >/dev/null
fi

docker run -d --name "$NAME" --privileged -p "${PORT}:6443" -v /home/xxs:/mnt/models "$IMAGE" server --write-kubeconfig-mode 644 >/dev/null

for i in $(seq 1 60); do
  if docker exec "$NAME" kubectl get nodes >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker exec "$NAME" cat /etc/rancher/k3s/k3s.yaml > "$WD/kubeconfig.yaml"
sed -i "s#https://127.0.0.1:6443#https://127.0.0.1:${PORT}#g" "$WD/kubeconfig.yaml"

IMAGES=(
  "rancher/mirrored-pause:3.6"
  "rancher/mirrored-coredns-coredns:1.11.3"
  "rancher/mirrored-metrics-server:v0.7.2"
  "rancher/local-path-provisioner:v0.0.30"
  "rancher/klipper-helm:v0.9.3-build20241008"
  "rancher/mirrored-library-traefik:2.11.10"
  "rancher/klipper-lb:v0.4.9"
  "vllm/vllm-openai:latest"
)

TS=$(cat /home/zhanghui/infer-control-sidecar-verify/TAG_TS)
IMAGES+=("wings-accel:verify-${TS}")
IMAGES+=("wings-infer:verify-${TS}")

for img in "${IMAGES[@]}"; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    docker pull "$img" >/dev/null
  fi
  docker save "$img" | docker exec -i "$NAME" ctr -n k8s.io images import - >/dev/null
done

echo "k3s recreated with model mount /home/xxs -> /mnt/models"
docker exec "$NAME" kubectl get nodes -o wide
