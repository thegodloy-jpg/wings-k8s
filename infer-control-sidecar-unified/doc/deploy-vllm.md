# vLLM 部署指南 (NVIDIA GPU)

## 场景一: 单机推理

### 适用场景
- 单张或多张 NVIDIA GPU 的节点
- 模型可在单节点 GPU 内存中容纳
- 使用 Tensor Parallel (TP) 实现单节点多卡推理

### 架构

```
Node
├── Pod (Deployment)
│   ├── wings-infer (sidecar)
│   │   ├── :18000 Proxy
│   │   └── :19000 Health
│   └── engine (vllm)
│       └── :17000 vLLM API Server
```

### 部署步骤

#### 1. 修改配置

编辑 `k8s/overlays/vllm-single/deployment.yaml`:

```yaml
# 模型配置
- name: MODEL_NAME
  value: "your-model-name"                  # ← 修改
- name: MODEL_PATH
  value: "/models/your-model-name"          # ← 修改

# 模型存储路径 (hostPath)
volumes:
  - name: model-volume
    hostPath:
      path: /data/models                    # ← 节点上模型的实际路径

# 镜像
image: wings-infer:latest                   # ← Sidecar 镜像
image: vllm/vllm-openai:latest              # ← vLLM 引擎镜像

# GPU 资源
resources:
  limits:
    nvidia.com/gpu: 1                       # ← GPU 数量
```

#### 2. 常用引擎参数

通过环境变量传递给 vLLM:

```yaml
env:
  - name: TENSOR_PARALLEL_SIZE
    value: "2"                    # 单节点多卡 TP
  - name: MAX_MODEL_LEN
    value: "4096"                 # 最大序列长度
  - name: GPU_MEMORY_UTILIZATION
    value: "0.9"                  # GPU 显存利用率
  - name: DTYPE
    value: "auto"                 # 数据类型
  - name: TRUST_REMOTE_CODE
    value: "true"                 # 信任远程代码
```

#### 3. 部署

```bash
kubectl apply -k k8s/overlays/vllm-single/
kubectl -n wings-infer get pods -w
```

#### 4. 验证

```bash
# 健康检查
curl http://<NODE_IP>:30190/health

# 推理测试
curl http://<NODE_IP>:30180/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"your-model","messages":[{"role":"user","content":"hello"}],"max_tokens":50}'
```

---

## 场景二: 多节点分布式推理 (Ray)

### 适用场景
- 大模型需要跨多节点 GPU 加载
- 使用 Ray 进行跨节点 Tensor Parallel
- 需要 2+ 个 NVIDIA GPU 节点

### 架构

```
Node-0                              Node-1
├── Pod infer-0 (rank-0)            ├── Pod infer-1 (rank-1)
│   ├── wings-infer                 │   ├── wings-infer
│   │   ├── :18000 Proxy            │   │   └── :19000 Health
│   │   └── :19000 Health           │   └── engine
│   └── engine                      │       └── Ray Worker
│       ├── Ray Head (:6379)        │           └── join → Node-0:6379
│       └── vLLM serve (:17000)     │
│           └── --distributed-executor-backend ray
```

### 网络要求

| 端口 | 用途 | 协议 |
|------|------|------|
| 6379 | Ray GCS | TCP |
| 8265 | Ray Dashboard | TCP |
| 17000 | vLLM API | TCP |
| 18000 | Wings Proxy | TCP |
| 19000 | Wings Health | TCP |

### 部署步骤

#### 1. 修改配置

编辑 `k8s/overlays/vllm-distributed/statefulset.yaml`:

```yaml
spec:
  replicas: 2                    # ← 节点数

env:
  - name: HEAD_NODE_ADDR
    value: "192.168.1.100"       # ← rank-0 节点 IP
  - name: NODE_IPS
    value: "192.168.1.100,192.168.1.101"   # ← 所有节点 IP
  - name: NNODES
    value: "2"
  - name: TENSOR_PARALLEL_SIZE
    value: "2"                   # ← 总 TP 数 (通常 = 总 GPU 数)
```

#### 2. 关键说明

- **hostNetwork: true**: 分布式模式使用宿主机网络，Ray 节点间直接通信
- **podManagementPolicy: Parallel**: StatefulSet 所有 Pod 同时启动
- **podAntiAffinity**: 确保每个 Pod 调度到不同物理节点
- **NODE_RANK**: 通过 K8s Downward API 自动注入 (从 Pod ordinal 推导)

#### 3. Ray 启动流程

1. rank-0: `ray start --head --port=6379` → 启动 Ray Head
2. rank-1..N: 扫描 `NODE_IPS` 中所有 IP 的 6379 端口，找到 Ray Head → `ray start --address=$HEAD_IP:6379`
3. rank-0 等待所有 worker 加入 → `ray.nodes()` 计数达到 `NNODES`
4. rank-0 执行 `vllm serve --distributed-executor-backend ray`

#### 4. /dev/shm 配置

Ray 分布式需要足够的共享内存:

```yaml
volumes:
  - name: dshm
    emptyDir:
      medium: Memory
      sizeLimit: 2Gi              # ← 根据模型大小调整
```

#### 5. 部署与验证

```bash
kubectl apply -k k8s/overlays/vllm-distributed/
kubectl -n wings-infer get pods -w

# 等待所有 Pod Ready
# 仅通过 rank-0 的代理端口访问
curl http://<RANK0_NODE_IP>:30180/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"your-model","messages":[{"role":"user","content":"hello"}],"max_tokens":50}'
```

### 故障排查

```bash
# 检查 Ray 集群状态
kubectl exec infer-0 -c engine -n wings-infer -- ray status

# 检查节点间连通性
kubectl exec infer-1 -c engine -n wings-infer -- \
  python3 -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('<HEAD_IP>',6379)); print('ok'); s.close()"
```
