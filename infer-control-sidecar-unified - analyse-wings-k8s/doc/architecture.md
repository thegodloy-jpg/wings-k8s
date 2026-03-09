# 架构说明

## 整体架构

Wings-Infer 采用 **Sidecar 模式**，在 K8s Pod 中与推理引擎容器并行运行。Sidecar 不直接启动引擎进程，而是生成启动脚本、启动代理和健康检查子进程。

```
┌─── K8s Pod ──────────────────────────────────────────────────────────┐
│                                                                       │
│  ┌─ Wings-Infer Sidecar ──────────────────────────────────────────┐  │
│  │                                                                 │  │
│  │   main.py (Launcher)                                            │  │
│  │     │                                                           │  │
│  │     ├── 1. parse_launch_args()   ← 环境变量 / CLI              │  │
│  │     ├── 2. derive_port_plan()    → 17000 / 18000 / 19000       │  │
│  │     ├── 3. build_launcher_plan()                                │  │
│  │     │       ├── detect_hardware()                               │  │
│  │     │       ├── load_and_merge_configs()                        │  │
│  │     │       └── start_engine_service() → engine adapter         │  │
│  │     ├── 4. write start_command.sh → /shared-volume/             │  │
│  │     ├── 5. spawn proxy  (uvicorn :18000)                        │  │
│  │     └── 6. spawn health (uvicorn :19000)                        │  │
│  │                                                                 │  │
│  │   ┌─ Proxy (simple_proxy.py) ─┐  ┌─ Health (health.py) ──────┐│  │
│  │   │ :18000                     │  │ :19000                     ││  │
│  │   │ → /v1/chat/completions     │  │ → /health (状态机)         ││  │
│  │   │ → /v1/completions          │  │ → 探测 engine :17000/health││  │
│  │   │ 反向代理 → engine :17000   │  │ → PID 文件活性检查         ││  │
│  │   │ 重试 / 排队 / 流式中继     │  │ → 200/201/502/503          ││  │
│  │   └───────────────────────────┘  └────────────────────────────┘│  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                          ↕ /shared-volume/start_command.sh            │
│  ┌─ Engine Container ─────────────────────────────────────────────┐  │
│  │ 等待 start_command.sh → bash 执行                               │  │
│  │ :17000 推理 API (vllm / sglang / mindie)                       │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

## 模块职责

### Launcher (`main.py`)

| 步骤 | 动作 | 说明 |
|------|------|------|
| 1 | `parse_launch_args()` | 从 `sys.argv` 和环境变量提取 engine、model、分布式参数 |
| 2 | `derive_port_plan()` | 固定端口方案: backend=17000, proxy=18000, health=19000 |
| 3 | `build_launcher_plan()` | 合并配置 → 选择引擎适配器 → 生成 bash 脚本 |
| 4 | 写文件 | `start_command.sh` → `/shared-volume/` |
| 5-6 | spawn | 启动 proxy 和 health 子进程 (uvicorn) |
| loop | supervise | 每 `PROCESS_POLL_SEC` 秒检测子进程存活，自动重启 |

> **分布式 rank>0**: 不启动 proxy，仅启动 health。

### 引擎适配器 (`engines/`)

```
engine_manager.py
    ↓ dispatch
    ├── vllm_adapter.py      ← ENGINE=vllm 或 ENGINE=vllm_ascend
    ├── sglang_adapter.py    ← ENGINE=sglang
    └── mindie_adapter.py    ← ENGINE=mindie
