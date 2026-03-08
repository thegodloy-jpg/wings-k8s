#!/usr/bin/env bash
set -euo pipefail

NAME="k3s-verify"

echo "[1] mkdir inside $NAME"
docker exec $NAME mkdir -p /usr/local/bin /etc/nvidia-container-runtime

echo "[2] copy nvidia binaries"
docker cp /usr/bin/nvidia-container-runtime $NAME:/usr/local/bin/
docker cp /usr/bin/nvidia-container-runtime-hook $NAME:/usr/local/bin/
docker cp /usr/bin/nvidia-container-cli $NAME:/usr/local/bin/

echo "[3] copy nvidia config"
docker cp /etc/nvidia-container-runtime/config.toml $NAME:/etc/nvidia-container-runtime/

echo "[4] test runtime binary"
docker exec $NAME /usr/local/bin/nvidia-container-runtime --version 2>&1 || true

echo "[5] check missing lib deps"
docker exec $NAME ldd /usr/local/bin/nvidia-container-runtime 2>&1 | grep 'not found' && echo "WARN: missing libs" || echo "OK: no missing libs"

echo "[6] write containerd config.toml.tmpl"
docker exec $NAME sh -c 'cat > /var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl << '"'"'EOF'"'"'
# k3s containerd config template with nvidia runtime
version = 2

[plugins."io.containerd.internal.v1.opt"]
  path = "/var/lib/rancher/k3s/agent/containerd"
[plugins."io.containerd.grpc.v1.cri"]
  stream_server_address = "127.0.0.1"
  stream_server_port = "10010"
  enable_selinux = false
  enable_unprivileged_ports = true
  enable_unprivileged_icmp = true
  sandbox_image = "rancher/mirrored-pause:3.6"

[plugins."io.containerd.grpc.v1.cri".containerd]
  snapshotter = "overlayfs"
  disable_snapshot_annotations = true

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc]
  runtime_type = "io.containerd.runc.v2"

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]
  SystemdCgroup = false

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia]
  runtime_type = "io.containerd.runc.v2"
  privileged_without_host_devices = false

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]
  BinaryName = "/usr/local/bin/nvidia-container-runtime"
  SystemdCgroup = false

[plugins."io.containerd.grpc.v1.cri".cni]
  bin_dir = "/bin"
  conf_dir = "/var/lib/rancher/k3s/agent/etc/cni/net.d"

[plugins."io.containerd.grpc.v1.cri".registry]
  config_path = "/var/lib/rancher/k3s/agent/etc/containerd/certs.d"
EOF
echo "tmpl written"'

echo "[7] restart k3s to regenerate containerd config"
docker exec $NAME sh -c 'kill -SIGHUP $(cat /var/run/k3s/k3s.pid 2>/dev/null || pgrep -x k3s | head -1) 2>/dev/null || true'
sleep 3

# Fallback: restart k3s service if SIGHUP did not work
docker exec $NAME sh -c 'rc-service k3s restart 2>/dev/null || service k3s restart 2>/dev/null || (kill $(pgrep -x k3s) && sleep 2 && nohup /usr/local/bin/k3s server --write-kubeconfig-mode 644 > /var/log/k3s-restart.log 2>&1 &) || true'
sleep 5

echo "[8] confirm nvidia runtime in new containerd config"
docker exec $NAME cat /var/lib/rancher/k3s/agent/etc/containerd/config.toml | grep -A3 'nvidia' || echo "WARN: nvidia not in config"

echo "=== done ==="
