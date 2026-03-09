# Analyse ↔ Main 同步比较报告

> **Analyse 项目**: `infer-control-sidecar-unified - analyse-wings-k8s`  
> **Main 项目**: `infer-control-sidecar-main/infer-control-sidecar-main`  
> **排除范围**: `.git`, `node_modules`, `__pycache__`, `.verify.yaml`, `wings-accel/`  
> **生成时间**: 2025-06

---

## 目录

1. [文件结构差异总览](#1-文件结构差异总览)
2. [仅存在于 Main 的文件](#2-仅存在于-main-的文件)
3. [仅存在于 Analyse 的文件](#3-仅存在于-analyse-的文件)
4. [内容完全一致的文件](#4-内容完全一致的文件)
5. [仅文档差异的文件（功能代码相同）](#5-仅文档差异的文件功能代码相同)
6. [存在功能差异的文件（需关注）](#6-存在功能差异的文件需关注)
7. [K8s YAML 差异](#7-k8s-yaml-差异)
8. [差异分类与同步建议](#8-差异分类与同步建议)

---

## 1. 文件结构差异总览

| 维度 | Main | Analyse |
|------|------|---------|
| 引擎支持 | 仅 vllm（代码中硬编码） | vllm + vllm_ascend + sglang + mindie |
| 分布式支持 | 禁用（raise ValueError） | 完整支持（Ray/DP/PD） |
| 架构阶段 | MVP + 遗留模块共存 | 重构后统一架构 |
| K8s 部署 | 扁平 YAML 文件 | Kustomize base + overlays |
| 启动入口 | `python -m app.main` | `bash wings_start.sh` |
| 文档风格 | `AUTOGEN_FILE_COMMENT` 英文头 | 中文 docstring + 模块概述 |

---

## 2. 仅存在于 Main 的文件

### 2.1 遗留业务模块（Analyse 已通过 core/ 架构替代，无需同步）

| 文件 | 说明 |
|------|------|
| `backend/app/api/__init__.py` | 空包 |
| `backend/app/api/routes.py` | 遗留 REST 路由（/health, /v1/completions 等），导入 services 层 |
| `backend/app/services/__init__.py` | 空包 |
| `backend/app/services/command_builder.py` | CommandBuilder 类（build_vllm_command/build_sglang_command） |
| `backend/app/services/engine_manager.py` | EngineManager 类（start_engine/wait_for_engine_ready） |
| `backend/app/services/proxy_service.py` | ProxyService 类（forward_completion/forward_chat） |
| `backend/app/utils/http_client.py` | HTTPClient 类（遗留 proxy_service 使用） |
| `backend/app/utils/wings_file_utils.py` | safe_write_file, check_permission_640 等工具 |

### 2.2 开发/调试/文档文件（无需同步）

| 文件/目录 | 说明 |
|-----------|------|
| `.env.example`, `.gitignore` | 开发配置 |
| `backend-*/` (4个目录) | 历史备份目录 |
| `backend.zip`, `current-ip.txt` | 临时文件 |
| `docker-compose*.yml` (2个) | Docker Compose 部署 |
| `Dockerfile.sidecar-*` (2个) | 历史 Dockerfile |
| `debug-accel.sh`, `deploy*.sh`, `run.sh`, `test_api.sh`, `verify_e2e.sh` | 脚本 |
| `import-accel-image.sh` | accel 镜像导入脚本 |
| `visualizations/` | 架构图文档 |
| `*.md` (ARCHITECTURE, LOGIC_DIAGRAM, SEQUENCE_DIAGRAM 等) | 开发文档 |
| `*.puml`, `*.d2`, `*.dot` | 架构图源 |
| `backend/FILE_COMMENT_INDEX.md` | 文件注释索引 |
| K8s 各 `*.verify.yaml`, `deployment.yaml.bak` | 测试/备份 |
| `k8s/deployment-sglang.yaml`, `k8s/deployment-sglang.verify.yaml`, `k8s/service-sglang.verify.yaml` | SGLang 扁平 YAML |

---

## 3. 仅存在于 Analyse 的文件

### 3.1 多引擎 / 分布式支持文件

| 文件 | 说明 |
|------|------|
| `backend/app/engines/sglang_adapter.py` | SGLang 引擎适配器（~200行） |
| `backend/app/engines/mindie_adapter.py` | MindIE 引擎适配器（~625行） |
| `backend/app/config/sglang_default.json` | SGLang 默认参数 |
| `backend/app/config/mindie_default.json` | MindIE 默认参数（华为昇腾） |
| `backend/app/config/distributed_config.json` | 分布式端口/策略配置 |

### 3.2 启动脚本

| 文件 | 说明 |
|------|------|
| `wings_start.sh` (根目录, 374行) | CLI 兼容启动包装脚本，处理 QAT 设备文件传输、参数解析 |

### 3.3 文档 / 测试文档 / K8s overlays

| 文件/目录 | 说明 |
|-----------|------|
| `doc/` (12个 md 文件) | 架构设计、部署指南、故障排除 |
| `test_doc/` | 分布式验证报告 |
| `k8s/base/` | Kustomize 基础层（namespace.yaml, kustomization.yaml） |
| `k8s/overlays/` (8个目录) | 各引擎 × 单机/分布式 overlay |

---

## 4. 内容完全一致的文件

| 文件 | 备注 |
|------|------|
| `backend/app/config/engine_parameter_mapping.json` | ✅ 一致 |
| `backend/app/config/vllm_default.json` | ✅ 一致 |
| `LICENSE` | ✅ 一致 |

---

## 5. 仅文档差异的文件（功能代码相同）

以下文件仅有注释头部风格（AUTOGEN vs 中文 docstring）和内联 docstring 的差异，**逻辑代码完全相同**：

| 文件 |
|------|
| `backend/app/utils/device_utils.py` |
| `backend/app/utils/file_utils.py` |
| `backend/app/utils/model_utils.py` |
| `backend/app/utils/noise_filter.py` |
| `backend/app/core/hardware_detect.py` |
| `backend/app/core/port_plan.py` |
| `backend/app/proxy/queueing.py` |
| `backend/app/proxy/tags.py` |
| `backend/app/proxy/speaker_logging.py` |

---

## 6. 存在功能差异的文件（需关注）

### 6.1 `backend/app/main.py`

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| `_start()` 异常处理 | 无 try/except | try/except OSError | **Analyse 更健壮** |
| `_stop()` SIGKILL 回退 | 无 | 有 try/except SIGKILL | **Analyse 更健壮** |
| `_build_child_env()` backend_host | 硬编码 `"127.0.0.1"` | MindIE 分布式时用 NODE_IPS/NODE_RANK | **多引擎需要** |
| `run()` rank>0 过滤 | 无 | 过滤掉 proxy（只启引擎） | **分布式需要** |

### 6.2 `backend/app/config/settings.py`

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| `HEALTH_CHECK_INTERVAL` | `5`（硬编码） | `int(os.getenv(..., "5"))` | **Analyse 更灵活** |
| `SERVICE_CLUSTER_IP` | `"10.255.128.184"` | `""` | **Main 有硬编码环境值** |
| `NODE_IP` | `"90.90.161.168"` | `""` | **Main 有硬编码环境值** |

### 6.3 `backend/requirements.txt`

| 差异点 | Main | Analyse |
|--------|------|---------|
| `ray` | `# ray` (注释掉) | `ray[default]>=2.9.0` (激活) |

**评估**: Main 不支持分布式所以注释掉了 ray。若同步分布式功能需激活。

### 6.4 `backend/app/core/config_loader.py` (1251 行 vs 1564 行)

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| 分布式模式 | `raise ValueError("Distributed mode is disabled")` | 完整支持 | **架构差异** |
| PID 文件路径 | 硬编码 `/var/log/wings/wings.txt` | `os.getenv("BACKEND_PID_FILE", ...)` | **Analyse 更灵活** |
| `DEFAULT_CONFIG_FILES` | 仅 vllm | 含 sglang, mindie | **多引擎需要** |
| `_load_engine_fallback_defaults()` | 无 | 有（引擎级默认值回退） | **Analyse 新增** |
| `_merge_mindie_params()` 分布式 | 无 | 处理 worldSize, multiNodesInferEnabled, node_ips, npuDeviceIds | **多引擎需要** |
| `_handle_mindie_distributed()` | 无 | 有（MindIE 分布式配置） | **多引擎需要** |
| `_get_pd_config()` KV 端口 | 硬编码 `"20001"` | `os.getenv("PD_KV_PORT", "20001")` | **Analyse 更灵活** |

### 6.5 `backend/app/core/engine_manager.py`

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| 引擎别名映射 | `if engine_name == 'vllm_ascend': engine_name = 'vllm'` 硬编码 | `ENGINE_ADAPTER_ALIASES` dict | **Analyse 更易扩展** |
| 启动方式优先级 | 仅 `build_start_command` | 优先 `build_start_script()` → 回退 `build_start_command()` | **架构差异** |
| 命令包装 | 无 | `build_start_command` 结果包装为 `exec ...\n` | **Analyse 行为更正确** |

### 6.6 `backend/app/core/start_args_compat.py`

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| `GPU_USAGE_MODE` 默认值 | `"default"` | `"full"` | **行为差异** |
| `MODEL_TYPE` 默认值 | `""` | `"auto"` | **行为差异** |
| 引擎验证 | 拒绝非 vllm 引擎 + 拒绝分布式 | `SUPPORTED_ENGINES` 集合验证 | **多引擎需要** |
| LaunchArgs 字段 | 31 个字段 | 35 个字段（+nnodes, node_rank, head_node_addr, distributed_executor_backend） | **分布式需要** |
| build_parser 参数 | 无分布式参数 | 含 `--nnodes`, `--node-rank`, `--head-node-addr`, `--distributed-executor-backend` | **分布式需要** |

### 6.7 `backend/app/core/wings_entry.py`

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| 引擎选择 | 硬编码 `merged["engine"] = "vllm"` | engine-agnostic | **架构差异** |
| 启动调用 | 直接 `vllm_adapter.build_start_command()` | `start_engine_service(merged)` 分发 | **架构差异** |
| 分布式信息注入 | 无 | 注入 nnodes, node_rank, head_node_addr 等 | **分布式需要** |
| rank>0 端口移除 | 无 | 移除非 head 节点的端口配置 | **分布式需要** |
| `exec` 封装 | 手动添加 `"exec "` 前缀 | adapter 的 `build_start_script` 处理 | **架构差异** |

### 6.8 `backend/app/engines/vllm_adapter.py`

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| `subprocess` 函数 | 有 `_start_vllm_single`, `_start_vllm_api_server`（未使用） | 无（已清理） | **Analyse 更干净** |
| 分布式 env | `_build_distributed_env_commands` raises ValueError | 完整 Ray/DP/PD 支持（~200行） | **分布式需要** |
| `build_start_script()` | 无 | 有（完整 shell 脚本生成，含 Ray/Triton 补丁/CANN 环境） | **分布式需要** |
| 安全函数 | 无 | `_sanitize_shell_path()` 路径清洗 | **Analyse 更安全** |
| 配置路径 | 硬编码 `{root}/wings/config/` | 检查本地 config 目录 + fallback | **Analyse 更灵活** |
| 库路径 | 硬编码 | 通过 env（`KV_AGENT_LIB_PATH`, `LMCACHE_LIB_PATH`, `NPU_MAX_SPLIT_SIZE_MB`） | **Analyse 更灵活** |

### 6.9 `backend/app/utils/env_utils.py`

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| 未使用 import | `from csv import Error` | 无未使用 import | **Analyse 更干净** |
| 端口解析 ValueError | 无 try/except | 有 try/except ValueError + logger.warning | **Analyse 更健壮** |

### 6.10 `backend/app/utils/process_utils.py`

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| `_LOG_DIR` | `os.path.join(root_dir, "wings", 'logs')` | `os.path.join(root_dir, 'logs')` | **路径差异** |

### 6.11 `Dockerfile`

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| 构建代理 | 有 `http_proxy`/`https_proxy` 硬编码 | 无 | **Main 有硬编码环境值** |
| 额外依赖 | 无 | 安装 `netcat-openbsd` | **Analyse 需要 nc 探测** |
| 端口暴露 | EXPOSE 9000 | EXPOSE 17000 18000 19000 | **端口策略不同** |
| 入口 | CMD `["python", "-m", "app.main"]` | ENTRYPOINT `["bash", "/app/wings_start.sh"]` | **启动方式不同** |
| 日志目录 | 无 | 创建 `/var/log/wings` | **Analyse 新增** |
| 目录验证 | 无 | 验证 engines/proxy/utils 存在 | **Analyse 更安全** |
| APP_WORKDIR | 无 | 设置 `APP_WORKDIR="/app"` | **Analyse 新增** |

### 6.12 `backend/app/proxy/settings.py`

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| BACKEND_URL 默认 | `"http://172.17.0.3:17000 "` (含尾空格) | `"http://127.0.0.1:17000"` | **Main 有硬编码环境值** |
| PORT 默认 | `6688` | `18000` | **端口不一致** |
| WORKERS | `WORKERS = 1`（硬编码） | `int(os.getenv("PROXY_WORKERS", "1"))` | **Analyse 可配置** |
| logging guard | 无（直接 `logging.basicConfig`） | `if not logging.root.handlers` guard | **Analyse 更安全** |
| 超时常量 | 无（散落在各处硬编码） | 集中定义: `HTTPX_CONNECT_TIMEOUT=20`, `HTTPX_WRITE_TIMEOUT=20`, `HTTPX_POOL_TIMEOUT=30`, `STREAM_BACKEND_CONNECT_TIMEOUT=20`, `METRICS_CONNECT_TIMEOUT=10`, `STATUS_CONNECT_TIMEOUT=10`, `STATUS_READ_TIMEOUT=30` | **Analyse 更规范** |
| log_boot_plan | 无超时信息 | 输出 CONNECT/POOL 超时信息 | **Analyse 更完整** |

### 6.13 `backend/app/proxy/http_client.py`

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| 超时配置 | 硬编码 `connect=10.0`, `write=10.0`，`pool=None` | `C.HTTPX_CONNECT_TIMEOUT`, `C.HTTPX_WRITE_TIMEOUT`, `C.HTTPX_POOL_TIMEOUT` | **Analyse 可配置** |

### 6.14 `backend/app/proxy/gateway.py` (813 行 vs 1097 行)

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| `_send_stream_request` 超时 | 硬编码 `connect=10` | `C.STREAM_BACKEND_CONNECT_TIMEOUT` 等 | **Analyse 可配置** |
| `/metrics` 超时 | 硬编码 `connect=5.0` | `C.METRICS_CONNECT_TIMEOUT` 等 | **Analyse 可配置** |
| `hv_text2video_status` 超时 | 硬编码 `connect=5.0, read=15.0` | `C.STATUS_CONNECT_TIMEOUT`, `C.STATUS_READ_TIMEOUT` | **Analyse 可配置** |
| JSON 解析错误日志 | 简单 try/except | 有 `elog("chat_json_parse_error", ...)` + rid | **Analyse 更可观测** |
| `_forward_stream` 防御代码 | `released_early` 标志 + finally 释放 | 无（已早释放，无需 finally） | **Main 更防御** |
| `version_proxy` 错误处理 | try/except + 错误 JSON | 直接返回（不会异常） | **风格差异** |

### 6.15 `backend/app/proxy/health.py` (687 行 vs 629 行)

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| MindIE 健康端口 | 硬编码 `"127.0.0.2"`, `1026` | `os.getenv("MINDIE_HEALTH_HOST", "127.0.0.2")`, `os.getenv("MINDIE_HEALTH_PORT", "1026")` | **Analyse 可配置** |
| `_read_pid_from_file` 异常日志 | 无 | `C.logger.debug("pid_file_read_error: %s", e)` | **Analyse 更可观测** |
| `_is_mindie/_is_sglang` 异常日志 | 无 | 有 debug 级别日志 | **Analyse 更可观测** |

### 6.16 `backend/app/proxy/simple_proxy.py` (621 行 vs 793 行)

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| startup 端口日志 | 硬编码 `port=18000` | `port=C.PORT` | **Analyse 可配置** |
| MAX_REQUEST_BYTES | 无 | 有（防 DoS） | **Analyse 更安全** |
| httpx connect 超时 | 硬编码 `10.0` | `os.getenv("HTTPX_CONNECT_TIMEOUT", "10")` | **Analyse 可配置** |
| 请求体读取 | 直接 `req.body()` 无校验 | 使用 `read_json_body` 含大小+格式校验 | **Analyse 更安全** |
| 额外导入 | 无 `read_json_body` | 导入 `read_json_body` | **Analyse 更安全** |

### 6.17 `backend/app/proxy/health_service.py`

| 差异点 | Main | Analyse | 分类 |
|--------|------|---------|------|
| 未使用 import | `import time`, `from typing import Optional, Tuple` | 无未使用 import | **Analyse 更干净** |
| 导入方式 | 分开导入 `build_health_headers` | 统一 import 块 | **风格差异** |

---

## 7. K8s YAML 差异

### Main: 扁平文件结构

```
k8s/
├─ deployment.yaml          # vLLM 单机 Deployment
├─ service.yaml             # Service (LoadBalancer, 仅 18000)
├─ deployment-sglang.yaml   # SGLang 专用
├─ deployment.yaml.bak      # 备份
└─ *.verify.yaml (5个)      # 验证用
```

### Analyse: Kustomize 结构

```
k8s/
├─ base/                          # 基础层
│   ├─ kustomization.yaml
│   └─ namespace.yaml (wings-infer)
└─ overlays/                      # 8 个 overlay
    ├─ vllm-single/               # vLLM 单机 (NVIDIA)
    ├─ vllm-distributed/          # vLLM 分布式 (NVIDIA)
    ├─ vllm-ascend-single/        # vLLM 昇腾单机
    ├─ vllm-ascend-distributed/   # vLLM 昇腾分布式
    ├─ sglang-single/             # SGLang 单机
    ├─ sglang-distributed/        # SGLang 分布式
    ├─ mindie-single/             # MindIE 单机
    └─ mindie-distributed/        # MindIE 分布式
```

### 部署 YAML 内容差异（以 vllm-single 为例）

| 差异点 | Main | Analyse |
|--------|------|---------|
| metadata.name | `wings-infer` | `infer` |
| selector label | `app: wings-infer` | `app.kubernetes.io/name: infer-vllm` |
| strategy | `Recreate` | 未指定（默认 RollingUpdate） |
| 端口名称 | `proxy`, `health` with named ports | 无名称 |
| 环境变量 | `ENGINE_TYPE`, `SERVICE_CLUSTER_IP`, `NODE_PORT`, `NODE_IP`, `TP_SIZE`, `MAX_MODEL_LEN`, `WINGS_PORT` | `ENGINE`, `BACKEND_URL`, `ENABLE_ACCEL` |
| 模型挂载 readOnly | `false` | `true` |
| 引擎容器名 | `vllm-engine` | `engine` |
| 引擎容器脚本 | 含端口探测循环 + 后台运行 + wait | 简化（直接 bash 执行） |
| GPU 资源 | `nvidia.com/gpu: "1"` | 未指定（预期用户自定义） |
| readinessProbe 延迟 | `initialDelaySeconds: 30`, `failureThreshold: 30` | `initialDelaySeconds: 60`, `failureThreshold: 36` |
| Service 类型 | `LoadBalancer` | 无 type（默认 ClusterIP） |
| Service 端口 | 仅 18000 | 18000 + 19000 |

---

## 8. 差异分类与同步建议

### 🟢 无需同步（设计分歧 / 遗留代码）

| 项目 | 原因 |
|------|------|
| Main 的 `api/`, `services/` 目录 | 遗留架构，已被 Analyse 的 core/ 替代 |
| Main 的 `utils/http_client.py`, `utils/wings_file_utils.py` | 遗留模块，Analyse 无需 |
| Main 的开发脚本 / docker-compose / 备份目录 | 开发环境专用 |
| Main 的 `.verify.yaml` 文件 | 测试专用 |
| Main 的 AUTOGEN 注释 vs Analyse 的中文 docstring | 文档风格差异 |
| `process_utils.py` `_LOG_DIR` 路径差异 | 部署环境差异 |

### 🟡 建议从 Analyse 同步到 Main（质量改进）

| 文件 | 差异项 | 理由 |
|------|--------|------|
| `config/settings.py` | HEALTH_CHECK_INTERVAL 从 env 读取 | 可配置性 |
| `config/settings.py` | 清空默认 IP 值 | 去除硬编码 |
| `utils/env_utils.py` | 端口解析 ValueError 保护 | 健壮性 |
| `utils/env_utils.py` | 移除 `from csv import Error` unused import | 代码整洁 |
| `proxy/settings.py` | BACKEND_URL 默认值改为 `127.0.0.1:17000`、PORT 改为 `18000` | 去除硬编码 |
| `proxy/settings.py` | 集中定义超时常量 | 可维护性 |
| `proxy/settings.py` | `logging.basicConfig` guard | 防重复初始化 |
| `proxy/settings.py` | WORKERS 从 env 读取 | 可配置性 |
| `proxy/http_client.py` | 使用配置常量替代硬编码超时 | 可维护性 |
| `proxy/gateway.py` | 使用配置常量替代硬编码超时 | 可维护性 |
| `proxy/gateway.py` | JSON 解析错误 elog + rid | 可观测性 |
| `proxy/health.py` | MindIE 健康端口从 env 读取 | 可配置性 |
| `proxy/health.py` | PID/engine 类型检测异常日志 | 可观测性 |
| `proxy/simple_proxy.py` | MAX_REQUEST_BYTES + read_json_body | 安全性 |
| `proxy/health_service.py` | 清理未使用 import | 代码整洁 |
| `main.py` | `_start()` OSError 处理 | 健壮性 |
| `main.py` | `_stop()` SIGKILL 回退 | 健壮性 |
| `core/engine_manager.py` | `ENGINE_ADAPTER_ALIASES` dict | 可扩展性 |
| `core/engine_manager.py` | 优先 `build_start_script()` | 架构一致性 |
| `core/config_loader.py` | PID 文件路径从 env 读取 | 可配置性 |
| `core/config_loader.py` | PD_KV_PORT 从 env 读取 | 可配置性 |
| `engines/vllm_adapter.py` | `_sanitize_shell_path()` | 安全性 |
| `engines/vllm_adapter.py` | 清理未使用的 subprocess 函数 | 代码整洁 |
| `engines/vllm_adapter.py` | lib 路径从 env 读取 | 可配置性 |
| `Dockerfile` | 移除构建代理、添加 netcat、修正端口 | 部署规范化 |

### 🔴 多引擎 / 分布式功能（需要整体迁移而非逐项同步）

以下差异是由"多引擎 + 分布式"设计产生的系统性差异，不建议逐项 cherry-pick，应作为完整功能集同步：

| 模块 | 涉及文件 | 能力 |
|------|----------|------|
| SGLang 引擎 | `engines/sglang_adapter.py`, `config/sglang_default.json` | SGLang 推理 |
| MindIE 引擎 | `engines/mindie_adapter.py`, `config/mindie_default.json` | 华为昇腾推理 |
| 分布式支持 | `config/distributed_config.json`, `config_loader.py` 多处, `wings_entry.py`, `start_args_compat.py`, `vllm_adapter.py` build_start_script | Ray/DP/PD |
| K8s 部署矩阵 | `k8s/base/`, `k8s/overlays/` (8个) | Kustomize 化 |
| 启动脚本 | `wings_start.sh` | CLI 兼容封装 |
| requirements.txt | `ray[default]>=2.9.0` | 分布式依赖 |

---

> **总结**: Analyse 项目相对于 Main 项目在三个维度上有显著提升：  
> 1. **可配置性** — 将散落各处的硬编码值统一改为 env 可配置  
> 2. **健壮性** — 增加异常捕获、debug 日志、输入校验  
> 3. **多引擎/分布式** — 从 vllm-only MVP 扩展到 4 引擎 × 分布式的完整架构  
>
> 建议优先同步 🟡 类（质量改进），然后视业务需求决定是否整体集成 🔴 类（多引擎/分布式）。
