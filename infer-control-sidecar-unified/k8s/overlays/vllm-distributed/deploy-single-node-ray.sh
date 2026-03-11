#!/bin/bash
# =============================================================================
# vLLM Ray 分布式 — 单机多 GPU 快速部署脚本
#
# 用法:
#   ./deploy-single-node-ray.sh [选项]
#
# 选项:
#   --node-name NAME       k3s 节点名 (kubectl get nodes)
#   --namespace NS         Kubernetes namespace (默认: wings-infer)
#   --gpu-count N          GPU 数量 (默认: 2)
#   --model-name NAME      模型名称
#   --model-host-path PATH 宿主机上的模型目录
#   --sidecar-image IMG    Sidecar 镜像 (默认: wings-infer:latest)
#   --engine-image IMG     vLLM 引擎镜像 (默认: vllm/vllm-openai:v0.13.0)
#   --nvidia-libs PATH     NVIDIA 用户态库路径 (可选)
#   --dry-run              只生成 YAML，不部署
#   --clean                清理部署
#   -h, --help             显示帮助
#
# 示例:
#   ./deploy-single-node-ray.sh \
#     --node-name my-node \
#     --gpu-count 2 \
#     --model-name DeepSeek-R1-Distill-Qwen-1.5B \
#     --model-host-path /mnt/models
# =============================================================================

set -euo pipefail

# ---- 默认值 ----
NAMESPACE="wings-infer"
GPU_COUNT=2
MODEL_NAME=""
MODEL_HOST_PATH="/mnt/models"
NODE_NAME=""
SIDECAR_IMAGE="wings-infer:latest"
ENGINE_IMAGE="vllm/vllm-openai:v0.13.0"
NVIDIA_LIBS="/mnt/nvidia-libs"
DRY_RUN=false
CLEAN=false
OUTPUT_FILE=""

# ---- 参数解析 ----
while [[ $# -gt 0 ]]; do
  case $1 in
    --node-name)      NODE_NAME="$2"; shift 2 ;;
    --namespace)      NAMESPACE="$2"; shift 2 ;;
    --gpu-count)      GPU_COUNT="$2"; shift 2 ;;
    --model-name)     MODEL_NAME="$2"; shift 2 ;;
    --model-host-path) MODEL_HOST_PATH="$2"; shift 2 ;;
    --sidecar-image)  SIDECAR_IMAGE="$2"; shift 2 ;;
    --engine-image)   ENGINE_IMAGE="$2"; shift 2 ;;
    --nvidia-libs)    NVIDIA_LIBS="$2"; shift 2 ;;
    --dry-run)        DRY_RUN=true; shift ;;
    --clean)          CLEAN=true; shift ;;
    -h|--help)
      head -30 "$0" | grep '^#' | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "未知选项: $1"; exit 1 ;;
  esac
done

