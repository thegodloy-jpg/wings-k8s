#!/bin/bash
# Test SGLang inference
POD=infer-sglang-54f7f7c6b4-r69m5
NS=wings-infer
K3S=k3s-verify-server-zhanghui

echo "=== SGLang /v1/models ==="
docker exec $K3S kubectl exec -n $NS $POD -c sglang-engine -- \
  curl -s http://127.0.0.1:17000/v1/models

echo ""
echo "=== SGLang /v1/chat/completions (direct) ==="
docker exec $K3S kubectl exec -n $NS $POD -c sglang-engine -- \
  curl -s -X POST http://127.0.0.1:17000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"1+1=?"}],"max_tokens":30}'

echo ""
echo "=== wings-infer /v1/models (proxy 18000) ==="
docker exec $K3S kubectl exec -n $NS $POD -c wings-infer -- \
  curl -s http://127.0.0.1:18000/v1/models

echo ""
echo "=== wings-infer /v1/chat/completions (proxy 18000) ==="
docker exec $K3S kubectl exec -n $NS $POD -c wings-infer -- \
  curl -s -X POST http://127.0.0.1:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"1+1=?"}],"max_tokens":30}'
