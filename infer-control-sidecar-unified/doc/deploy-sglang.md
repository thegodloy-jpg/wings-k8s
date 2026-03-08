# SGLang 部署指南

## 场景一: 单机推理

### 适用场景
- 单张或多张 NVIDIA GPU 的节点
- 模型可在单节点 GPU 内存中容纳
- SGLang 使用 RadixAttention 和 continuous batching 优化推理

### 架构

```
Node
├── Pod (Deployment)
│   ├── wings-infer (sidecar)
│   │   ├── :18000 Proxy
│   │   └── :19000 Health
│   └── engine (sglang)
│       └── :17000 sglang.launch_server
```

### 部署步骤

#### 1. 修改配置

编辑 `k8s/overlays/sglang-single/deployment.yaml`:

```yaml
# 模型配置
- name: ENGINE
  value: "sglang"
- name: MODEL_NAME
  value: "your-model-name"
- name: MODEL_PATH
  value: "/models/your-model-name"

# 镜像
image: wings-infer:latest                      # Sidecar
image: lmsysorg/sglang:latest                  # SGLang 引擎

# 模型存储
volumes:
  - name: model-volume
    hostPath:
      path: /data/models                       # 节点模型路径

# GPU 资源
resources:
  limits:
    nvidia.com/gpu: 1
```

#### 2. 常用 SGLang 参数

通过 `engine_config` 传入 (`sglang.launch_server` 命令行参数):

```yaml
env:
  - name: TP_SIZE
    value: "1"                  # Tensor Parallel (单节点多卡)
  - name: CONTEXT_LENGTH
    value: "4096"               # 最大上下文长度
  - name: MEM_FRACTION_STATIC
    value: "0.88"               # 静态内存分配比例
```

参数映射规则: 环境变量下划线名 → SGLang CLI `--kebab-case` 参数。  
例如: `TP_SIZE` → `--tp-size`

#### 3. 部署与验证

```bash
kubectl apply -k k8s/overlays/sglang-single/
kubectl -n wings-infer get pods -w

# 健康检查
curl http://<NODE_IP>:30190/health

# 推理测试
curl http://<NODE_IP>:30180/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"your-model","messages":[{"role":"user","content":"hello"}],"max_tokens":50}'
```

### SGLang 健康检查特殊处理

SGLang 引擎的健康检查有特殊参数:

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `SGLANG_FAIL_BUDGET` | 6.0 | 失败预算 (权重衰减) |
| `SGLANG_PID_GRACE_MS` | 30000 | PID 宽限期 |
| `SGLANG_SILENCE_MAX_MS` | 60000 | 最大静默时间 → 503 降级 |
| `SGLANG_CONSEC_TIMEOUT_MAX` | 8 | 连续超时最大次数 |

---

## 场景二: 多节点分布式推理

### 适用场景
- 大模型需要跨节点加载 (如 70B+ 模型)
- 使用 SGLang 原生 `--nnodes` 分布式通信

### 架构

```
Node-0                                    Node-1
├── Pod infer-0 (rank-0)                  ├── Pod infer-1 (rank-1)
│   ├── wings-infer                       │   ├── wings-infer
│   │   ├── :18000 Proxy                  │   │   └── :19000 Health
│   │   └── :19000 Health                 │   └── engine
│   └── engine                            │       └── sglang.launch_server
│       └── sglang.launch_server          │           --nnodes=2 --node-rank=1
│           --nnodes=2 --node-rank=0      │           --dist-init-addr=<HEAD>:28030
│           --dist-init-addr=0.0.0.0:28030│
```

### 网络要求

| 端口 | 用途 | 协议 |
|------|------|------|
| 28030 | SGLang 分布式初始化 | TCP |
| 17000 | SGLang API | TCP |
| 18000 | Wings Proxy | TCP (仅 rank-0) |
| 19000 | Wings Health | TCP |
| NCCL 端口范围 | GPU 通信 | TCP/RDMA |

### 部署步骤

#### 1. 修改配置

编辑 `k8s/overlays/sglang-distributed/statefulset.yaml`:

```yaml
spec:
  replicas: 2                              # 节点数

env:
  - name: NNODES
    value: "2"
  - name: HEAD_NODE_ADDR
    value: "192.168.1.100"                 # rank-0 节点 IP
  - name: NODE_IPS
    value: "192.168.1.100,192.168.1.101"   # 所有节点 IP

# 镜像
image: lmsysorg/sglang:latest              # SGLang 引擎
```

#### 2. 分布式启动流程

SGLang 分布式与 vLLM Ray 不同，使用 PyTorch 原生 `torch.distributed`:

1. rank-0: `python3 -m sglang.launch_server --nnodes=2 --node-rank=0 --dist-init-addr=0.0.0.0:28030`
2. rank-1: `python3 -m sglang.launch_server --nnodes=2 --node-rank=1 --dist-init-addr=<HEAD_IP>:28030`
3. 两个节点通过 TCP 28030 端口完成 rendezvous
4. 使用 NCCL 进行 GPU 间数据传输

#### 3. 关键配置

```yaml
# 共享内存 (NCCL 通信需要)
volumes:
  - name: dshm
    emptyDir:
      medium: Memory
      sizeLimit: 2Gi

# TP 总数 = 节点数 × 每节点 GPU 数
env:
  - name: TP_SIZE
    value: "2"                  # 总 TP (跨节点)
```

#### 4. 部署与验证

```bash
kubectl apply -k k8s/overlays/sglang-distributed/
kubectl -n wings-infer get pods -w

# 仅通过 rank-0 代理端口访问
curl http://<RANK0_IP>:30180/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"your-model","messages":[{"role":"user","content":"hello"}],"max_tokens":50}'
```

### 故障排查

```bash
# 检查 SGLang 启动日志
kubectl logs infer-0 -c engine -n wings-infer --tail=50

# 检查分布式通信端口
kubectl exec infer-1 -c engine -n wings-infer -- \
  python3 -c "import socket;s=socket.socket();s.settimeout(2);s.connect(('<HEAD_IP>',28030));print('ok');s.close()"

# NCCL 调试日志
# 在 env 中添加:
# - name: NCCL_DEBUG
#   value: "INFO"
```
