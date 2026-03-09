#!/bin/bash
# ===== 新增的日志重定向部分 =====
LOG_DIR="/var/log/wings"

[ -d "$LOG_DIR" ] || mkdir -p "$LOG_DIR"
chmod 777 "$LOG_DIR" 2>/dev/null

LOG_FILE="$LOG_DIR/wings_start.log"
WINGS_LOG_FILE="$LOG_DIR/wings.log"
WINGS_MASTER_LOG_FILE="$LOG_DIR/wings_master.log"
WINGS_WORKER_LOG_FILE="$LOG_DIR/wings_worker.log"
WINGS_PROXY_LOG_FILE="$LOG_DIR/wings_proxy.log"
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")

# 备份旧日志（保留最近5个）
ls -t $LOG_FILE.* 2>/dev/null | tail -n +6 | xargs rm -f -- 2>/dev/null
[ -f "$LOG_FILE" ] && mv "$LOG_FILE" "$LOG_FILE.$TIMESTAMP"
ls -t $WINGS_LOG_FILE.* 2>/dev/null | tail -n +6 | xargs rm -f -- 2>/dev/null
[ -f "$WINGS_LOG_FILE" ] && mv "$WINGS_LOG_FILE" "$WINGS_LOG_FILE.$TIMESTAMP"
ls -t $WINGS_MASTER_LOG_FILE.* 2>/dev/null | tail -n +6 | xargs rm -f -- 2>/dev/null
[ -f "$WINGS_MASTER_LOG_FILE" ] && mv "$WINGS_MASTER_LOG_FILE" "$WINGS_MASTER_LOG_FILE.$TIMESTAMP"
ls -t $WINGS_WORKER_LOG_FILE.* 2>/dev/null | tail -n +6 | xargs rm -f -- 2>/dev/null
[ -f "$WINGS_WORKER_LOG_FILE" ] && mv "$WINGS_WORKER_LOG_FILE" "$WINGS_WORKER_LOG_FILE.$TIMESTAMP"
ls -t "$WINGS_PROXY_LOG_FILE".* 2>/dev/null | tail -n +6 | xargs rm -f -- 2>/dev/null
[ -f "$WINGS_PROXY_LOG_FILE" ] && mv "$WINGS_PROXY_LOG_FILE" "$WINGS_PROXY_LOG_FILE.$TIMESTAMP"

# 重定向所有输出到日志文件
exec > >(tee -a "$LOG_FILE") 2>&1

echo "===== [$(date)] Script started ====="
# ===== 日志重定向结束 =====

# ===== QAT设备文件转移 ======
if [ "$LMCACHE_QAT" = True ]; then
    DEVICE_PATTERNS=("uio*" "qat_*" "usdm_drv")

    # 遍历所有设备模式
    for pattern in "${DEVICE_PATTERNS[@]}"; do
    # 查找匹配该模式的文件
        for device in /tmp/host_dev/$pattern; do
            # 检查是否是真实文件（避免匹配到目录）
            if [ -e "$device" ] && [ ! -d "$device" ]; then
                dev_name=$(basename "$device")
                ln -sf "$device" "/dev/$dev_name"
            fi
        done
    done
fi

# 显示使用帮助
usage() {
    echo "Usage: $0 [OPTIONS]"
    echo "Options:"
    echo "  --host <value>            Set the host address"
    echo "  --port <value>            Set the port number"
    echo "  --model-name <value>      Set the model name"
    echo "  --trust-remote-code       Enable Trust remote code"
    echo "  --dtype <value>           Set the data type"
    echo "  --kv-cache-dtype <value>  Set the key-value cache data type"
    echo "  --quantization <value>    Set the quantization"
    echo "  --quantization-param-path <value> Set the quantization parameter path"
    echo "  --gpu-memory-utilization <value> Set the GPU memory utilization"
    echo "  --enable-chunked-prefill  Enable chunked prefill"
    echo "  --block-size <value>       Set the block size"
    echo "  --max-num-seqs <value>     Set the maximum number of sequences"
    echo "  --seed <value>             Set the seed"
    echo "  --enable-expert-parallel   Enable EP MOE"
    echo "  --max-num-batched-tokens <value>   Set max batch tokens for prefill"
    echo "  --enable-prefix-caching   Enable prefix caching"
    echo "  --model-path <value>      Set the model path"
    echo "  --engine <value>          Set the engine type"
    echo "  --input-length <value>    Set the max input length"
    echo "  --output-length <value>   Set the max output length"
    echo "  --distributed             Enable distributed mode"
    echo "  --config-file <value>     Specify a config file"
    echo "  --gpu-usage-mode <value>  Specify gpu usage mode"
    echo "  --device-count <value>    device count"
    echo "  --model-type <value>      model type, should be llm or embedding or rerank or mmum or mmgm"
    echo "  --save-path <value>       Top-level output dir for generated outputs (mmgm/wings)"
    echo "  --enable-speculative-decode     Enable speculative decoding feature"
    echo "  --speculative-decode-model-path <value>  Path to auxiliary model for speculative decoding"
    echo "  --enable-rag-acc            Enable RAG acceleration feature"
    echo "  --enable-auto-tool-choice   Enable function call feature"
    echo ""
    echo "Example:"
    echo "  $0 --model-name my_model --model-path my_model_path --input-length 4096 --output-length 1024"
    exit 1
}

