# Wings-Infer 统一推理控制 Sidecar

统一的推理引擎控制 sidecar，支持多种引擎和部署模式。

## 支持矩阵

| 引擎 | 单机 | 分布式 | 硬件 |
|------|------|--------|------|
| **vllm** | ✅ | ✅ (Ray) | NVIDIA GPU |
| **vllm_ascend** | ✅ | ✅ (Ray) | Ascend 910B NPU |
| **sglang** | ✅ | ✅ (nnodes) | NVIDIA GPU |
| **mindie** | ✅ | ✅ (HCCL) | Ascend 910B NPU |

## 架构

```
┌─ Pod ─────────────────────────────────────┐
│                                            │
│  ┌─ wings-infer (sidecar) ──────────┐     │
│  │ 1. 读取环境变量                   │     │
│  │ 2. 生成 start_command.sh          │     │
│  │ 3. 启动 proxy (→ engine)          │     │
│  │ 4. 启动 health check              │     │
│  └────────────────────────────────────┘     │
│       │ /shared-volume/start_command.sh     │
│  ┌─ engine (推理引擎) ──────────────┐     │
│  │ 等待 start_command.sh 生成        │     │
│  │ 执行引擎启动脚本                 │     │
│  │ 监听 ENGINE_PORT (默认 17000)     │     │
│  └────────────────────────────────────┘     │
└────────────────────────────────────────────┘
```

**端口规划**：
- `17000` — 推理引擎 API
- `18000` — Wings 代理端口
- `19000` — Wings 健康检查端口

## 快速开始

### 1. 构建 Sidecar 镜像

```bash
docker build -t wings-infer:latest .
```

### 2. 选择部署场景

```bash
# 查看所有可用场景
ls k8s/overlays/

# 输出:
# vllm-single/              — vLLM 单机 (NV GPU)
# vllm-distributed/         — vLLM + Ray 分布式 (NV GPU)
# vllm-ascend-single/       — vLLM-Ascend 单机 (Ascend NPU)
# vllm-ascend-distributed/  — vLLM-Ascend + Ray 分布式 (Ascend NPU)
# sglang-single/            — SGLang 单机 (NV GPU)
# sglang-distributed/       — SGLang 分布式 (NV GPU)
# mindie-single/            — MindIE 单机 (Ascend NPU)
# mindie-distributed/       — MindIE + HCCL 分布式 (Ascend NPU)
```

### 3. 配置参数

每个 overlay 目录下的 YAML 文件中，标记为 `CUSTOMIZE` 的参数需要根据实际环境修改：

```yaml
# 必须修改的参数:
HEAD_NODE_ADDR: "CHANGE_ME"           # 分布式模式: rank-0 节点 IP
NODE_IPS: "CHANGE_ME,CHANGE_ME"       # 分布式模式: 所有节点 IP
MODEL_NAME: "your-model-name"         # 模型名称
MODEL_PATH: "/models/your-model"      # 容器内模型路径
image: wings-infer:latest             # Sidecar 镜像
image: quay.io/ascend/vllm-ascend:... # 引擎镜像
```

### 4. 部署

```bash
# 使用 Kustomize 部署
kubectl apply -k k8s/overlays/vllm-ascend-distributed/

# 或先预览
kubectl kustomize k8s/overlays/vllm-ascend-distributed/ | less
```

### 5. 验证

```bash
# 查看 Pod 状态
kubectl -n wings-infer get pods -w

# 健康检查 (200=就绪, 201=启动中)
curl http://<NODE_IP>:19000/health

# 推理测试
curl http://<NODE_IP>:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"your-model","messages":[{"role":"user","content":"hello"}],"max_tokens":50}'
```

## 项目结构

