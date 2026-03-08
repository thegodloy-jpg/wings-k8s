#!/bin/bash

# API 测试脚本

set -e

# 默认URL，可以通过参数覆盖
BASE_URL=${1:-"http://localhost:9000"}

# 颜色定义
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=== Wings-Infer API 测试 ===${NC}"
echo -e "服务地址: ${GREEN}${BASE_URL}${NC}"
echo ""

# 测试函数
test_endpoint() {
    local name=$1
    local method=$2
    local url=$3
    local data=$4

    echo -e "${BLUE}[测试]${NC} $name"
    echo "请求: $method $url"
    if [ -n "$data" ]; then
        echo "数据: $data"
    fi
    echo ""

    if [ "$method" = "GET" ]; then
        curl -s -X GET "${BASE_URL}${url}" | jq . || echo "响应不是有效的JSON"
    else
        curl -s -X POST "${BASE_URL}${url}" \
            -H "Content-Type: application/json" \
            -d "$data" | jq . || echo "响应不是有效的JSON"
    fi
    echo ""
    echo "---"
    echo ""
}

# 1. 健康检查
test_endpoint "健康检查" "GET" "/health"

# 2. 引擎状态
test_endpoint "引擎状态" "GET" "/engine/status"

# 3. 文本补全
test_endpoint "文本补全" "POST" "/v1/completions" '{
    "prompt": "Once upon a time",
    "max_tokens": 50,
    "temperature": 0.7
}'

# 4. 聊天补全
test_endpoint "聊天补全" "POST" "/v1/chat/completions" '{
    "messages": [
        {"role": "user", "content": "Hello, how are you?"}
    ],
    "max_tokens": 100,
    "temperature": 0.7
}'

# 5. 文本生成
test_endpoint "文本生成" "POST" "/generate" '{
    "prompt": "The future of AI is",
    "max_tokens": 100,
    "temperature": 0.8
}'

echo -e "${GREEN}=== 测试完成 ===${NC}"