DEFAULT_MODEL_NAME=""
DEFAULT_MODEL_PATH="/weights"
DEFAULT_TRUST_REMOTE_CODE=""
DEFAULT_DTYPE=""
DEFAULT_KV_CACHE_DTYPE=""
DEFAULT_QUANTIZATION=""
DEFAULT_QUANTIZATION_PARAM_PATH=""
DEFAULT_GPU_MEMORY_UTILIZATION=""
DEFAULT_ENABLE_CHUNKED_PREFILL=""
DEFAULT_BLOCK_SIZE=""
DEFAULT_MAX_NUM_SEQS=""
DEFAULT_SEED=""
DEFAULT_ENABLE_EXPERT_PARALLEL=""
DEFAULT_ENGINE=""
DEFAULT_MAX_NUM_BATCHED_TOKENS=""
DEFAULT_ENABLE_PREFIX_CACHING=""
DEFAULT_INPUT_LENGTH=""
DEFAULT_OUTPUT_LENGTH=""
DEFAULT_SAVE_PATH="/opt/wings/outputs"

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --host requires a value"
                usage
            fi
            HOST="$2"
            shift 2
            ;;
        --port)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --port requires a value"
                usage
            fi
            PORT="$2"
            shift 2
            ;;
        --model-name)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --model-name requires a value"
                usage
            fi
            MODEL_NAME="$2"
            shift 2
            ;;
        --trust-remote-code)
            TRUST_REMOTE_CODE=true
            shift
            ;;
        --dtype)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --dtype requires a value"
                usage
            fi
            DTYPE="$2"
            shift 2
            ;;
        --kv-cache-dtype)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --kv-cache-dtype requires a value"
                usage
            fi
            KV_CACHE_DTYPE="$2"
            shift 2
            ;;
        --quantization)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --quantization requires a value"
                usage
            fi
            QUANTIZATION="$2"
            shift 2
            ;;
        --quantization-param-path)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --quantization-param-path requires a value"
                usage
            fi
            QUANTIZATION_PARAM_PATH="$2"
            shift 2
            ;;
        --gpu-memory-utilization)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --gpu-memory-utilization requires a value"
                usage
            fi
            GPU_MEMORY_UTILIZATION="$2"
            shift 2
            ;;
        --enable-chunked-prefill)
            ENABLE_CHUNKED_PREFILL=true
            shift
            ;;
        --block-size)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --block-size requires a value"
                usage
            fi
            BLOCK_SIZE="$2"
            shift 2
            ;;
        --max-num-seqs)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --max-num-seqs requires a value"
                usage
            fi
            MAX_NUM_SEQS="$2"
            shift 2
            ;;
        --seed)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --seed requires a value"
                usage
            fi
            SEED="$2"
            shift 2
            ;;
        --enable-expert-parallel)
            ENABLE_EXPERT_PARALLEL=true
            shift
            ;;
        --max-num-batched-tokens)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --max-num-batched-tokens requires a value"
                usage
            fi
            MAX_NUM_BATCHED_TOKENS="$2"
            shift 2
            ;;
        --enable-prefix-caching)
            ENABLE_PREFIX_CACHING=true
            shift
            ;;
        --model-path)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --model-path requires a value"
                usage
            fi
            MODEL_PATH="$2"
            shift 2
            ;;
        --save-path)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --save-path requires a value"
                usage
            fi
            SAVE_PATH="$2"
            shift 2
            ;;

        --engine)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --engine requires a value"
                usage
            fi
            ENGINE="$2"
            shift 2
            ;;
        --input-length)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --input-length requires a value"
                usage
            fi
            INPUT_LENGTH="$2"
            shift 2
            ;;
        --output-length)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --output-length requires a value"
                usage
            fi
            OUTPUT_LENGTH="$2"
            shift 2
            ;;
        --distributed)
            DISTRIBUTED=true
            shift
            ;;
        --config-file)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --config-file requires a value"
                usage
            fi
            CONFIG_FILE="$2"
            shift 2
            ;;
        --gpu-usage-mode)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --gpu-usage-mode requires a value"
                usage
            fi
            GPU_USAGE_MODE="$2"
            shift 2
            ;;
        --device-count)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --device-count requires a value"
                usage
            fi
            DEVICE_COUNT="$2"
            shift 2
            ;;
        --model-type)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --model-type requires a value"
                usage
            fi
            MODEL_TYPE="$2"
            shift 2
            ;;
        --enable-speculative-decode)
            ENABLE_SPECULATIVE_DECODE=true
            shift
            ;;
        --speculative-decode-model-path)
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "Error: --speculative-decode-model-path requires a value"
                usage
            fi
            SPECULATIVE_DECODE_MODEL_PATH="$2"
            shift 2
            ;;
        --enable-rag-acc)
            ENABLE_RAG_ACC=true
            shift
            ;;
        --enable-auto-tool-choice)
            ENABLE_AUTO_TOOL_CHOICE=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Error: Unknown parameter: $1"
            echo "Did you mean one of these?"
            echo "  --host, --port, --model-name, --model-path, --engine"
            echo "  --input-length, --output-length, --distributed, --config-file"
            echo "  --enable-speculative-decode, --speculative-decode-model-path, --enable-rag-acc"
            usage
            ;;
    esac
