# vLLM 单机验证指南 — infer-control-sidecar-unified on L20

> **目标**: 使用 `infer-control-sidecar-unified` 代码在 7.6.52.148 的 L20 上验证 vLLM 单机推理链路  
> **引擎**: vllm/vllm-openai:v0.13.0  
> **模型**: DeepSeek-R1-Distill-Qwen-1.5B  
> **GPU**: CUDA_VISIBLE_DEVICES=1 (L20, 46GB)

---

## 环境概况

| 项目 | 详情 |
|------|------|
| 宿主机 | 7.6.52.148 (root / xfusion@1234!) |
| 工作目录 | /home/zhanghui |
| k3s 容器 | k3s-verify-server-zhanghui |
| k3s namespace | wings-infer |
| GPU | GPU1 = L20 (46GB), CUDA_VISIBLE_DEVICES=1 |
| 模型路径 | /mnt/models/DeepSeek-R1-Distill-Qwen-1.5B |
| wings-infer 镜像 | wings-infer:unified-zhanghui |
| vLLM 引擎镜像 | vllm/vllm-openai:v0.13.0 (已在 k3s 中) |
| K8s hostname (.148) | ca4109381399 |

---

## 前置条件

以下环境在之前的分布式验证中已完成（参见 [nv.md](nv.md)）：

- [x] k3s 双节点集群运行中 (k3s-verify-server-zhanghui / k3s-verify-agent-zhanghui)
- [x] nvidia 驱动库已复制到 /mnt/nvidia-libs
- [x] /dev/nvidia1, /dev/nvidiactl, /dev/nvidia-uvm 设备存在
- [x] 模型文件已 hardlink 到 /mnt/models/DeepSeek-R1-Distill-Qwen-1.5B
- [x] vLLM 引擎镜像已导入 k3s containerd

---

## 步骤 1: 将 unified 代码传输到 .148

```bash
# 方式 A: 直接从本机 scp 整个项目到远程
scp -r F:\zhanghui\wings-k8s-260309\infer-control-sidecar-unified root@7.6.52.148:/home/zhanghui/infer-control-sidecar-unified

# 方式 B: 如果远程已有 git，直接 clone
ssh root@7.6.52.148
cd /home/zhanghui
git clone https://github.com/thegodloy-jpg/wings-k8s.git
# unified 代码在 wings-k8s/infer-control-sidecar-unified/
```

---

## 步骤 2: 构建 unified sidecar 镜像

```bash
ssh root@7.6.52.148

cd /home/zhanghui/infer-control-sidecar-unified

# 构建镜像（使用新 tag 区分于之前的 dist-nv-dev-zhanghui）
docker build -t wings-infer:unified-zhanghui .

# 验证构建
docker run --rm wings-infer:unified-zhanghui --help
```

---

## 步骤 3: 导入镜像到 k3s containerd

```bash
# 导出并导入到 k3s 容器
docker save wings-infer:unified-zhanghui -o /tmp/wings-infer-unified.tar
docker cp /tmp/wings-infer-unified.tar k3s-verify-server-zhanghui:/tmp/
docker exec k3s-verify-server-zhanghui ctr -n k8s.io images import /tmp/wings-infer-unified.tar

# 验证镜像已导入
docker exec k3s-verify-server-zhanghui ctr -n k8s.io images list | grep wings-infer
```

---

## 步骤 4: 清理之前的部署（如有）

```bash
# 检查是否有残留的 Pod 占用 GPU
docker exec k3s-verify-server-zhanghui kubectl get pods -A

# 如果 wings-infer namespace 中有运行中的 Pod，先删除
docker exec k3s-verify-server-zhanghui kubectl delete statefulset infer -n wings-infer --ignore-not-found
docker exec k3s-verify-server-zhanghui kubectl delete deployment infer-dp infer-sglang -n wings-infer --ignore-not-found

# 确认 GPU 已释放
docker exec k3s-verify-server-zhanghui kubectl get pods -n wings-infer
# 预期: No resources found
```

---

## 步骤 5: 创建 Namespace 和 Service（如未创建）

```bash
# 创建 namespace
docker exec k3s-verify-server-zhanghui kubectl create namespace wings-infer --dry-run=client -o yaml | \
  docker exec -i k3s-verify-server-zhanghui kubectl apply -f -

# Headless Service（单机其实不需要，但保持一致）
cat <<'EOF' | docker exec -i k3s-verify-server-zhanghui kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: infer-hl
  namespace: wings-infer
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
EOF

# ClusterIP Service（对外 API）
cat <<'EOF' | docker exec -i k3s-verify-server-zhanghui kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: infer-api
  namespace: wings-infer
spec:
  selector:
    app.kubernetes.io/name: infer-vllm
  ports:
    - name: proxy
      port: 18000
      targetPort: 18000
    - name: health
      port: 19000
      targetPort: 19000
EOF
```

