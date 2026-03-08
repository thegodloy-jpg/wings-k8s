# Wings 推理服务项目分析报告

> 分析日期：2026-03-08

## 一、项目概述

**项目路径**：`wings/wings/`

这是 **Wings 大模型推理服务的原始版本**（相对于 `infer-control-sidecar-unified` 为重构后的 Sidecar 版本）。它是一个完整的推理服务启动、管理、分布式调度和性能测试框架。

- **总文件数**：186 个
- **Python 代码行数**：约 13,299 行
- **技术栈**：Python + FastAPI + subprocess + Ray + Transformers + torch

---

## 二、核心架构

### 运行模式

与 `infer-control-sidecar-unified` 的关键区别：**Wings 直接启动引擎进程**，而非通过共享卷传递脚本。

```
┌─ wings.py (主入口) ──────────────────────────────────────┐
│                                                            │
│  1. 解析 CLI 参数 (argparse)                               │
│  2. 检测硬件 (NVIDIA/Ascend/CPU)                           │
│  3. 加载合并配置 (config_loader.py, 1163 行)               │
│  4. 动态加载引擎适配器 (engine_manager.py)                  │
│  5. 通过 subprocess.Popen 直接启动引擎进程                  │
│  6. wings_proxy.py 启动反向代理                             │
│                                                            │
│  分布式模式:                                                │
│    master.py → MonitorService + TaskScheduler               │
│    worker.py → 注册到 master, 接收启动命令                   │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### 端口规划

| 端口 | 用途 |
|------|------|
| `17000` | 推理引擎 API |
| `6688` (默认) | Proxy 代理端口 |
| `16000` (分布式) | Master 节点端口 |
| 各 Worker 端口 | Worker 节点注册端口 |

---

## 三、支持矩阵

| 引擎 | 单机 | 分布式 | 硬件 | 适配器文件 |
|------|------|--------|------|-----------|
| **vLLM** | ✅ | ✅ (Ray) | NVIDIA GPU | `vllm_adapter.py` (631 行) |
| **vLLM-Ascend** | ✅ | ✅ (Ray) | 昇腾 910B NPU | 复用 `vllm_adapter.py` |
| **SGLang** | ✅ | ✅ (nnodes) | NVIDIA GPU | `sglang_adapter.py` |
| **MindIE** | ✅ | ✅ (HCCL) | 昇腾 910B NPU | `mindie_adapter.py` |
| **Wings (HunyuanVideo)** | ✅ | ✅ (torch.dist) | NVIDIA/Ascend | `wings_adapter.py` (493 行) |
| **Wings (Transformers LLM)** | ✅ | ❌ | NVIDIA/Ascend | `wings_adapter.py` |

---

## 四、代码结构详解

```
wings/wings/
├── wings.py                  # 主入口 (314 行) — 参数解析→配置合并→启动引擎
├── wings_proxy.py            # Proxy 启动入口（uvicorn 启动 gateway）
├── wings_start.sh            # Shell 主入口 (615 行) — 完整的参数解析+启动流程
├── wings_stop.py             # 停止服务（读取 PID 文件 SIGTERM）
├── run.sh                    # Docker 运行示例脚本
├── __init__.py
│
├── config/                   # 配置层 (14 个文件)
│   ├── nvidia_default.json            # NVIDIA GPU 默认参数
│   ├── ascend_default.json            # Ascend NPU 默认参数
│   ├── vllm_default.json              # vLLM 引擎默认参数
│   ├── sglang_default.json            # SGLang 引擎默认参数
│   ├── mindie_default.json            # MindIE 引擎默认参数
│   ├── engine_parameter_mapping.json  # 引擎参数映射表
│   ├── distributed_config.json        # 分布式配置
│   ├── set_vllm_env.sh                # vLLM 环境脚本
│   ├── set_vllm_ascend_env.sh         # vLLM-Ascend 环境脚本
│   ├── set_sglang_env.sh              # SGLang 环境脚本
│   ├── set_mindie_single_env.sh       # MindIE 单机环境脚本
│   ├── set_mindie_multi_env.sh        # MindIE 分布式环境脚本
│   ├── set_wings_nvidia_env.sh        # Wings NVIDIA 环境脚本
│   └── set_wings_ascend_env.sh        # Wings Ascend 环境脚本
│
├── core/                     # 核心控制链路
│   ├── engine_manager.py              # 适配器调度（importlib 动态加载）
│   ├── config_loader.py               # 多层配置合并 (1163 行，最大模块)
│   ├── hardware_detect.py             # 硬件探测
│   └── __init__.py
│
├── engines/                  # 引擎适配器 (直接 subprocess 启动)
│   ├── vllm_adapter.py                # vLLM + vLLM-Ascend (631 行)
│   ├── sglang_adapter.py              # SGLang
│   ├── mindie_adapter.py              # MindIE
│   ├── wings_adapter.py               # Wings (HunyuanVideo + Transformers LLM, 493 行)
│   └── __init__.py
│
├── distributed/              # 分布式调度框架
│   ├── master.py                      # Master 节点 (FastAPI, 246 行)
│   ├── worker.py                      # Worker 节点 (FastAPI, 173 行)
│   ├── monitor.py                     # 节点健康监控 (心跳检测)
│   ├── scheduler.py                   # 任务调度器 (最少负载/轮询/随机)
│   └── __init__.py
│
├── proxy/                    # 代理层 (与 unified 共享大量代码)
│   ├── gateway.py                     # OpenAI 兼容反向代理 (750 行)
│   ├── health.py                      # 健康状态机
│   ├── health_service.py              # 独立健康服务
│   ├── queueing.py                    # 双闸门 FIFO 排队
│   ├── settings.py                    # 代理配置
│   ├── http_client.py                 # httpx 客户端
│   ├── simple_proxy.py                # 简单代理
│   ├── tags.py                        # 请求标签处理
│   ├── speaker_logging.py             # 日志配置
│   ├── gateway copy.py                # 旧版备份
│   ├── tags copy.py                   # 旧版备份
│   └── __init__.py
│
├── servers/                  # 自有推理服务
│   ├── transformers_server.py         # Transformers LLM 推理服务 (1347 行)
│   └── model/
│       └── hunyuanvideo_server/       # HunyuanVideo 多模态视频生成服务
│           ├── app.py                 # FastAPI 应用 (1027 行)
│           ├── core.py                # 核心逻辑
│           ├── distributed.py         # 分布式支持 (torch.distributed)
│           ├── state.py               # 全局状态
│           └── ...
│
├── utils/                    # 工具模块
│   ├── device_utils.py                # 设备检测 (NVIDIA/Ascend)
│   ├── env_utils.py                   # 环境变量读取
│   ├── file_utils.py                  # 文件操作
│   ├── model_utils.py                 # 模型类型识别
│   ├── noise_filter.py                # 日志噪声过滤
│   ├── process_utils.py               # 进程管理 (PID/等待/日志)
│   └── __init__.py
│
├── benchmark/                # 性能测试工具
│   ├── run_benchmark.py               # 单场景性能测试
│   ├── run_batch_test.py              # 批量测试
│   ├── performance_base.py            # 测试基类
│   ├── performance_llm.py             # LLM 特化测试
│   ├── performance_mmum.py            # 多模态测试
│   ├── data_generator.py              # 测试数据生成
│   ├── llm_batch_perf_test_config.json    # LLM 批量测试配置
│   ├── mmum_batch_perf_test_config.json   # 多模态批量测试配置
│   └── README.md                      # 测试工具使用指南
│
├── test/                     # 测试用例
│   ├── core/
│   │   └── test_config_loader.py
│   └── function_call.py               # 函数调用测试
│
├── logs/                     # 日志输出目录
├── output/                   # 输出目录
└── outputs/                  # 输出目录
```

---

## 五、关键模块对比（Wings vs infer-control-sidecar-unified）

| 维度 | Wings (原始版) | Sidecar Unified (重构版) |
|------|---------------|------------------------|
| **代码规模** | ~13,299 行 / 186 文件 | ~7,594 行 / 81 文件 |
| **引擎启动方式** | `subprocess.Popen` 直接启动 | 生成脚本 → 共享卷 → engine 容器执行 |
| **进程管理** | PID 文件 + SIGTERM/SIGKILL | K8s 原生容器生命周期 |
| **分布式框架** | 自建 Master/Worker/Monitor/Scheduler | 依赖 K8s StatefulSet + Ray/HCCL |
| **配置管理** | argparse + JSON 文件 + 环境变量 | pydantic-settings + CLI + JSON + 环境变量 |
| **部署方式** | Shell 脚本 (wings_start.sh) | Dockerfile + Kustomize |
| **额外引擎** | Wings (HunyuanVideo + Transformers) | 无 (仅 vLLM/SGLang/MindIE) |
| **性能测试** | 内置 benchmark 模块 | 无 |
| **遗留文件** | 有 (`*copy.py`, `*_old.py`) | 已清理 |

---

## 六、核心数据流

### 单机模式
```
wings_start.sh / wings.py
    ↓
