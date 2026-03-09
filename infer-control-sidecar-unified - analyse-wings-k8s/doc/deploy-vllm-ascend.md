# vLLM-Ascend 部署指南 (华为昇腾 Ascend NPU)

## 概述

vLLM-Ascend 是 vLLM 的昇腾 NPU 适配版本，使用 `torch_npu` 替代 CUDA，但保持 vLLM 的全部 API 兼容。

与标准 vLLM 的区别:
- 使用 HCCL 替代 NCCL 进行设备间通信
- 需要 Ascend CANN Toolkit 环境
- 分布式模式中 Ray 使用 `--resources='{"NPU": 1}'` 而非 `--num-gpus=1`
- 需要 Triton NPU 驱动补丁 (Sidecar 自动处理)
- 推荐使用 `--enforce-eager` (绕过 Triton 编译)

## 场景一: 单机推理

### 架构

```
Node (Ascend 910B)
├── Pod (Deployment)
│   ├── wings-infer (sidecar)
│   │   ├── :18000 Proxy
│   │   └── :19000 Health
│   └── engine (vllm-ascend)
│       └── :17000 vLLM API Server
│           ├── source Ascend env (CANN + ATB)
│           └── python3 -m vllm.entrypoints.openai.api_server
```

### 部署步骤

#### 1. 前提条件

- 节点安装 Ascend 驱动 + CANN Toolkit
- vLLM-Ascend 引擎镜像 (如 `quay.io/ascend/vllm-ascend:v0.7.3`)

```bash
npu-smi info    # 验证 NPU 可见
```

#### 2. 修改配置

编辑 `k8s/overlays/vllm-ascend-single/deployment.yaml`:

```yaml
env:
  - name: ENGINE
    value: "vllm_ascend"
  - name: MODEL_NAME
    value: "DeepSeek-R1-Distill-Qwen-1.5B"
  - name: MODEL_PATH
    value: "/models/DeepSeek-R1-Distill-Qwen-1.5B"
  - name: WINGS_DEVICE
    value: "ascend"

# 引擎镜像
- name: engine
  image: quay.io/ascend/vllm-ascend:v0.7.3    # ← vLLM-Ascend 镜像

# Ascend 驱动挂载
volumes:
  - name: ascend-driver
    hostPath: { path: /usr/local/Ascend/driver }
  - name: ascend-dcmi
    hostPath: { path: /usr/local/dcmi }
  - name: npu-smi
    hostPath: { path: /usr/local/bin/npu-smi }
```

#### 3. CANN 环境自动配置

Sidecar 生成的 `start_command.sh` 自动执行:

```bash
set +u
[ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ] && source /usr/local/Ascend/ascend-toolkit/set_env.sh
[ -f /usr/local/Ascend/nnal/atb/set_env.sh ] && source /usr/local/Ascend/nnal/atb/set_env.sh
set -u
exec python3 -m vllm.entrypoints.openai.api_server --model ... --host 0.0.0.0 --port 17000
```

> `set +u / set -u`: Ascend 环境脚本可能引用未定义变量 (如 `ZSH_VERSION`)，需要临时关闭 `nounset`。

#### 4. 部署与验证

```bash
kubectl apply -k k8s/overlays/vllm-ascend-single/
kubectl -n wings-infer get pods -w

# 健康检查
curl http://<NODE_IP>:30190/health

# 推理
curl http://<NODE_IP>:30180/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"你好"}],"max_tokens":50}'
```

---

## 场景二: 多节点分布式推理 (Ray + Ascend)

### 适用场景
- 大模型需要跨多个 Ascend 910B 节点
- 使用 Ray 进行跨节点分布式计算
- 配合 vLLM 的 `--distributed-executor-backend ray`

### 架构

```
Node-0 (Ascend 910B)                      Node-1 (Ascend 910B)
├── Pod infer-0 (rank-0)                   ├── Pod infer-1 (rank-1)
│   ├── wings-infer                        │   ├── wings-infer
│   │   ├── :18000 Proxy                   │   │   └── :19000 Health
│   │   └── :19000 Health                  │   └── engine
│   └── engine                             │       ├── Triton NPU 补丁 ✓
│       ├── Triton NPU 补丁 ✓              │       ├── HCCL env vars ✓
│       ├── CANN env source ✓              │       ├── 动态发现 Ray Head
│       ├── Ray Head (:6379)               │       └── ray start --address=$HEAD:6379
│       ├── 等待 worker 加入               │           --resources='{"NPU": 1}'
│       └── vllm serve (:17000)
│           --distributed-executor-backend ray
│           --enforce-eager
│
│    ← Ray + HCCL 通信 →
```

