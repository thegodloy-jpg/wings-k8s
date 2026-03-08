#!/usr/bin/env bash
set -euo pipefail
POD=$(docker exec k3s-verify kubectl -n wings-verify get pod -l app=wings-infer -o jsonpath='{.items[0].metadata.name}')
docker exec k3s-verify kubectl -n wings-verify exec "$POD" -c wings-infer -- sh -c '
cat >/tmp/p.json <<JSON
{"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"hello"}],"max_tokens":8}
JSON
curl -sS -i -m 8 -H "Content-Type: application/json" --data @/tmp/p.json http://127.0.0.1:18000/v1/chat/completions
'
