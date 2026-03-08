#!/bin/bash
# 注意！本脚本用于在物理机上一键启动xPyD服务，而非容器内使用

# 默认参数设置
DEFAULT_P_NODES=1
DEFAULT_D_NODES=1
DEFAULT_GPU_P="0"          # 默认每个P节点使用1个GPU
DEFAULT_GPU_D="1"          # 默认每个D节点使用1个GPU
DEFAULT_PORT_P_START=18100
DEFAULT_PORT_D_START=18200
DEFAULT_MODEL_PATH=""
DEFAULT_MODEL_NAME=""
DEFAULT_LOG_PATH="/var/log"
IMAGE=""
HEALTH_CHECK_PATH="/health"
HEALTH_CHECK_TIMEOUT=1200  # 20分钟超时

# 使用帮助
usage() {
    echo "Usage: $0 [options]"
    echo "Options:"
    echo "  -p, --p-nodes       Number of P nodes (default: $DEFAULT_P_NODES)"
    echo "  -d, --d-nodes       Number of D nodes (default: $DEFAULT_D_NODES)"
    echo "  -gp, --gpu-p        GPU devices for P nodes (semicolon separated list of comma-separated GPU ids per node, e.g., \"0;1,2;3,4,5\")"
    echo "  -gd, --gpu-d        GPU devices for D nodes (semicolon separated list of comma-separated GPU ids per node, e.g., \"6;7,8\")"
    echo "  -pp, --port-p-start Starting host port for P nodes (default: $DEFAULT_PORT_P_START)"
    echo "  -pd, --port-d-start Starting host port for D nodes (default: $DEFAULT_PORT_D_START)"
    echo "  -mp, --model-path   Model weights path (default: $DEFAULT_MODEL_PATH)"
    echo "  -mn, --model-name   Model name (default: $DEFAULT_MODEL_NAME)"
    echo "  -image              images name(default: $IMAGE)"
    echo "  -h, --help          Show this help"
    echo ""
    echo "GPU配置示例:"
    echo "  3个P节点: 第一个使用GPU0, 第二个使用GPU1和GPU2, 第三个使用GPU3,4,5"
    echo "    -gp \"0;1,2;3,4,5\""
    echo "  2个D节点: 第一个使用GPU6, 第二个使用GPU7和GPU8"
    echo "    -gd \"6;7,8\""
    exit 1
}

# 等待服务启动的函数
wait_for_server() {
    local port=$1
    local node_type=$2
    local gpu_list=$3
    local timeout_seconds=$4
    local start_time=$(date +%s)
    local health_endpoint="http://localhost:${port}${HEALTH_CHECK_PATH}"

    echo "[Health Check] Waiting for ${node_type} node on port ${port} (GPUs: ${gpu_list})"
    
    while true; do
        if curl -sSf "$health_endpoint" >/dev/null 2>&1; then
            local end_time=$(date +%s)
            local duration=$((end_time - start_time))
            echo "[Health Check] ${node_type} node on port ${port} is ready! (${duration}s)"
            return 0
        fi

        local current_time=$(date +%s)
        if (( current_time - start_time >= timeout_seconds )); then
            echo "[Health Check] Timeout waiting for ${node_type} node on port ${port}"
            return 1
        fi
        
        local elapsed=$((current_time - start_time))
        if (( elapsed % 10 == 0 )); then
            echo "[Health Check] Still waiting for ${node_type} node on port ${port} (${elapsed}/${timeout_seconds}s)"
        fi
        
        sleep 1
    done
}

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    key="$1"
    case $key in
        -p|--p-nodes)
        P_NODES="$2"
        shift; shift ;;
        -d|--d-nodes)
        D_NODES="$2"
        shift; shift ;;
        -gp|--gpu-p)
        GPU_P_STR="$2"
        shift; shift ;;
        -gd|--gpu-d)
        GPU_D_STR="$2"
        shift; shift ;;
        -pp|--port-p-start)
        PORT_P_START="$2"
        shift; shift ;;
        -pd|--port-d-start)
        PORT_D_START="$2"
        shift; shift ;;
        -mp|--model-path)
        MODEL_PATH="$2"
        shift; shift ;;
        -mn|--model-name)
        MODEL_NAME="$2"
        shift; shift ;;
        -image)
        IMAGE="$2"
        shift; shift ;;
        -h|--help)
        usage ;;
        *)
        echo "Unknown option: $1"
        usage ;;
    esac
done

# 设置默认值
P_NODES=${P_NODES:-$DEFAULT_P_NODES}
D_NODES=${D_NODES:-$DEFAULT_D_NODES}
GPU_P_STR=${GPU_P_STR:-$DEFAULT_GPU_P}
GPU_D_STR=${GPU_D_STR:-$DEFAULT_GPU_D}
PORT_P_START=${PORT_P_START:-$DEFAULT_PORT_P_START}
PORT_D_START=${PORT_D_START:-$DEFAULT_PORT_D_START}
MODEL_PATH=${MODEL_PATH:-$DEFAULT_MODEL_PATH}
MODEL_NAME=${MODEL_NAME:-$DEFAULT_MODEL_NAME}
LOG_PATH=${LOG_PATH:-$DEFAULT_LOG_PATH}