# ---- 参数校验 ----
if [ "$CLEAN" = true ]; then
  echo "=== 清理部署 ==="
  kubectl delete statefulset infer -n "$NAMESPACE" 2>/dev/null || true
  kubectl delete service infer-hl infer-api -n "$NAMESPACE" 2>/dev/null || true
  echo "清理 IP 交换目录..."
  rm -rf /tmp/wings-ip-exchange/* 2>/dev/null || true
  echo "✓ 清理完成"
  exit 0
fi

if [ -z "$NODE_NAME" ]; then
  echo "错误: 必须指定 --node-name"
  echo "运行 'kubectl get nodes -o wide' 查看可用节点"
  exit 1
fi

if [ -z "$MODEL_NAME" ]; then
  echo "错误: 必须指定 --model-name"
  exit 1
fi

OUTPUT_FILE="/tmp/vllm-single-node-ray-${NAMESPACE}.yaml"

echo "=== vLLM Ray 分布式单机部署 ==="
echo "  节点:     $NODE_NAME"
echo "  GPU 数量: $GPU_COUNT"
echo "  模型:     $MODEL_NAME"
echo "  模型路径: $MODEL_HOST_PATH"
echo "  Sidecar:  $SIDECAR_IMAGE"
echo "  Engine:   $ENGINE_IMAGE"
echo "  Namespace: $NAMESPACE"
echo ""

# ---- 生成 GPU 设备卷 ----
generate_gpu_volumes() {
  for i in $(seq 0 $((GPU_COUNT - 1))); do
    cat <<EOF
      - name: dev-nvidia${i}
        hostPath:
          path: /dev/nvidia${i}
          type: CharDevice
EOF
  done
}

generate_gpu_volume_mounts() {
  for i in $(seq 0 $((GPU_COUNT - 1))); do
    cat <<EOF
        - name: dev-nvidia${i}
          mountPath: /dev/nvidia${i}
EOF
  done
}

# ---- 生成 YAML ----
cat > "$OUTPUT_FILE" <<YAML
# 自动生成: $(date '+%Y-%m-%d %H:%M:%S')
# GPU 数量: ${GPU_COUNT}, 模型: ${MODEL_NAME}, 节点: ${NODE_NAME}

---
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}

---
apiVersion: v1
kind: Service
metadata:
  name: infer-hl
  namespace: ${NAMESPACE}
spec:
  clusterIP: None
  selector:
    app.kubernetes.io/name: infer-vllm
  ports:
    - name: engine
      port: 17000
    - name: proxy
      port: 18000
    - name: health
      port: 19000

---
apiVersion: v1
kind: Service
metadata:
  name: infer-api
  namespace: ${NAMESPACE}
spec:
  selector:
    app.kubernetes.io/name: infer-vllm
    statefulset.kubernetes.io/pod-name: infer-0
  ports:
    - name: proxy
      port: 18000
      targetPort: 18000
    - name: health
      port: 19000
      targetPort: 19000

---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: infer
  namespace: ${NAMESPACE}
spec:
  serviceName: infer-hl
  replicas: ${GPU_COUNT}
  podManagementPolicy: Parallel
  selector:
    matchLabels:
      app.kubernetes.io/name: infer-vllm
  template:
    metadata:
      labels:
        app.kubernetes.io/name: infer-vllm
    spec:
      nodeName: ${NODE_NAME}
      dnsPolicy: Default
      volumes:
      - name: shared-volume
        emptyDir: {}
      - name: model-volume
        hostPath:
          path: ${MODEL_HOST_PATH}
          type: DirectoryOrCreate
      - name: ip-exchange
        hostPath:
          path: /tmp/wings-ip-exchange
          type: DirectoryOrCreate
      - name: dshm
        emptyDir:
          medium: Memory
          sizeLimit: 2Gi
$(generate_gpu_volumes)
      - name: dev-nvidiactl
        hostPath:
          path: /dev/nvidiactl
          type: CharDevice
      - name: dev-nvidia-uvm
        hostPath:
          path: /dev/nvidia-uvm
          type: CharDevice
      - name: nvidia-libs
        hostPath:
          path: ${NVIDIA_LIBS}
          type: Directory

      containers:
      - name: wings-infer
        image: ${SIDECAR_IMAGE}
        imagePullPolicy: IfNotPresent
        command: ["/bin/sh", "-c"]
        args:
        - |
          export NODE_RANK=\${POD_NAME##*-}
          rm -f /ip-exchange/pod-\${NODE_RANK}-ip
          echo \$POD_IP > /ip-exchange/pod-\${NODE_RANK}-ip
          if [ "\$NODE_RANK" = "0" ]; then
            export MASTER_IP=\$POD_IP
            export HEAD_NODE_ADDR=\$POD_IP
            NNODES_VAL=\${NNODES:-${GPU_COUNT}}
            for rank in \$(seq 1 \$((NNODES_VAL - 1))); do
              for i in \$(seq 1 60); do
                [ -f /ip-exchange/pod-\${rank}-ip ] && break
                sleep 2
              done
            done
            WORKER_IPS=""
            for rank in \$(seq 1 \$((NNODES_VAL - 1))); do
              WIP=\$(cat /ip-exchange/pod-\${rank}-ip 2>/dev/null || echo "unknown")
              WORKER_IPS="\${WORKER_IPS},\${WIP}"
            done
            export NODE_IPS="\${POD_IP}\${WORKER_IPS}"
          else
            for i in \$(seq 1 60); do
              [ -f /ip-exchange/pod-0-ip ] && break
              sleep 2
            done
            export MASTER_IP=\$(cat /ip-exchange/pod-0-ip 2>/dev/null || echo "unknown")
            export HEAD_NODE_ADDR=\$MASTER_IP
            export NODE_IPS="\${MASTER_IP},\${POD_IP}"
          fi
          echo "[wings] POD_NAME=\$POD_NAME NODE_RANK=\$NODE_RANK"
          echo "[wings] POD_IP=\$POD_IP MASTER_IP=\$MASTER_IP"
          echo "[wings] NODE_IPS=\$NODE_IPS"
          exec python -m app.main
        env:
        - name: POD_NAME
          valueFrom:
            fieldRef:
              fieldPath: metadata.name
        - name: POD_IP
          valueFrom:
            fieldRef:
              fieldPath: status.podIP
        - name: DISTRIBUTED
          value: "true"
        - name: RANK_IP
          valueFrom:
            fieldRef:
              fieldPath: status.podIP
        - name: ENGINE
          value: vllm
        - name: DISTRIBUTED_EXECUTOR_BACKEND
          value: ray
        - name: TENSOR_PARALLEL_SIZE
          value: "1"
        - name: NNODES
          value: "${GPU_COUNT}"
        - name: MODEL_NAME
          value: "${MODEL_NAME}"
        - name: MODEL_PATH
          value: "/models/${MODEL_NAME}"
        - name: ENGINE_PORT
          value: "17000"
        - name: PORT
          value: "18000"
        - name: HEALTH_PORT
          value: "19000"
        - name: WINGS_SKIP_PID_CHECK
          value: "true"
        volumeMounts:
        - name: shared-volume
          mountPath: /shared-volume
        - name: model-volume
          mountPath: /models
          readOnly: true
        - name: ip-exchange
          mountPath: /ip-exchange
        resources:
          requests:
            cpu: 500m
            memory: 1Gi
          limits:
            cpu: "2"
            memory: 4Gi

      - name: engine
        image: ${ENGINE_IMAGE}
        imagePullPolicy: IfNotPresent
        securityContext:
          privileged: true
        command: ["/bin/sh", "-c"]
        env:
        - name: POD_NAME
          valueFrom:
            fieldRef:
              fieldPath: metadata.name
        - name: POD_IP
          valueFrom:
            fieldRef:
              fieldPath: status.podIP
        args:
        - |
          ORDINAL=\${POD_NAME##*-}
          export CUDA_VISIBLE_DEVICES=\$ORDINAL
          echo "[engine] POD_NAME=\$POD_NAME ordinal=\$ORDINAL GPU=\$CUDA_VISIBLE_DEVICES POD_IP=\$POD_IP"
          echo '[engine] Waiting for start_command.sh...'
          while [ ! -f /shared-volume/start_command.sh ]; do sleep 2; done
          echo '[engine] Executing start_command.sh:'
          cat /shared-volume/start_command.sh
          echo '---'
          export LD_LIBRARY_PATH=${NVIDIA_LIBS}:\${LD_LIBRARY_PATH:-}
          cd /shared-volume && bash start_command.sh
        volumeMounts:
        - name: shared-volume
          mountPath: /shared-volume
        - name: model-volume
          mountPath: /models
          readOnly: true
$(generate_gpu_volume_mounts)
        - name: dev-nvidiactl
          mountPath: /dev/nvidiactl
        - name: dev-nvidia-uvm
          mountPath: /dev/nvidia-uvm
        - name: nvidia-libs
          mountPath: ${NVIDIA_LIBS}
        - name: dshm
          mountPath: /dev/shm
        resources:
          requests:
            cpu: "2"
            memory: 4Gi
          limits:
            cpu: "12"
            memory: 32Gi
YAML

echo "✓ YAML 已生成: $OUTPUT_FILE"
echo ""

if [ "$DRY_RUN" = true ]; then
  echo "=== Dry Run 模式 — 不部署 ==="
  echo "查看生成的 YAML: cat $OUTPUT_FILE"
  echo "手动部署: kubectl apply -f $OUTPUT_FILE"
  exit 0
fi

# ---- 清理旧 IP 交换文件 ----
echo "清理 IP 交换目录..."
rm -rf /tmp/wings-ip-exchange/* 2>/dev/null || true

# ---- 部署 ----
echo "部署中..."
kubectl apply -f "$OUTPUT_FILE"

echo ""
echo "=== 部署完成 ==="
echo ""
echo "监控 Pod 状态:"
echo "  kubectl -n $NAMESPACE get pods -w"
echo ""
echo "查看日志:"
echo "  kubectl -n $NAMESPACE logs infer-0 -c wings-infer --tail=50"
echo "  kubectl -n $NAMESPACE logs infer-0 -c engine --tail=50"
echo ""
echo "推理测试:"
echo "  kubectl -n $NAMESPACE exec infer-0 -c wings-infer -- curl -s http://127.0.0.1:18000/v1/models"
echo ""
echo "清理:"
echo "  $0 --clean --namespace $NAMESPACE"
