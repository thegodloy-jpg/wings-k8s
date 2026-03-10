# Wings-Infer 统一推理控制 Sidecar

> **引擎**: vLLM · vLLM-Ascend · SGLang · MindIE · Wings  
> **硬件**: NVIDIA GPU · Ascend 910B NPU  
> **模式**: 单机 · 分布式 (Ray/HCCL/nnodes) · Master-Worker  
> **兼容**: 与 wings/wings_start.sh 100% CLI 兼容

---

## 快速开始

```bash
# 1. 构建镜像
cd infer-control-sidecar-unified/
docker build -t wings-infer:latest .

# 2. 单容器测试（仅生成启动脚本）
docker run --rm -it \
  -e WINGS_SKIP_PID_CHECK=true \
  -p 18000:18000 -p 19000:19000 \
  wings-infer:latest \
  --model-name test-model --model-path /weights

# 3. 验证
curl http://localhost:19000/health
```

更多场景参见 [doc/QUICKSTART.md](doc/QUICKSTART.md)

---

## 架构

```
┌─ K8s Pod ──────────────────────────────────────────────┐
│  (可选) initContainer: accel-init → /accel-volume/     │
│                                                         │
│  wings-infer (Sidecar)          engine 容器             │
│  ┌────────────────────┐   ┌──────────────────────┐     │
│  │ wings_start.sh     │   │ 等待 start_command.sh│     │
│  │  → python -m app.main │ │  → bash 执行         │     │
│  │  → 生成脚本 ───────┼──→│  → serve :17000      │     │
│  │  → proxy :18000    │   │                      │     │
│  │  → health :19000   │   │                      │     │
│  └────────────────────┘   └──────────────────────┘     │
│              共享卷: /shared-volume/                     │
└─────────────────────────────────────────────────────────┘
```

**数据流**: CLI/环境变量 → `wings_start.sh` → `app.main` → 配置合并(4层) → 引擎适配器 → `start_command.sh` → engine 容器执行

**配置优先级**: CLI 参数 > 环境变量 > 用户配置文件 > 模型特定配置 > 硬件默认配置

---

## 支持矩阵

| 引擎 | 单机 | 分布式 | 硬件 | K8s Overlay |
|------|------|--------|------|-------------|
| vllm | ✅ | ✅ Ray/DP | NVIDIA GPU | `vllm-single/` · `vllm-distributed/` |
| vllm_ascend | ✅ | ✅ Ray | Ascend 910B | `vllm-ascend-single/` · `vllm-ascend-distributed/` |
| sglang | ✅ | ✅ nnodes | NVIDIA GPU | `sglang-single/` · `sglang-distributed/` |
| mindie | ✅ | ✅ HCCL | Ascend 910B | `mindie-single/` · `mindie-distributed/` |
| wings | ✅ | — | GPU/NPU | — |

**自动引擎选择**: Ascend + vllm → `vllm_ascend` · mmgm 模型 → `wings` · embedding/rerank + Ascend → `vllm_ascend`

---

## 项目结构

```
infer-control-sidecar-unified/
├── Dockerfile                        # Sidecar 镜像
├── wings_start.sh                    # 启动入口 (ENTRYPOINT)
├── .env.example                      # 环境变量模板
├── build-accel-image.sh              # Accel 镜像构建
├── backend/app/
│   ├── main.py                       # 主入口 (角色分发)
│   ├── config/                       # 配置 (settings.py + 引擎默认 JSON)
│   ├── core/                         # 核心 (config_loader · engine_manager · hardware_detect · wings_entry)
│   ├── engines/                      # 适配器 (vllm · sglang · mindie)
│   ├── distributed/                  # 分布式 (master · worker · monitor · scheduler)
│   ├── proxy/                        # 代理 (gateway · health · queueing)
│   └── utils/                        # 工具 (env · file · device · model · noise_filter · process)
├── k8s/{base,overlays/}              # Kustomize (8 个部署 overlay)
├── wings-accel/                      # 加速包 (可选 initContainer)
└── doc/                              # 详细文档
```

---

## 部署

### Docker Compose (单机)

```yaml
services:
  wings-infer:
    image: wings-infer:latest
    ports: ["18000:18000", "19000:19000"]
    environment:
      ENGINE: vllm
      MODEL_NAME: DeepSeek-R1-Distill-Qwen-1.5B
      MODEL_PATH: /models/DeepSeek-R1-Distill-Qwen-1.5B
      WINGS_SKIP_PID_CHECK: "true"
      BACKEND_URL: http://engine:17000
    volumes: [shared-vol:/shared-volume, /path/to/models:/models:ro]

  engine:
    image: vllm/vllm-openai:latest
    runtime: nvidia
    command: /bin/sh -c "while [ ! -f /shared-volume/start_command.sh ]; do sleep 2; done; bash /shared-volume/start_command.sh"
    volumes: [shared-vol:/shared-volume, /path/to/models:/models:ro]

volumes:
  shared-vol:
```