```
infer-control-sidecar-unified/
├── Dockerfile                    # Wings-Infer sidecar 容器镜像
├── backend/                      # 后端代码
│   ├── requirements.txt
│   └── app/
│       ├── main.py               # 主入口
│       ├── config/               # 配置文件 (引擎参数映射, 默认值)
│       ├── core/                 # 核心逻辑 (引擎管理, 端口规划, 配置加载)
│       ├── engines/              # 引擎适配器
│       │   ├── vllm_adapter.py   # vLLM + vLLM-Ascend (含 Triton NPU 补丁)
│       │   ├── sglang_adapter.py # SGLang
│       │   └── mindie_adapter.py # MindIE (HCCL rank table, config.json 合并)
│       ├── proxy/                # 反向代理 + 健康检查状态机
│       └── utils/                # 工具模块
├── k8s/
│   ├── base/                     # Kustomize base (namespace)
│   └── overlays/                 # 8 个部署场景
│       ├── vllm-single/
│       ├── vllm-distributed/
│       ├── vllm-ascend-single/
│       ├── vllm-ascend-distributed/
│       ├── sglang-single/
│       ├── sglang-distributed/
│       ├── mindie-single/
│       └── mindie-distributed/
└── doc/                          # 详细部署文档
    ├── architecture.md           # 架构详解
    ├── troubleshooting.md        # 故障排查 (9 类问题)
    ├── QUICKSTART.md             # 快速上手
    ├── deploy-vllm.md            # vLLM 单机/分布式 (NVIDIA)
    ├── deploy-vllm-ascend.md     # vLLM-Ascend 单机/分布式 (NPU)
    ├── deploy-sglang.md          # SGLang 单机/分布式
    ├── deploy-mindie.md          # MindIE 单机/分布式 (NPU)
    └── deploy-vllm-ascend-dist-ray.md  # 已验证部署记录
```

## 文档索引

| 文档 | 说明 |
|------|------|
| [快速上手](doc/QUICKSTART.md) | 6 步完成从构建到推理验证 |
| [架构详解](doc/architecture.md) | 模块职责、端口规划、状态机、数据流 |
| [故障排查](doc/troubleshooting.md) | CrashLoop、201/502/503、Ray/HCCL、Triton 等 9 类问题 |
| [vLLM 部署](doc/deploy-vllm.md) | NVIDIA GPU 单机 + Ray 分布式 |
| [vLLM-Ascend 部署](doc/deploy-vllm-ascend.md) | Ascend NPU 单机 + Ray 分布式 (含 Triton 补丁说明) |
| [SGLang 部署](doc/deploy-sglang.md) | 单机 + nnodes 分布式 |
| [MindIE 部署](doc/deploy-mindie.md) | Ascend NPU 单机 + HCCL 分布式 (含 rank table) |

## 环境变量

### 通用

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ENGINE` | 引擎类型 | `vllm` |
| `MODEL_NAME` | 模型名称 | — |
| `MODEL_PATH` | 模型路径 | `/models/<MODEL_NAME>` |
| `ENGINE_PORT` | 引擎端口 | `17000` |
| `PORT` | 代理端口 | `18000` |
| `HEALTH_PORT` | 健康检查端口 | `19000` |
| `BACKEND_URL` | 代理后端 URL | `http://127.0.0.1:17000` |
| `WINGS_SKIP_PID_CHECK` | 跳过 PID 检查 | `false` |

### 分布式模式

| 变量 | 说明 |
|------|------|
| `DISTRIBUTED` | 是否分布式 (`true`/`false`) |
| `NNODES` | 节点总数 |
| `NODE_RANK` | 当前节点序号 (推荐用 Downward API) |
| `HEAD_NODE_ADDR` | Head 节点 IP |
| `NODE_IPS` | 所有节点 IP (逗号分隔) |
| `DISTRIBUTED_EXECUTOR_BACKEND` | 分布式后端 (`ray`/`mp`) |

### 硬件相关

| 变量 | 说明 |
|------|------|
| `WINGS_DEVICE` | 硬件类型 (`ascend`/空) |
| `DEVICE` | 同上 |
| `ASCEND_VISIBLE_DEVICES` | Ascend NPU 设备号 |
| `NVIDIA_VISIBLE_DEVICES` | NVIDIA GPU 设备号 |

## 健康检查

| HTTP 状态码 | 阶段 | 含义 |
|-------------|------|------|
| **200** | `ready` | 引擎就绪，可处理推理请求 |
| **201** | `starting` | 启动过渡期，引擎初始化中 |
| **502** | `start_failed` | 超过启动宽限期仍未就绪 |
| **503** | `degraded` | 曾就绪后变为不可用 |

## License

MIT