done


# ===== 代理相关的环境控制（简化） =====
# 是否启动 Wings-Proxy（默认启动）
ENABLE_REASON_PROXY="${ENABLE_REASON_PROXY:-true}"

# 默认参数值（端口随代理开关切换）
DEFAULT_HOST=""
DEFAULT_PORT="18000"
if [[ "${ENABLE_REASON_PROXY,,}" == "false" ]]; then
  # 不启用代理：默认端口“归还”给 WINGS → 18000
  BACKEND_PORT=${PORT:-$DEFAULT_PORT}
else
  # 启用代理：WINGS 后端默认 17000，代理监听 18000
  PROXY_PORT=${PORT:-$DEFAULT_PORT}
  BACKEND_PORT="17000"
fi

# 设置默认值（如果未提供）
HOST=${HOST:-$DEFAULT_HOST}
MODEL_NAME=${MODEL_NAME:-$DEFAULT_MODEL_NAME}
MODEL_PATH=${MODEL_PATH:-$DEFAULT_MODEL_PATH}
ENGINE=${ENGINE:-$DEFAULT_ENGINE}
SAVE_PATH=${SAVE_PATH:-$DEFAULT_SAVE_PATH}

# 验证必要参数
if [[ -z "$MODEL_NAME" ]]; then
    echo "Error: Model name is required"
    usage
fi

# 构建WINGS命令参数
WINGS_ARGS="--model-name $MODEL_NAME --model-path $MODEL_PATH"