# 解析GPU配置（分号分隔每个节点的GPU配置）
IFS=';' read -ra GPU_P_NODES <<< "$GPU_P_STR"
IFS=';' read -ra GPU_D_NODES <<< "$GPU_D_STR"

# 验证GPU配置数量匹配
if [[ ${#GPU_P_NODES[@]} -ne $P_NODES ]]; then
    echo "Error: Number of P GPU configurations (${#GPU_P_NODES[@]}) does not match P nodes count ($P_NODES)"
    echo "Expected $P_NODES configurations but got ${#GPU_P_NODES[@]} in: $GPU_P_STR"
    exit 1
fi
if [[ ${#GPU_D_NODES[@]} -ne $D_NODES ]]; then
    echo "Error: Number of D GPU configurations (${#GPU_D_NODES[@]}) does not match D nodes count ($D_NODES)"
    echo "Expected $D_NODES configurations but got ${#GPU_D_NODES[@]} in: $GPU_D_STR"
    exit 1
fi

# 存储所有容器信息用于后续健康检查
declare -a ALL_NODES

# 启动P节点
echo "Starting $P_NODES P nodes..."
for ((i=0; i<P_NODES; i++)); do
    GPU_LIST=${GPU_P_NODES[$i]}
    PORT=$((PORT_P_START + i))
    NAME="AIspaceWings_test_P$i"
    
    # 记录节点信息（端口:类型:GPU列表）
    ALL_NODES+=("$PORT:P:$GPU_LIST")
    
    echo "  [P$i] Port: $PORT, GPUs: $GPU_LIST"
    docker run -d --shm-size=512g \
        --name "$NAME" \
        --gpus '"device='"$GPU_LIST"'"' \
        -v "$MODEL_PATH":/weights \
        -v "$LOG_PATH":/var/log \
        -p "$PORT":18000 \
        -e PD_ROLE=P \
        "$IMAGE" bash /opt/wings/wings_start.sh \
        --model-name "$MODEL_NAME" \
        --model-path /weights
done

# 启动D节点
echo "Starting $D_NODES D nodes..."
for ((i=0; i<D_NODES; i++)); do
    GPU_LIST=${GPU_D_NODES[$i]}
    PORT=$((PORT_D_START + i))
    NAME="AIspaceWings_test_D$i"
    
    # 记录节点信息（端口:类型:GPU列表）
    ALL_NODES+=("$PORT:D:$GPU_LIST")
    
    echo "  [D$i] Port: $PORT, GPUs: $GPU_LIST"
    docker run -d --shm-size=512g \
        --name "$NAME" \
        --gpus '"device='"$GPU_LIST"'"' \
        -v "$MODEL_PATH":/weights \
        -v "$LOG_PATH":/var/log \
        -p "$PORT":18000 \
        -e PD_ROLE=D \
        "$IMAGE" bash /opt/wings/wings_start.sh \
        --model-name "$MODEL_NAME" \
        --model-path /weights
done

# 健康检查所有节点
echo "Starting health check for all nodes (timeout: ${HEALTH_CHECK_TIMEOUT}s)"
all_success=true

for node_info in "${ALL_NODES[@]}"; do
    IFS=':' read -r port node_type gpu_list <<< "$node_info"
    
    if ! wait_for_server "$port" "$node_type" "$gpu_list" "$HEALTH_CHECK_TIMEOUT"; then
        echo "[Health Check] ERROR: ${node_type} node on port ${port} (GPUs: ${gpu_list}) failed to start"
        all_success=false
        
        # 获取容器日志以帮助诊断
        container_name=""
        if [[ "$node_type" == "P" ]]; then
            container_index=$((port - PORT_P_START))
            container_name="AIspaceWings_test_P${container_index}"
        else
            container_index=$((port - PORT_D_START))
            container_name="AIspaceWings_test_D${container_index}"
        fi
        
        echo "[Debug] Fetching last 20 lines of logs for container ${container_name}:"
        docker logs --tail 20 "$container_name"
        echo "----------------------------------------"
    fi
done

# 最终状态报告
if $all_success; then
    echo -e "\n\033[32mSUCCESS: All nodes started and healthy!\033[0m"
    
    # 打印P节点详情
    echo -e "\nP nodes ($P_NODES):"
    for ((i=0; i<P_NODES; i++)); do
        port=$((PORT_P_START + i))
        gpu_list=${GPU_P_NODES[$i]}
        echo "  [P$i] Port: $port, GPUs: $gpu_list"
    done
    
    # 打印D节点详情
    echo -e "\nD nodes ($D_NODES):"
    for ((i=0; i<D_NODES; i++)); do
        port=$((PORT_D_START + i))
        gpu_list=${GPU_D_NODES[$i]}
        echo "  [D$i] Port: $port, GPUs: $gpu_list"
    done
    
    exit 0
else
    echo -e "\n\033[31mERROR: One or more nodes failed to start\033[0m"
    exit 1
fi