# vLLM Ray 分布式部署指南 — 单机多 GPU（Pod 网络模式）

> **版本**：2026-03-10  
> **作者**：zhanghui  
> **状态**：已验证通过（7.6.16.150, 2×RTX 4090, DeepSeek-R1-Distill-Qwen-1.5B）

---

## 1. 适用场景

在**同一台物理机**上使用多张 NVIDIA GPU 进行 vLLM + Ray 分布式推理。  
每张 GPU 对应一个 Pod（StatefulSet replica），通过 Pod 网络获得独立 IP。

### 典型场景

- 单节点多卡 Ray 分布式验证
- Docker-in-Docker k3s 集群（CoreDNS 跨节点不通）
- 无 nvidia-device-plugin 环境下的 GPU 直通

### 不适用场景

- 多物理节点分布式 → 使用 `statefulset.yaml` + hostNetwork 方案
- Ascend NPU → 参考 [deploy-vllm-ascend-dist-ray.md](deploy-vllm-ascend-dist-ray.md)

---

## 2. 架构

```
┌─────────── 同一物理节点 ───────────────────────────────────────────────────┐
│                                                                           │
│  Pod: infer-0 (rank 0, GPU 0)          Pod: infer-1 (rank 1, GPU 1)      │
│  IP: 10.42.1.x                         IP: 10.42.1.y                     │
│                                                                           │
│  ┌─ wings-infer (sidecar) ────┐        ┌─ wings-infer (sidecar) ────┐    │
│  │ • 写 Pod IP → hostPath     │        │ • 写 Pod IP → hostPath     │    │
│  │ • 读 worker IP ← hostPath  │        │ • 读 master IP ← hostPath  │    │
│  │ • 生成 start_command.sh   │        │ • 生成 start_command.sh   │    │
│  │ • Proxy → :17000 (:18000) │        │ • (不启动 Proxy)          │    │
│  │ • Health check :19000     │        │ • Health check :19001     │    │
│  └─────────────────────────────┘        └─────────────────────────────┘    │
│                                                                           │
│  ┌─ vllm engine ──────────────┐        ┌─ vllm engine ──────────────┐    │
│  │ • CUDA_VISIBLE_DEVICES=0  │        │ • CUDA_VISIBLE_DEVICES=1  │    │
│  │ • Ray Head :28020         │        │ • Ray Worker → master IP  │    │
│  │ • vllm serve :17000       │        │ • --block (不启动 serve)   │    │
│  │ • NCCL over eth0          │        │ • NCCL over eth0          │    │
│  └─────────────────────────────┘        └─────────────────────────────┘    │
│                                                                           │
│  ┌─ hostPath: /tmp/wings-ip-exchange ─┐                                   │
│  │ pod-0-ip  pod-1-ip                 │  ← 共享 IP 交换文件               │
│  └─────────────────────────────────────┘                                   │
└───────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 核心设计决策

### 3.1 为什么用 Pod 网络而非 hostNetwork？

| 对比项 | hostNetwork | Pod 网络 (本方案) |
|--------|-------------|-------------------|
| Pod IP | 所有 Pod 共享宿主 IP | 每 Pod 独立 IP |
| 端口冲突 | 同端口只有一个 Pod 能绑定 | 无冲突 |
| Ray 节点识别 | 无法区分不同 Pod | IP 不同，正常区分 |
| wings NODE_RANK 判断 | 依赖 IP，同 IP 则误判 | 根据 Pod name 序号派生 |

### 3.2 为什么用 hostPath IP 交换？

在 Docker-in-Docker k3s 环境中，常见问题：
- 多个 k3s 节点 InternalIP 相同（都是 docker bridge 的 172.17.0.x）
- flannel 跨节点路由不通 → CoreDNS Pod 从其他节点不可达
- StatefulSet Headless Service DNS 解析不可用

**解决方案**：同节点 Pod 共享 hostPath `/tmp/wings-ip-exchange`，各自写入 Pod IP 文件。

### 3.3 GPU 隔离机制

```
StatefulSet Pod Name → 序号 → CUDA_VISIBLE_DEVICES
infer-0              → 0    → GPU 0
infer-1              → 1    → GPU 1
infer-2              → 2    → GPU 2
...
```

entrypoint 中：`ORDINAL=${POD_NAME##*-}; export CUDA_VISIBLE_DEVICES=$ORDINAL`

---

## 4. 前提条件

### 4.1 环境要求

| 组件 | 要求 |
|------|------|
| K8s 集群 | k3s / k8s 1.25+ |
| GPU | 2+ 张 NVIDIA GPU（已安装驱动） |
| GPU 设备 | `/dev/nvidia0`, `/dev/nvidia1` 等设备文件可访问 |
| NVIDIA 库 | nvidia 用户态库路径（或已在标准 LD_LIBRARY_PATH 中） |
| 模型 | 预下载到节点本地路径 |
| 镜像 | wings-infer sidecar 镜像 + vLLM 引擎镜像 |

### 4.2 验证 GPU 可用

```bash
# 在宿主机上确认 GPU
nvidia-smi

# 确认设备文件
ls -la /dev/nvidia*
```

### 4.3 确认 k3s 节点名

```bash
kubectl get nodes -o wide
# 记录目标节点的 NAME，用于 YAML 中的 nodeName
```

---

## 5. 部署步骤

### 5.1 准备模板

复制模板文件并按需修改：

```bash
cp k8s/overlays/vllm-distributed/statefulset-single-node-ray-template.yaml \
   k8s/overlays/vllm-distributed/my-deployment.yaml
```

### 5.2 修改配置

搜索 `← CUSTOMIZE` 标记，逐一修改:

```yaml
# 1. 节点名
nodeName: YOUR_NODE_NAME           # kubectl get nodes 获取

# 2. GPU 数量 / replicas
replicas: 2                        # = 你的 GPU 数量

# 3. NNODES 环境变量
- name: NNODES
  value: "2"                       # 与 replicas 一致

# 4. 模型配置
- name: MODEL_NAME
  value: "DeepSeek-R1-Distill-Qwen-1.5B"
- name: MODEL_PATH
  value: "/models/DeepSeek-R1-Distill-Qwen-1.5B"

# 5. 模型 hostPath
volumes:
  - name: model-volume
    hostPath:
      path: /mnt/models            # 宿主机模型目录

# 6. 镜像
image: wings-infer:latest          # sidecar
image: vllm/vllm-openai:v0.13.0   # engine

# 7. GPU 设备卷 & 挂载
# 有几张 GPU 就挂几个 /dev/nvidiaX
```

### 5.3 清理 IP 交换目录

部署前清理旧的 IP 交换文件（避免读到过期 IP）：

```bash
# 在宿主机上（或 k3s agent 容器内）
rm -rf /tmp/wings-ip-exchange/*
```

### 5.4 部署

```bash
# 创建 namespace（如不存在）
kubectl create namespace wings-infer 2>/dev/null || true

# 部署
kubectl apply -f k8s/overlays/vllm-distributed/my-deployment.yaml

# 观察 Pod 状态
kubectl -n wings-infer get pods -w
```

### 5.5 验证启动

```bash
# 查看 sidecar 日志（master）
kubectl -n wings-infer logs infer-0 -c wings-infer --tail=50

# 查看 engine 日志（master）
kubectl -n wings-infer logs infer-0 -c engine --tail=50

# 查看 worker 日志
kubectl -n wings-infer logs infer-1 -c wings-infer --tail=20
kubectl -n wings-infer logs infer-1 -c engine --tail=20
```

预期日志关键词:

```
[wings] NODE_RANK=0 ... MASTER_IP=10.42.1.x
Ray runtime started
Application startup complete
INFO: Started server process, listening on http://0.0.0.0:17000
```

---

## 6. 推理测试

### 6.1 从 Pod 内部测试

```bash
# 进入 master Pod
kubectl -n wings-infer exec -it infer-0 -c wings-infer -- /bin/sh

# 健康检查
curl -s http://127.0.0.1:19000/health

# 获取模型列表
curl -s http://127.0.0.1:18000/v1/models | python3 -m json.tool

# 推理测试 (Completions API)
curl -s http://127.0.0.1:18000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "DeepSeek-R1-Distill-Qwen-1.5B",
    "prompt": "Hello, what is 2+2?",
    "max_tokens": 30,
    "temperature": 0
  }' | python3 -m json.tool

# 推理测试 (Chat API)
curl -s http://127.0.0.1:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "DeepSeek-R1-Distill-Qwen-1.5B",
    "messages": [{"role":"user","content":"What is the capital of France?"}],
    "max_tokens": 50,
    "temperature": 0
  }' | python3 -m json.tool
```

### 6.2 通过 Python 脚本测试

如果 `kubectl exec` 不可用（跨节点路由不通），可以使用 hostPath 传递测试脚本:

```bash
# 1. 在宿主机 (或 k3s agent 容器) 创建测试脚本
cat > /tmp/wings-ip-exchange/test_infer.py << 'EOF'
import urllib.request, json

data = json.dumps({
    "model": "DeepSeek-R1-Distill-Qwen-1.5B",
    "prompt": "Hello, what is 2+2?",
    "max_tokens": 30,
    "temperature": 0
}).encode()

req = urllib.request.Request(
    "http://127.0.0.1:18000/v1/completions",
    data=data,
    headers={"Content-Type": "application/json"}
)

r = urllib.request.urlopen(req, timeout=60)
result = json.loads(r.read().decode())
print(json.dumps(result, indent=2, ensure_ascii=False))
EOF

# 2. 在 Pod 内执行 (脚本通过 hostPath 自动可用)
kubectl -n wings-infer exec infer-0 -c wings-infer -- python3 /ip-exchange/test_infer.py
# 或者用 crictl:
crictl exec <CONTAINER_ID> python3 /ip-exchange/test_infer.py
```

### 6.3 绕过多层 Shell 引号问题

在 Docker-in-Docker 环境中，常需要 `PowerShell → SSH → docker exec → crictl exec → sh` 五层传递。  
JSON 引号会被逐层剥离。推荐解决方案：

**方案 A: Base64 编码 + hostPath 写文件**

```powershell
# PowerShell 中
$script = @"
import urllib.request, json
data = json.dumps({"model":"YOUR_MODEL","prompt":"Hello","max_tokens":30}).encode()
req = urllib.request.Request("http://127.0.0.1:18000/v1/completions", data=data, headers={"Content-Type":"application/json"})
print(urllib.request.urlopen(req, timeout=60).read().decode())
"@
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script))

# 写入到容器内的 hostPath
ssh root@HOST "docker exec K3S_CONTAINER sh -c 'echo $b64 | base64 -d > /tmp/wings-ip-exchange/test.py'"

# 执行
ssh root@HOST "docker exec K3S_CONTAINER crictl exec CONTAINER_ID python3 /ip-exchange/test.py"
```

**方案 B: 简单 GET 请求验证**

```bash
# 不涉及 JSON body，无引号问题
ssh root@HOST "docker exec K3S_CONTAINER crictl exec CONTAINER_ID curl -s http://127.0.0.1:18000/v1/models"
ssh root@HOST "docker exec K3S_CONTAINER crictl exec CONTAINER_ID curl -s http://127.0.0.1:19000/health"
```

---

## 7. 扩展到 N 张 GPU

模板天然支持 N 卡扩展。只需:

1. **replicas** 改为 N
2. **NNODES** 改为 N
3. 添加 `/dev/nvidiaN` 对应的 volume & volumeMount
4. IP 交换逻辑已自动支持 N 节点（master 等待 pod-1-ip 到 pod-(N-1)-ip）

```yaml
# 4 × GPU 示例
spec:
  replicas: 4
  ...
  env:
  - name: NNODES
    value: "4"
  volumes:
  - name: dev-nvidia0
    hostPath: { path: /dev/nvidia0, type: CharDevice }
  - name: dev-nvidia1
    hostPath: { path: /dev/nvidia1, type: CharDevice }
  - name: dev-nvidia2
    hostPath: { path: /dev/nvidia2, type: CharDevice }
  - name: dev-nvidia3
    hostPath: { path: /dev/nvidia3, type: CharDevice }
```

---

## 8. 与标准 hostNetwork 方案对比

| 维度 | 标准 hostNetwork 方案 | 本方案 (Pod 网络 + IP 交换) |
|------|----------------------|---------------------------|
| 适用拓扑 | 多物理节点，每节点 1 Pod | 单节点多 Pod (多 GPU) |
| 网络模式 | hostNetwork: true | Pod 网络 (CNI) |
| Pod IP | = 宿主机 IP | CNI 分配独立 IP |
| DNS 依赖 | CoreDNS 或静态配置 | 不依赖 DNS |
| IP 发现 | 静态 NODE_IPS 环境变量 | hostPath 文件交换 |
| 端口冲突 | 同端口冲突 | 无冲突 |
| podAntiAffinity | 需要（分散到不同节点） | 不需要（同节点部署） |
| GPU 隔离 | nvidia-device-plugin 或 CUDA_VISIBLE_DEVICES | CUDA_VISIBLE_DEVICES by ordinal |

---

## 9. 故障排查

### 9.1 Pod 卡在 Pending

```bash
kubectl -n wings-infer describe pod infer-0
# 检查 nodeName 是否正确、资源是否充足
```

### 9.2 IP 交换超时

```bash
# 检查 hostPath 目录是否存在
ls -la /tmp/wings-ip-exchange/

# 检查两个 Pod 是否同时启动
kubectl -n wings-infer get pods -o wide
```

### 9.3 Ray Worker 连不上 Head

```bash
# 检查 Pod 间网络连通性
kubectl -n wings-infer exec infer-1 -c engine -- \
  python3 -c "import socket; s=socket.socket(); s.settimeout(5); s.connect(('MASTER_POD_IP',28020)); print('OK'); s.close()"

# 检查 NCCL_SOCKET_IFNAME
kubectl -n wings-infer exec infer-0 -c engine -- env | grep NCCL
```

### 9.4 CUDA 设备不可见

```bash
kubectl -n wings-infer exec infer-0 -c engine -- nvidia-smi
kubectl -n wings-infer exec infer-0 -c engine -- env | grep CUDA_VISIBLE
```

### 9.5 vLLM OOM

增加 engine 容器的 memory limit 和 dshm sizeLimit：

```yaml
resources:
  limits:
    memory: 64Gi         # 增大
volumes:
  - name: dshm
    emptyDir:
      medium: Memory
      sizeLimit: 4Gi     # 增大
```

---

## 10. 清理

```bash
kubectl delete -f k8s/overlays/vllm-distributed/my-deployment.yaml
rm -rf /tmp/wings-ip-exchange/*
```

---

## 附录 A: 已验证环境

| 项目 | 详情 |
|------|------|
| 物理机 | 7.6.16.150 |
| GPU | 2×NVIDIA RTX 4090 + 2×NVIDIA L20 |
| 使用 GPU | GPU 0 + GPU 1 (RTX 4090) |
| k3s 版本 | v1.30.6+k3s1 (Docker-in-Docker) |
| Sidecar 镜像 | wings-infer:ray-dist-zhanghui |
| Engine 镜像 | vllm/vllm-openai:v0.13.0 |
| 模型 | DeepSeek-R1-Distill-Qwen-1.5B |
| Pod IP | infer-0: 10.42.1.8, infer-1: 10.42.1.9 |
| Ray 端口 | 28020 |
| 推理结果 | ✅ /v1/completions 正常返回 |

## 附录 B: 关键代码变更

本方案涉及的 Sidecar 代码修改：

1. **`backend/app/main.py`** — `_resolve()` DNS 辅助函数  
   当 `_wait_and_distribute_to_workers()` 比较已注册 worker IP 和配置中的 worker IP 时，  
   先解析 DNS/hostname 为实际 IP 再比较，避免 hostname vs IP 不匹配导致的等待超时。

2. **`backend/app/engines/vllm_adapter.py`** — NVIDIA Ray 配置  
   - `NCCL_SOCKET_IFNAME=eth0`（Pod 网络的 CNI 虚拟接口）
   - `ray start --node-ip-address=$VLLM_HOST_IP --num-gpus=1`
