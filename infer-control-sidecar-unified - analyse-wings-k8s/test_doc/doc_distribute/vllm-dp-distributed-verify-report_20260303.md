# vLLM DP 双机分布式验证报告

> 验证时间：2026-03-03
> 验证人：zhanghui
> 文档引用：docker+k8s/doc_distribute/nv.md

## 1. 环境信息

| 项目 | 值 |
|---|---|
| **k3s 版本** | v1.30.6+k3s1 |
| **节点 1 (server)** | a100 / 7.6.52.148 / NVIDIA A100 40GB + L20 46GB / 驱动 550.90.07 |
| **节点 2 (agent)** | ubuntu2204 / 7.6.16.150 / RTX 4090 x2 + L20 x2 / 驱动 570.124.06 |
| **k3s 部署方式** | Docker-in-Docker（`--privileged --net=host`） |
| **模型** | DeepSeek-R1-Distill-Qwen-1.5B |
| **vLLM 版本** | v0.13.0（NCCL 2.27.5 + CUDA 12.9） |
| **wings-infer 镜像** | `wings-infer:dist-nv-dev-zhanghui` |
| **部署方式** | StatefulSet（2 replicas） + hostNetwork 模式 |

## 2. 架构

```
StatefulSet: infer (replicas=2)
├── infer-0 (ubuntu2204 / 7.6.16.150) → rank 0 (DP Coordinator + EngineCore_DP0)
│   ├── wings-infer container → 生成 head 脚本
│   └── engine container → vLLM API Server (port 17000) + DP Coordinator (port 13355)
└── infer-1 (a100 / 7.6.52.148) → rank 1 (headless)
    ├── wings-infer container → 生成 worker 脚本
    └── engine container → vLLM headless worker (连接 7.6.16.150:13355)
```

**关键参数:**
- `--data-parallel-size 2` / `--data-parallel-size-local 1`
- `--data-parallel-external-lb` / `--data-parallel-rank 0|1`
- `--data-parallel-address 7.6.16.150` / `--data-parallel-rpc-port 13355`
- rank 1 使用 `--headless` 模式

## 3. 解决的关键问题

### 3.1 网络层
| 问题 | 原因 | 解决方案 |
|---|---|---|
| ZMQ bind 失败 | CoreDNS 在 k3s-in-Docker 中不工作 | 使用真实 IP 替代 DNS 名称 |
| 跨 Pod 网络不通 | Flannel overlay 在 Docker-in-Docker 中不工作 | 使用 `hostNetwork: true` |
| c10d hostname 解析失败 | hostNetwork 下 pod 的 hostname=节点名(ubuntu2204/a100)，无法 DNS 解析 | 注入 `/etc/hosts` + 设置 `GLOO_SOCKET_IFNAME=ens65f0` |

### 3.2 NCCL 层
| 问题 | 原因 | 解决方案 |
|---|---|---|
| PTX JIT compiler not found | k3s 容器中缺少 `libnvidia-ptxjitcompiler.so` | 从宿主机复制驱动库到 `/mnt/nvidia-libs/`，engine 启动脚本动态创建 symlink |
| NCCL IB/RoCE 通信失败 | Soft-RoCE (rxe0) 在两节点间不兼容（驱动版本不同：550 vs 570） | 设置 `NCCL_IB_DISABLE=1` + `NCCL_NET=Socket` |
| /dev/shm 空间不足 | 默认 64MB，NCCL 需要 ~33MB 共享内存段 | 挂载 `emptyDir(medium: Memory, sizeLimit: 1Gi)` 到 `/dev/shm` |

### 3.3 代码修复
| 文件 | 修复内容 |
|---|---|
| `vllm_adapter.py` | 6 处硬编码 `infer-0.infer-hl` → 使用 `head_node_addr` 变量；DP 参数 3 次迭代修正 |
| StatefulSet YAML | 多次迭代：hostNetwork → GLOO/NCCL env → /etc/hosts → PTX symlink → IB disable → /dev/shm |

## 4. NCCL 初始化验证

```
(EngineCore_DP0 pid=287) world_size=2 rank=0 local_rank=0 distributed_init_method=tcp://7.6.16.150:40937 backend=nccl
(EngineCore_DP0 pid=287) vLLM is using nccl==2.27.5
NCCL INFO Assigned NET plugin Socket to comm
NCCL INFO Using network Socket
NCCL INFO ncclCommInitRank comm 0x467012d0 rank 0 nranks 2 cudaDev 0 nvmlDev 0 busId 56000 - Init START
NCCL INFO comm 0x467012d0 rank 0 nRanks 2 nNodes 2 localRanks 1 localRank 0 MNNVL 0
NCCL INFO Channel 00/02 : 0 1
NCCL INFO Channel 01/02 : 0 1
NCCL INFO ncclCommInitRank comm 0x467012d0 rank 0 nranks 2 cudaDev 0 nvmlDev 0 busId 56000 - Init COMPLETE
```