---

## 步骤 6: 部署 vLLM 单机 StatefulSet

> 使用已有的 `statefulset-nv-single-148.yaml`，仅需更新镜像 tag 为 `wings-infer:unified-zhanghui`

```bash
cat <<'EOF' | docker exec -i k3s-verify-server-zhanghui kubectl apply -f -
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: infer
  namespace: wings-infer
spec:
  serviceName: infer-hl
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: infer-vllm
  template:
    metadata:
      labels:
        app.kubernetes.io/name: infer-vllm
    spec:
      nodeSelector:
        kubernetes.io/hostname: ca4109381399
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      volumes:
      - name: shared-volume
        emptyDir: {}
      - name: model-volume
        hostPath:
          path: /mnt/models
          type: DirectoryOrCreate
      - name: dshm
        emptyDir:
          medium: Memory
          sizeLimit: 4Gi
      - name: dev-nvidia1
        hostPath:
          path: /dev/nvidia1
          type: CharDevice
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
          path: /mnt/nvidia-libs
          type: Directory
      containers:
      - name: wings-infer
        image: wings-infer:unified-zhanghui
        imagePullPolicy: IfNotPresent
        env:
        - name: ENGINE
          value: vllm
        - name: MODEL_NAME
          value: DeepSeek-R1-Distill-Qwen-1.5B
        - name: MODEL_PATH
          value: /models/DeepSeek-R1-Distill-Qwen-1.5B
        - name: ENGINE_PORT
          value: "17000"
        - name: PORT
          value: "18000"
        - name: HEALTH_PORT
          value: "19000"
        - name: TENSOR_PARALLEL_SIZE
          value: "1"
        - name: WINGS_SKIP_PID_CHECK
          value: "true"
        - name: BACKEND_URL
          value: "http://127.0.0.1:17000"
        volumeMounts:
        - name: shared-volume
          mountPath: /shared-volume
        - name: model-volume
          mountPath: /models
          readOnly: true
      - name: engine
        image: vllm/vllm-openai:v0.13.0
        imagePullPolicy: IfNotPresent
        securityContext:
          privileged: true
        command: ["/bin/bash", "-c"]
        args:
        - |
          echo '[engine] Waiting for start_command.sh...'
          while [ ! -f /shared-volume/start_command.sh ]; do sleep 2; done
          echo '[engine] start_command.sh found, executing:'
          cat /shared-volume/start_command.sh
          export LD_LIBRARY_PATH="/mnt/nvidia-libs:${LD_LIBRARY_PATH:-}"
          cd /shared-volume && bash start_command.sh
        env:
        - name: CUDA_VISIBLE_DEVICES
          value: "1"
        - name: CUDA_DEVICE_ORDER
          value: "PCI_BUS_ID"
        - name: VLLM_HOST_IP
          value: "127.0.0.1"
        - name: NCCL_SOCKET_IFNAME
          value: "lo"
        - name: GLOO_SOCKET_IFNAME
          value: "lo"
        volumeMounts:
        - name: shared-volume
          mountPath: /shared-volume
        - name: model-volume
          mountPath: /models
          readOnly: true
        - name: dev-nvidia1
          mountPath: /dev/nvidia1
        - name: dev-nvidiactl
          mountPath: /dev/nvidiactl
        - name: dev-nvidia-uvm
          mountPath: /dev/nvidia-uvm
        - name: nvidia-libs
          mountPath: /mnt/nvidia-libs
        - name: dshm
          mountPath: /dev/shm
EOF
```

---

## 步骤 7: 观察 Pod 启动

```bash
# 实时观察 Pod 状态
docker exec k3s-verify-server-zhanghui kubectl get pods -n wings-infer -w

# 预期：
# NAME      READY   STATUS    RESTARTS   AGE
# infer-0   2/2     Running   0          2m

# 查看 wings-infer 容器日志（脚本生成）
docker exec k3s-verify-server-zhanghui kubectl logs -n wings-infer infer-0 -c wings-infer -f

# 查看 engine 容器日志（vLLM 启动）
docker exec k3s-verify-server-zhanghui kubectl logs -n wings-infer infer-0 -c engine -f

# 查看生成的启动脚本内容
docker exec k3s-verify-server-zhanghui kubectl exec -n wings-infer infer-0 -c wings-infer -- \
  cat /shared-volume/start_command.sh
```