### K8s (Kustomize)

```bash
# 单机
kubectl apply -k k8s/overlays/vllm-single/

# 分布式
kubectl apply -k k8s/overlays/vllm-distributed/
```

### Docker 命令行

```bash
# 单机
docker run --runtime nvidia -p 18000:18000 -p 19000:19000 \
  -v /models:/models:ro wings-infer:latest \
  --model-name Qwen2-7B --model-path /models/Qwen2-7B --engine vllm

# 分布式 rank-0
docker run --network host -e DISTRIBUTED=true -e NNODES=2 \
  -e NODE_RANK=0 -e HEAD_NODE_ADDR=192.168.1.100 \
  wings-infer:latest --model-name DeepSeek-R1 --model-path /models/DeepSeek-R1 --distributed
```

---

## 端口规划

| 端口 | 用途 | 暴露 |
|------|------|------|
| 17000 | 推理引擎 | Pod 内部 |
| 18000 | API 代理 (OpenAI 兼容) | NodePort/LB |
| 19000 | 健康检查 (K8s 探针) | 探针 |

分布式端口: Ray `6379` · SGLang `28030` · vLLM DP `13355` · MindIE `27070` · NIXL `5759`

---

## 健康检查

```bash
curl http://<host>:19000/health         # 200=就绪 201=启动中 502=失败 503=降级
curl http://<host>:19000/health/detail  # JSON 详情
```

```yaml
readinessProbe:
  httpGet: { path: /health, port: 19000 }
  initialDelaySeconds: 60
  periodSeconds: 10
  failureThreshold: 36
livenessProbe:
  httpGet: { path: /health, port: 19000 }
  initialDelaySeconds: 120
  periodSeconds: 30
  failureThreshold: 5
```

---

## CLI 参数

| 参数 | 环境变量 | 默认 | 说明 |
|------|----------|------|------|
| `--model-name` | `MODEL_NAME` | **必填** | 模型名 |
| `--model-path` | `MODEL_PATH` | `/weights` | 模型路径 |
| `--engine` | `ENGINE` | `vllm` | vllm/vllm_ascend/sglang/mindie/wings |
| `--port` | `PORT` | `18000` | 监听端口 |
| `--input-length` | `INPUT_LENGTH` | `4096` | 最大输入 |
| `--output-length` | `OUTPUT_LENGTH` | `1024` | 最大输出 |
| `--gpu-memory-utilization` | `GPU_MEMORY_UTILIZATION` | `0.9` | 显存利用率 |
| `--max-num-seqs` | `MAX_NUM_SEQS` | `32` | 最大并发序列 |
| `--dtype` | `DTYPE` | `auto` | 数据类型 |
| `--model-type` | `MODEL_TYPE` | `auto` | auto/llm/embedding/rerank/mmum/mmgm |
| `--distributed` | `DISTRIBUTED` | `false` | 分布式模式 |
| `--trust-remote-code` | `TRUST_REMOTE_CODE` | `true` | 信任远程代码 |
| `--enable-prefix-caching` | `ENABLE_PREFIX_CACHING` | `false` | 前缀缓存 |
| `--enable-chunked-prefill` | `ENABLE_CHUNKED_PREFILL` | `false` | 分块预填充 |
| `--enable-expert-parallel` | `ENABLE_EXPERT_PARALLEL` | `false` | MoE 专家并行 |
| `--enable-speculative-decode` | `ENABLE_SPECULATIVE_DECODE` | `false` | 推测解码 |
| `--enable-rag-acc` | `ENABLE_RAG_ACC` | `false` | RAG 加速 |
| `--enable-auto-tool-choice` | `ENABLE_AUTO_TOOL_CHOICE` | `false` | 函数调用 |
| `--config-file` | `CONFIG_FILE` | — | 自定义配置 |
| `--device-count` | `DEVICE_COUNT` | `1` | 设备数 |
| `--save-path` | `SAVE_PATH` | `/opt/wings/outputs` | 输出目录 |

完整 30+ 参数详见 `wings_start.sh --help`

---

## 环境变量速查

### Sidecar

| 变量 | 默认 | 说明 |
|------|------|------|
| `SHARED_VOLUME_PATH` | `/shared-volume` | 共享卷 |
| `WINGS_SKIP_PID_CHECK` | `false` | 跳过 PID 检查 (sidecar 必须 true) |
| `ENABLE_REASON_PROXY` | `true` | 启用代理 |
| `BACKEND_URL` | `http://127.0.0.1:17000` | 后端 URL |

