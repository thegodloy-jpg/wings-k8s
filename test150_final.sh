#!/bin/bash
cat > /tmp/req.json << 'JSONEOF'
{"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"2+2=?"}],"max_tokens":20,"temperature":0}
JSONEOF
echo "=== Testing vLLM on .150 via proxy port 18000 ==="
curl -s --max-time 60 -X POST http://172.17.0.3:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d @/tmp/req.json
echo ""
echo "=== Test done ==="
