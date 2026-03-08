# 剩余模块逐行 Diff 分析报告

**生成时间**: 2026-03-08  
**Project A (原始)**: `wings/wings/`  
**Project B (迁移)**: `infer-control-sidecar-unified/backend/app/`

---

## 目录

- [一、core/engine_manager.py](#一coreengine_managerpy)
- [二、core/hardware_detect.py](#二corehardware_detectpy)
- [三、proxy/gateway.py](#三proxygatewaypy)
- [四、proxy/health.py](#四proxyhealthpy)
- [五、proxy/health_service.py](#五proxyhealth_servicepy)
- [六、utils/env_utils.py](#六utilsenv_utilspy)
- [七、utils/file_utils.py](#七utilsfile_utilspy)
- [八、utils/process_utils.py](#八utilsprocess_utilspy)
- [九、B 新增专属模块](#九b-新增专属模块)
- [十、全局风险汇总](#十全局风险汇总)

---

## 一、core/engine_manager.py

### 行数对比

| 版本 | 行数 |
|------|------|
| A | 82 |
| B | 99 |

### Import 变化

| 变更 | 说明 |
|------|------|
| `ENGINE_ADAPTER_PACKAGE = "wings.engines"` → `"app.engines"` | 包路径适配 |
| B 新增 `ENGINE_ADAPTER_ALIASES = {"vllm_ascend": "vllm"}` | 引擎别名映射 |

### 函数级对比

| 函数 | 状态 | 关键差异 |
|------|------|----------|
| `start_engine_service()` | **重大重构** | **返回类型**：`-> None (return True)` → `-> str`（返回 shell 脚本内容） |
| | | **调用接口**：`adapter.start_engine(params)` → `adapter.build_start_script(params)`，fallback `build_start_command(params)` |
| | | **执行模型**：进程内启动 → 脚本生成 |
| | | **vllm_ascend 处理**：硬编码 `if` → 别名字典查询 |
| | | **新增 `exec` 包装**：对 `build_start_command` 返回的单行命令自动加 `exec`，确保引擎进程成为 PID 1 |
| | | **日志格式**：f-string → %-style（性能+安全） |

### 风险项

| 优先级 | 风险 |
|--------|------|
| **P0** | 所有 adapter 必须从 `start_engine()` 迁移到 `build_start_script()` / `build_start_command()` |
| P2 | `build_start_script` 异常未在此层捕获，需确保上层有兜底 |

---

## 二、core/hardware_detect.py

### 行数对比

| 版本 | 行数 |
|------|------|
| A | 42 |
| B | 118 |

### 架构级变更：SDK 探测 → 环境变量驱动

| 维度 | A | B |
|------|---|---|
| **依赖** | `wings.utils.device_utils.get_device_info()` — torch/pynvml SDK | 仅 `os.getenv()` — 零外部依赖 |
| **探测方式** | 实时查询硬件 | 从 `WINGS_DEVICE`/`DEVICE`、`WINGS_DEVICE_COUNT`/`DEVICE_COUNT` 读取 |
| **适用场景** | 单容器（引擎和控制逻辑在同一容器） | **Sidecar 架构**（控制容器无 GPU 访问权） |

### B 新增函数

| 函数 | 作用 |
|------|------|
| `_normalize_device(raw)` | 设备类型标准化：支持 `nvidia/gpu/cuda/ascend/npu` 5 种别名 |
| `_parse_count(raw)` | 防御性数量解析：非数字/负数/零 → 回退 `1` |

### 环境变量双层回退

| 环境变量 | 优先级 | 回退 | 默认值 |
|----------|--------|------|--------|
| `WINGS_DEVICE` | 高 | `DEVICE` | `"nvidia"` |
| `WINGS_DEVICE_COUNT` | 高 | `DEVICE_COUNT` | `"1"` |
| `WINGS_DEVICE_NAME` | — | — | `""` |

### 输出差异

| 字段 | A | B |
|------|---|---|
| `details` | SDK 完整设备列表（含型号、显存等） | 仅 `[{"name": "..."}]` 或 `[]` |

### 风险项

| 优先级 | 风险 |
|--------|------|
| **P1** | K8s 部署 YAML 未设置 `WINGS_DEVICE_COUNT` → 默认值 `1` → 多卡环境静默性能降级 |
| P2 | `details` 信息大幅简化，若下游依赖显存字段做分片决策将失败 |
| P2 | 未识别设备类型（如 `"tpu"`）静默回退 `"nvidia"` 而非报错 |

---

## 三、proxy/gateway.py

### 行数对比

| 版本 | 行数 |
|------|------|
| A | 750 |
| B | 849 |

### 函数级对比

#### 完全一致（~25 个函数/路由）

`_raw_send`, `_RETRIABLE_EXC`, `_should_retry_status`, `_close_resp_quiet`, `_mark_retry_count`, `_log_and_wait_status_retry`, `_is_retriable_exception`, `_log_and_maybe_wait_exception`, `_startup`, `_shutdown`, `_copy_entity_headers`, `_merge_obs_and_retry_headers`, `_content_length`, `_send_nonstream_request`, `_pipe_nonstream`, `_acquire_gate_early_nonstream`, `_forward_nonstream`, `_should_flush_first_packet`, `_should_flush`, `_stream_gen`, `_build_passthrough_headers`, `_acquire_gate_early`, `rerank`, `embeddings`, `tokenize`, `_extract_metrics_headers`, `_pipe_metrics`, `hv_text2video`

#### 逻辑变更

| 函数/路由 | 变更内容 |
|-----------|----------|
| `_send_stream_request()` | 超时从硬编码 → `C.STREAM_BACKEND_CONNECT_TIMEOUT` 等可配置常量 |
| `_forward_stream()` | 移除冗余 `released_early` 防御代码块 |
| `chat_completions()` | 新增 JSON 解析异常结构化日志 `elog("chat_json_parse_error", ...)` |
| `completions()` | 同上 `elog("completions_json_parse_error")` |
| `responses()` | 同上 `elog("responses_json_parse_error")` |
| `metrics()` | 超时从硬编码 → `C.METRICS_CONNECT_TIMEOUT` 可配置 |
| `hv_text2video_status()` | 超时从硬编码 → `C.STATUS_CONNECT_TIMEOUT` / `C.STATUS_READ_TIMEOUT` |
| `version_proxy()` | 移除冗余 try/except（`os.getenv` 不抛异常） |

#### B 新增路由

| 路由 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 显式健康检查路由，返回完整 JSON |
| `/health` | HEAD | K8s 轻量级探针路由 |
| `/v1/models` | GET | 透传后端模型列表接口 |

### 风险项

| 优先级 | 风险 |
|--------|------|
| P1 | B 依赖 `C.STREAM_BACKEND_CONNECT_TIMEOUT` 等新常量，settings.py 若未定义将 `AttributeError` |

---

## 四、proxy/health.py

### 行数对比

| 版本 | 行数 |
|------|------|
| A | 669 |
| B | 629 |

### Import 变化

`wings.proxy.settings` → `app.proxy.settings`，`wings.proxy.tags` → `app.proxy.tags`

### 关键变更

| 函数 | 类型 | 说明 |
|------|------|------|
| `WINGS_SKIP_PID_CHECK` | **新增** | 环境变量，K8s sidecar 场景跳过进程 PID 校验 |
| `_strict_probe_backend_health()` | 修改 | MindIE 地址从硬编码 `127.0.0.2:1026` → `os.getenv("MINDIE_HEALTH_HOST/PORT")` |
| `_advance_state_machine()` | 修改 | 引入 `effective_pid_ok`，与 `WINGS_SKIP_PID_CHECK` 联动 |
| `_handle_sglang_specifics()` | **行为变更** | A: 异常被吞掉（`# raise`）→ B: 异常重新抛出（`raise`） |
| `map_http_code_from_state()` | 修改 | 使用 `effective_pid_ok` 替代 `h["pid_alive"]` |
| `_read_pid_from_file()` | 增强 | 异常新增 `debug` 日志（A 静默 pass） |
| `_is_mindie()` / `_is_sglang()` | 增强 | 异常新增 `debug` 日志 |

### Bug 修复 🐛

| Bug | A 代码 | B 修复 |
|-----|--------|--------|
| **URL 空格** | `f"http://127.0.0.1: {proxy_port}/..."` — 冒号后有空格 | `f"http://127.0.0.1:{proxy_port}/..."` |
| **端口默认值** | 默认 `"18080"` | 默认 `"18000"`（与 PROXY_PORT 一致） |

### 风险项

| 优先级 | 风险 |
|--------|------|
| **P1** | sglang 异常从吞掉改为重新抛出，如果该函数频繁抛异常可能影响状态机推进 |
| P2 | `WINGS_SKIP_PID_CHECK=true` 后引擎进程死亡仅依赖 K8s 探针兜底 |

---

## 五、proxy/health_service.py

### 行数对比

| 版本 | 行数 |
|------|------|
| A | 103 |
| B | 74 |

### 变更

| 变更 | 说明 |
|------|------|
| **清理死代码** | 移除未使用的 `import time`, `from typing import Optional, Tuple`，修复重复导入 `build_health_headers` |
| **shutdown 顺序修正** | A: 手动 `task.cancel()` → `await` → `client.aclose()` → `teardown`（冗余二次取消）；B: `teardown()` → `client.aclose()`（正确、简洁） |

### 风险项

无高风险项。

---

## 六、utils/env_utils.py

### 行数对比

| 版本 | 行数 |
|------|------|
| A | 277 |
| B | 307 |

### 变更

| 变更 | 说明 |
|------|------|
| 清理 `from csv import Error` | A 中的未使用 import，B 移除 |
| `get_server_port()` / `get_master_port()` / `get_worker_port()` | B 新增 `try/except ValueError` 保护，防止非数字字符串导致崩溃 |
| 文件头 | B 新增 18 行模块文档注释 |

#### 完全一致的函数（18 个）

`validate_ip`, `get_master_ip`, `get_local_ip`, `get_node_ips`, `get_vllm_distributed_port`, `get_sglang_distributed_port`, `get_lmcache_env`, `get_qat_env`, `get_pd_role_env`, `get_router_env`, `get_router_instance_group_name_env`, `get_router_instance_name_env`, `get_router_nats_path_env`, `get_operator_acceleration_env`, `get_soft_fp8_env`, `get_config_force_env`, `log_kvcache_offload_config`, `check_env`

### 风险项

| 优先级 | 风险 |
|--------|------|
| P2 | `get_vllm_distributed_port()` / `get_sglang_distributed_port()` 未添加同样的 ValueError 保护，不一致 |

---

## 七、utils/file_utils.py

### 行数对比

| 版本 | 行数 |
|------|------|
| A | 157 |
| B | 169 |

### 变更

**零逻辑变更**。所有 5 个函数（`get_directory_size`, `safe_write_file`, `check_permission_640`, `check_torch_dtype`, `load_json_config`）的函数体完全一致。B 仅增加文件头文档注释。

---

## 八、utils/process_utils.py

### 行数对比

| 版本 | 行数 |
|------|------|
| A | 131 |
| B | 149 |

### 变更

| 变更 | 说明 |
|------|------|
| `wings.utils.file_utils` → `app.utils.file_utils` | import 路径更新 |
| 文件头 | B 新增文档注释 |

**零逻辑变更**。所有 3 个函数（`wait_for_process_startup`, `log_process_pid`, `log_stream`）完全一致。

### 注意

B 中 `_LOG_DIR` 仍指向 `"wings"` 子目录，迁移后可能需要调整。

### B 中的冗余文件

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `utils/http_client.py` | 88 | **死代码** | 标注 deprecated，全项目无引用 |
| `utils/wings_file_utils.py` | 173 | **死代码** | `file_utils.py` 完整副本，全项目无引用 |

---

## 九、B 新增专属模块

### 9.1 core/port_plan.py（65 行）

**用途**：Sidecar 三层端口规划（backend 17000 / proxy 18000 / health 19000）

| 导出 | 说明 |
|------|------|
| `PortPlan` | frozen dataclass: `enable_proxy`, `backend_port`, `proxy_port`, `health_port` |
| `derive_port_plan()` | 根据启动参数推导端口方案 |

**设计**：零外部依赖、Value Object 模式。

**注意**：`port=0` 时 `port or 18000` 会隐式回退；无端口冲突检测。

---

### 9.2 core/start_args_compat.py（224 行）

**用途**：将旧版 `wings_start.sh` 的 shell 参数语义迁移到 Python，支持 CLI + 环境变量双来源。

| 导出 | 说明 |
|------|------|
| `LaunchArgs` | frozen dataclass (34 字段)：host/port/model/engine/量化/分布式等 |
| `build_parser()` | 构建完整 CLI 解析器 |
| `parse_launch_args()` | 解析+校验 → `LaunchArgs` |

**优先级**：CLI 参数 > 环境变量 > 硬编码默认值

**注意**：`engine` 校验用 `.lower()` 但存储原始值，可能有大小写不一致风险。

---

### 9.3 core/wings_entry.py（113 行）

**用途**：Launcher 编排中枢，将所有子系统串联为一个 Pipeline。

| 导出 | 说明 |
|------|------|
| `LauncherPlan` | frozen dataclass: `command`, `merged_params`, `hardware_env` |
| `build_launcher_plan()` | 核心编排函数：硬件探测 → 配置合并 → 参数覆盖 → 脚本生成 |

**执行流**：

```
LaunchArgs + PortPlan
    ↓
detect_hardware()
    ↓
load_and_merge_configs(hardware, namespace)
    ↓
显式覆盖 engine/model_name/model_path
    ↓
注入分布式参数 + backend_port
    ↓
start_engine_service(merged) → shell 脚本
    ↓
包装 "#!/usr/bin/env bash\nset -euo pipefail\n"
    ↓
LauncherPlan(command=..., merged_params=..., hardware_env=...)
```

**跨模块架构**：

```
start_args_compat.py ──→ LaunchArgs
                              │
port_plan.py ──→ PortPlan     │
                    │         ↓
                    │    wings_entry.py ──→ LauncherPlan
                    │         │
                    │    ┌────┼────────────┐
                    │    ↓    ↓            ↓
                    │  hardware  config   engine_manager
                    │  _detect   _loader  (adapter)
                    │
              ┌─────┴──────┬──────────┐
              ↓            ↓          ↓
         gateway.py   health_svc   K8s YAML
        (proxy_port)  (health_port)
```

---

## 十、全局风险汇总

### P0（高优先级）

| # | 模块 | 风险 |
|---|------|------|
| 1 | `engine_manager.py` | adapter 接口从 `start_engine()` → `build_start_script()`，**不向后兼容** |

### P1（中优先级）

| # | 模块 | 风险 |
|---|------|------|
| 2 | `hardware_detect.py` | K8s 未设置 `WINGS_DEVICE_COUNT` → 默认 `1` → 多卡静默降级 |
| 3 | `health.py` | sglang 异常从吞掉改为重新抛出，可能影响健康监控循环 |
| 4 | `gateway.py` | 新增超时常量依赖 `C.STREAM_BACKEND_CONNECT_TIMEOUT` 等 |

### P2（低优先级）

| # | 模块 | 风险 |
|---|------|------|
| 5 | `hardware_detect.py` | details 字段简化，下游若依赖显存字段可能失败 |
| 6 | `env_utils.py` | 分布式端口解析函数缺少 ValueError 保护（不一致） |
| 7 | `process_utils.py` | `_LOG_DIR` 仍指向 `"wings"` 子目录 |
| 8 | `start_args_compat.py` | `engine` 校验 lower 但存储原值，大小写不一致 |
| 9 | B 通用 | 2 个死文件（`http_client.py`, `wings_file_utils.py`）应清理 |

### 统计摘要

| 维度 | 数值 |
|------|------|
| 分析文件对数 | 8 对 + 3 个新增 = 11 个文件 |
| 完全一致的函数 | ~55 个 |
| 逻辑变更的函数 | ~15 个 |
| Bug 修复 | 4 处（URL 空格、端口默认值、死 import、shutdown 冗余） |
| B 新增路由 | 3 个（`/health` GET/HEAD、`/v1/models`） |
| B 新增模块 | 3 个（port_plan 65 行、start_args_compat 224 行、wings_entry 113 行） |
| B 冗余文件 | 2 个（可安全删除） |
