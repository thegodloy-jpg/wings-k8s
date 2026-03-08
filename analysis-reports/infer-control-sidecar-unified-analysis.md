# Wings-Infer 统一推理控制 Sidecar 项目分析报告

> 分析日期：2026-03-08

## 一、项目概述

**项目路径**：`infer-control-sidecar-unified/`

这是一个 **K8s Sidecar 模式的 LLM 推理引擎控制平面**，用于统一管理多种推理引擎（vLLM、SGLang、MindIE）的启动、代理和健康检查。

- **总文件数**：81 个
- **Python 代码行数**：约 7594 行
- **技术栈**：Python 3.10 + FastAPI + Pydantic + httpx + Kustomize

---

## 二、核心架构

### 双容器 Sidecar 模式

Pod 内包含两个容器，通过共享卷 (`/shared-volume`) 通信：

```
┌─ Pod ─────────────────────────────────────────────────┐
│                                                        │
│  ┌─ wings-infer (sidecar) ──────────────────┐         │
│  │ 1. 解析 CLI/环境变量                      │         │
│  │ 2. 硬件探测 + 多层配置合并                 │         │
│  │ 3. 生成 start_command.sh → 共享卷          │         │
│  │ 4. 启动 Proxy (:18000) → 反向代理         │         │
│  │ 5. 启动 Health (:19000) → 健康检查         │         │
│  └────────────────────────────────────────────┘         │
│       │ /shared-volume/start_command.sh                 │
│  ┌─ engine (推理引擎) ──────────────────────┐         │
│  │ 轮询等待 start_command.sh 生成            │         │
│  │ 执行引擎启动脚本                         │         │
│  │ 监听 ENGINE_PORT (:17000)                │         │
│  └────────────────────────────────────────────┘         │
└────────────────────────────────────────────────────────┘
```

### 端口规划

| 端口 | 用途 |
|------|------|
| `17000` | 推理引擎 API（仅 Pod 内部） |
| `18000` | Wings 代理端口（对外暴露） |
| `19000` | Wings 健康检查端口（K8s 探针） |

---

## 三、支持矩阵

| 引擎 | 单机 | 分布式 | 硬件平台 | 适配器文件 |
|------|------|--------|----------|-----------|
| **vLLM** | ✅ | ✅ (Ray) | NVIDIA GPU | `vllm_adapter.py` (694 行) |
| **vLLM-Ascend** | ✅ | ✅ (Ray) | 昇腾 910B NPU | 复用 `vllm_adapter.py` |
| **SGLang** | ✅ | ✅ (nnodes) | NVIDIA GPU | `sglang_adapter.py` (235 行) |
| **MindIE** | ✅ | ✅ (HCCL) | 昇腾 910B NPU | `mindie_adapter.py` (625 行) |

---

## 四、代码结构详解

### 4.1 目录树

```
infer-control-sidecar-unified/
├── .env.example            # 环境变量模板
├── .gitignore
├── Dockerfile              # Sidecar 容器镜像构建
├── LICENSE
├── README.md               # 总体文档
├── backend/                # Python 后端代码
│   ├── requirements.txt    # 依赖：FastAPI, uvicorn, pydantic, httpx, ray
│   └── app/
│       ├── __init__.py
│       ├── main.py         # 主入口 (335 行) — 生命周期管理 + 子进程守护
│       ├── config/         # 配置层
│       │   ├── settings.py                    # pydantic-settings 全局配置单例
│       │   ├── engine_parameter_mapping.json   # 引擎参数映射表
│       │   ├── distributed_config.json         # 分布式配置
│       │   ├── vllm_default.json               # vLLM 默认参数
│       │   ├── sglang_default.json             # SGLang 默认参数
│       │   └── mindie_default.json             # MindIE 默认参数
│       ├── core/           # 核心控制链路
│       │   ├── wings_entry.py      # 中枢桥接层：CLI → LauncherPlan → shell
│       │   ├── engine_manager.py   # 适配器动态调度器 (importlib)
│       │   ├── config_loader.py    # 多层配置合并
│       │   ├── hardware_detect.py  # 硬件探测 (GPU/NPU)
│       │   ├── port_plan.py        # 三层端口分配
│       │   └── start_args_compat.py # CLI 参数解析兼容层
│       ├── engines/        # 引擎适配器 (策略模式)
│       │   ├── vllm_adapter.py     # vLLM + vLLM-Ascend
│       │   ├── sglang_adapter.py   # SGLang
│       │   └── mindie_adapter.py   # MindIE
│       ├── proxy/          # 代理 & 健康检查层
│       │   ├── gateway.py          # OpenAI 兼容反向代理 (849 行，最大文件)
│       │   ├── health_service.py   # 独立健康检查 FastAPI 应用
│       │   ├── health.py           # 健康状态机
│       │   ├── queueing.py         # 双闸门 FIFO 排队控制器
│       │   ├── simple_proxy.py     # 简单代理
│       │   ├── http_client.py      # httpx 异步客户端工厂
│       │   ├── settings.py         # 代理层独立配置
│       │   ├── tags.py             # 请求标签处理
│       │   └── speaker_logging.py  # 日志配置
│       └── utils/          # 工具模块
│           ├── device_utils.py     # 设备检测
│           ├── env_utils.py        # 环境变量工具
│           ├── file_utils.py       # 文件操作
│           ├── http_client.py      # HTTP 客户端
│           ├── model_utils.py      # 模型工具
│           ├── noise_filter.py     # 噪声过滤
│           ├── process_utils.py    # 进程工具
│           └── wings_file_utils.py # Wings 文件操作
├── doc/                    # 文档
│   ├── QUICKSTART.md       # 快速开始
│   ├── architecture.md     # 架构文档
│   ├── troubleshooting.md  # 故障排查
│   ├── deploy-vllm.md      # vLLM 部署指南
│   ├── deploy-vllm-ascend.md
│   ├── deploy-sglang.md
│   ├── deploy-mindie.md
│   ├── deploy-vllm-ascend-dist-ray.md
│   ├── code-cleanup-log.md
│   └── security-audit-fix-report.md
└── k8s/                    # Kubernetes 部署清单
    ├── base/
    │   ├── kustomization.yaml
    │   └── namespace.yaml (wings-infer)
    └── overlays/           # 8 种部署场景
        ├── vllm-single/            # Deployment
        ├── vllm-distributed/       # StatefulSet
        ├── vllm-ascend-single/
        ├── vllm-ascend-distributed/
        ├── sglang-single/
        ├── sglang-distributed/
        ├── mindie-single/
        └── mindie-distributed/
```

