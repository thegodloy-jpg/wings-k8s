# Wings-Infer — 统一大模型推理服务平台

基于 Kubernetes Sidecar 架构的统一大模型推理引擎管理平台，支持多种推理引擎在异构硬件上的自动化部署、代理转发和健康检查。

## 概览

Wings-Infer 通过 K8s Sidecar 双容器模式，实现对 vLLM、SGLang、MindIE 等主流推理引擎的统一管理，同时兼容 NVIDIA GPU 和华为昇腾 910B NPU 双硬件平台。

### 核心特性

- **多引擎支持**：vLLM、vLLM-Ascend、SGLang、MindIE、HuggingFace Transformers
- **异构硬件**：NVIDIA GPU（A100 / L20）、华为昇腾 910B NPU
- **K8s 云原生**：Sidecar 双容器模式 + Kustomize 8 种部署场景
- **OpenAI 兼容代理**：`/v1/chat/completions`、`/v1/models` 等标准接口
- **健康检查状态机**：200（就绪）→ 201（启动中）→ 502（失败）→ 503（降级）
- **分布式推理**：基于 Ray / torch.distributed / HCCL 的多卡 / 多节点推理

## 项目结构

```
wings-k8s/
├── wings/                              # 原始版 — 直接进程管理模式
│   └── wings/
│       ├── core/                       # 引擎管理器、配置加载器、硬件探测
│       ├── engines/                    # 引擎适配器（vLLM/SGLang/MindIE/Wings）
│       ├── distributed/               # 自建 Master-Worker 分布式框架
│       ├── proxy/                      # OpenAI 兼容反向代理 + 健康检查
│       ├── servers/                    # 自有推理服务（Transformers/HunyuanVideo）
│       ├── benchmark/                  # 性能测试工具（TPS/TTFT）
│       └── config/                     # 多层配置（硬件/引擎/环境变量）
│
├── infer-control-sidecar-unified/      # 当前主版本 — K8s Sidecar 架构
│   ├── backend/app/
│   │   ├── main.py                     # 生命周期管理器
│   │   ├── core/                       # 中枢桥接、引擎调度、配置合并、端口分配
│   │   ├── engines/                    # vLLM/SGLang/MindIE 适配器
│   │   └── proxy/                      # 双闸门 FIFO 排队代理（849 行）
│   ├── k8s/
│   │   ├── base/                       # Kustomize 基础层
│   │   └── overlays/                   # 8 种部署场景 overlay
│   └── doc/                            # 架构文档、故障排查指南
│
├── infer-control-sidecar-main/         # 早期 Demo — Sidecar 原型
│   ├── infer-control-sidecar-main/     # 基础版
│   ├── *-nv-dist/                      # NVIDIA 分布式变体
│   └── *-st-dist/                      # 昇腾分布式变体
│
├── docker+k8s/                         # 部署验证记录
│   ├── GPU 部署手册（A100/L20 + K3s）
│   ├── SGLang / vLLM 分布式验证报告
│   └── 昇腾 NPU 单测部署记录
│
├── doc - 副本/                         # 设计文档
│   └── wings-sidecar-migration/        # 迁移设计方案 v1~v6（11 份文档）
│
└── analysis-reports/                   # 迁移审计报告（9 份）
    ├── wings-analysis.md               # Wings 原始版全量分析
    ├── final-migration-audit.md        # 最终迁移审查（迁移率 98%+）
    └── *-diff-report.md                # 各模块逐行 Diff 报告
```

## 架构

### Sidecar 双容器模式（当前主版本）

```
┌─────────────────────────────── K8s Pod ───────────────────────────────┐
│                                                                       │
│  ┌─── wings-infer (sidecar) ───┐      ┌─── engine (推理引擎) ───┐    │
│  │                              │      │                          │    │
│  │  1. 生成 start_command.sh    │─────▶│  轮询共享卷              │    │
│  │  2. 写入共享卷               │      │  执行启动脚本            │    │
│  │                              │      │  监听 :17000             │    │
│  │  Proxy   (:18000) ──────────▶│──────│──▶ 引擎 API             │    │
│  │  Health  (:19000)            │      │                          │    │
│  └──────────────────────────────┘      └──────────────────────────┘    │
│                    │                                                   │
│              共享卷 (emptyDir)                                         │
└───────────────────────────────────────────────────────────────────────┘
```

### 架构演进

```
Wings 原始版 (subprocess 直接管理)
  ├── 自建分布式框架
  ├── 自有推理服务（HunyuanVideo / Transformers）
  │
  ↓  迁移重构（v1 → v6，11 份迭代文档）
  │
infer-control-sidecar-main (早期 Demo)
  │
  ↓  统一 + 增强
  │
infer-control-sidecar-unified (当前主版本)
  ├── K8s Sidecar 双容器模式
  ├── Kustomize 8 场景 overlay
  ├── 安全增强（shell 注入防护）
  └── 7 个 Bug 修复
```

## 技术栈

| 维度 | 技术 |
|------|------|
| 语言 | Python 3.10 |
| Web 框架 | FastAPI + uvicorn |
| 配置管理 | pydantic-settings + JSON + 环境变量多层合并 |
| 容器化 | Docker |
| 编排 | Kubernetes + Kustomize |
| 分布式 | Ray / torch.distributed / HCCL |
| 推理引擎 | vLLM · SGLang · MindIE · HuggingFace Transformers |
| 硬件平台 | NVIDIA GPU (A100/L20) · 华为昇腾 910B NPU |

## 部署场景

通过 Kustomize overlay 提供 8 种预设部署方案：

| 场景 | 引擎 | 硬件 | 模式 |
|------|------|------|------|
| vllm-nvidia | vLLM | NVIDIA GPU | 单机 |
| vllm-nvidia-dist | vLLM | NVIDIA GPU | 分布式 |
| vllm-ascend | vLLM-Ascend | 昇腾 NPU | 单机 |
| vllm-ascend-dist | vLLM-Ascend | 昇腾 NPU | 分布式 |
| sglang-nvidia | SGLang | NVIDIA GPU | 单机 |
| sglang-nvidia-dist | SGLang | NVIDIA GPU | 分布式 |
| mindie-ascend | MindIE | 昇腾 NPU | 单机 |
| mindie-ascend-dist | MindIE | 昇腾 NPU | 分布式 |

## 快速开始

### 前置要求

- Python 3.10+
- Kubernetes 集群（K3s / K8s）
- 至少一块 NVIDIA GPU 或华为昇腾 910B NPU
- kubectl + kustomize

### 部署示例（vLLM + NVIDIA）

```bash
# 应用 Kustomize overlay
kubectl apply -k infer-control-sidecar-unified/k8s/overlays/vllm-nvidia/

# 检查 Pod 状态
kubectl get pods -n wings-infer

# 验证健康检查
curl http://<pod-ip>:19000/health

# 发送推理请求
curl http://<pod-ip>:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "your-model-name",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## 许可证

Copyright © xFusion Digital Technologies Co., Ltd.