**关键确认:**
- ✅ NCCL 使用 Socket 传输（非 IB）
- ✅ 2 节点 2 rank，Init COMPLETE
- ✅ 2 个通信 channel 建立

## 5. Pod 状态

```
NAME      READY   STATUS    RESTARTS        AGE   IP           NODE         
infer-0   2/2     Running   1 (71m ago)     72m   7.6.16.150   ubuntu2204   
infer-1   2/2     Running   2 (9m19s ago)   71m   7.6.52.148   a100         
```

## 6. 推理测试

> 测试时间: Wed Mar  4 13:57:26 CST 2026
> 测试节点: 7.6.16.150 (infer-0, rank 0)

### 6.1 端口 17000 — vLLM 引擎直连

| API | HTTP | 耗时 | 结果 |
|---|---|---|---|
| GET /health | ✅ 200 | 0.001s | 空响应体（正常） |
| GET /v1/models | ✅ 200 | 0.002s | `DeepSeek-R1-Distill-Qwen-1.5B`, max_model_len=5120 |
| POST /v1/chat/completions | ✅ 200 | 0.208s | 生成 30 tokens, `<think>` 推理模式生效 |
| GET /version | ✅ 200 | 0.003s | `{"version":"0.13.0"}` |
| POST /v1/completions | ✅ 200 | 0.078s | 补全 "The capital of France is" → " a city named ____.\nThe capital of the United" |

**Chat Completion 响应示例 (17000):**
```json
{
    "id": "chatcmpl-870ed3c89668cf0d",
    "model": "DeepSeek-R1-Distill-Qwen-1.5B",
    "choices": [{
        "message": {
            "role": "assistant",
            "content": "<think>\nI need to provide a brief answer to the question \"What is 1+1?\"\n\nFirst, I recognize that the question is straightforward and"
        },
        "finish_reason": "length"
    }],
    "usage": {"prompt_tokens": 13, "total_tokens": 43, "completion_tokens": 30}
}
```

### 6.2 端口 18000 — wings-infer 代理

| API | HTTP | 耗时 | 结果 |
|---|---|---|---|
| GET /health | ✅ 200 | 0.004s | `{"s":1,"p":"ready","backend_ok":true,"backend_code":200,"ever_ready":true}` |
| GET /v1/models | ✅ 200 | 0.006s | 同 17000，正确代理 |
| POST /v1/chat/completions | ✅ 200 | 0.191s | 生成 30 tokens，代理正常转发 |

**Health 响应 (18000):**
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
    "lat_ms": 5
}
```

### 6.3 端口 19000 — 健康检查端口

| API | HTTP | 耗时 | 结果 |
|---|---|---|---|
| GET /health | ✅ 200 | 0.002s | 同 18000 health 响应 |
| GET / | ❌ 404 | 0.001s | `{"detail":"Not Found"}`（预期行为，仅 /health 可用） |

### 6.4 汇总

| 端口 | 用途 | /health | /v1/models | /v1/chat/completions | /v1/completions | /version |
|---|---|---|---|---|---|---|
| **17000** | vLLM 引擎直连 | ✅ | ✅ | ✅ (0.208s) | ✅ (0.078s) | ✅ |
| **18000** | wings-infer 代理 | ✅ | ✅ | ✅ (0.191s) | — | — |
| **19000** | 健康检查 | ✅ | — | — | — | — |

## 7. StatefulSet YAML 关键配置

```yaml
# 网络
hostNetwork: true
dnsPolicy: ClusterFirstWithHostNet

# NCCL 环境变量
GLOO_SOCKET_IFNAME: ens65f0
NCCL_SOCKET_IFNAME: ens65f0
NCCL_DEBUG: INFO
NCCL_P2P_DISABLE: "1"
NCCL_SHM_DISABLE: "1"
NCCL_IB_DISABLE: "1"
NCCL_NET: Socket

# /dev/shm
volumes:
  - name: dshm
    emptyDir:
      medium: Memory
      sizeLimit: 1Gi

# nvidia 驱动库
volumes:
  - name: nvidia-libs
    hostPath:
      path: /mnt/nvidia-libs  # 包含 libcuda.so, libnvidia-ml.so, libnvidia-ptxjitcompiler.so
```

## 8. 结论

vLLM DP (Data Parallel) 双机分布式验证 **通过**。

在 k3s-in-Docker 环境下，通过以下关键配置成功实现跨节点 vLLM DP 推理：
1. `hostNetwork` 模式绕过 broken flannel overlay
2. NCCL Socket 传输替代 IB/RoCE
3. 动态 nvidia 驱动库 symlink 创建
4. /dev/shm 共享内存扩展

**文件路径:**
- StatefulSet YAML: `infer-control-sidecar-main/infer-control-sidecar-main-nv-dist/k8s/statefulset-vllm-dp.yaml`
- vLLM 适配器: `infer-control-sidecar-main/infer-control-sidecar-main-nv-dist/backend-dist-nv-20260303/app/engines/vllm_adapter.py`