# 添加可选参数
[ -n "$SAVE_PATH" ] && WINGS_ARGS+=" --save-path $SAVE_PATH"
[ -n "$ENGINE" ] && WINGS_ARGS+=" --engine $ENGINE"
[ "$TRUST_REMOTE_CODE" = true ] && WINGS_ARGS+=" --trust-remote-code"
[ -n "$DTYPE" ] && WINGS_ARGS+=" --dtype $DTYPE"
[ -n "$KV_CACHE_DTYPE" ] && WINGS_ARGS+=" --kv-cache-dtype $KV_CACHE_DTYPE"
[ -n "$QUANTIZATION" ] && WINGS_ARGS+=" --quantization $QUANTIZATION"
[ -n "$QUANTIZATION_PARAM_PATH" ] && WINGS_ARGS+=" --quantization-param-path $QUANTIZATION_PARAM_PATH"
[ -n "$GPU_MEMORY_UTILIZATION" ] && WINGS_ARGS+=" --gpu-memory-utilization $GPU_MEMORY_UTILIZATION"
[ "$ENABLE_CHUNKED_PREFILL" = true ] && WINGS_ARGS+=" --enable-chunked-prefill"
[ -n "$BLOCK_SIZE" ] && WINGS_ARGS+=" --block-size $BLOCK_SIZE"
[ -n "$MAX_NUM_SEQS" ] && WINGS_ARGS+=" --max-num-seqs $MAX_NUM_SEQS"
[ -n "$SEED" ] && WINGS_ARGS+=" --seed $SEED"
[ "$ENABLE_EXPERT_PARALLEL" = true ] && WINGS_ARGS+=" --enable-expert-parallel"
[ -n "$MAX_NUM_BATCHED_TOKENS" ] && WINGS_ARGS+=" --max-num-batched-tokens $MAX_NUM_BATCHED_TOKENS"
[ "$ENABLE_PREFIX_CACHING" = true ] && WINGS_ARGS+=" --enable-prefix-caching"
[ -n "$HOST" ] && WINGS_ARGS+=" --host $HOST"
[ -n "$BACKEND_PORT" ] && WINGS_ARGS+=" --port $BACKEND_PORT"
[ -n "$INPUT_LENGTH" ] && WINGS_ARGS+=" --input-length $INPUT_LENGTH"
[ -n "$OUTPUT_LENGTH" ] && WINGS_ARGS+=" --output-length $OUTPUT_LENGTH"
[ -n "$CONFIG_FILE" ] && WINGS_ARGS+=" --config-file $CONFIG_FILE"
[ "$DISTRIBUTED" = true ] && WINGS_ARGS+=" --distributed"
[ -n "$GPU_USAGE_MODE" ] && WINGS_ARGS+=" --gpu-usage-mode $GPU_USAGE_MODE"
[ -n "$DEVICE_COUNT" ] && WINGS_ARGS+=" --device-count $DEVICE_COUNT"
[ -n "$MODEL_TYPE" ] && WINGS_ARGS+=" --model-type $MODEL_TYPE"
[ "$ENABLE_SPECULATIVE_DECODE" = true ] && WINGS_ARGS+=" --enable-speculative-decode"
[ -n "$SPECULATIVE_DECODE_MODEL_PATH" ] && WINGS_ARGS+=" --speculative-decode-model-path $SPECULATIVE_DECODE_MODEL_PATH"
[ "$ENABLE_RAG_ACC" = true ] && WINGS_ARGS+=" --enable-rag-acc"
[ "$ENABLE_AUTO_TOOL_CHOICE" = true ] && WINGS_ARGS+=" --enable-auto-tool-choice"

# 进入工作目录
cd /opt || exit 1

# 确保保存目录存在（不影响已有逻辑）
if [ -n "$SAVE_PATH" ]; then
  mkdir -p "$SAVE_PATH" 2>/dev/null || true
  echo "Resolved save-path: $SAVE_PATH"
fi


# ======= 主程序 PID 导出与文件路径（新增，最小改动）=======
# 统一约定 PID 文件路径，便于代理读取
mkdir -p /opt/wings/logs 2>/dev/null || true
export BACKEND_PID_FILE="${BACKEND_PID_FILE:-/var/log/wings/wings.txt}"
: > "$BACKEND_PID_FILE" 2>/dev/null || true   # 预创建空文件，代理可立即开始监控该文件

# 取容器内首个 IP
CONTAINER_IP="${RANK_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"

# 代理端口默认 18000（可覆盖）
PROXY_PORT="${PROXY_PORT:-18000}"
# 后端主机：默认容器 IP（可通过 BACKEND_HOST 覆盖）
BACKEND_HOST="${BACKEND_HOST:-${CONTAINER_IP}}"
# 后端端口取当前 WINGS 实际监听端口（PORT 变量）
BACKEND_URL="http://${BACKEND_HOST}:${BACKEND_PORT}"

# 代理监听地址固定为环回地址
PROXY_HOST="0.0.0.0"

# 以模块方式启动 wings_proxy；确保能 import 到 /opt/wings
export PYTHONPATH="/opt/wings:${PYTHONPATH}"
PYTHON_BIN="${PYTHON_BIN:-python}"

