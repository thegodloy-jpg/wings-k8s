# MindIE 部署指南 (华为昇腾 Ascend NPU)

## 概述

MindIE (Mind Inference Engine) 是华为昇腾原生推理引擎。与 vLLM/SGLang 不同，MindIE:
- 使用 `mindieservice_daemon` 守护进程运行
- 通过 `config.json` 文件配置（非命令行参数）
- 分布式模式使用 HCCL (Huawei Collective Communication Library) 而非 NCCL/Ray
- 需要特权容器和 Ascend 驱动挂载

## 场景一: 单机推理

### 适用场景
- 单个 Ascend 910B 节点
- 模型可在单节点 NPU 内存中容纳

### 架构

```
Node (Ascend 910B)
├── Pod (Deployment)
│   ├── wings-infer (sidecar)
│   │   ├── :18000 Proxy
│   │   └── :19000 Health
│   └── engine (mindie)
│       └── :17000 mindieservice_daemon
│           └── config.json (自动合并生成)
```

### 部署步骤

#### 1. 前提条件

确保节点已安装:
- Ascend 驱动 + Firmware
- CANN Toolkit (通常在 `/usr/local/Ascend/ascend-toolkit/`)
- MindIE 软件包 (通常在 `/usr/local/Ascend/mindie/`)

验证:
```bash
npu-smi info                    # 查看 NPU 状态
ls /usr/local/Ascend/driver/    # 驱动目录
```

#### 2. 修改配置

编辑 `k8s/overlays/mindie-single/deployment.yaml`:

```yaml
# 模型配置
- name: ENGINE
  value: "mindie"
- name: MODEL_NAME
  value: "your-model-name"
- name: MODEL_PATH
  value: "/models/your-model-name"

# 镜像
image: wings-infer:latest                    # Sidecar
image: your-mindie-image:latest              # MindIE 引擎镜像

# 模型存储
volumes:
  - name: model-volume
    hostPath:
      path: /data/models                     # 节点模型路径
```

#### 3. MindIE 配置参数

Sidecar 会自动读取 MindIE 镜像内的 `config.json` 并合并以下覆盖项:

| 参数 | 环境变量 / engine_config 键 | 默认值 | 说明 |
|------|---------------------------|--------|------|
| ServerConfig.port | `port` | 17000 | API 端口 |
| ServerConfig.ipAddress | `ipAddress` | 0.0.0.0 | 监听地址 |
| ModelConfig.modelWeightPath | `modelWeightPath` | MODEL_PATH | 模型权重路径 |
| ModelConfig.worldSize | `worldSize` | 1 | TP 大小 |
| ModelDeployConfig.maxSeqLen | `maxSeqLen` | 4096 | 最大序列长度 |
| ModelDeployConfig.maxInputTokenLen | `maxInputTokenLen` | 2048 | 最大输入 token |
| BackendConfig.npuDeviceIds | `npuDeviceIds` | [[0]] | NPU 设备 ID |
| ScheduleConfig.maxBatchSize | `maxBatchSize` | 200 | 最大批处理大小 |
| ModelConfig.npuMemSize | `npuMemSize` | -1 | NPU 内存 (GB, -1=自动) |

配置合并流程:
```
镜像内 config.json → 读取 → 合并 engine_config 覆盖 → 写回 → daemon 启动
```

#### 4. Ascend 驱动挂载

MindIE 需要访问宿主机的 Ascend 驱动:

```yaml
volumes:
  - name: ascend-driver
    hostPath: { path: /usr/local/Ascend/driver }
  - name: ascend-dcmi
    hostPath: { path: /usr/local/dcmi }
  - name: npu-smi
    hostPath: { path: /usr/local/bin/npu-smi }

# 特权模式 (HCCL 通信需要)
securityContext:
  privileged: true
```

#### 5. 部署与验证

```bash
kubectl apply -k k8s/overlays/mindie-single/
kubectl -n wings-infer get pods -w

# 健康检查
curl http://<NODE_IP>:30190/health

# 推理测试 (MindIE 支持 OpenAI 兼容 API)
curl http://<NODE_IP>:30180/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"your-model","messages":[{"role":"user","content":"你好"}],"max_tokens":50}'
```

---

## 场景二: 多节点分布式推理 (HCCL)

### 适用场景
- 大模型需要跨多个 Ascend 910B 节点
- 使用 HCCL 进行节点间集合通信
- 支持跨节点 TP (Data Parallel 由上层协调)

### 架构

```
Node-0 (Ascend 910B)                      Node-1 (Ascend 910B)
├── Pod infer-0 (rank-0)                   ├── Pod infer-1 (rank-1)
│   ├── wings-infer                        │   ├── wings-infer
│   │   ├── :18000 Proxy                   │   │   └── :19000 Health
│   │   └── :19000 Health                  │   └── engine
│   └── engine                             │       ├── RANK=1, WORLD_SIZE=2
│       ├── RANK=0, WORLD_SIZE=2           │       ├── MASTER_ADDR=Node-0 IP
│       ├── MASTER_ADDR=self               │       ├── HCCL via :27070
│       ├── HCCL via :27070                │       └── mindieservice_daemon
│       ├── ranktable.json (自动生成)      │           └── config.json (ipAddress=127.0.0.1)
│       └── mindieservice_daemon
│           └── config.json (ipAddress=0.0.0.0)
│
│   ← HCCL 集合通信 (:27070) →
```