argparse → 参数解析
    ↓
detect_hardware() → 硬件探测
    ↓
load_and_merge_configs() → 多层配置合并 (1163 行)
    ↓
start_engine_service() → engine_manager → importlib → *_adapter.py
    ↓
subprocess.Popen() → 直接启动引擎进程 (PID 记录到文件)
    ↓
wings_proxy.py → uvicorn → proxy.gateway:app → 反向代理到 engine
```

### 分布式模式
```
[Master 节点]                        [Worker 节点]
wings_start.sh                       wings_start.sh
    ↓                                    ↓
master.py (FastAPI :16000)           worker.py (FastAPI)
    ↓                                    ↓
MonitorService (心跳检测)            register_node → Master
TaskScheduler (调度策略)             等待 /api/start_engine 命令
    ↓                                    ↓
检查所有节点注册完成                  start_engine_service()
    ↓
通过 API 广播启动命令 → 各 Worker
```

---

## 七、分布式框架详解

Wings 自建了一套分布式管理框架：

### master.py
- FastAPI 应用，监听 `16000` 端口
- 接口：
  - `POST /api/nodes/register` — Worker 注册
  - `GET /api/nodes` — 获取所有节点
  - `POST /api/start_engine` — 启动引擎
- 内嵌 `MonitorService` 和 `TaskScheduler`

### worker.py
- FastAPI 应用，启动后自注册到 Master
- 接口：`POST /api/start_engine` — 接收并执行启动命令
- 心跳上报给 Master

### monitor.py
- 后台线程定期检查节点心跳
- 超过 60 次心跳丢失则移除节点

### scheduler.py
- 三种调度策略：最少负载(默认)、轮询、随机
- 带重试机制（最多 3 次）

---

## 八、自有推理服务

Wings 除了封装第三方引擎，还自建了两个推理服务：

### transformers_server.py (1347 行)
- 基于 HuggingFace Transformers 的 LLM 推理服务
- OpenAI 兼容接口 (`/v1/chat/completions`, `/v1/completions`)
- 支持 SSE 流式输出
- 支持 CUDA 和 NPU
- 单机单卡/多卡（`device_map="auto"`)

### hunyuanvideo_server (app.py 1027 行)
- 腾讯混元视频生成模型服务
- 支持 GPU 和 Ascend NPU
- 支持 torch.distributed 多卡并行
- 任务队列 + UUID 状态查询

---

## 九、性能测试工具 (benchmark/)

- 基于 asyncio + aiohttp 异步架构
- 支持 LLM 和多模态模型测试
- 关键指标：TPS（每秒 Token 数）、TTFT（首 Token 延迟）
- 支持批量多场景自动化测试
- CSV + JSON 输出报告

---

## 十、关键设计特点

1. **Shell 入口**：`wings_start.sh` (615 行) 是主启动脚本，包含完整的参数解析、环境设置、分布式节点管理
2. **直接进程管理**：通过 `subprocess.Popen` 启动引擎，PID 写入文件，`wings_stop.py` 通过 SIGTERM 停止
3. **自建分布式**：Master-Worker 架构，心跳监控，负载调度
4. **引擎解耦**：与 Unified 版相同的 importlib 动态加载模式
5. **多层配置合并**：`config_loader.py` (1163 行) 是最复杂的模块，处理硬件默认、引擎默认、用户参数的多层合并
6. **代理层共享**：`proxy/` 目录下的代码与 Unified 版高度相似，是代码迁移的来源
7. **补充场景**：Wings 引擎（HunyuanVideo/Transformers）是 Unified 版没有的
