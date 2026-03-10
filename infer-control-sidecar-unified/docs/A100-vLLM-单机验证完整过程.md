# A100 vLLM 单机验证完整过程

> **验证日期**: 2026-03-10  
> **验证环境**: 7.6.52.148 → k3s-in-Docker (k3s-verify-server-zhanghui)  
> **GPU**: NVIDIA A100-PCIE-40GB (编号 0, 40960 MiB)  
> **模型**: DeepSeek-R1-Distill-Qwen-1.5B  
> **引擎**: vLLM v0.13.0  
> **镜像**: wings-infer:entrypoint-zhanghui  

---

## 一、架构概述

```
┌─────────────────── Pod infer-0 ───────────────────┐
│                                                    │
│  ┌──────────────────┐   ┌───────────────────────┐  │
│  │  wings-infer      │   │  engine (vllm)         │  │
│  │  (Sidecar 控制)   │   │  (推理引擎)            │  │
│  │                    │   │                        │  │
│  │  :18000 proxy     │──▶│  :17000 vLLM API       │  │
│  │  :19000 health    │   │                        │  │
│  │                    │   │                        │  │
│  │  写入:             │   │  读取并执行:            │  │
│  │  /shared-volume/  │──▶│  /shared-volume/       │  │
│  │  start_command.sh │   │  start_command.sh      │  │
│  └──────────────────┘   └───────────────────────┘  │
│                                                    │
│  共享卷: emptyDir (shared-volume)                   │
│  模型卷: hostPath /mnt/models (只读)                │
│  GPU设备: /dev/nvidia0 (A100)                       │
└────────────────────────────────────────────────────┘
```

**核心设计**: wings-infer sidecar 根据 Dockerfile ENTRYPOINT 中的参数自动生成 `start_command.sh`，engine 容器轮询等待该脚本出现后执行。两个容器通过共享卷协调启动。

---

## 二、关键配置文件

### 2.1 Dockerfile（显式 ENTRYPOINT 模式）

```dockerfile
FROM python:3.10-slim
WORKDIR /app

# ... 依赖安装省略 ...

# 显式声明引擎和模型参数，构建即确定运行配置。
ENTRYPOINT ["bash", "/app/wings_start.sh", \
            "--engine", "vllm", \
            "--model-name", "DeepSeek-R1-Distill-Qwen-1.5B", \
            "--model-path", "/models/DeepSeek-R1-Distill-Qwen-1.5B", \
            "--device-count", "1", \
            "--trust-remote-code"]
CMD []
```

**优势**: 引擎/模型参数写死在镜像里，K8s YAML 无需传递 ENGINE、MODEL_NAME 等 env vars，部署更简洁。

### 2.2 StatefulSet YAML（精简版）

```yaml
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
    spec:
      nodeSelector:
        kubernetes.io/hostname: ca4109381399
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      volumes:
      - name: shared-volume
        emptyDir: {}
      - name: model-volume
        hostPath: { path: /mnt/models, type: DirectoryOrCreate }
      - name: dshm
        emptyDir: { medium: Memory, sizeLimit: 4Gi }
      - name: dev-nvidia0            # A100 GPU 0
        hostPath: { path: /dev/nvidia0, type: CharDevice }
      - name: dev-nvidiactl
        hostPath: { path: /dev/nvidiactl, type: CharDevice }
      - name: dev-nvidia-uvm
        hostPath: { path: /dev/nvidia-uvm, type: CharDevice }
      - name: nvidia-libs
        hostPath: { path: /mnt/nvidia-libs, type: Directory }

      containers:
      # --- wings-infer: Sidecar ---
      # 引擎参数已在 Dockerfile ENTRYPOINT 中声明
      # 此处仅保留运行时配置
      - name: wings-infer
        image: wings-infer:entrypoint-zhanghui
        env:
        - name: WINGS_SKIP_PID_CHECK
          value: "true"
        - name: BACKEND_URL
          value: "http://127.0.0.1:17000"
        volumeMounts:
        - { name: shared-volume, mountPath: /shared-volume }
        - { name: model-volume, mountPath: /models, readOnly: true }

      # --- engine: vLLM 推理引擎 ---
      - name: engine
        image: vllm/vllm-openai:v0.13.0
        securityContext: { privileged: true }
        command: ["/bin/sh", "-c"]
        args:
        - |
          echo '[engine] Waiting for start_command.sh...'
          while [ ! -f /shared-volume/start_command.sh ]; do sleep 2; done
          echo '[engine] start_command.sh found, executing:'
          cat /shared-volume/start_command.sh
          export LD_LIBRARY_PATH=/mnt/nvidia-libs:${LD_LIBRARY_PATH:-}
          cd /shared-volume && bash start_command.sh
        env:
        - { name: CUDA_VISIBLE_DEVICES, value: "0" }          # A100 = GPU 0
        - { name: CUDA_DEVICE_ORDER, value: "PCI_BUS_ID" }
        - { name: VLLM_HOST_IP, value: "127.0.0.1" }          # k3s-in-Docker 必需
        - { name: NCCL_SOCKET_IFNAME, value: "lo" }           # k3s-in-Docker 必需
        - { name: GLOO_SOCKET_IFNAME, value: "lo" }           # k3s-in-Docker 必需
        volumeMounts:
        - { name: shared-volume, mountPath: /shared-volume }
        - { name: model-volume, mountPath: /models, readOnly: true }
        - { name: dev-nvidia0, mountPath: /dev/nvidia0 }
        - { name: dev-nvidiactl, mountPath: /dev/nvidiactl }
        - { name: dev-nvidia-uvm, mountPath: /dev/nvidia-uvm }
        - { name: nvidia-libs, mountPath: /mnt/nvidia-libs }
        - { name: dshm, mountPath: /dev/shm }
```