```

- `vllm_ascend` 复用 `vllm_adapter.py`（通过 `ENGINE_ADAPTER_ALIASES`），在运行时根据 `params["engine"]` 区分行为
- 每个适配器实现 `build_start_script(params) → str`，返回完整 bash 脚本

#### vllm_adapter.py 特殊处理

| 功能 | 说明 |
|------|------|
| Triton NPU 补丁 | Ascend 环境下自动补丁 Triton 驱动 (`_patch_triton_driver()`) |
| Ray 分布式 | rank=0 启动 `ray start --head`，rank>0 启动 `ray start --address=<head>:6379`，等待连接后执行 `vllm serve` |
| 动态 head 发现 | 通过 DNS 查询 StatefulSet headless service `<sts>-0.<svc>` 获取 head IP |

### 配置加载 (`config_loader.py`)

三层配置合并（优先级从低到高）：

```
1. 默认 JSON 文件 (config/vllm_default.json 等)
2. 用户配置 (环境变量 USER_CONFIG)
3. CLI 参数 / 环境变量覆盖
```

- 参数名映射通过 `engine_parameter_mapping.json` 实现 (Wings 参数名 → 引擎原生参数名)

### 硬件检测 (`hardware_detect.py`)

通过环境变量静态检测（不依赖 torch/pynvml）：

```python
device = WINGS_DEVICE | DEVICE → 归一化为 "nvidia" 或 "ascend"
count  = WINGS_DEVICE_COUNT | DEVICE_COUNT → 设备数量
```

### 代理 (`simple_proxy.py`)

FastAPI ASGI 应用：

- 反向代理所有 `/v1/*` 请求到 `BACKEND_URL` (默认 `http://127.0.0.1:17000`)
- httpx 异步 HTTP 客户端，支持 keepalive 连接池
- 流式响应中继 (SSE)
- 自动重试 (5xx / 连接失败)
- 请求排队 (`QueueGate`)

### 健康检查 (`health.py`)

状态机模型:

```
                ┌──────────────┐
     启动 ───→  │  starting    │ ── 201
                │  (ever_ready │
                │   = false)   │
                └──────┬───────┘
                       │ engine /health → 200
                       ▼
                ┌──────────────┐
                │   ready      │ ── 200
                │  (status=1)  │◄────────┐
                └──────┬───────┘         │
                       │ fail_count      │ recover
                       │ > threshold     │
                       ▼                 │
                ┌──────────────┐         │
                │  degraded    │ ── 503──┘
                │ (status=-1)  │
                └──────────────┘

     超过 STARTUP_GRACE_MS 仍未就绪 → start_failed (502)
```

**关键参数**:
- `STARTUP_GRACE_MS`: 启动宽限期（默认 3600000ms = 60分钟）
- `POLL_INTERVAL_MS`: 探测间隔（默认 5000ms）
- `FAIL_THRESHOLD`: 连续失败次数阈值（默认 5）
- `WINGS_SKIP_PID_CHECK`: 跳过 PID 文件检查（K8s sidecar 模式必须 `true`）

## 端口规划

```
derive_port_plan(enable_reason_proxy=True):
    backend_port = 17000   # 引擎 API (Pod 内部)
    proxy_port   = 18000   # Wings 代理 (对外)
    health_port  = 19000   # 健康检查 (K8s probe)
```

K8s 中通过 `NodePort` 或 `containerPort` 暴露：

| 用途 | containerPort | NodePort 范围 |
|------|--------------|---------------|
| 引擎 API | 17000 | 30170xx |
| 代理 | 18000 | 30180xx |
| 健康检查 | 19000 | 30190xx |

## 分布式模式

### vLLM / vLLM-Ascend (Ray)

```
StatefulSet (replicas=NNODES, podManagementPolicy=Parallel)
    │
    ├── rank-0: ray start --head → vllm serve --tensor-parallel-size=TP
    │           + proxy(:18000) + health(:19000)
    │
    └── rank-1..N: ray start --address=<head>:6379
                   + health(:19000) (无 proxy)
```

- `NODE_RANK` 通过 K8s Downward API (`metadata.labels['apps.kubernetes.io/pod-index']`) 注入
- Head 发现: DNS 查询 `<sts>-0.<headless-svc>` 或 `HEAD_NODE_ADDR` 环境变量

### SGLang (nnodes)

```
StatefulSet (replicas=NNODES)
    │
    ├── rank-0: python -m sglang.launch_server --nnodes=N --node-rank=0
    │
    └── rank-1..N: python -m sglang.launch_server --nnodes=N --node-rank=K
```

### MindIE (HCCL)

```
StatefulSet (replicas=NNODES)
    │
    ├── rank-0: 生成 ranktable.json → mindie_llm_server
    │
    └── rank-1..N: 共享 ranktable.json → mindie_llm_server
```

- 需要特权容器 (`privileged: true`)
- HCCL 通信端口: 27070
- 驱动挂载: `/usr/local/dcmi`, `/usr/local/bin/npu-smi`, `/usr/local/Ascend/driver`

## 数据流

```
用户请求
    ↓
NodePort :30180 → Service → Pod
    ↓
Proxy (:18000) ──→ Engine (:17000)
    │                    ↓
    │              推理计算 (GPU/NPU)
    │                    ↓
    ←──────────── 流式/批量响应
    ↓
用户收到响应

K8s 探针
    ↓
NodePort :30190 → Service → Pod
    ↓
Health (:19000) ──→ 探测 Engine :17000/health
    ↓
200 / 201 / 502 / 503
```