# ===== 启动 Wings-Proxy 的控制逻辑（与 DISTRIBUTED 保持一致） =====
PROXY_STARTED=0
export PROXY_PORT="${PROXY_PORT:-18000}"
export RAG_ACC_ENABLED=${ENABLE_RAG_ACC:-false}
export MODEL_NAME=${MODEL_NAME:-$DEFAULT_MODEL_NAME}
if [[ "${ENABLE_REASON_PROXY,,}" != "false" ]]; then
  if [ "$DISTRIBUTED" = true ]; then
    # 参与并发判定：仅在 MASTER 节点启动
    if [ "$MASTER_IP" = "$RANK_IP" ]; then
      echo "Wings-Proxy (distributed mode): start on MASTER only"
      echo "Starting Wings-Proxy... backend=${BACKEND_URL} listen=${PROXY_HOST}:${PROXY_PORT}"
      nohup "${PYTHON_BIN}" -m wings_proxy \
        --backend "${BACKEND_URL}" \
        --host "${PROXY_HOST}" \
        --port "${PROXY_PORT}" \
        > "$WINGS_PROXY_LOG_FILE" 2>&1 &
      WINGS_PROXY_PID=$!
      PROXY_STARTED=1
      echo "Wings-Proxy started (PID: $WINGS_PROXY_PID)"
      echo "Wings-Proxy log: $WINGS_PROXY_LOG_FILE"
    else
      echo "Wings-Proxy (distributed mode): not MASTER, skip starting proxy."
    fi
  else
    # 非分布式：直接启动代理
    echo "Wings-Proxy (standalone mode): start"
    echo "Starting Wings-Proxy... backend=${BACKEND_URL} listen=${PROXY_HOST}:${PROXY_PORT}"
    nohup "${PYTHON_BIN}" -m wings_proxy \
      --backend "${BACKEND_URL}" \
      --host "${PROXY_HOST}" \
      --port "${PROXY_PORT}" \
      > "$WINGS_PROXY_LOG_FILE" 2>&1 &
    WINGS_PROXY_PID=$!
    PROXY_STARTED=1
    echo "Wings-Proxy started (PID: $WINGS_PROXY_PID)"
    echo "Wings-Proxy log: $WINGS_PROXY_LOG_FILE"
  fi
else
  echo "Wings-Proxy disabled by env (ENABLE_REASON_PROXY=false). WINGS serves directly on port ${PORT}."
fi




write_pid_atomic() {  # $1=pid $2=path
  local tmp="${2}.tmp.$$"
  printf "%s" "$1" >"$tmp" && mv "$tmp" "$2"
}

if [ "$DISTRIBUTED" = true ]; then
  # ========== Master 进程（仅在 master 节点启动） ==========
  if [ "$MASTER_IP" = "$RANK_IP" ]; then
    echo "Starting distributed master on $MASTER_IP ..."
    nohup python -m wings.distributed.master >> "$WINGS_MASTER_LOG_FILE" 2>&1 &
    MASTER_PID=$!
    echo "Master PID: $MASTER_PID"
  fi

  # Temporarily disabled sleep to speed up startup for debugging purposes
  #sleep 20
  # ========== Worker 进程（每个节点/每个 rank 启动） ==========
  echo "Starting distributed worker ..."
  nohup python -m wings.distributed.worker >> "$WINGS_WORKER_LOG_FILE" 2>&1 &
  WORKER_PID=$!
  WORKER_TAG="${RANK:-$(hostname)}"
  echo "Worker PID: $WORKER_PID (tag=$WORKER_TAG)"
  export BACKEND_PID="$WORKER_PID"
  write_pid_atomic "$WORKER_PID" "$BACKEND_PID_FILE"   # 唯一健康探针 PID
  echo "Wings app PID: $WORKER_PID (wrote to $BACKEND_PID_FILE)"

  # ========== 主应用（仅在对外提供 HTTP 的节点启动与落盘） ==========
  if [ "$MASTER_IP" = "$RANK_IP" ]; then
    sleep 5
    echo "Starting wings application with args: $WINGS_ARGS"
    # 记录新功能启用状态
    [ "$ENABLE_SPECULATIVE_DECODE" = true ] && echo "Speculative decode feature enabled"
    [ -n "$SPECULATIVE_DECODE_MODEL_PATH" ] && echo "Speculative decode model path: $SPECULATIVE_DECODE_MODEL_PATH"
    [ "$ENABLE_RAG_ACC" = true ] && echo "RAG acceleration feature enabled"
    nohup python -m wings.wings $WINGS_ARGS >> "$WINGS_LOG_FILE" 2>&1 &
    WINGS_PID=$!
  else
    # 非 HTTP 节点不写 BACKEND_PID_FILE，避免误导健康探针
    echo "Non-HTTP node: skip writing $BACKEND_PID_FILE"
  fi