### 关键特性

#### Triton NPU 补丁 (自动)

vLLM-Ascend 的 `worker.py` 导入 `torch_npu._inductor` 时触发 Triton 驱动发现。
Ascend NPU 没有标准 Triton 后端，导致 `"0 active drivers"` RuntimeError。

Sidecar 自动在 `start_command.sh` 中注入补丁:

```python
# 自动补丁 triton/runtime/driver.py
# 将 "raise RuntimeError(..." 替换为返回 NpuDummyDrv 实例
# 补丁在 Ray start 之前执行，所有 worker 进程也会继承
```

补丁日志标识: `[triton-patch] Patched ... for Ascend NPU`

#### 动态 Ray Head 发现

StatefulSet + Parallel 模式中 Pod 到节点的映射不确定。Worker 节点:

1. 遍历 `NODE_IPS` 中所有 IP
2. 探测 6379 端口 (`socket.connect`)
3. 找到响应的 IP 即为 Ray Head
4. 最多重试 120 次 × 5 秒

```bash
# 日志特征:
[worker] Scanning NODE_IPS for Ray head on port 6379...
[worker] Found Ray head at 7.6.52.110:6379
```

#### HCCL 环境变量 (自动)

```bash
export HCCL_WHITELIST_DISABLE=1
export HCCL_IF_IP=$VLLM_HOST_IP
export HCCL_SOCKET_IFNAME=$(awk '$2=="00000000"{print $1;exit}' /proc/net/route)
export GLOO_SOCKET_IFNAME=$HCCL_SOCKET_IFNAME
```

### 部署步骤

#### 1. 修改配置

编辑 `k8s/overlays/vllm-ascend-distributed/statefulset.yaml`:

```yaml
spec:
  replicas: 2                              # 节点数

env:
  - name: HEAD_NODE_ADDR
    value: "7.6.52.110"                    # rank-0 节点 IP
  - name: NODE_IPS
    value: "7.6.52.110,7.6.52.170"         # 所有节点 IP
  - name: NNODES
    value: "2"
  - name: TENSOR_PARALLEL_SIZE
    value: "2"                             # 总 TP = 总 NPU 数

# 引擎镜像
- name: engine
  image: quay.io/ascend/vllm-ascend:v0.7.3
```

#### 2. 部署

```bash
kubectl apply -k k8s/overlays/vllm-ascend-distributed/
kubectl -n wings-infer get pods -w

# 预期: 2 Pod × 2 容器全部 Running
NAME      READY   STATUS    RESTARTS   AGE
infer-0   2/2     Running   0          3m
infer-1   2/2     Running   0          3m
```

#### 3. 验证

```bash
# 健康检查 (201→200 过渡)
watch -n 5 'curl -s http://7.6.52.110:30190/health'

# 推理测试
curl http://7.6.52.110:30180/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "DeepSeek-R1-Distill-Qwen-1.5B",
    "messages": [{"role": "user", "content": "你好"}],
    "max_tokens": 50
  }'

# Ray 集群状态
kubectl exec infer-0 -c engine -n wings-infer -- ray status
```

### 故障排查

| 问题 | 排查 |
|------|------|
| Triton 补丁失败 | `kubectl logs infer-0 -c engine \| grep triton` |
| Ray Worker 连接失败 | `kubectl exec infer-1 -- python3 -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('<HEAD_IP>',6379))"` |
| HCCL 通信错误 | 检查 `HCCL_IF_IP` / `HCCL_SOCKET_IFNAME` 是否正确 |
| CANN 库冲突 | `find / -name "libascendcl.so*"` 检查重复库 |
| `--enforce-eager` 未生效 | 确认 `ENGINE=vllm_ascend`，Sidecar 会自动添加该标志 |
| 201 持续不变 | 大模型加载需时间；检查 engine 日志是否有错误 |

### 更多信息

详细的已验证部署记录参考 [deploy-vllm-ascend-dist-ray.md](deploy-vllm-ascend-dist-ray.md)。
