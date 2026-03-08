#!/usr/bin/env bash
set -euo pipefail

NAME="${K3S_NAME:-k3s-verify}"
PORT="${K3S_PORT:-16443}"
IMAGE="${K3S_IMAGE:-rancher/k3s:v1.30.6-k3s1}"
WD="/home/zhanghui/docker-k8s-verify"

PRELOAD_IMAGES=(
  "rancher/mirrored-pause:3.6"
  "rancher/mirrored-coredns-coredns:1.11.3"
  "rancher/mirrored-metrics-server:v0.7.2"
  "rancher/local-path-provisioner:v0.0.30"
  "rancher/klipper-helm:v0.9.3-build20241008"
  "rancher/mirrored-library-traefik:2.11.10"
  "rancher/klipper-lb:v0.4.9"
)

preload_images() {
  for img in "${PRELOAD_IMAGES[@]}"; do
    if ! docker image inspect "$img" >/dev/null 2>&1; then
      docker pull "$img" >/dev/null
    fi
    docker save "$img" | docker exec -i "$NAME" ctr -n k8s.io images import - >/dev/null
  done
}

start_cluster() {
  mkdir -p "$WD"
  if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    docker rm -f "$NAME" >/dev/null
  fi
  docker run -d --name "$NAME" --privileged -p "${PORT}:6443" "$IMAGE" server --write-kubeconfig-mode 644 >/dev/null

  for _ in $(seq 1 30); do
    if docker exec "$NAME" kubectl get nodes >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done

  preload_images

  docker exec "$NAME" cat /etc/rancher/k3s/k3s.yaml > "$WD/kubeconfig.yaml"
  sed -i "s#https://127.0.0.1:6443#https://127.0.0.1:${PORT}#g" "$WD/kubeconfig.yaml"
  echo "k3s started: name=${NAME}, api=https://127.0.0.1:${PORT}"
}

status_cluster() {
  docker ps --filter "name=${NAME}" --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
  docker exec "$NAME" kubectl get nodes -o wide
}

stop_cluster() {
  docker rm -f "$NAME"
  echo "k3s stopped: ${NAME}"
}

kctl() {
  shift
  docker exec "$NAME" kubectl "$@"
}

case "${1:-status}" in
  start) start_cluster ;;
  status) status_cluster ;;
  stop) stop_cluster ;;
  kubectl) kctl "$@" ;;
  *) echo "Usage: $0 {start|status|stop|kubectl ...}"; exit 2 ;;
esac
