# SGLang 分布式推理验证报告

**验证日期**: 2026-03-04  
**验证人**: zhanghui  
**文档路径**: `docker+k8s/doc_distribute/sglang-dist-verify-report_20260304.md`

---

## 1. 验证概述

| 项目 | 内容 |
|------|------|
| 推理框架 | SGLang (分布式双节点) |
| 模型 | DeepSeek-R1-Distill-Qwen-1.5B |
| 部署方式 | k3s StatefulSet (hostNetwork) |
| 节点数 | 2 (tp_size=2, ep_size=2) |
| GPU | L20 46GB × 2 (每节点各1块) |
| 通信 | NCCL Socket via ens65f0 |
| 验证结果 | **全部通过 ✅** |

---

## 2. 集群环境

### 节点信息

| 角色 | 主机 | IP | 节点名 | GPU |
|------|------|----|--------|-----|
| k3s Server (rank-1) | .148 | 7.6.52.148 | a100 | nvidia0=A100(40GB), **nvidia1=L20(46GB)** |
| k3s Agent (rank-0, HEAD) | .150 | 7.6.16.150 | ubuntu2204 | nvidia0/1=RTX4090(24GB), **nvidia2/3=L20(46GB)** |

### 镜像

| 镜像 | 版本 | 说明 |
|------|------|------|
| `sglang-infer:zhanghui-20260228` | 21.7 GiB | SGLang 推理引擎 |
| `wings-infer:dist-nv-dev-zhanghui` | - | wings-infer 代理 sidecar |

---

## 3. 部署配置

### StatefulSet 关键参数

```yaml
spec:
  serviceName: infer-hl
  replicas: 2
  podManagementPolicy: Parallel   # 关键: 两个 Pod 必须同时启动
  affinity:
    nodeAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
        - weight: 100
          preference:
            matchExpressions:
              - key: kubernetes.io/hostname
                operator: In
                values: ["ubuntu2204"]   # infer-0 优先调度到 .150 (rank-0 HEAD节点)
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - topologyKey: kubernetes.io/hostname   # 强制两个 Pod 在不同节点
```

### wings-infer 关键环境变量

```yaml
DISTRIBUTED: "true"
NNODES: "2"
NODE_RANK: <pod-index>         # 从 Pod 标签自动注入 (0 或 1)
HEAD_NODE_ADDR: "7.6.16.150"
ENGINE: "sglang"
ENGINE_PORT: "17000"
MODEL_NAME: "DeepSeek-R1-Distill-Qwen-1.5B"
MODEL_PATH: "/models/DeepSeek-R1-Distill-Qwen-1.5B"
TP_SIZE: "1"                   # 每节点 tp=1, config_loader 自动乘以节点数
NODE_IPS: "7.6.16.150,7.6.52.148"  # 驱动 config_loader 计算 total tp_size=2
PORT: "18000"
HEALTH_PORT: "19000"
```

### engine 关键环境变量 (NCCL Socket 配置)

```yaml
NVIDIA_VISIBLE_DEVICES: "all"
GLOO_SOCKET_IFNAME: "ens65f0"
NCCL_SOCKET_IFNAME: "ens65f0"
NCCL_IB_DISABLE: "1"
NCCL_NET: "Socket"
NCCL_P2P_DISABLE: "1"
NCCL_SHM_DISABLE: "1"
```

### SGLang 启动命令 (engine 容器内)

```bash
# rank-0 (infer-0, .150, HEAD节点)
python3 -m sglang.launch_server \
  --tp-size 2 --ep-size 2 \
  --nnodes 2 --node-rank 0 \
  --dist-init-addr 7.6.16.150:28030 \
  --host 0.0.0.0 --port 17000 \
  --model /models/DeepSeek-R1-Distill-Qwen-1.5B \
  --context-length 5120

# rank-1 (infer-1, .148)
python3 -m sglang.launch_server \
  --tp-size 2 --ep-size 2 \
  --nnodes 2 --node-rank 1 \
  --dist-init-addr 7.6.16.150:28030 \
  --model /models/DeepSeek-R1-Distill-Qwen-1.5B \
  --context-length 5120
```

### GPU 选择 (Hostname-based)

```bash
HOSTNAME=$(hostname)
case "$HOSTNAME" in
  *a100*)      L20_IDX=1 ;;   # .148: nvidia0=A100, nvidia1=L20
  *ubuntu2204*) L20_IDX=2 ;;  # .150: nvidia0/1=RTX4090, nvidia2/3=L20
  *)
    # 内存阈值 fallback: awk $2 > 45000
    L20_IDX=$(nvidia-smi --query-gpu=index,memory.total ...)
    ;;
esac
export CUDA_VISIBLE_DEVICES=$L20_IDX
```

---

## 4. 验证过程中的关键问题及解决

