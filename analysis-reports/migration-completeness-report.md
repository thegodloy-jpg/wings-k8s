# Wings → infer-control-sidecar-unified 迁移完整度分析报告

> 分析日期：2026-03-08

## 总体评估

| 指标 | 结果 |
|------|------|
| **核心业务逻辑迁移率** | **≥ 98%** |
| **设计排除组件** | 4 个（distributed / servers / benchmark / test） |
| **待清理项** | 2 个冗余文件 |
| **待建设项** | 测试体系 |

---

## 一、迁移完整度矩阵

### 1.1 core/ 核心控制链路

| 文件 | Wings (A) | Unified (B) | 状态 | 关键变更 |
|------|-----------|-------------|------|---------|
| engine_manager.py | 85 行 | 100 行 | ✅ **完全迁移** | 返回值从 `bool` → 脚本字符串；新增别名映射表 |
| config_loader.py | 1163 行 | 1564 行 | ✅ **完全迁移 + 增强** | 所有 45+ 函数保留；新增 MindIE 分布式、H20 环境变量化、防御性校验 |
| hardware_detect.py | 38 行 | 120 行 | ✅ **架构重写** | 从 torch/pynvml SDK → 纯环境变量驱动（适配 Sidecar 无 GPU SDK） |
| — | — | wings_entry.py (新) | ✅ **新增** | 控制链路中枢桥接层（CLI → LauncherPlan → shell 脚本） |
| — | — | port_plan.py (新) | ✅ **新增** | 三层端口规划（backend/proxy/health） |
| — | — | start_args_compat.py (新) | ✅ **新增** | CLI 参数兼容层，37 字段的 LaunchArgs dataclass |

**评估**：100% 迁移完成，核心配置合并链路无遗漏。

---

### 1.2 engines/ 引擎适配器

| 适配器 | Wings (A) | Unified (B) | 状态 | 关键变更 |
|--------|-----------|-------------|------|---------|
| vllm_adapter.py | 631 行 | 694 行 | ✅ **完全迁移** | `start_engine()` → `build_start_script()`；Ray 等待逻辑改为 bash 内循环；新增 `_sanitize_shell_path()` |
| sglang_adapter.py | 136 行 | 235 行 | ✅ **完全迁移** | 同上模式；新增 `shlex.quote` 安全处理 |
| mindie_adapter.py | 254 行 | 625 行 | ✅ **完全迁移 + 大幅扩展** | 配置合并重构为内联 Python；新增 HCCL rank table 生成；行数翻倍 |
| wings_adapter.py | 493 行 | 不存在 | ❌ **设计排除** | HunyuanVideo/Transformers 自有引擎，非 Sidecar 统一管理范围 |

**架构模式变更**：所有适配器统一从 `start_engine()` (直接 subprocess) → `build_start_script()` (脚本生成)，旧接口保留但抛出 `RuntimeError`。

---

### 1.3 proxy/ 代理层

| 文件 | Wings (A) | Unified (B) | 状态 | 关键变更 |
|------|-----------|-------------|------|---------|
| gateway.py | 750 行 | 849 行 | ✅ **完全迁移** | 新增 `/v1/models` 路由；timeout 参数化 |
| health.py | 504 行 | 516 行 | ✅ **完全迁移** | 新增 `WINGS_SKIP_PID_CHECK` 适配跨容器健康检查 |
| health_service.py | 91 行 | 67 行 | ✅ **完全迁移** | 简化实现 |
| queueing.py | 266 行 | 317 行 | ✅ **完全迁移** | 逻辑完全一致，增加文档 |
| settings.py | 97 行 | 128 行 | ✅ **完全迁移** | 默认值调优；新增细粒度 timeout 常量 |
| http_client.py | 35 行 | 67 行 | ✅ **完全迁移** | timeout 参数化 |
| simple_proxy.py | 503 行 | 510 行 | ✅ **完全迁移** | 新增请求体大小限制 |
| tags.py | 115 行 | 140 行 | ✅ **完全迁移** | 逻辑完全一致 |
| speaker_logging.py | 290 行 | 355 行 | ✅ **完全迁移** | 逻辑完全一致 |

**评估**：9/9 文件 100% 迁移，核心代理逻辑零遗漏。

---

### 1.4 utils/ 工具模块

| 文件 | Wings (A) | Unified (B) | 状态 | 关键变更 |
|------|-----------|-------------|------|---------|
| device_utils.py | 274 行 | 334 行 | ✅ **完全迁移** | 函数签名+逻辑完全一致，仅增强文档 |
| env_utils.py | 277 行 | 307 行 | ✅ **完全迁移** | 同上 |
| file_utils.py | 173 行 | 195 行 | ✅ **完全迁移** | 同上 |
| model_utils.py | 135 行 | 181 行 | ✅ **完全迁移** | 同上 |
| noise_filter.py | 251 行 | 287 行 | ✅ **完全迁移** | 同上 |
| process_utils.py | 148 行 | 169 行 | ✅ **完全迁移** | 同上 |
| — | — | http_client.py (新) | ⚠️ **已废弃** | 标注无活跃调用者，待清理 |
| — | — | wings_file_utils.py (新) | ⚠️ **冗余重复** | file_utils.py 完全副本，标注"迁移期待收敛" |

