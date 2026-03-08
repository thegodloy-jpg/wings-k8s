#!/usr/bin/env bash
set -euo pipefail

echo '=== HOST ==='
which nvidia-smi || true
nvidia-smi -L || true

echo '=== K3S CONTAINER ==='
docker exec k3s-verify sh -lc 'which nvidia-smi || true; ls -l /dev/nvidia* 2>/dev/null || true'

echo '=== VLLM ENGINE CONTAINER ==='
POD=$(docker exec k3s-verify kubectl -n wings-verify get pod -l app=wings-infer -o jsonpath='{.items[0].metadata.name}')
docker exec k3s-verify kubectl -n wings-verify exec "$POD" -c vllm-engine -- sh -lc '
  which nvidia-smi || true
  ls -l /dev/nvidia* 2>/dev/null || true
  python3 -c "import torch; print(\"cuda_available\", torch.cuda.is_available()); print(\"cuda_count\", torch.cuda.device_count())"
'
