#!/bin/bash
K3S_IP=$(docker inspect k3s-verify-server-zhanghui --format "{{.NetworkSettings.IPAddress}}")
echo "HEAD K3S_IP=$K3S_IP"
cat > /tmp/req_dp.json << 'JSONEOF'
{"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"3+3=?"}],"max_tokens":20,"temperature":0}
JSONEOF
echo "=== Testing DP Head proxy port 18000 ==="
curl -s --max-time 60 -X POST http://${K3S_IP}:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d @/tmp/req_dp.json
echo ""
echo "=== Test done ==="
