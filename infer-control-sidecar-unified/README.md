# Wings-Infer 统一推理控制 Sidecar — 完整操作指南

> **版本**: v4 (Sidecar 架构)  
> **兼容性**: 与 wings/wings_start.sh 100% CLI 兼容，编排层零修改可替换  
> **引擎支持**: vLLM / vLLM-Ascend / SGLang / MindIE  
> **部署模式**: 单机 / 分布式(Ray/HCCL/nnodes)  
> **硬件支持**: NVIDIA GPU / Ascend 910B NPU

---

## 目录

- [一、项目概述](#一项目概述)
- [二、架构说明](#二架构说明)
- [三、支持矩阵](#三支持矩阵)
- [四、项目结构](#四项目结构)
- [五、路线 A：从 Dockerfile 构建镜像并拉起服务](#五路线-a从-dockerfile-构建镜像并拉起服务)
  - [5.1 前提条件](#51-前提条件)
  - [5.2 构建 Sidecar 镜像](#52-构建-sidecar-镜像)
  - [5.3 场景一：Docker 单容器快速验证](#53-场景一docker-单容器快速验证)
  - [5.4 场景二：Docker Compose 双容器部署](#54-场景二docker-compose-双容器部署)
  - [5.5 场景三：K8s 单机部署 (Kustomize)](#55-场景三k8s-单机部署-kustomize)
  - [5.6 场景四：K8s 分布式部署 (Kustomize)](#56-场景四k8s-分布式部署-kustomize)
- [六、路线 B：wings-infer 镜像已存在，直接启动服务](#六路线-bwings-infer-镜像已存在直接启动服务)
  - [6.1 方式一：通过 wings_start.sh 启动（推荐，与原始 wings 完全兼容）](#61-方式一通过-wings_startsh-启动推荐与原始-wings-完全兼容)
  - [6.2 方式二：通过 python -m app.main 启动](#62-方式二通过-python--m-appmain-启动)
  - [6.3 方式三：K8s 部署（镜像已推送到仓库）](#63-方式三k8s-部署镜像已推送到仓库)
- [七、CLI 参数完整参考](#七cli-参数完整参考)
- [八、环境变量参考](#八环境变量参考)
- [九、端口规划](#九端口规划)
- [十、健康检查](#十健康检查)
- [十一、各引擎部署场景详解](#十一各引擎部署场景详解)
- [十二、故障排查](#十二故障排查)
- [十三、从 wings 迁移](#十三从-wings-迁移)
- [十四、文档索引](#十四文档索引)

---

## 一、项目概述

Wings-Infer 是一个统一的推理引擎控制 Sidecar，负责：

1. **参数解析与配置合并** — 接收 CLI / 环境变量，合并引擎配置，生成完整的引擎启动脚本
2. **脚本传递** — 将生成的 `start_command.sh` 写入共享卷，引擎容器读取并执行
3. **代理服务** — 对外暴露统一的 OpenAI 兼容 API（端口 18000）
4. **健康检查** — 提供 K8s 就绪/存活探针接口（端口 19000）

---

## 二、架构说明

### 2.1 Sidecar 双容器架构

```
┌─ K8s Pod ──────────────────────────────────────────────────┐
│                                                             │
│  ┌─ wings-infer (Sidecar 容器) ──────────────────────┐     │
│  │                                                    │     │
│  │  wings_start.sh                                    │     │
│  │       ↓                                            │     │
│  │  python -m app.main                                │     │
│  │       ├── 解析参数 → 生成 start_command.sh ────────┼──┐  │
│  │       ├── 启动 proxy   (uvicorn :18000)            │  │  │
│  │       └── 启动 health  (uvicorn :19000)            │  │  │
│  │                                                    │  │  │
│  └────────────────────────────────────────────────────┘  │  │
│                                                          │  │
│  ┌─ engine (推理引擎容器) ───────────────────────────┐  │  │
│  │                                                    │  │  │
│  │  等待 start_command.sh 生成                        │  │  │
│  │       ↓                                            │  │  │
│  │  exec bash /shared-volume/start_command.sh  ←──────┘  │  │
│  │       ↓                                               │  │
│  │  vllm/sglang/mindie serve :17000                      │  │
│  │                                                       │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌─ 共享卷 /shared-volume/ ─────────────────────────────┐  │
│  │  start_command.sh   (由 wings-infer 写入)             │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 数据流

```
用户 CLI 参数 / 环境变量
         │
         ▼
  wings_start.sh (参数解析 + 环境变量导出)
         │
         ▼
  python -m app.main
         │
         ├── parse_launch_args()     → LaunchArgs (frozen)
         ├── derive_port_plan()      → PortPlan (17000/18000/19000)
         ├── build_launcher_plan()   → LauncherPlan
         │      ├── config_loader    → 4 层配置合并
         │      ├── engine_manager   → 适配器选择
         │      └── adapter          → 生成 bash 脚本
         │
         ├── write_start_command()   → /shared-volume/start_command.sh
         │
         └── spawn 子进程
                ├── proxy  (uvicorn :18000)  ← 请求转发到 engine:17000
                └── health (uvicorn :19000)  ← K8s 探针
```

---

## 三、支持矩阵

| 引擎 | 单机 | 分布式 | 硬件 | K8s Overlay |
|------|------|--------|------|-------------|
| **vllm** | ✅ | ✅ (Ray) | NVIDIA GPU | `vllm-single/` / `vllm-distributed/` |
| **vllm_ascend** | ✅ | ✅ (Ray) | Ascend 910B NPU | `vllm-ascend-single/` / `vllm-ascend-distributed/` |
| **sglang** | ✅ | ✅ (nnodes) | NVIDIA GPU | `sglang-single/` / `sglang-distributed/` |
| **mindie** | ✅ | ✅ (HCCL) | Ascend 910B NPU | `mindie-single/` / `mindie-distributed/` |

---

## 四、项目结构

```
infer-control-sidecar-unified/
├── Dockerfile                    # Sidecar 容器镜像定义
├── wings_start.sh                # 兼容 wings 的启动脚本（ENTRYPOINT）
├── README.md                     # 本文档
├── backend/
│   ├── requirements.txt          # Python 依赖
│   └── app/
│       ├── main.py               # Sidecar 主入口（launcher）
│       ├── config/               # 配置定义 + 引擎参数映射 JSON
│       │   ├── settings.py       # 全局配置单例（pydantic-settings）
│       │   └── *.json            # 引擎默认配置文件
│       ├── core/                 # 核心控制逻辑
│       │   ├── config_loader.py  # 4 层配置合并（1500+ 行）
│       │   ├── engine_manager.py # 引擎适配器动态加载
│       │   ├── hardware_detect.py# 硬件探测（环境变量驱动）
│       │   ├── port_plan.py      # 三层端口规划
│       │   ├── start_args_compat.py # CLI 兼容层（30 个参数）
│       │   └── wings_entry.py    # LauncherPlan 构建
│       ├── engines/              # 引擎适配器
│       │   ├── vllm_adapter.py   # vLLM + vLLM-Ascend
│       │   ├── sglang_adapter.py # SGLang
│       │   └── mindie_adapter.py # MindIE
│       ├── proxy/                # 反向代理 + 健康检查
│       │   ├── gateway.py        # FastAPI 代理（流式/非流式）
│       │   ├── health.py         # 健康状态机（7 阶段）
│       │   ├── health_service.py # 健康 FastAPI 应用
│       │   ├── settings.py       # 代理配置（连接池/重试/超时）
│       │   ├── simple_proxy.py   # 低层 HTTP 转发
│       │   └── ...
│       └── utils/                # 工具模块
├── k8s/
│   ├── base/                     # Kustomize base
│   └── overlays/                 # 8 个部署场景
│       ├── vllm-single/          # vLLM 单机 (NV GPU)
│       ├── vllm-distributed/     # vLLM + Ray 分布式 (NV GPU)
│       ├── vllm-ascend-single/   # vLLM-Ascend 单机 (Ascend NPU)
│       ├── vllm-ascend-distributed/ # vLLM-Ascend + Ray 分布式 (Ascend NPU)
│       ├── sglang-single/        # SGLang 单机 (NV GPU)
│       ├── sglang-distributed/   # SGLang 分布式 (NV GPU)
│       ├── mindie-single/        # MindIE 单机 (Ascend NPU)
│       └── mindie-distributed/   # MindIE + HCCL 分布式 (Ascend NPU)
└── doc/                          # 详细文档
```

---

## 五、路线 A：从 Dockerfile 构建镜像并拉起服务

### 5.1 前提条件

| 组件 | 要求 |
|------|------|
| Docker | ≥ 20.10 |
| Python | 3.10+（仅开发/本地调试需要） |
| K8s | k3s ≥ 1.27 或 k8s ≥ 1.25（K8s 部署时需要） |
| GPU 驱动 | NVIDIA: CUDA ≥ 12.1 / Ascend: CANN ≥ 8.0 |
| 模型 | 已下载到宿主机指定路径 |

### 5.2 构建 Sidecar 镜像

```bash
# 进入项目根目录
cd infer-control-sidecar-unified/

# 构建镜像
docker build -t wings-infer:latest .

# 验证构建结果
docker run --rm wings-infer:latest --help
```

构建产物：
- `/app/app/` — Python 后端代码
- `/app/wings_start.sh` — 启动脚本
- `/shared-volume/` — 共享卷目录（空）
- `/var/log/wings/` — 日志目录

### 5.3 场景一：Docker 单容器快速验证

> 适用于：本地开发验证 sidecar 逻辑（不启动真实引擎）

```bash
# 仅启动 sidecar（会生成 start_command.sh 到 /shared-volume/）
docker run --rm -it \
  -e MODEL_NAME=test-model \
  -e MODEL_PATH=/weights \
  -e WINGS_SKIP_PID_CHECK=true \
  -p 18000:18000 \
  -p 19000:19000 \
  wings-infer:latest \
  --model-name test-model --model-path /weights

# 查看生成的启动脚本
docker exec <container_id> cat /shared-volume/start_command.sh
```

### 5.4 场景二：Docker Compose 双容器部署

> 适用于：单机全栈部署（sidecar + 引擎）

创建 `docker-compose.yml`：

```yaml
version: "3.8"

services:
  wings-infer:
    build: .
    # 或: image: wings-infer:latest
    ports:
      - "18000:18000"   # 代理端口
      - "19000:19000"   # 健康检查端口
    environment:
      - ENGINE=vllm
      - MODEL_NAME=DeepSeek-R1-Distill-Qwen-1.5B
      - MODEL_PATH=/models/DeepSeek-R1-Distill-Qwen-1.5B
      - ENGINE_PORT=17000
      - PORT=18000
      - HEALTH_PORT=19000
      - WINGS_SKIP_PID_CHECK=true
      - BACKEND_URL=http://engine:17000
    volumes:
      - shared-vol:/shared-volume
      - /path/to/models:/models:ro
    command: ["--model-name", "DeepSeek-R1-Distill-Qwen-1.5B", "--model-path", "/models/DeepSeek-R1-Distill-Qwen-1.5B"]
    depends_on:
      - engine

  engine:
    image: vllm/vllm-openai:latest
    runtime: nvidia                    # NVIDIA GPU
    # deploy:                          # 或使用 deploy 限定 GPU
    #   resources:
    #     reservations:
    #       devices:
    #         - capabilities: [gpu]
    command: >
      /bin/sh -c "while [ ! -f /shared-volume/start_command.sh ]; do sleep 2; done;
      cd /shared-volume && bash start_command.sh"
    volumes:
      - shared-vol:/shared-volume
      - /path/to/models:/models:ro
    ports:
      - "17000:17000"                 # 引擎端口（可选暴露）

volumes:
  shared-vol:
```

启动：

```bash
# 启动全部服务
docker compose up -d

# 查看日志
docker compose logs -f wings-infer
docker compose logs -f engine

# 健康检查
curl http://localhost:19000/health

# 推理请求
curl http://localhost:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "DeepSeek-R1-Distill-Qwen-1.5B",
    "messages": [{"role": "user", "content": "你好"}],
    "max_tokens": 100
  }'

# 停止
docker compose down
```

**Ascend NPU 适配**（替换 engine 容器配置）:

```yaml
  engine:
    image: quay.io/ascend/vllm-ascend:v0.7.3
    devices:
      - /dev/davinci0:/dev/davinci0
      - /dev/davinci_manager:/dev/davinci_manager
      - /dev/hisi_hdc:/dev/hisi_hdc
    volumes:
      - /usr/local/Ascend/driver:/usr/local/Ascend/driver:ro
      - shared-vol:/shared-volume
      - /path/to/models:/models:ro
    environment:
      - ASCEND_VISIBLE_DEVICES=0
```

### 5.5 场景三：K8s 单机部署 (Kustomize)

> 适用于：vLLM / SGLang / MindIE 单机

```bash
# 1. 构建并推送镜像
docker build -t registry.example.com/wings-infer:latest .
docker push registry.example.com/wings-infer:latest

# 2. 选择 overlay
ls k8s/overlays/
# vllm-single/  vllm-ascend-single/  sglang-single/  mindie-single/

# 3. 修改配置（替换 CUSTOMIZE 标记的字段）
vim k8s/overlays/vllm-single/deployment.yaml
```

需要修改的关键配置：

```yaml
# deployment.yaml 中标记为 CUSTOMIZE 的字段:
- image: registry.example.com/wings-infer:latest   # Sidecar 镜像
- image: vllm/vllm-openai:latest                   # 引擎镜像
- MODEL_NAME: "your-model-name"                     # 模型名称
- MODEL_PATH: "/models/your-model-name"             # 模型路径
- hostPath.path: "/mnt/models"                      # 宿主机模型目录
```

```bash
# 4. 预览 + 部署
kubectl kustomize k8s/overlays/vllm-single/
kubectl apply -k k8s/overlays/vllm-single/

# 5. 观察启动
kubectl -n wings-infer get pods -w

# 6. 验证
kubectl -n wings-infer port-forward deploy/infer 18000:18000 19000:19000

curl http://localhost:19000/health
curl http://localhost:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"your-model","messages":[{"role":"user","content":"hello"}],"max_tokens":50}'
```

### 5.6 场景四：K8s 分布式部署 (Kustomize)

> 适用于：多卡/多机分布式推理

```bash
# 选择分布式 overlay
ls k8s/overlays/
# vllm-distributed/  vllm-ascend-distributed/  sglang-distributed/  mindie-distributed/

# 修改 StatefulSet 配置
vim k8s/overlays/vllm-distributed/statefulset.yaml
```

```yaml
# statefulset.yaml 中需要修改的关键字段:
env:
  - name: HEAD_NODE_ADDR
    value: "192.168.1.100"            # ← rank-0 节点 IP
  - name: NODE_IPS
    value: "192.168.1.100,192.168.1.101"  # ← 所有节点 IP
  - name: NNODES
    value: "2"                        # ← 节点总数
  - name: DISTRIBUTED
    value: "true"
  - name: MODEL_NAME
    value: "DeepSeek-R1"
  - name: MODEL_PATH
    value: "/models/DeepSeek-R1"
```

```bash
# 部署
kubectl apply -k k8s/overlays/vllm-distributed/

# 观察所有 Pod 启动
kubectl -n wings-infer get pods -w
# 预期: infer-0 (rank-0), infer-1 (rank-1), ...

# 仅 rank-0 暴露代理服务
kubectl -n wings-infer port-forward pod/infer-0 18000:18000 19000:19000
```

---

## 六、路线 B：wings-infer 镜像已存在，直接启动服务

### 6.1 方式一：通过 wings_start.sh 启动（推荐，与原始 wings 完全兼容）

> 这是与 wings 项目 `wings_start.sh` 100% 兼容的入口。编排层可直接替换。

#### Docker 运行

```bash
# 基本用法 — 与 wings 完全相同的 CLI
docker run --rm -it \
  --runtime nvidia \
  -p 18000:18000 -p 19000:19000 \
  -v /path/to/models:/models:ro \
  wings-infer:latest \
  --model-name DeepSeek-R1 \
  --model-path /models/DeepSeek-R1 \
  --engine vllm

# 全参数示例
docker run --rm -it \
  --runtime nvidia \
  -p 18000:18000 -p 19000:19000 \
  -v /path/to/models:/models:ro \
  wings-infer:latest \
  --model-name DeepSeek-R1 \
  --model-path /models/DeepSeek-R1 \
  --engine vllm \
  --dtype auto \
  --gpu-memory-utilization 0.9 \
  --max-num-seqs 32 \
  --input-length 4096 \
  --output-length 1024 \
  --trust-remote-code \
  --enable-prefix-caching

# 分布式模式
docker run --rm -it \
  --runtime nvidia \
  --network host \
  -v /path/to/models:/models:ro \
  -e HEAD_NODE_ADDR=192.168.1.100 \
  -e NODE_IPS=192.168.1.100,192.168.1.101 \
  -e NNODES=2 \
  -e NODE_RANK=0 \
  wings-infer:latest \
  --model-name DeepSeek-R1 \
  --model-path /models/DeepSeek-R1 \
  --engine vllm \
  --distributed

# 禁用代理（引擎直接监听 18000）
docker run --rm -it \
  --runtime nvidia \
  -p 18000:18000 \
  -v /path/to/models:/models:ro \
  -e ENABLE_REASON_PROXY=false \
  wings-infer:latest \
  --model-name test-model --model-path /models/test-model
```

#### 直接在容器内执行

```bash
# 进入已运行的容器
docker exec -it <container_id> bash

# 使用 wings_start.sh
bash /app/wings_start.sh --model-name test-model --model-path /weights

# 使用环境变量（与 wings_start.sh 等效）
export MODEL_NAME=test-model
export MODEL_PATH=/weights
export ENGINE=vllm
bash /app/wings_start.sh --model-name $MODEL_NAME --model-path $MODEL_PATH
```

#### K8s Pod 中使用（与 wings 完全相同）

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: infer
spec:
  template:
    spec:
      containers:
        - name: wings-infer
          image: wings-infer:latest
          # args 直接传给 wings_start.sh（ENTRYPOINT）
          args:
            - "--model-name"
            - "DeepSeek-R1"
            - "--model-path"
            - "/models/DeepSeek-R1"
            - "--engine"
            - "vllm"
            - "--trust-remote-code"
            - "--gpu-memory-utilization"
            - "0.9"
```

### 6.2 方式二：通过 python -m app.main 启动

> 跳过 wings_start.sh，直接调用 Python 入口。适用于开发调试或自定义入口场景。

#### Docker 运行（覆盖 ENTRYPOINT）

```bash
docker run --rm -it \
  --entrypoint python \
  -p 18000:18000 -p 19000:19000 \
  -e WINGS_SKIP_PID_CHECK=true \
  wings-infer:latest \
  -m app.main \
  --model-name test-model \
  --model-path /weights
```

#### 本地开发运行（不使用 Docker）

```bash
# 1. 安装依赖
cd infer-control-sidecar-unified/backend
pip install -r requirements.txt

# 2. 设置环境变量
export PYTHONPATH=$(pwd)
export WINGS_SKIP_PID_CHECK=true
export SHARED_VOLUME_PATH=/tmp/shared-volume
mkdir -p $SHARED_VOLUME_PATH

# 3. 运行
python -m app.main \
  --model-name test-model \
  --model-path /weights \
  --engine vllm

# 4. 查看生成的启动脚本
cat /tmp/shared-volume/start_command.sh

# 5. 检查健康状态
curl http://localhost:19000/health

# 6. 代理将转发到 127.0.0.1:17000（引擎端口）
# 如果没有真实引擎运行，代理会返回 502
curl http://localhost:18000/v1/models
```

#### 容器内直接运行

```bash
docker exec -it <container_id> bash

# 确保 PYTHONPATH 正确
export PYTHONPATH=/app
python -m app.main --model-name test-model --model-path /weights
```

### 6.3 方式三：K8s 部署（镜像已推送到仓库）

```bash
# 镜像已存在于仓库中，直接部署

# 方式 A: 使用 Kustomize overlay
vim k8s/overlays/vllm-single/deployment.yaml
# 修改 image: your-registry/wings-infer:your-tag
kubectl apply -k k8s/overlays/vllm-single/

# 方式 B: 快速创建 Pod（临时测试）
kubectl run infer-test \
  --image=your-registry/wings-infer:latest \
  --port=18000 \
  --env="MODEL_NAME=test-model" \
  --env="MODEL_PATH=/weights" \
  --env="WINGS_SKIP_PID_CHECK=true" \
  -- --model-name test-model --model-path /weights

# 方式 C: 从现有的 wings deployment 直接替换镜像
kubectl set image deployment/infer \
  wings-infer=your-registry/wings-infer:latest \
  -n your-namespace
```

---

## 七、CLI 参数完整参考

以下 30 个参数与 wings/wings_start.sh 完全一致：

| 参数 | 类型 | 默认值 | 环境变量 | 说明 |
|------|------|--------|----------|------|
| `--host` | string | `""` | `HOST` | 监听地址 |
| `--port` | int | `18000` | `PORT` | 监听端口（代理模式下为后端端口 17000） |
| `--model-name` | string | **必填** | `MODEL_NAME` | 模型名称 |
| `--model-path` | string | `/weights` | `MODEL_PATH` | 模型文件路径 |
| `--engine` | string | `vllm` | `ENGINE` | 引擎类型: vllm/vllm_ascend/sglang/mindie |
| `--input-length` | int | `4096` | `INPUT_LENGTH` | 最大输入长度 |
| `--output-length` | int | `1024` | `OUTPUT_LENGTH` | 最大输出长度 |
| `--config-file` | string | `""` | `CONFIG_FILE` | 自定义配置文件路径 |
| `--gpu-usage-mode` | string | `full` | `GPU_USAGE_MODE` | GPU 使用模式 |
| `--device-count` | int | `1` | `DEVICE_COUNT` | 设备数量 |
| `--model-type` | string | `auto` | `MODEL_TYPE` | 模型类型: auto/llm/embedding/rerank/mmum/mmgm |
| `--save-path` | string | `/opt/wings/outputs` | `SAVE_PATH` | 输出目录 |
| `--trust-remote-code` | bool | `true` | `TRUST_REMOTE_CODE` | 信任远程代码 |
| `--dtype` | string | `auto` | `DTYPE` | 数据类型 |
| `--kv-cache-dtype` | string | `auto` | `KV_CACHE_DTYPE` | KV 缓存数据类型 |
| `--quantization` | string | `""` | `QUANTIZATION` | 量化方法 |
| `--quantization-param-path` | string | `""` | `QUANTIZATION_PARAM_PATH` | 量化参数路径 |
| `--gpu-memory-utilization` | float | `0.9` | `GPU_MEMORY_UTILIZATION` | GPU 显存利用率 |
| `--enable-chunked-prefill` | bool | `false` | `ENABLE_CHUNKED_PREFILL` | 启用分块预填充 |
| `--block-size` | int | `16` | `BLOCK_SIZE` | KV cache 块大小 |
| `--max-num-seqs` | int | `32` | `MAX_NUM_SEQS` | 最大并发序列数 |
| `--seed` | int | `0` | `SEED` | 随机种子 |
| `--enable-expert-parallel` | bool | `false` | `ENABLE_EXPERT_PARALLEL` | 启用专家并行 (MoE) |
| `--max-num-batched-tokens` | int | `4096` | `MAX_NUM_BATCHED_TOKENS` | 预填充最大 batch tokens |
| `--enable-prefix-caching` | bool | `false` | `ENABLE_PREFIX_CACHING` | 启用前缀缓存 |
| `--enable-speculative-decode` | bool | `false` | `ENABLE_SPECULATIVE_DECODE` | 启用推测解码 |
| `--speculative-decode-model-path` | string | `""` | `SPECULATIVE_DECODE_MODEL_PATH` | 推测解码辅助模型路径 |
| `--enable-rag-acc` | bool | `false` | `ENABLE_RAG_ACC` | 启用 RAG 加速 |
| `--enable-auto-tool-choice` | bool | `false` | `ENABLE_AUTO_TOOL_CHOICE` | 启用函数调用 |
| `--distributed` | bool | `false` | `DISTRIBUTED` | 启用分布式模式 |

**优先级**: CLI 参数 > 环境变量 > 代码默认值

---

## 八、环境变量参考

### 核心配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ENGINE` | `vllm` | 引擎类型 |
| `MODEL_NAME` | — | 模型名称（必填） |
| `MODEL_PATH` | `/weights` | 模型路径 |
| `ENGINE_PORT` | `17000` | 引擎真实监听端口 |
| `PORT` | `18000` | 代理端口 |
| `HEALTH_PORT` | `19000` | 健康检查端口 |
| `ENABLE_REASON_PROXY` | `true` | 是否启用代理（生产环境建议 true） |
| `BACKEND_URL` | `http://127.0.0.1:17000` | 代理后端 URL |

### Sidecar 专用

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SHARED_VOLUME_PATH` | `/shared-volume` | 共享卷路径 |
| `START_COMMAND_FILENAME` | `start_command.sh` | 启动脚本文件名 |
| `WINGS_SKIP_PID_CHECK` | `false` | 跳过引擎 PID 检查（sidecar 必须为 true） |
| `PYTHON_BIN` | `python` | Python 解释器路径 |
| `PROCESS_POLL_SEC` | `1.0` | 子进程守护轮询间隔 |

### 分布式配置

| 变量 | 说明 |
|------|------|
| `DISTRIBUTED` | 是否分布式 (`true`/`false`) |
| `NNODES` | 节点总数 |
| `NODE_RANK` | 当前节点序号 |
| `HEAD_NODE_ADDR` | Head 节点 IP |
| `NODE_IPS` | 所有节点 IP（逗号分隔） |
| `MASTER_ADDR` | Master 地址（部分引擎使用） |
| `MASTER_PORT` | Master 端口 |
| `DISTRIBUTED_EXECUTOR_BACKEND` | 分布式后端 (`ray`/`mp`) |

### 硬件配置

| 变量 | 说明 |
|------|------|
| `WINGS_DEVICE` | 硬件类型: `nvidia`/`ascend` |
| `WINGS_DEVICE_COUNT` | 设备数量 |
| `WINGS_DEVICE_NAME` | 设备名称 |
| `ASCEND_VISIBLE_DEVICES` | Ascend NPU 设备号 |
| `NVIDIA_VISIBLE_DEVICES` | NVIDIA GPU 设备号 |

### 代理调优（可选，已对齐 wings 默认值）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HTTPX_MAX_CONNECTIONS` | `2048` | 连接池最大连接数 |
| `HTTPX_MAX_KEEPALIVE` | `256` | 最大 keepalive 连接数 |
| `HTTPX_KEEPALIVE_EXPIRY` | `30` | keepalive 超时（秒） |
| `HTTP2_ENABLED` | `true` | 是否启用 HTTP/2 |
| `RETRY_TRIES` | `3` | 重试次数（含首次） |
| `RETRY_INTERVAL_MS` | `100` | 重试间隔（毫秒） |
| `QUEUE_TIMEOUT` | `15.0` | 队列等待超时（秒） |

---

## 九、端口规划

```
                外部请求
                    │
                    ▼
            ┌──────────────┐
            │  proxy:18000 │ ← 对外 API（OpenAI 兼容）
            └──────┬───────┘
                   │ 转发
                   ▼
            ┌──────────────┐
            │ engine:17000 │ ← 内部推理端口（不对外暴露）
            └──────────────┘

            ┌──────────────┐
            │ health:19000 │ ← K8s 探针（readiness/liveness）
            └──────────────┘
```

| 端口 | 用途 | 暴露方式 |
|------|------|----------|
| `17000` | 推理引擎 API | 仅 Pod 内部 |
| `18000` | Wings 代理端口 | NodePort / LoadBalancer |
| `19000` | 健康检查端口 | K8s 探针 |

当 `ENABLE_REASON_PROXY=false` 时，不启动代理，引擎直接监听 `--port` 指定的端口。

---

## 十、健康检查

### 端点

```bash
# 健康状态
curl http://<host>:19000/health

# 详细状态（JSON）
curl http://<host>:19000/health/detail
```

### 状态码

| HTTP 码 | 阶段 | 含义 | K8s 行为 |
|---------|------|------|----------|
| **200** | `ready` | 引擎就绪 | readinessProbe 通过 |
| **201** | `starting` | 启动中 | readinessProbe 失败（不接受流量） |
| **502** | `start_failed` | 启动超时 | livenessProbe 失败 → 重启 |
| **503** | `degraded` | 曾就绪后降级 | livenessProbe 失败 → 重启 |

### K8s 探针配置建议

```yaml
readinessProbe:
  httpGet:
    path: /health
    port: 19000
  initialDelaySeconds: 60      # 引擎启动可能需要 1-5 分钟
  periodSeconds: 10
  failureThreshold: 36         # 允许 6 分钟启动窗口
livenessProbe:
  httpGet:
    path: /health
    port: 19000
  initialDelaySeconds: 120
  periodSeconds: 30
  failureThreshold: 5
```

---

## 十一、各引擎部署场景详解

### 11.1 vLLM (NVIDIA GPU)

```bash
# 单机
docker run --runtime nvidia \
  -p 18000:18000 -p 19000:19000 \
  -v /models:/models:ro \
  wings-infer:latest \
  --model-name Qwen2-7B --model-path /models/Qwen2-7B --engine vllm

# K8s
kubectl apply -k k8s/overlays/vllm-single/
```

详细文档：[doc/deploy-vllm.md](doc/deploy-vllm.md)

### 11.2 vLLM-Ascend (Ascend NPU)

```bash
docker run \
  --device /dev/davinci0 --device /dev/davinci_manager --device /dev/hisi_hdc \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver:ro \
  -p 18000:18000 -p 19000:19000 \
  -v /models:/models:ro \
  -e WINGS_DEVICE=ascend \
  -e ASCEND_VISIBLE_DEVICES=0 \
  wings-infer:latest \
  --model-name Qwen2-7B --model-path /models/Qwen2-7B --engine vllm_ascend

# K8s
kubectl apply -k k8s/overlays/vllm-ascend-single/
```

详细文档：[doc/deploy-vllm-ascend.md](doc/deploy-vllm-ascend.md)

### 11.3 SGLang (NVIDIA GPU)

```bash
docker run --runtime nvidia \
  -p 18000:18000 -p 19000:19000 \
  -v /models:/models:ro \
  wings-infer:latest \
  --model-name Qwen2-7B --model-path /models/Qwen2-7B --engine sglang

# K8s
kubectl apply -k k8s/overlays/sglang-single/
```

详细文档：[doc/deploy-sglang.md](doc/deploy-sglang.md)

### 11.4 MindIE (Ascend NPU)

```bash
docker run \
  --device /dev/davinci0 --device /dev/davinci_manager --device /dev/hisi_hdc \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver:ro \
  -p 18000:18000 -p 19000:19000 \
  -v /models:/models:ro \
  -e WINGS_DEVICE=ascend \
  wings-infer:latest \
  --model-name Qwen2-7B --model-path /models/Qwen2-7B --engine mindie

# K8s
kubectl apply -k k8s/overlays/mindie-single/
```

详细文档：[doc/deploy-mindie.md](doc/deploy-mindie.md)

### 11.5 分布式模式（通用）

所有分布式场景的核心配置：

```bash
# 必需环境变量
export DISTRIBUTED=true
export NNODES=2                              # 节点总数
export HEAD_NODE_ADDR=192.168.1.100          # rank-0 IP
export NODE_IPS=192.168.1.100,192.168.1.101  # 所有节点 IP

# rank-0 节点
docker run --network host \
  -e NODE_RANK=0 \
  -e DISTRIBUTED=true \
  -e HEAD_NODE_ADDR=192.168.1.100 \
  -e NODE_IPS=192.168.1.100,192.168.1.101 \
  -e NNODES=2 \
  wings-infer:latest \
  --model-name DeepSeek-R1 --model-path /models/DeepSeek-R1 --distributed

# rank-1 节点
docker run --network host \
  -e NODE_RANK=1 \
  -e DISTRIBUTED=true \
  -e HEAD_NODE_ADDR=192.168.1.100 \
  -e NODE_IPS=192.168.1.100,192.168.1.101 \
  -e NNODES=2 \
  wings-infer:latest \
  --model-name DeepSeek-R1 --model-path /models/DeepSeek-R1 --distributed
```

K8s 分布式 overlay 请参考：

| 引擎 | K8s Overlay | 详细文档 |
|------|------------|----------|
| vLLM (Ray) | `kubectl apply -k k8s/overlays/vllm-distributed/` | [doc/deploy-vllm.md](doc/deploy-vllm.md) |
| vLLM-Ascend (Ray) | `kubectl apply -k k8s/overlays/vllm-ascend-distributed/` | [doc/deploy-vllm-ascend-dist-ray.md](doc/deploy-vllm-ascend-dist-ray.md) |
| SGLang (nnodes) | `kubectl apply -k k8s/overlays/sglang-distributed/` | [doc/deploy-sglang.md](doc/deploy-sglang.md) |
| MindIE (HCCL) | `kubectl apply -k k8s/overlays/mindie-distributed/` | [doc/deploy-mindie.md](doc/deploy-mindie.md) |

---

## 十二、故障排查

### 常见问题

| 症状 | 可能原因 | 解决方案 |
|------|----------|----------|
| 健康检查持续 201 | 引擎启动慢 | 增大 `readinessProbe.failureThreshold` |
| 健康检查 502 | 引擎启动失败 | 检查 engine 容器日志 |
| 代理返回 502 | 引擎未启动或端口不对 | 确认 `BACKEND_URL` 和 `ENGINE_PORT` |
| start_command.sh 未生成 | sidecar 启动失败 | 检查 wings-infer 容器日志 |
| 分布式 Pod 卡住 | HEAD_NODE_ADDR 不对 | 确认 rank-0 节点 IP 可达 |
| `model_name is required` | 未传模型名 | 添加 `--model-name` 或 `MODEL_NAME` 环境变量 |
| GPU 未被识别 | 驱动未挂载 | 确认 `--runtime nvidia` 或设备挂载正确 |

### 日志位置

| 日志 | 路径 |
|------|------|
| 启动脚本日志 | `/var/log/wings/wings_start.log` |
| Launcher 日志 | 标准输出（`docker logs` / `kubectl logs`） |
| 代理日志 | 标准输出（与 launcher 混合） |
| 引擎启动脚本 | `/shared-volume/start_command.sh` |
| 引擎日志 | engine 容器的标准输出 |

### 调试命令

```bash
# 查看生成的引擎启动脚本
kubectl exec -it deploy/infer -c wings-infer -- cat /shared-volume/start_command.sh

# 查看 sidecar 内部状态
kubectl exec -it deploy/infer -c wings-infer -- curl localhost:19000/health/detail

# 直接测试引擎端口
kubectl exec -it deploy/infer -c wings-infer -- curl localhost:17000/v1/models

# 查看环境变量
kubectl exec -it deploy/infer -c wings-infer -- env | sort

# 进入 engine 容器调试
kubectl exec -it deploy/infer -c engine -- bash
```

详细故障排查：参见 [doc/troubleshooting.md](doc/troubleshooting.md)

---

## 十三、从 wings 迁移

### 13.1 核心原则

- **CLI 接口 100% 兼容**：30 个参数名称、默认值完全一致
- **环境变量 100% 兼容**：所有 A 使用的环境变量在 B 中均有对应
- **端口兼容**：代理端口默认 18000，与 A 一致

### 13.2 迁移步骤

1. **替换镜像**：将 wings 镜像替换为 `wings-infer:latest`
2. **无需修改参数**：原有的 `--model-name`、`--engine` 等参数原样保留
3. **新增 engine 容器**：B 使用双容器架构，需添加引擎容器 + 共享卷
4. **设置 `WINGS_SKIP_PID_CHECK=true`**：sidecar 模式下必须跳过 PID 检查

### 13.3 K8s 迁移示例

```yaml
# 原来 (wings)
spec:
  containers:
    - name: wings
      image: wings:latest
      args: ["--model-name", "DeepSeek-R1", "--engine", "vllm"]

# 迁移后 (unified)
spec:
  volumes:
    - name: shared-volume
      emptyDir: {}
  containers:
    - name: wings-infer              # ← 改名（可选）
      image: wings-infer:latest      # ← 替换镜像
      args: ["--model-name", "DeepSeek-R1", "--engine", "vllm"]  # ← 参数不变
      env:
        - name: WINGS_SKIP_PID_CHECK # ← 新增
          value: "true"
      volumeMounts:
        - name: shared-volume
          mountPath: /shared-volume
    - name: engine                   # ← 新增引擎容器
      image: vllm/vllm-openai:latest
      command: ["/bin/sh", "-c"]
      args:
        - |
          while [ ! -f /shared-volume/start_command.sh ]; do sleep 2; done
          cd /shared-volume && bash start_command.sh
      volumeMounts:
        - name: shared-volume
          mountPath: /shared-volume
```

### 13.4 已知差异

| 维度 | wings (A) | unified (B) | 说明 |
|------|-----------|-------------|------|
| 容器数 | 1 | 2 (sidecar + engine) | 需添加 engine 容器 |
| 进程管理 | PID 文件 | K8s 容器生命周期 | B 更可靠 |
| 分布式 | 自建 master-worker | K8s StatefulSet | B 使用 K8s 原生 |
| 硬件探测 | SDK 实时探测 | 环境变量驱动 | B 需设置 `WINGS_DEVICE_COUNT` |

---

## 十四、文档索引

| 文档 | 说明 |
|------|------|
| [快速上手](doc/QUICKSTART.md) | 6 步完成从构建到推理验证 |
| [架构详解](doc/architecture.md) | 模块职责、端口规划、状态机、数据流 |
| [故障排查](doc/troubleshooting.md) | CrashLoop、201/502/503、Ray/HCCL、Triton 等 9 类问题 |
| [vLLM 部署](doc/deploy-vllm.md) | NVIDIA GPU 单机 + Ray 分布式 |
| [vLLM-Ascend 部署](doc/deploy-vllm-ascend.md) | Ascend NPU 单机 + Ray 分布式 (含 Triton 补丁说明) |
| [SGLang 部署](doc/deploy-sglang.md) | 单机 + nnodes 分布式 |
| [MindIE 部署](doc/deploy-mindie.md) | Ascend NPU 单机 + HCCL 分布式 (含 rank table) |
| [安全审计](doc/security-audit-fix-report.md) | 安全审计修复报告 |
| [代码清理](doc/code-cleanup-log.md) | 代码清理记录 |

## License

MIT
