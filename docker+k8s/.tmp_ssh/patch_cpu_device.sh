#!/usr/bin/env bash
set -euo pipefail
f="/home/zhanghui/infer-control-sidecar-verify/project-src/k8s/deployment.verify.yaml"
python3 - <<'PY'
from pathlib import Path
p = Path('/home/zhanghui/infer-control-sidecar-verify/project-src/k8s/deployment.verify.yaml')
s = p.read_text()
old = '              cd /shared-volume\n              bash start_command.sh &'
new = '              cd /shared-volume\n              sed -i "s#vllm.entrypoints.openai.api_server #vllm.entrypoints.openai.api_server --device cpu #" /shared-volume/start_command.sh\n              bash start_command.sh &'
if old in s:
    s = s.replace(old, new)
    p.write_text(s)
    print('patched deployment.verify.yaml')
else:
    print('pattern not found; no patch')
PY

grep -n "start_command.sh" "$f" | head -n 20
cat "$f" | docker exec -i k3s-verify kubectl apply -f -
docker exec k3s-verify kubectl -n wings-verify rollout restart deploy/wings-infer
docker exec k3s-verify kubectl -n wings-verify rollout status deploy/wings-infer --timeout=240s