### 网络要求

| 端口 | 用途 | 协议 |
|------|------|------|
| 27070 | HCCL 集合通信 (MASTER_PORT) | TCP |
| 17000 | MindIE API | TCP |
| 18000 | Wings Proxy (仅 rank-0) | TCP |
| 19000 | Wings Health | TCP |

### 部署步骤

#### 1. 修改配置

编辑 `k8s/overlays/mindie-distributed/statefulset.yaml`:

```yaml
spec:
  replicas: 2                              # 节点数

env:
  - name: NNODES
    value: "2"
  - name: HEAD_NODE_ADDR
    value: "192.168.1.110"                 # rank-0 节点 IP
  - name: NODE_IPS
    value: "192.168.1.110,192.168.1.170"   # 所有节点 IP
  - name: DEVICE_COUNT
    value: "1"                             # 每节点 NPU 数

# MindIE 引擎镜像
image: your-mindie-image:latest

# HCCL 设备 IP (可选, 格式: "ip0;ip1" 分号分隔节点)
- name: HCCL_DEVICE_IPS
  value: "192.168.1.110;192.168.1.170"
```

#### 2. HCCL Rank Table

Sidecar 会自动生成 `/tmp/hccl_ranktable.json`:

```json
{
  "version": "1.0",
  "server_count": "1",
  "server_list": [
    {
      "server_id": "192.168.1.110",
      "device": [
        {"device_id": "0", "device_ip": "192.168.1.110", "rank_id": "0"}
      ],
      "container_ip": "192.168.1.110",
      "host_nic_ip": "192.168.1.110"
    }
  ],
  "status": "completed"
}
```

> 注: 每个节点生成**单节点** rank table (`server_count=1`)，MindIE 验证 `worldSize % n_nodes == 0` 始终通过。跨节点协调通过 MASTER_ADDR/RANK/WORLD_SIZE 环境变量实现。

#### 3. 分布式环境变量 (自动设置)

Sidecar 的 `mindie_adapter.py` 自动注入:

```bash
MASTER_ADDR=<head_node_ip>
MASTER_PORT=27070
RANK=<node_rank>
WORLD_SIZE=<nnodes>
HCCL_WHITELIST_DISABLE=1
HCCL_IF_IP=<this_node_ip>
HCCL_SOCKET_IFNAME=eth0
GLOO_SOCKET_IFNAME=eth0
MIES_CONTAINER_IP=<this_node_ip>
RANK_TABLE_FILE=/tmp/hccl_ranktable.json
```

#### 4. 关键说明

- **所有节点都启动 `mindieservice_daemon`**: rank-0 监听 0.0.0.0（对外），rank>0 监听 127.0.0.1
- **config.json 合并**: 每个节点独立合并自己的 config.json，rank>0 的 `ipAddress` 设为 `127.0.0.1`
- **multiNodesInferEnabled**: 保持 `false`，由 sidecar 的 HCCL 环境变量完成跨节点协调
- **privileged: true**: HCCL 通信需要特权容器

#### 5. 部署与验证

```bash
kubectl apply -k k8s/overlays/mindie-distributed/
kubectl -n wings-infer get pods -w

# 等待就绪
watch -n 5 'curl -s http://192.168.1.110:30190/health'

# 通过 rank-0 代理端口推理
curl http://192.168.1.110:30180/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"your-model","messages":[{"role":"user","content":"你好"}],"max_tokens":50}'
```

### 故障排查

```bash
# 检查 HCCL 通信
kubectl exec infer-0 -c engine -n wings-infer -- ss -tlnp | grep 27070

# 检查 rank table
kubectl exec infer-0 -c engine -n wings-infer -- cat /tmp/hccl_ranktable.json

# 检查 config.json
kubectl exec infer-0 -c engine -n wings-infer -- \
  cat /usr/local/Ascend/mindie/latest/mindie-service/conf/config.json | python3 -m json.tool

# 查看 MindIE 日志
kubectl logs infer-0 -c engine -n wings-infer --tail=100

# 常见错误:
# "Invalid DP number per node: 0"
#   → multiNodesInferEnabled 应为 false
#   → 检查 worldSize 和 npuDeviceIds 配置

# "HCCL connection timeout"
#   → 检查 27070 端口连通性
#   → 检查 HCCL_IF_IP 是否正确
#   → 确认 privileged: true
```

---

## MindIE MOE 模型

对于 Mixture of Experts 模型，额外配置:

```yaml
env:
  - name: IS_MOE
    value: "true"
  - name: TP
    value: "2"           # Tensor Parallel
  - name: MOE_TP
    value: "2"           # MOE Expert Tensor Parallel
  - name: MOE_EP
    value: "-1"          # Expert Parallel (-1=自动)
```

## MindIE MTP (Multi-Token Prediction)

```yaml
env:
  - name: IS_MTP
    value: "true"
  # 自动注入 plugin_params: {"plugin_type": "mtp", "num_speculative_tokens": 1}
```