**评估**：6/6 核心文件 100% 迁移，零逻辑变更。2 个新增文件为迁移遗留，待清理。

---

### 1.5 入口文件替代

| Wings 入口 | 行数 | Unified 替代 | 说明 |
|-----------|------|-------------|------|
| `wings_start.sh` | 615 行 | `main.py` + K8s 共享卷 | Shell 编排逻辑拆分：参数解析进 Python，启动脚本写共享卷 |
| `wings.py` | 314 行 | `main.py` + `wings_entry.py` + `start_args_compat.py` | 单体启动器拆解为三步流程 |
| `wings_proxy.py` | 16 行 | `main.py` 守护子进程 | uvicorn 由 launcher 以子进程方式启动 |
| `wings_stop.py` | 84 行 | K8s SIGTERM | 容器生命周期管理取代 PID 文件停服 |

---

## 二、设计排除分析

以下组件在迁移中被**有意排除**，这是架构设计决策而非遗漏：

### 2.1 distributed/ (master/worker/monitor/scheduler) — 共 517 行

**排除原因**：Sidecar 架构下分布式协调由 K8s 原生能力替代：
- `StatefulSet` + `podManagementPolicy: Parallel` 替代 Master-Worker 注册
- Pod index (`NODE_RANK`) 替代 Worker 自注册
- K8s liveness/readiness probe 替代 Monitor 心跳检测
- K8s Service 替代 Scheduler 任务调度

### 2.2 servers/ (transformers_server + hunyuanvideo_server) — 共 ~2,700 行

**排除原因**：Sidecar 采用引擎外置架构——推理引擎在独立 Engine 容器运行，Sidecar 只负责脚本生成、代理和健康检查。Wings 自有引擎（HunyuanVideo/Transformers）为特殊场景，不在统一管理范围。

### 2.3 benchmark/ — 共 ~1,935 行

**排除原因**：性能测试为独立工具，可作为 K8s Job 或独立镜像运行，不属于运行时组件。

### 2.4 test/ — 共 ~461 行

**排除原因**：A 的测试用例与旧架构强绑定（直接进程启动），需为新 Sidecar 架构重写。**这是待建设项**。

---

## 三、架构级变更总结

| 变更维度 | Wings (A) | Unified (B) |
|---------|-----------|-------------|
| **引擎启动** | `subprocess.Popen` 直接启动 | 生成 bash 脚本 → 共享卷 → Engine 容器执行 |
| **进程管理** | PID 文件 + SIGTERM | K8s 容器生命周期 |
| **分布式** | 自建 Master-Worker 框架 | K8s StatefulSet + hostNetwork |
| **硬件探测** | torch/pynvml SDK 调用 | 环境变量驱动（Sidecar 无 GPU SDK） |
| **配置管理** | argparse + JSON + env | pydantic-settings + LaunchArgs + JSON + env |
| **部署** | Shell 脚本 + Docker | Dockerfile + Kustomize (8 overlays) |
| **健康检查** | 外部 cURL 或 proxy 内嵌 | 独立 Health 服务 (:19000) + K8s probe |
| **安全** | 无防注入 | `_sanitize_shell_path()` + `shlex.quote` + 请求体限制 |
| **PID 检查** | 必选 | `WINGS_SKIP_PID_CHECK` 可选跳过（Sidecar 模式） |

---

## 四、发现的改进项

### 4.1 Bug 修复 (A→B)

| # | 位置 | 描述 |
|---|------|------|
| 1 | config_loader.py `_set_soft_fp8` | A 中 JSON dict 嵌套缩进错误，B 已修正 |
| 2 | health.py warmup URL | A 中有空格 bug (`127.0.0.1: {port}`)，B 已修正 |
| 3 | settings.py `BACKEND_URL` | A 默认值有尾部空格 (`"http://172.17.0.3:17000 "`)，B 已修正 |

### 4.2 待清理 (B)

| # | 文件 | 描述 |
|---|------|------|
| 1 | utils/http_client.py | 标注废弃，无活跃调用者 |
| 2 | utils/wings_file_utils.py | file_utils.py 完全重复，标注待收敛 |

### 4.3 待建设 (B)

| # | 描述 |
|---|------|
| 1 | 缺少测试体系（test/ 目录为空） |
| 2 | Wings 自有引擎（HunyuanVideo/Transformers）需独立部署方案 |

---

## 五、结论

**迁移完整度评估：≥ 98%**

所有核心业务逻辑（配置合并、引擎适配、代理转发、健康检查、工具模块）均已完整迁移至 Sidecar 架构。4 个排除组件均为架构设计决策（由 K8s 原生能力替代或作为独立工具存在）。迁移过程中修复了 3 个已知 Bug，新增了安全防护和大量文档增强。主要待办项为建立测试体系和清理 2 个冗余工具文件。