**预期 engine 日志关键行**：
```
INFO:     Uvicorn running on http://0.0.0.0:17000
INFO:     Model loaded: DeepSeek-R1-Distill-Qwen-1.5B
INFO:     KV cache: ... GiB
INFO:     CUDA graphs captured
```

---

## 步骤 8: 验证健康检查

```bash
# 由于使用 hostNetwork，直接在 .148 宿主机上访问
curl http://127.0.0.1:19000/health
# 预期: 200 OK → 引擎就绪
# 如果 201 → 还在启动中，等待

# 详细状态
curl http://127.0.0.1:19000/health/detail
```

或通过 k3s 容器内部访问：
```bash
docker exec k3s-verify-server-zhanghui sh -c "curl -s http://127.0.0.1:19000/health"
```

---

## 步骤 9: 验证推理请求

```bash
# 方式 A: 从宿主机直接调用（hostNetwork 模式）
curl -X POST http://127.0.0.1:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "DeepSeek-R1-Distill-Qwen-1.5B",
    "messages": [{"role": "user", "content": "2 + 2 = ?"}],
    "max_tokens": 50
  }'

# 方式 B: 从 k3s 容器内调用
docker exec k3s-verify-server-zhanghui sh -c 'curl -s -X POST http://127.0.0.1:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"DeepSeek-R1-Distill-Qwen-1.5B\",
    \"messages\": [{\"role\": \"user\", \"content\": \"2 + 2 = ?\"}],
    \"max_tokens\": 50
  }"'

# 验证模型列表
curl http://127.0.0.1:18000/v1/models
# 预期: {"data":[{"id":"DeepSeek-R1-Distill-Qwen-1.5B",...}]}

# 验证直接引擎端口
curl http://127.0.0.1:17000/v1/models
# 预期: 与 18000 返回的模型列表一致（验证 proxy 转发正确）
```

**预期推理返回**：
```json
{
  "id": "chatcmpl-...",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "2 + 2 = 4..."
    }
  }]
}
```

---

## 步骤 10: 验证通过后清理

```bash
# 删除 StatefulSet（释放 GPU）
docker exec k3s-verify-server-zhanghui kubectl delete statefulset infer -n wings-infer

# 确认 Pod 已删除
docker exec k3s-verify-server-zhanghui kubectl get pods -n wings-infer
```

---

## 验证检查清单

| # | 检查项 | 预期结果 | 实际结果 |
|---|--------|----------|----------|
| 1 | 镜像构建 | `wings-infer:unified-zhanghui` 构建成功 | ✅ 构建成功 |
| 2 | 镜像导入 k3s | `ctr images list` 可见 | ✅ 已导入 |
| 3 | Pod 启动 | infer-0 2/2 Running | ✅ 2/2 Running |
| 4 | start_command.sh 生成 | 包含 `vllm.entrypoints.openai.api_server` | ✅ 正确生成 |
| 5 | /health 返回 200 | 引擎就绪 | ✅ `{"s":1,"p":"ready","backend_ok":true}` |
| 6 | /v1/models 正常 | 返回 DeepSeek-R1-Distill-Qwen-1.5B | ✅ max_model_len=5120 |
| 7 | /v1/chat/completions | 返回正确推理结果 | ✅ "1+1=2", "2+2=4" |
| 8 | proxy 链路 | 18000 → 17000 转发正常 | ✅ 转发正常 |

---

## 常见问题

| 问题 | 解决方案 |
|------|---------|
| `Failed to infer device type` | 检查 /dev/nvidia1 等设备挂载是否正确 |
| `libcuda.so.1 not found` | 确认 /mnt/nvidia-libs 中有实体驱动库文件（非 symlink） |
| `config.json not found` | 确认模型路径，使用 `cp -al` hardlink 而非 symlink |
| health 返回 201 | 模型加载中，等待 1-3 分钟 |
| health 返回 502 | 检查 engine 容器日志 `kubectl logs infer-0 -c engine` |
| proxy 502 | 确认 BACKEND_URL=http://127.0.0.1:17000 |
| `export: /shared-volume: bad variable name` | engine 容器需使用 `command: ["/bin/bash", "-c"]` 而非 `/bin/sh`，并给 LD_LIBRARY_PATH 加双引号 |
| vLLM 使用了错误的 GPU | 必须同时设置 `CUDA_DEVICE_ORDER=PCI_BUS_ID` 和 `CUDA_VISIBLE_DEVICES=1` |
| c10d hostname hang（进程 0% CPU 挂死） | 设置 `VLLM_HOST_IP=127.0.0.1` + `NCCL_SOCKET_IFNAME=lo` + `GLOO_SOCKET_IFNAME=lo` |