| # | 问题 | 根因 | 解决方案 |
|---|------|------|---------|
| 1 | .150 k3s 缺少 SGLang 镜像 | 从未传输 | `docker save` → sshpass SCP → `docker cp` → `ctr import` |
| 2 | StatefulSet 死锁 (infer-1 未创建) | 默认 `OrderedReady`: infer-0 等 Ready 才建 infer-1，但 infer-0 等 infer-1 加入分布式初始化 | 改为 `podManagementPolicy: Parallel` |
| 3 | infer-0 节点调度不确定 | podAntiAffinity 只保证分散，不保证哪个 Pod 去哪个节点 | 添加 nodeAffinity `preferred ubuntu2204` → infer-0 优先到 .150 |
| 4 | `tp_size must be divisible by number of nodes` | SGLang 要求 tp_size ≥ nnodes；`TP_SIZE=1` + `NNODES=2` 断言失败 | 添加 `NODE_IPS="7.6.16.150,7.6.52.148"` → config_loader 计算 tp_size=1×2=2 |
| 5 | "No L20 found via nvidia-smi, using GPU 0" | sglang 容器内 nvidia-smi grep L20 不可靠 | 改为 hostname 匹配: a100→GPU1, ubuntu2204→GPU2 |
| 6 | GPU 显存严重不均衡 (rank-0=8.32GB, rank-1=43.81GB) | .150 删除 vLLM DP Pod 后遗留孤立进程 (PID 2064330/2065931, 来自 VLLM::EngineCore) 仍占用 GPU 2+3 共 ~79GB | `kill -9 2064330 2065931` 释放 GPU，重新启动 Pod |
| 7 | config_loader tp_size 仍为 1 | `get_node_ips()` 读取 `NODE_IPS` 环境变量，wings-infer 容器未设置 | 在 wings-infer 容器 env 中添加 `NODE_IPS` |

---

## 5. NCCL 初始化日志

```
# rank-1 (infer-1, .148) NCCL Init 日志:
Channel 00/0 : 0[2] -> 1[1] [receive] via NET/Socket/0
# 解读: GPU2(.150) <-> GPU1(.148) 通过 Socket 网络通信

ncclCommInitRank rank 1 nranks 2 cudaDev 0 nvmlDev 1 busId ab000 - Init COMPLETE
```

---

## 6. 模型加载日志 (rank-1, .148)

```
[TP1 EP1] Load weight end. elapsed=1.70s, avail mem=43.81 GB, mem usage=1.81 GB
[TP1 EP1] KV Cache allocated. #tokens: 2817714, K: 18.81 GB, V: 18.81 GB
[TP1 EP1] Capture cuda graph end. Time elapsed: 26.77s
Dummy health check server started at 127.0.0.1:30000
```

---

## 7. API 验证结果

验证时间: 2026-03-04 14:29~14:30

### Pod 最终状态

```
NAME      READY   STATUS    RESTARTS   AGE     IP           NODE
infer-0   2/2     Running   0          2m30s   7.6.16.150   ubuntu2204
infer-1   2/2     Running   0          2m30s   7.6.52.148   a100
```

### 端口验证

#### 端口 17000 — SGLang 引擎直连

```bash
# Health Check
curl http://7.6.16.150:17000/health
→ {} (HTTP 200) ✅

# Models List
curl http://7.6.16.150:17000/v1/models
→ {
    "object": "list",
    "data": [{
      "id": "DeepSeek-R1-Distill-Qwen-1.5B",
      "owned_by": "sglang",
      "max_model_len": 5120
    }]
  } ✅

# Chat Completion
curl -X POST http://7.6.16.150:17000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"2+2=?"}],"max_tokens":30}'
→ {
    "choices": [{
      "message": {
        "role": "assistant",
        "content": "<think>\nTo solve the problem 2 + 2, I start by identifying the two numbers involved, which are both 2.\n\nNext, I"
      },
      "finish_reason": "length"
    }],
    "usage": {"prompt_tokens": 7, "total_tokens": 37, "completion_tokens": 30}
  } ✅
```

#### 端口 18000 — wings-infer 代理

```bash
# Health Check
curl http://7.6.16.150:18000/health
→ {"s":1,"p":"ready","backend_ok":true,"backend_code":200,"ever_ready":true} ✅

# Chat Completion (通过代理)
curl -X POST http://7.6.16.150:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"DeepSeek-R1-Distill-Qwen-1.5B","messages":[{"role":"user","content":"2+2=?"}],"max_tokens":30}'
→ {
    "choices": [{
      "message": {
        "role": "assistant",
        "content": "<think>\n\n</think>\n\n2 + 2 equals 4."
      },
      "finish_reason": "stop"
    }],
    "usage": {"prompt_tokens": 7, "total_tokens": 20, "completion_tokens": 13}
  } ✅
```

#### 端口 19000 — 健康检查 Sidecar

```bash
curl http://7.6.16.150:19000/health
→ {"s":1,"p":"ready","backend_ok":true,"backend_code":200,"ever_ready":true} ✅
```

---

## 8. 验证结论

| 验证项 | 结果 |
|--------|------|
| 双节点部署 (infer-0 → .150, infer-1 → .148) | ✅ |
| NCCL Socket 网络初始化 | ✅ |
| L20 GPU 选择 (各节点各1块 L20 46GB) | ✅ |
| 模型权重加载 (两节点) | ✅ |
| KV Cache 分配 | ✅ |
| CUDA Graph 捕获 | ✅ |
| Port 17000 SGLang 引擎直连推理 | ✅ |
| Port 18000 wings-infer 代理推理 | ✅ |
| Port 19000 健康检查状态 | ✅ |
| DeepSeek-R1 推理正确输出 | ✅ |

**SGLang 分布式双节点推理验证全部通过。**

---

## 9. 相关文件

| 文件 | 说明 |
|------|------|
| `infer-control-sidecar-main/infer-control-sidecar-main-nv-dist/k8s/statefulset-sglang-dist.yaml` | SGLang 分布式 StatefulSet 完整配置 |
| `docker+k8s/doc_distribute/nv.md` | 分布式验证方案文档 |
| `docker+k8s/doc_distribute/sglang-dist-verify-report_20260304.md` | 本报告 |