---

## 三、验证步骤

### 步骤 1: 确认 GPU 状态

```bash
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader
```

**输出**:
```
0, NVIDIA A100-PCIE-40GB, 4 MiB, 40960 MiB, 0 %     ← 空闲，可用
1, NVIDIA L20, 41993 MiB, 46068 MiB, 100 %           ← 已被 tjl-vllm-blend-150 占用
```

### 步骤 2: 修改 YAML 使用 GPU 0 (A100)

将 YAML 中所有 `nvidia1` → `nvidia0`，`CUDA_VISIBLE_DEVICES: "1"` → `"0"`

### 步骤 3: 上传并部署

```bash
# 上传 YAML
scp statefulset-nv-single-148.yaml root@7.6.52.148:/tmp/statefulset-a100.yaml

# 复制进 k3s 容器并应用
docker cp /tmp/statefulset-a100.yaml k3s-verify-server-zhanghui:/tmp/
docker exec k3s-verify-server-zhanghui kubectl apply -f /tmp/statefulset-a100.yaml
```

**输出**: `statefulset.apps/infer created`

### 步骤 4: 确认 Pod 状态

```bash
kubectl get pods -n wings-infer -o wide
```

**输出**:
```
NAME      READY   STATUS    RESTARTS   AGE   IP           NODE
infer-0   2/2     Running   0          6s    172.17.0.3   ca4109381399
```

### 步骤 5: 检查 wings-infer (Sidecar) 日志

```bash
kubectl logs infer-0 -n wings-infer -c wings-infer --tail=30
```

**关键输出**:
```
===== [Tue Mar 10 06:32:47 UTC 2026] Script started =====
Starting wings application (sidecar launcher) with args:
  --model-name DeepSeek-R1-Distill-Qwen-1.5B
  --model-path /models/DeepSeek-R1-Distill-Qwen-1.5B
  --engine vllm --trust-remote-code --port 18000 --device-count 1
Port plan: backend=17000 proxy=18000 health=19000
[launcher] start command written: /shared-volume/start_command.sh
[launcher] 启动子进程 proxy: ... --port 18000
[launcher] 启动子进程 health: ... --port 19000
launcher running: backend=17000 proxy=18000 health=19000
Uvicorn running on http://0.0.0.0:19000
```

### 步骤 6: 检查 engine 容器日志

```bash
kubectl logs infer-0 -n wings-infer -c engine --tail=30
```

**关键输出**:
```
[engine] start_command.sh found, executing:
exec python3 -m vllm.entrypoints.openai.api_server \
  --trust-remote-code --max-model-len 5120 \
  --host 0.0.0.0 --port 17000 \
  --served-model-name DeepSeek-R1-Distill-Qwen-1.5B \
  --model /models/DeepSeek-R1-Distill-Qwen-1.5B \
  --tensor-parallel-size 1

vLLM API server version 0.13.0
Resolved architecture: Qwen2ForCausalLM
dtype=torch.bfloat16, max_seq_len=5120, tensor_parallel_size=1
Starting to load model /models/DeepSeek-R1-Distill-Qwen-1.5B...
...
Application startup complete.
GET /health → 200 OK  (健康检查持续通过)
```

### 步骤 7: 展示共享卷内容

```bash
kubectl exec infer-0 -n wings-infer -c wings-infer -- ls -la /shared-volume/
```