### 4.2 核心数据流

```
CLI/环境变量
    ↓
parse_launch_args() → LaunchArgs
    ↓
detect_hardware()          → 硬件信息 (设备类型/数量/型号)
load_and_merge_configs()   → 多层配置合并 (默认JSON + 硬件 + 环境变量 + CLI)
derive_port_plan()         → PortPlan (backend=17000, proxy=18000, health=19000)
    ↓
build_launcher_plan()      → LauncherPlan.command (完整 bash 脚本)
    ↓
safe_write_file()          → /shared-volume/start_command.sh
    ↓
Engine 容器执行脚本        → 引擎监听 :17000
    ↓
Proxy(:18000) 反向代理     → Engine(:17000)
Health(:19000) 轮询探测    → Engine(:17000)
```

### 4.3 关键模块说明

#### main.py — 生命周期管理器
- 解析参数 → 生成启动脚本 → 写入共享卷
- 启动 proxy 和 health 两个 FastAPI 子服务
- 守护进程模式：子进程异常退出时自动重启
- 优雅退出：捕获 SIGINT/SIGTERM 信号

#### engine_manager.py — 适配器调度器
- 通过 `importlib` 动态加载 `engines/<engine>_adapter.py`
- 支持别名映射：`vllm_ascend` → 复用 `vllm_adapter`
- 协商接口：优先 `build_start_script()`，回退 `build_start_command()`

#### gateway.py — OpenAI 兼容代理 (849 行)
- 暴露 `/v1/chat/completions` 等 OpenAI API 接口
- 流式/非流式响应采用不同传输策略
- 双闸门 FIFO 排队控制（QueueGate）
- 请求体大小限制（默认 2MB）、重试机制

#### health_service.py — 独立健康服务
- 独立于 gateway 运行在 19000 端口
- 后台轮询 engine 状态
- HTTP 200 = 就绪，201 = 启动中
- 支持 HEAD 请求（K8s 探针轻量化）

---

## 五、K8s 部署架构

### 5.1 Kustomize 分层

- **base/**：共享资源（`wings-infer` 命名空间）
- **overlays/**：8 种场景，每个包含独立的 Deployment/StatefulSet + Service

### 5.2 部署模式

| 场景 | K8s 资源类型 | 网络模式 |
|------|-------------|---------|
| 单机部署 | Deployment | ClusterIP |
| 分布式部署 | StatefulSet | hostNetwork |

### 5.3 分布式关键配置
- `podManagementPolicy: Parallel` — 所有副本同时启动
- `podAntiAffinity` — 确保分散到不同节点
- `NODE_RANK` 从 Pod index 自动获取
- `HEAD_NODE_ADDR` / `NODE_IPS` 需手动配置

---

## 六、关键设计亮点

1. **引擎解耦（策略模式）**：适配器模式 + importlib 动态加载，新增引擎只需添加 `*_adapter.py`，零修改调度层
2. **共享卷通信**：sidecar 只写脚本、不启动引擎进程，避免跨容器进程管理复杂性
3. **多层配置合并**：默认 JSON → 硬件探测 → 环境变量 → CLI 参数，优先级递增
4. **安全防护**：`_sanitize_shell_path()` 防注入、请求体大小限制、shlex.quote 参数转义
5. **双服务分离**：Proxy 和 Health 独立端口，高负载时健康探针仍可靠
6. **守护进程模式**：主进程监控 proxy/health 子进程，异常时自动重启
7. **Kustomize 场景化**：8 种 overlay 覆盖完整支持矩阵

---

## 七、依赖清单

| 包 | 版本 | 用途 |
|----|------|------|
| fastapi | 0.104.1 | Web 框架 (proxy + health) |
| uvicorn | 0.24.0 | ASGI 服务器 |
| pydantic | 2.5.0 | 数据验证 |
| pydantic-settings | 2.1.0 | 环境变量绑定 |
| httpx | 0.25.2 | 异步 HTTP 客户端 |
| orjson | latest | 高性能 JSON 序列化 |
| python-dotenv | 1.0.0 | .env 文件加载 |
| ray | ≥2.9.0 | 分布式支持 |
