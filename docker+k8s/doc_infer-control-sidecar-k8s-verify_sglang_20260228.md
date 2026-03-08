# SGLang 引擎 K8s 验证文档

**日期**: 2026-02-28  
**验证人员**: zhanghui  
**目标**: 在 k3s-verify 集群上用 A100 GPU 验证 `backend-20260228` 多引擎代码中的 SGLang 引擎

---

## 目录

1. [环境信息](#1-环境信息)
2. [前置准备](#2-前置准备)
3. [镜像构建](#3-镜像构建)
4. [K8s 资源文件](#4-k8s-资源文件)
5. [部署流程](#5-部署流程)
6. [遇到的问题与修复](#6-遇到的问题与修复)
7. [验证测试](#7-验证测试)
8. [验证结果](#8-验证结果)
9. [注意事项](#9-注意事项)

---

## 1. 环境信息

| 项目 | 值 |
|------|-----|
| 远程服务器 | `root@7.6.52.148`（hostname: `a100`） |
| k3s 集群 | `k3s-verify`（Docker 容器，privileged，Alpine/musl libc） |
| Namespace | `wings-verify` |
| A100 UUID | `GPU-3ad4c258-3338-a49d-b6db-5059e89d9811`（40GB，本次验证使用） |
| L20 UUID | `GPU-715b6a2e-d331-6e21-ac7b-40d382d6bf04`（46GB，生产服务，**严禁触碰**） |
| Nvidia Driver | 550.90.07 |
| SGLang 版本 | 0.5.9 |
| 模型 | `DeepSeek-R1-Distill-Qwen-1.5B` |
| 模型路径（宿主机） | `/mnt/models/DeepSeek-R1-Distill-Qwen-1.5B` |
| 模型路径（容器内） | `/models/DeepSeek-R1-Distill-Qwen-1.5B` |
| 端口规划 | engine=17000, proxy=18000, health=19000 |
| NodePort | 31800→18000（proxy），31900→19000（health） |

### GPU 情况说明

服务器上有两块 GPU，L20 被生产业务占用，**整个验证过程必须锁定在 A100 上**：

```
GPU 0: NVIDIA A100-PCIE-40GB (UUID: GPU-3ad4c258-3338-a49d-b6db-5059e89d9811)  ← 本次使用
GPU 1: NVIDIA L20             (UUID: GPU-715b6a2e-d331-6e21-ac7b-40d382d6bf04)  ← 禁止触碰
```

L20 上运行的生产服务（不得干扰）：
- `tjl-vllm-blend-150`（L20，41946 MiB）
- `wgd_vllm0130_origin`、`wgd_vllm0130_rkv`
- `swh-vllm`、`ragflow-server`、`piston_api` 等

---

## 2. 前置准备

### 2.1 确认 k3s-verify 容器在运行

k3s-verify 是一个 Docker 容器（privileged 模式），有时会自动停止，每次操作前需确认：

```bash
# 检查状态
docker ps | grep k3s-verify

# 若未运行，启动它
docker start k3s-verify

# 确认 k3s 内部 node 就绪
docker exec k3s-verify kubectl get nodes
```

### 2.2 确认驱动库目录

k3s-verify 使用 hostPath 挂载宿主机驱动库，目录为 `/mnt/nvidia-libs`（容器内映射为 `/usr/lib/nvidia-host`）：

```bash
# 检查驱动库是否存在
docker exec k3s-verify ls /mnt/nvidia-libs/ | head -10

# 确认 nvidia-smi 已复制进去（SGLang 0.5.9 启动时必须调用）
docker exec k3s-verify ls -la /mnt/nvidia-libs/nvidia-smi
```

> **注意**：SGLang 0.5.9 在 `server_args.py __post_init__` 中调用 `nvidia-smi` 查询 GPU 内存。
> 若 `/mnt/nvidia-libs/` 中没有 `nvidia-smi`，需要从宿主机复制：
> ```bash
> docker cp /usr/bin/nvidia-smi k3s-verify:/mnt/nvidia-libs/nvidia-smi
> ```

### 2.3 确认 Namespace 存在

```bash
docker exec k3s-verify kubectl get namespace wings-verify || \
  docker exec k3s-verify kubectl create namespace wings-verify
```

### 2.4 确认模型文件存在

```bash
ls /mnt/models/DeepSeek-R1-Distill-Qwen-1.5B/
# 预期看到 config.json、model.safetensors 等文件
```

---

## 3. 镜像构建

### 3.1 构建 sglang-infer 镜像

基于 `vllm/vllm-openai:latest`（已有 CUDA 环境）追加安装 sglang：

```dockerfile
# Dockerfile.sglang
FROM vllm/vllm-openai:latest
RUN pip install sglang==0.5.9
```

```bash
# 在服务器上构建（约 23.2 GB）
docker build -f Dockerfile.sglang -t sglang-infer:zhanghui-20260228 .

# 导入到 k3s containerd（k3s 不使用 docker daemon）
docker save sglang-infer:zhanghui-20260228 | docker exec -i k3s-verify ctr images import -

# 确认导入成功
docker exec k3s-verify ctr images ls | grep sglang-infer
```

### 3.2 构建 wings-infer 镜像（backend-20260228 代码）

```dockerfile
# Dockerfile.sidecar-20260228
FROM python:3.10-slim
COPY backend-20260228/requirements.txt .
RUN pip install -r requirements.txt
COPY backend-20260228/app ./app
CMD ["python3", "-m", "app.main"]
```

```bash
# 上传代码到服务器
scp -r backend-20260228/ root@7.6.52.148:/tmp/wings-build/

# 在服务器上构建（约 172 MB）
cd /tmp/wings-build
docker build -f Dockerfile.sidecar-20260228 -t wings-infer:zhanghui-20260228 .

# 导入到 k3s containerd
docker save wings-infer:zhanghui-20260228 | docker exec -i k3s-verify ctr images import -

# 确认两个镜像都已导入
docker exec k3s-verify ctr images ls | grep "zhanghui-20260228"
```

预期输出：
```
docker.io/library/sglang-infer:zhanghui-20260228   ...  23.2 GiB
docker.io/library/wings-infer:zhanghui-20260228    ...  172 MiB
```

---

## 4. K8s 资源文件

### 4.1 Deployment（`k8s/deployment-sglang.verify.yaml`）

双容器 Pod 架构：
- **wings-infer**：控制容器，负责生成引擎启动命令、提供 proxy（18000）和 health（19000）接口
- **sglang-engine**：引擎容器，运行 SGLang server（17000），通过 `shared-volume` 接收启动命令

关键配置要点：

```yaml
# wings-infer 容器 - 引擎类型必须用 ENGINE（不是 ENGINE_TYPE）
- name: ENGINE
  value: "sglang"

# sglang-engine 容器 - GPU 锁定（必须用 UUID，不能用索引 0/1）
- name: CUDA_DEVICE_ORDER
  value: "PCI_BUS_ID"
- name: CUDA_VISIBLE_DEVICES
  value: "GPU-3ad4c258-3338-a49d-b6db-5059e89d9811"   # A100 UUID
- name: NVIDIA_VISIBLE_DEVICES
  value: "GPU-3ad4c258-3338-a49d-b6db-5059e89d9811"

# sglang-engine 容器 - 安全上下文（打通 /dev/nvidia* 访问）
securityContext:
  privileged: true

# sglang-engine 启动脚本头部（双重保险，脚本内显式设置）
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=GPU-3ad4c258-3338-a49d-b6db-5059e89d9811
export LD_LIBRARY_PATH=/usr/lib/nvidia-host:${LD_LIBRARY_PATH:-}
export PATH=/usr/lib/nvidia-host:${PATH}

# Volume 挂载
volumes:
  - name: nvidia-libs
    hostPath:
      path: /mnt/nvidia-libs    # 宿主机驱动库（含 nvidia-smi）
      type: Directory
volumeMounts:
  - name: nvidia-libs
    mountPath: /usr/lib/nvidia-host
```

### 4.2 Service（`k8s/service-sglang.verify.yaml`）

```yaml
apiVersion: v1
kind: Service
metadata:
  name: wings-infer-sglang-service
  namespace: wings-verify
spec:
  type: NodePort
  ports:
    - port: 18000
      targetPort: 18000
      nodePort: 31800     # proxy 对外端口
      name: proxy
    - port: 19000
      targetPort: 19000
      nodePort: 31900     # health 对外端口
      name: health
  selector:
    app: wings-infer-sglang
```

---

## 5. 部署流程

### 5.1 上传 YAML 并部署

```bash
# 上传 YAML 到服务器
scp k8s/deployment-sglang.verify.yaml \
    k8s/service-sglang.verify.yaml \
    root@7.6.52.148:/tmp/

# 确认 k3s-verify 运行
ssh root@7.6.52.148 "docker start k3s-verify 2>/dev/null; docker ps | grep k3s-verify"

# 应用资源
ssh root@7.6.52.148 "
  docker cp /tmp/deployment-sglang.verify.yaml k3s-verify:/tmp/
  docker cp /tmp/service-sglang.verify.yaml k3s-verify:/tmp/
  docker exec k3s-verify kubectl apply -f /tmp/deployment-sglang.verify.yaml
  docker exec k3s-verify kubectl apply -f /tmp/service-sglang.verify.yaml
"
```

### 5.2 监控 Pod 启动

```bash
# 查看 Pod 状态（直到 2/2 Running）
ssh root@7.6.52.148 "docker exec k3s-verify kubectl -n wings-verify get pods -w"

# 查看 wings-infer 日志（确认引擎命令生成正确）
ssh root@7.6.52.148 "docker exec k3s-verify kubectl -n wings-verify \
  logs <POD_NAME> -c wings-infer --tail=30"

# 查看 sglang-engine 日志（主要观察 GPU 选择和加载进度）
ssh root@7.6.52.148 "docker exec k3s-verify kubectl -n wings-verify \
  logs <POD_NAME> -c sglang-engine --tail=30"
```

### 5.3 启动进度关键日志

sglang-engine 容器日志中，依次出现以下内容表示启动正常：

```
# 1. GPU 识别正确（必须是 A100，不能是 L20）
nvidia-smi path: /usr/lib/nvidia-host/nvidia-smi
GPU 0: NVIDIA A100-PCIE-40GB (UUID: GPU-3ad4c258-3338-a49d-b6db-5059e89d9811)

# 2. wings-infer 写入启动命令
Start command found:
python3 -m sglang.launch_server --context-length 5120 ...

# 3. 模型开始加载（显示可用显存，A100 应为 ~38 GB）
[HH:MM:SS] Load weight begin. avail mem=38.87 GB

# 4. 模型加载完成
[HH:MM:SS] Load weight end. elapsed=2.73s, avail mem=35.36 GB

# 5. KV Cache 分配
[HH:MM:SS] KV Cache allocated. #tokens: 1178703, K size: 15.74 GB, V size: 15.74 GB

# 6. CUDA Graph Capture（可能需要数分钟）
[HH:MM:SS] Capture cuda graph begin. bs [1, 2, 4, 8, 12, 16, 24, 32]

# 7. 启动完成
[HH:MM:SS] The server is fired up and ready to roll!
```

### 5.4 重新部署（如需）

```bash
# 删除已有部署
ssh root@7.6.52.148 "docker exec k3s-verify kubectl -n wings-verify \
  delete deployment wings-infer-sglang --ignore-not-found"

# 等待 Pod 完全终止后重新 apply
ssh root@7.6.52.148 "docker exec k3s-verify kubectl apply \
  -f /tmp/deployment-sglang.verify.yaml"
```

---

## 6. 遇到的问题与修复

### 问题一：ENGINE_TYPE vs ENGINE 环境变量名错误

**现象**：sglang-engine 容器启动后，`start_command.sh` 仍生成 vllm 命令，而非 sglang 命令

**根因**：`app/core/start_args_compat.py` 读取的是 `ENGINE` 环境变量：
```python
p.add_argument("--engine", default=_env("ENGINE", "vllm"))
```
YAML 中误写为 `ENGINE_TYPE`，导致 engine 默认保持为 vllm

**修复**：
```yaml
# ❌ 错误
- name: ENGINE_TYPE
  value: "sglang"

# ✅ 正确
- name: ENGINE
  value: "sglang"
```

---

### 问题二：nvidia-smi 缺失导致启动失败

**现象**：
```
OSError: [Errno 8] Exec format error: 'nvidia-smi'
```

**根因**：SGLang 0.5.9 在 `server_args.py __post_init__` 中调用 `nvidia-smi` 查询 GPU 显存。k3s-verify 是 Alpine/musl 容器，其内的 glibc ELF 格式的 `nvidia-smi` 无法直接执行。

**修复**：将宿主机的 `nvidia-smi`（原生 x86 glibc ELF）复制到 nvidia-libs 目录，通过 hostPath 挂载暴露进容器：
```bash
docker cp /usr/bin/nvidia-smi k3s-verify:/mnt/nvidia-libs/nvidia-smi
```

同时在启动脚本中将该目录加入 PATH：
```bash
export PATH=/usr/lib/nvidia-host:${PATH}
```

---

### 问题三：容器内 GPU 索引错乱（L20 被误选）

**现象**：
```
torch.OutOfMemoryError: GPU 0 has 44.52 GiB of which 35.38 MiB is free
```

44.52 GiB 是 L20 的容量（非 A100 的 40 GB），说明 `CUDA_VISIBLE_DEVICES=0` 在容器内映射到了 L20。

**根因**：容器内 CUDA 设备枚举顺序与宿主机 PCI 总线顺序可能不同，用数字索引 `0` 不可靠。

**修复**：改用 GPU UUID 精确锁定 A100：
```yaml
- name: CUDA_DEVICE_ORDER
  value: "PCI_BUS_ID"
- name: CUDA_VISIBLE_DEVICES
  value: "GPU-3ad4c258-3338-a49d-b6db-5059e89d9811"   # A100 UUID
- name: NVIDIA_VISIBLE_DEVICES
  value: "GPU-3ad4c258-3338-a49d-b6db-5059e89d9811"
```

启动脚本内也同步设置（双重保险）：
```bash
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=GPU-3ad4c258-3338-a49d-b6db-5059e89d9811
```

---

## 7. 验证测试

Pod 状态变为 `2/2 Running` 后执行以下测试。

### 7.1 获取 Pod 名称

```bash
POD=$(ssh root@7.6.52.148 "docker exec k3s-verify kubectl -n wings-verify \
  get pods -l app=wings-infer-sglang -o jsonpath='{.items[0].metadata.name}'")
echo "Pod: $POD"
```

### 7.2 健康检查

```bash
# 写测试脚本到服务器
cat > /tmp/test_sglang.sh << 'EOF'
#!/bin/bash
POD=$(docker exec k3s-verify kubectl -n wings-verify \
  get pods -l app=wings-infer-sglang -o jsonpath='{.items[0].metadata.name}')
NS=wings-verify

echo "=== Pod Status ==="
docker exec k3s-verify kubectl -n $NS get pod $POD

echo ""
echo "=== Health Check (19000/health) ==="
docker exec k3s-verify kubectl -n $NS exec $POD -c wings-infer -- \
  curl -s http://127.0.0.1:19000/health

echo ""
echo "=== Model List (18000/v1/models) ==="
docker exec k3s-verify kubectl -n $NS exec $POD -c wings-infer -- \
  curl -s http://127.0.0.1:18000/v1/models

echo ""
echo "=== Direct SGLang Test (17000) ==="
docker exec k3s-verify kubectl -n $NS exec $POD -c sglang-engine -- \
  sh -c 'echo "{\"model\":\"DeepSeek-R1-Distill-Qwen-1.5B\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"max_tokens\":30}" > /tmp/req.json && \
  curl -s -X POST http://127.0.0.1:17000/v1/chat/completions \
  -H "Content-Type: application/json" -d @/tmp/req.json'

echo ""
echo "=== Proxy Test (18000) ==="
docker exec k3s-verify kubectl -n $NS exec $POD -c wings-infer -- \
  sh -c 'echo "{\"model\":\"DeepSeek-R1-Distill-Qwen-1.5B\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"max_tokens\":30}" > /tmp/req.json && \
  curl -s -X POST http://127.0.0.1:18000/v1/chat/completions \
  -H "Content-Type: application/json" -d @/tmp/req.json'

echo ""
echo "=== GPU Memory Usage ==="
nvidia-smi --query-gpu=index,name,memory.used --format=csv,noheader
EOF

scp /tmp/test_sglang.sh root@7.6.52.148:/tmp/test_sglang.sh
ssh root@7.6.52.148 "bash /tmp/test_sglang.sh"
```

---

## 8. 验证结果

### 8.1 Pod 状态

```
NAME                                  READY   STATUS    RESTARTS   AGE
wings-infer-sglang-78988cbfb9-zbkqq   2/2     Running   0          2m30s
```

### 8.2 启动日志关键输出

```
nvidia-smi path: /usr/lib/nvidia-host/nvidia-smi       ← nvidia-smi 找到
GPU 0: NVIDIA A100-PCIE-40GB (UUID: GPU-3ad4c258-...)  ← A100 被正确选中

[08:29:50] Load weight begin. avail mem=38.87 GB       ← A100 40GB 确认
[08:29:52] Load weight end. elapsed=2.73s, avail mem=35.36 GB
[08:29:52] KV Cache allocated. #tokens: 1178703, K size: 15.74 GB, V size: 15.74 GB
[08:29:53] Capture cuda graph begin. bs [1, 2, 4, 8, 12, 16, 24, 32]
[08:30:18] The server is fired up and ready to roll!   ← 启动完成
```

### 8.3 健康检查

```json
{
  "s": 1,
  "p": "ready",
  "backend_ok": true,
  "backend_code": 200,
  "ever_ready": true
}
```

### 8.4 模型列表

```json
{
  "object": "list",
  "data": [{
    "id": "DeepSeek-R1-Distill-Qwen-1.5B",
    "object": "model",
    "owned_by": "sglang",
    "max_model_len": 5120
  }]
}
```

### 8.5 推理测试（直连 SGLang 17000）

请求：
```json
{"model": "DeepSeek-R1-Distill-Qwen-1.5B", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 30}
```

响应：
```json
{
  "id": "f41481b61b5d40828491e1fc4efd56ab",
  "object": "chat.completion",
  "model": "DeepSeek-R1-Distill-Qwen-1.5B",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "<think>\n\n</think>\n\nHello! How can I assist you today? 😊"
    },
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 4, "completion_tokens": 16, "total_tokens": 20}
}
```

### 8.6 推理测试（经 wings-infer proxy 18000）

响应与直连完全一致，proxy 转发正常 ✅

### 8.7 GPU 显存确认

```
0, NVIDIA A100-PCIE-40GB, 37190 MiB   ← sglang 使用 A100（KV cache ~31.5 GB）
1, NVIDIA L20,            42053 MiB   ← L20 生产服务完全未受影响 ✅
```

### 8.8 验证汇总

| 验证项 | 结果 |
|--------|------|
| Pod 状态 2/2 Running | ✅ PASS |
| GPU 正确选中 A100（38.87 GB 可用）| ✅ PASS |
| L20 生产服务未受影响 | ✅ PASS |
| wings-infer 健康检查 /health | ✅ PASS |
| SGLang ENGINE 路由正确 | ✅ PASS |
| 推理直连 sglang 17000 | ✅ PASS |
| 推理经 proxy 18000 转发 | ✅ PASS |

---

## 9. 注意事项

### 9.1 GPU 锁定必须用 UUID

容器内 CUDA 设备索引（`0`、`1`）与宿主机 PCI 顺序可能不一致，**务必使用 UUID**：

```bash
# 查询宿主机所有 GPU 的 UUID
nvidia-smi -L
# GPU 0: NVIDIA A100-PCIE-40GB (UUID: GPU-3ad4c258-3338-a49d-b6db-5059e89d9811)
# GPU 1: NVIDIA L20             (UUID: GPU-715b6a2e-d331-6e21-ac7b-40d382d6bf04)
```

### 9.2 SGLang 需要宿主机 nvidia-smi

SGLang 0.5.9 启动时强制调用 `nvidia-smi` 查询 GPU 显存。非 glibc 容器（如 Alpine/musl）内需要通过 hostPath 挂载宿主机的 `nvidia-smi`：

```bash
docker cp /usr/bin/nvidia-smi k3s-verify:/mnt/nvidia-libs/nvidia-smi
```

### 9.3 环境变量名

wings-infer 的引擎选择读取 `ENGINE` 变量（不是 `ENGINE_TYPE`）：

```yaml
- name: ENGINE       # ← 必须是 ENGINE
  value: "sglang"
```

### 9.4 k3s-verify 稳定性

k3s-verify Docker 容器可能自动停止，每次操作前需检查：

```bash
docker ps | grep k3s-verify || docker start k3s-verify
```

### 9.5 生成的 SGLang 启动命令

wings-infer 根据配置自动生成（写入 `/shared-volume/start_command.sh`）：

```bash
python3 -m sglang.launch_server \
  --context-length 5120 \
  --trust-remote-code \
  --dtype auto \
  --kv-cache-dtype auto \
  --mem-fraction-static 0.9 \
  --max-running-requests 32 \
  --chunked-prefill-size 4096 \
  --random-seed 0 \
  --disable-chunked-prefix-cache \
  --host 0.0.0.0 \
  --port 17000 \
  --served-model-name DeepSeek-R1-Distill-Qwen-1.5B \
  --model-path /models/DeepSeek-R1-Distill-Qwen-1.5B \
  --tp-size 1 \
  --ep-size 1
```

---

## 1. 环境信息

| 项目 | 值 |
|------|-----|
| 远程服务器 | `root@7.6.52.148`（hostname: `a100`） |
| k3s 集群 | `k3s-verify`（Docker 容器，privileged，Alpine/musl） |
| Namespace | `wings-verify` |
| A100 UUID | `GPU-3ad4c258-3338-a49d-b6db-5059e89d9811`（40GB，用于本次验证） |
| L20 UUID | `GPU-715b6a2e-d331-6e21-ac7b-40d382d6bf04`（46GB，生产服务，**严禁触碰**） |
| Nvidia Driver | 550.90.07 |
| 模型 | `DeepSeek-R1-Distill-Qwen-1.5B`（路径: `/mnt/models/DeepSeek-R1-Distill-Qwen-1.5B`） |

---

## 2. 镜像构建

### 2.1 sglang-infer 镜像

```dockerfile
# Dockerfile.sglang（临时构建）
FROM vllm/vllm-openai:latest
RUN pip install sglang==0.5.9
```

```bash
# 构建
docker build -f Dockerfile.sglang -t sglang-infer:zhanghui-20260228 .

# 导入到 k3s containerd
docker save sglang-infer:zhanghui-20260228 | docker exec -i k3s-verify ctr images import -
```

**结果**: 23.2GB，成功导入 k3s containerd ✅

### 2.2 wings-infer 镜像（backend-20260228 代码）

```dockerfile
# Dockerfile.sidecar-20260228
FROM python:3.10-slim
COPY backend-20260228/requirements.txt .
RUN pip install -r requirements.txt
COPY backend-20260228/app ./app
```

```bash
docker build -f Dockerfile.sidecar-20260228 -t wings-infer:zhanghui-20260228 .
docker save wings-infer:zhanghui-20260228 | docker exec -i k3s-verify ctr images import -
```

**结果**: 172MB，成功导入 k3s containerd ✅

---

## 3. 遇到的问题及修复

### 问题一：ENGINE_TYPE vs ENGINE 环境变量名错误

**现象**: sglang-engine 容器启动后，`start_command.sh` 仍生成 vllm 命令

**根因**: `app/core/start_args_compat.py` 第102行读取 `ENGINE` 环境变量：
```python
p.add_argument("--engine", default=_env("ENGINE", "vllm"))
```
YAML 中误写为 `ENGINE_TYPE`，导致 engine 默认为 vllm

**修复**:
```yaml
# 错误写法
- name: ENGINE_TYPE
  value: "sglang"

# 正确写法
- name: ENGINE
  value: "sglang"
```

---

### 问题二：nvidia-smi 缺失

**现象**:
```
OSError: [Errno 8] Exec format error: 'nvidia-smi'
```

**根因**: SGLang 0.5.9 在 `server_args.py __post_init__` 中调用 `nvidia-smi` 查询 GPU 内存，但容器内 `/usr/bin/nvidia-smi` 是宿主机 x86 ELF，k3s-verify 是 musl libc 环境无法执行

**修复**:
```bash
# 将 nvidia-smi 复制到 nvidia-libs 目录（已 bind mount 进容器）
docker cp /usr/bin/nvidia-smi k3s-verify:/mnt/nvidia-libs/nvidia-smi

# YAML 启动脚本中添加 PATH
export PATH=/usr/lib/nvidia-host:${PATH}
```

容器内 `/usr/lib/nvidia-host/` 是宿主机 `/mnt/nvidia-libs/` 的 hostPath 挂载，其中 `nvidia-smi` 来自宿主机，可直接执行

---

### 问题三：容器内 GPU 索引错乱（L20 被误选）

**现象**:
```
torch.OutOfMemoryError: GPU 0 has 44.52 GiB of which 35.38 MiB is free
```

44.52 GiB 是 L20 的容量，说明 `CUDA_VISIBLE_DEVICES=0` 在容器内映射到了 L20，而非 A100

**根因**: 容器内 CUDA 设备顺序与宿主机 PCI 顺序不同，`CUDA_VISIBLE_DEVICES=0` 不可靠

**修复**: 使用 A100 UUID 精确锁定
```yaml
# env 段
- name: CUDA_DEVICE_ORDER
  value: "PCI_BUS_ID"
- name: CUDA_VISIBLE_DEVICES
  value: "GPU-3ad4c258-3338-a49d-b6db-5059e89d9811"
- name: NVIDIA_VISIBLE_DEVICES
  value: "GPU-3ad4c258-3338-a49d-b6db-5059e89d9811"

# 启动脚本头部（双重保险）
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=GPU-3ad4c258-3338-a49d-b6db-5059e89d9811
```

---

## 4. 最终 YAML 配置要点

文件: `k8s/deployment-sglang.verify.yaml`

### sglang-engine 容器关键配置

```yaml
env:
  - name: ENGINE
    value: "sglang"
  - name: CUDA_DEVICE_ORDER
    value: "PCI_BUS_ID"
  - name: CUDA_VISIBLE_DEVICES
    value: "GPU-3ad4c258-3338-a49d-b6db-5059e89d9811"
  - name: NVIDIA_VISIBLE_DEVICES
    value: "GPU-3ad4c258-3338-a49d-b6db-5059e89d9811"

securityContext:
  privileged: true

volumeMounts:
  - name: nvidia-libs
    mountPath: /usr/lib/nvidia-host

volumes:
  - name: nvidia-libs
    hostPath:
      path: /mnt/nvidia-libs
```

### 启动脚本头部（关键顺序）

```bash
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=GPU-3ad4c258-3338-a49d-b6db-5059e89d9811
export LD_LIBRARY_PATH=/usr/lib/nvidia-host:${LD_LIBRARY_PATH:-}
export PATH=/usr/lib/nvidia-host:${PATH}
```

---

## 5. 生成的 SGLang 启动命令

```bash
python3 -m sglang.launch_server \
  --context-length 5120 \
  --trust-remote-code \
  --dtype auto \
  --kv-cache-dtype auto \
  --mem-fraction-static 0.9 \
  --max-running-requests 32 \
  --chunked-prefill-size 4096 \
  --random-seed 0 \
  --disable-chunked-prefix-cache \
  --host 0.0.0.0 \
  --port 17000 \
  --served-model-name DeepSeek-R1-Distill-Qwen-1.5B \
  --model-path /models/DeepSeek-R1-Distill-Qwen-1.5B \
  --tp-size 1 \
  --ep-size 1
```

---

## 6. 验证结果

### 6.1 启动日志关键输出

```
nvidia-smi path: /usr/lib/nvidia-host/nvidia-smi  ✅ 找到
GPU 0: NVIDIA A100-PCIE-40GB (UUID: GPU-3ad4c258...)  ✅ 正确 GPU
[08:29:50] Load weight begin. avail mem=38.87 GB      ✅ A100 40GB 确认
[08:29:52] Load weight end. elapsed=2.73s, avail mem=35.36 GB
[08:29:52] KV Cache allocated. #tokens: 1178703, K size: 15.74 GB, V size: 15.74 GB
[08:30:18] The server is fired up and ready to roll!  ✅ 启动成功
```

### 6.2 Pod 状态

```
NAME                                  READY   STATUS    RESTARTS   AGE
wings-infer-sglang-78988cbfb9-zbkqq   2/2     Running   0          2m30s
```

### 6.3 接口验证

**健康检查（18000/health）**:
```json
{"s":1,"p":"ready","pid_alive":false,"backend_ok":true,"backend_code":200,"interrupted":false,"ever_ready":true}
```

**模型列表（18000/v1/models）**:
```json
{"object":"list","data":[{"id":"DeepSeek-R1-Distill-Qwen-1.5B","object":"model","max_model_len":5120}]}
```

**推理测试 - 直连 sglang 17000**:
```json
{
  "id": "f41481b61b5d40828491e1fc4efd56ab",
  "object": "chat.completion",
  "model": "DeepSeek-R1-Distill-Qwen-1.5B",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "<think>\n\n</think>\n\nHello! How can I assist you today? 😊"
    },
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 4, "completion_tokens": 16, "total_tokens": 20}
}
```

**推理测试 - 经 proxy wings-infer 18000**:  
响应完全一致 ✅（proxy 转发正常）

### 6.4 GPU 内存使用确认

```
0, NVIDIA A100-PCIE-40GB, 37190 MiB   ← sglang 使用 A100（KV cache 约 31.5 GB）
1, NVIDIA L20,            42053 MiB   ← L20 业务服务完全未受影响 ✅
```

---

## 7. 验证结论

| 验证项 | 结果 |
|--------|------|
| wings-infer proxy 健康检查 | ✅ PASS |
| SGLang 引擎启动 | ✅ PASS（A100 40GB 确认） |
| 推理直连 17000 | ✅ PASS |
| proxy 转发 18000 | ✅ PASS |
| L20 业务服务未被干扰 | ✅ PASS（42053 MiB 不变） |
| backend-20260228 ENGINE 路由 | ✅ PASS（sglang 命令正确生成） |

**结论：`backend-20260228` 多引擎代码 SGLang 引擎在 K8s(k3s) 环境中验证通过。**

---

## 8. 注意事项（后续部署参考）

1. **GPU 锁定必须用 UUID**：容器内 CUDA 设备索引与宿主机 PCI 顺序可能不同，务必用 `GPU-xxxx` UUID  
2. **nvidia-smi 需要宿主机版本**：SGLang 启动时调用 `nvidia-smi`，需从宿主机复制并通过 PATH 暴露  
3. **环境变量名**: wings proxy 读取 `ENGINE`（非 `ENGINE_TYPE`）  
4. **k3s-verify 稳定性**: 该容器会自动停止，操作前需 `docker start k3s-verify`  