**输出**:
```
total 12
drwxrwxrwx 2 root root 4096 Mar 10 06:32 .
drwxr-xr-x 1 root root 4096 Mar 10 06:32 ..
-rw------- 1 root root  468 Mar 10 06:32 start_command.sh
```

**start_command.sh 内容** (由 wings-infer sidecar 自动生成):
```bash
#!/usr/bin/env bash
set -euo pipefail
exec python3 -m vllm.entrypoints.openai.api_server \
  --trust-remote-code \
  --max-model-len 5120 \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --host 0.0.0.0 \
  --port 17000 \
  --served-model-name DeepSeek-R1-Distill-Qwen-1.5B \
  --model /models/DeepSeek-R1-Distill-Qwen-1.5B \
  --dtype auto \
  --kv-cache-dtype auto \
  --gpu-memory-utilization 0.9 \
  --max-num-batched-tokens 4096 \
  --block-size 16 \
  --max-num-seqs 32 \
  --seed 0 \
  --tensor-parallel-size 1
```

### 步骤 8: 健康检查 (端口 19000)

```bash
curl -s http://127.0.0.1:19000/health
```

**输出**:
```json
{
  "s": 1,
  "p": "ready",
  "pid_alive": false,
  "backend_ok": true,
  "backend_code": 200,
  "interrupted": false,
  "ever_ready": true,
  "cf": 0,
  "lat_ms": 6
}
```

### 步骤 9: 推理测试 — Engine 直连 (端口 17000)

```bash
curl -s http://127.0.0.1:17000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"Hi"}],"max_tokens":50}'
```

**输出**:
```json
{
  "id": "chatcmpl-aa6c5faec53b8630",
  "model": "DeepSeek-R1-Distill-Qwen-1.5B",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "<think>\n\n</think>\n\nHello! How can I assist you today? 😊"
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 4,
    "completion_tokens": 16,
    "total_tokens": 20
  }
}
```

### 步骤 10: 推理测试 — Proxy 转发 (端口 18000)

```bash
curl -s http://127.0.0.1:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"Hi"}],"max_tokens":50}'
```

**输出**:
```json
{
  "id": "chatcmpl-9f7e6514161ad33e",
  "model": "DeepSeek-R1-Distill-Qwen-1.5B",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "<think>\n\n</think>\n\nHello! How can I assist you today? 😊"
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 4,
    "completion_tokens": 16,
    "total_tokens": 20
  }
}
```

---

## 四、GPU 显存确认

模型加载完成后的 GPU 状态:
```
0, NVIDIA A100-PCIE-40GB, 36877 MiB, 40960 MiB, 0 %     ← 我们的 vLLM 实例
1, NVIDIA L20, 41993 MiB, 46068 MiB, 100 %               ← 其他用户 (tjl-vllm-blend-150)
```

A100 显存占用 36877/40960 MiB ≈ 90%，符合 `--gpu-memory-utilization 0.9` 配置。

---

## 五、验证结果汇总

| 验证项 | 结果 | 说明 |
|--------|------|------|
| GPU 绑定 | ✅ | A100-PCIE-40GB (GPU 0), CUDA_VISIBLE_DEVICES=0 |
| Pod 状态 | ✅ | 2/2 Running, 无重启 |
| Sidecar 启动 | ✅ | 参数解析正确，start_command.sh 已生成 |
| 共享卷协调 | ✅ | engine 容器成功读取并执行 start_command.sh |
| 模型加载 | ✅ | Qwen2ForCausalLM, bfloat16, 36877 MiB |
| 健康检查 (:19000) | ✅ | s=1, p=ready, backend_ok=true |
| Engine 直连 (:17000) | ✅ | 推理正常，16 tokens, finish_reason=stop |
| Proxy 转发 (:18000) | ✅ | 请求正确转发到 backend |
| ENTRYPOINT 模式 | ✅ | K8s YAML 无需 ENGINE/MODEL 等 env vars |

---

## 六、k3s-in-Docker 特殊配置说明

由于验证环境使用 k3s-in-Docker（而非裸金属 K8s），需要额外配置：

| 环境变量 | 值 | 原因 |
|----------|-----|------|
| `CUDA_DEVICE_ORDER` | `PCI_BUS_ID` | 确保 GPU 编号与 nvidia-smi 一致 |
| `VLLM_HOST_IP` | `127.0.0.1` | 避免 c10d 分布式初始化使用容器内部 hostname |
| `NCCL_SOCKET_IFNAME` | `lo` | k3s-in-Docker 内无标准网络接口 |
| `GLOO_SOCKET_IFNAME` | `lo` | 同上 |

这些变量在生产裸金属 K8s 上**不需要**。