else
  # ========== 单机模式：仅主应用 ==========
  echo "Starting wings application with args: $WINGS_ARGS"
  # 记录新功能启用状态
  [ "$ENABLE_SPECULATIVE_DECODE" = true ] && echo "Speculative decode feature enabled"
  [ -n "$SPECULATIVE_DECODE_MODEL_PATH" ] && echo "Speculative decode model path: $SPECULATIVE_DECODE_MODEL_PATH"
  [ "$ENABLE_RAG_ACC" = true ] && echo "RAG acceleration feature enabled"
  nohup python -m wings.wings $WINGS_ARGS >> "$WINGS_LOG_FILE" 2>&1 &
  WINGS_PID=$!
  export BACKEND_PID="$WINGS_PID"
  write_pid_atomic "$WINGS_PID" "$BACKEND_PID_FILE"
  echo "Wings app PID: $WINGS_PID (wrote to $BACKEND_PID_FILE)"
fi


# 设置退出时的清理函数
cleanup() {
    echo "Shutting down processes..."
    # 杀死所有后台进程（包括tee进程）
    pkill -P $$  # 杀死当前进程的所有子进程
    # 杀死Python进程
    [ "$PROXY_STARTED" -eq 1 ] && [ -n "$WINGS_PROXY_PID" ] && kill "$WINGS_PROXY_PID" 2>/dev/null
    [ -n "$WINGS_PID" ] && kill $WINGS_PID 2>/dev/null
    if [ "$DISTRIBUTED" = true ]; then
        [ -n "$WORKER_PID" ] && kill $WORKER_PID 2>/dev/null
        # 只有在MASTER_IP等于RANK_IP时才尝试关闭MASTER_PID
        if [ "$MASTER_IP" = "$RANK_IP" ] && [ -n "$MASTER_PID" ]; then
            kill $MASTER_PID 2>/dev/null
        fi
    fi
    # 清理 PID 文件
    [ -n "$BACKEND_PID_FILE" ] && rm -f "$BACKEND_PID_FILE" 2>/dev/null || true
    # 关闭日志跟踪进程
    [ -n "$TAIL_PID" ] && kill $TAIL_PID 2>/dev/null
    # 杀死tee进程（如果有）
    if [ -f "$TEE_PID_FILE" ]; then
        TEE_PID=$(cat "$TEE_PID_FILE")
        kill $TEE_PID 2>/dev/null
        rm -f "$TEE_PID_FILE"
    fi
    
    wait
    echo "All processes stopped"
    exit 0
}

# 捕获退出信号
trap cleanup SIGTERM SIGINT

# 启动日志跟踪（后台运行）
echo "All services started. Container is running..."
echo "Wings log: $WINGS_LOG_FILE"
[ "$DISTRIBUTED" = true ] && echo "Worker log: $WINGS_WORKER_LOG_FILE"
[ "$DISTRIBUTED" = true ] && [ "$MASTER_IP" = "$RANK_IP" ] && echo "Master log: $WINGS_MASTER_LOG_FILE"
[ "$PROXY_STARTED" -eq 1 ] && echo "Wings-Proxy log: $WINGS_PROXY_LOG_FILE"
echo "BACKEND_PID_FILE: $BACKEND_PID_FILE"
[ -n "$BACKEND_PID" ] && echo "BACKEND_PID: $BACKEND_PID"

# 跟踪日志
if [ "$DISTRIBUTED" = true ]; then
    if [ "$PROXY_STARTED" -eq 1 ]; then
        TAIL_TARGETS=("$WINGS_WORKER_LOG_FILE")
    else
        TAIL_TARGETS=("$WINGS_WORKER_LOG_FILE")
    fi
else
    if [ "$PROXY_STARTED" -eq 1 ]; then
        TAIL_TARGETS=("$WINGS_LOG_FILE")
    else
        TAIL_TARGETS=("$WINGS_LOG_FILE")
    fi
fi

tail -f "${TAIL_TARGETS[@]}" &
TAIL_PID=$!

# 设置等待函数监控关键进程
wait_for_exit() {
    # 等待所有关键Python进程
    if [ "$DISTRIBUTED" = true ]; then
        if [ "$MASTER_IP" = "$RANK_IP" ]; then
            wait $WORKER_PID $WINGS_PID
        else
            wait $WORKER_PID
        fi
    else
        wait $WINGS_PID
    fi
}

# 监控进程退出
wait_for_exit

# 根据环境变量判断，执行清理
if [ "$KEEP_WINGS" = True ]; then
    tail -f /dev/null 
else
    cleanup
fi