### 分布式

| 变量 | 说明 |
|------|------|
| `DISTRIBUTED` | 是否分布式 |
| `NNODES` | 节点总数 |
| `NODE_RANK` | 当前节点序号 |
| `HEAD_NODE_ADDR` | Head 节点 IP |
| `NODE_IPS` | 所有节点 IP (逗号分隔) |
| `MASTER_IP` | Master 节点 IP (Master-Worker 模式) |

### 硬件

| 变量 | 说明 |
|------|------|
| `WINGS_DEVICE` | nvidia/ascend |
| `WINGS_DEVICE_COUNT` | 设备数 |
| `WINGS_DEVICE_MEMORY` | 显存 GB (cuda_graph_sizes 计算) |

### 加速

| 变量 | 说明 |
|------|------|
| `ENABLE_ACCEL` | 启用 Accel 补丁注入 |
| `WINGS_ENGINE_PATCH_OPTIONS` | 覆盖补丁选项 (JSON) |

完整模板: [.env.example](.env.example)

---

## Accel 加速包

可选的 initContainer，将 `wings_engine_patch` 注入 engine 容器：

```bash
bash build-accel-image.sh  # 构建 wings-accel:latest
```

启用: 设置 `ENABLE_ACCEL=true`，sidecar 自动注入 `WINGS_ENGINE_PATCH_OPTIONS`

详情: [doc/deploy-accel.md](doc/deploy-accel.md)

---

## Master-Worker 分布式

`main.py` 自动判断角色:

| 条件 | 角色 | 行为 |
|------|------|------|
| `DISTRIBUTED=false` | standalone | 直接生成脚本 + proxy/health |
| `DISTRIBUTED=true` + `MASTER_IP=本机` | master | FastAPI 协调 (注册/心跳/调度) |
| `DISTRIBUTED=true` + `MASTER_IP≠本机` | worker | 注册 → 等待指令 → 生成脚本 |

调度策略: `least_load` (默认) · `round_robin` · `random`

---

## 从 wings 迁移

```yaml
# 原版 wings — 单容器
containers:
  - name: wings
    image: wings:latest
    args: ["--model-name", "DeepSeek-R1", "--engine", "vllm"]

# 迁移后 unified — 双容器 (参数不变)
volumes:
  - name: shared-volume
    emptyDir: {}
containers:
  - name: wings-infer
    image: wings-infer:latest
    args: ["--model-name", "DeepSeek-R1", "--engine", "vllm"]
    env: [{name: WINGS_SKIP_PID_CHECK, value: "true"}]
    volumeMounts: [{name: shared-volume, mountPath: /shared-volume}]
  - name: engine
    image: vllm/vllm-openai:latest
    command: ["/bin/sh", "-c", "while [ ! -f /shared-volume/start_command.sh ]; do sleep 2; done; bash /shared-volume/start_command.sh"]
    volumeMounts: [{name: shared-volume, mountPath: /shared-volume}]
```

---

## 故障排查

| 症状 | 解决 |
|------|------|
| health 持续 201 | 引擎启动慢，增大 failureThreshold |
| health 502 | 引擎启动失败，查 engine 容器日志 |
| proxy 502 | 确认 BACKEND_URL 和 ENGINE_PORT |
| start_command.sh 未生成 | 查 wings-infer 日志 |
| 分布式卡住 | 检查 HEAD_NODE_ADDR 可达性 |
| Ascend 用了 vllm | 设置 WINGS_DEVICE=ascend |

```bash
# 常用调试
kubectl exec -it deploy/infer -c wings-infer -- cat /shared-volume/start_command.sh
kubectl exec -it deploy/infer -c wings-infer -- curl localhost:19000/health/detail
```

详情: [doc/troubleshooting.md](doc/troubleshooting.md)

---

## 文档索引

| 文档 | 说明 |
|------|------|
| [快速上手](doc/QUICKSTART.md) | 6 步构建到推理 |
| [架构详解](doc/architecture.md) | 模块·端口·状态机 |
| [故障排查](doc/troubleshooting.md) | 9 类问题 |
| [vLLM](doc/deploy-vllm.md) · [vLLM-Ascend](doc/deploy-vllm-ascend.md) · [SGLang](doc/deploy-sglang.md) · [MindIE](doc/deploy-mindie.md) | 引擎部署 |
| [Accel](doc/deploy-accel.md) | 加速包 |
| [版本差异](doc/version-diff-report.md) | wings vs unified |
| [Bug 修复](BUG_FIX_REPORT.md) | 9 Bug 详情 |

## License

MIT
