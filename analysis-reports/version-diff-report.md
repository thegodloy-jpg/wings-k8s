# Unified vs Analyse 全量差异对比报告

> **unified 目录**: `infer-control-sidecar-unified/`（生产候选版，含 Bug 修复）  
> **analyse 目录**: `infer-control-sidecar-unified - analyse-wings-k8s/`  
> **排除项**: Accel 相关变更已同步，不再列出  
> **比对时间**: 2025-07

---

## 一、差异总览

| # | 文件 | 差异位置 | 类别 | 正确/更优版本 |
|---|------|----------|------|---------------|
| 1 | `wings_start.sh` | PORT/PROXY_PORT 逻辑 | **[BUG FIX]** | unified |
| 2 | `backend/app/main.py` | noise_filter 安装 | **[FEATURE]** | analyse |
| 3a | `backend/app/core/config_loader.py` L133 | VRAM free_memory 字段检查 | **[BUG FIX]** | unified |
| 3b | `backend/app/core/config_loader.py` L279 | WINGS_DEVICE_MEMORY 环境变量 | **[BUG FIX]** | analyse（但有 KeyError 风险） |
| 3c | `backend/app/core/config_loader.py` L677 | model_deploy_config 遗留配置补充 | **[BUG FIX]** | analyse |
| 3d | `backend/app/core/config_loader.py` L1003 | embedding/rerank 引擎选择 | **[BUG]** | unified（analyse 错误返回 vllm） |
| 3e | `backend/app/core/config_loader.py` L1013 | operator_acceleration 引擎选择 | **[BUG]** | unified（analyse 错误返回 vllm） |
| 3f | `backend/app/core/config_loader.py` L1523 | nodes_count 解析 | **[BUG]** | unified（analyse 用错误键名 "nodes"） |
| 4 | `backend/app/core/start_args_compat.py` | SUPPORTED_ENGINES | **[FEATURE]** | unified |
| 5 | `backend/app/core/wings_entry.py` | 文档注释增强 | **[REFACTOR]** | unified |
| 6 | `backend/app/engines/vllm_adapter.py` | NV 分布式 IP 检测 | **[BUG FIX]** | unified |
| 7 | `backend/app/engines/mindie_adapter.py` | NPU 环境脚本 + 守护进程模式 | **[FEATURE]** | unified |
| 8 | `backend/app/proxy/gateway.py` | noise_filter 安装 | **[FEATURE]** | analyse |
| 9 | `backend/app/proxy/health_service.py` | noise_filter 安装 | **[FEATURE]** | analyse |
| 10 | `backend/app/proxy/queueing.py` | @staticmethod + self 参数 | **[BUG]** | unified（analyse 有 TypeError） |
| 11 | `backend/app/proxy/__init__.py` | `__all__` 包含 warmup | **[BUG]** | unified（analyse 引用不存在模块） |
| 12 | `Dockerfile` | sed 去除 Windows 换行符 | **[BUG FIX]** | unified |
| 13 | `backend/app/distributed/` | 分布式编排模块 | **[FEATURE]** | 仅 unified |
| 14 | `k8s/` | 验证测试 YAML | **[CONFIG]** | 仅 unified |

---

## 二、逐项详细对比

### 1. wings_start.sh — [BUG FIX] unified ✅

**差异**: 端口导出逻辑

```bash
# ── unified（正确）──
if [ "$ENABLE_REASON_PROXY" = "false" ]; then
    export PORT=$BACKEND_PORT
fi
# ... 启动时传递:
python -m app.main --port $PROXY_PORT

# ── analyse ──
export PORT=$BACKEND_PORT          # 无条件导出
python -m app.main --port $BACKEND_PORT
```

**影响**: 当 `ENABLE_REASON_PROXY=true` 时，analyse 版本 PORT 和 BACKEND_PORT 相同，导致 proxy 与引擎端口冲突。unified 版本仅在禁用 proxy 时才导出 `PORT=BACKEND_PORT`，避免冲突。

**建议**: → **同步至 analyse**

---

### 2. backend/app/main.py — [FEATURE] analyse ✅

**差异**: 模块级噪声过滤器安装

```python
# ── analyse（多出）──
from app.utils.noise_filter import install_noise_filters
# ... 在 logger 创建后:
install_noise_filters()
```

unified 版本缺少此调用。`noise_filter` 模块在两个版本中都存在且内容一致。

**影响**: launcher 进程的日志噪声未被抑制（proxy/health 子进程在 analyse 中也有对应安装）。

**建议**: → **同步至 unified**

---

### 3. backend/app/core/config_loader.py — 多处差异

#### 3a. VRAM free_memory 字段检查 (L133) — [BUG FIX] unified ✅

```python
# ── unified（多出的保护逻辑）──
logger.warning("Cannot get VRAM details, skipping VRAM check")
return

# 如果 details 中缺少 free_memory 字段（只有 name），跳过 VRAM 检查
if not all("free_memory" in d for d in hardware_env["details"]):
    logger.warning("VRAM details lack free_memory field, skipping VRAM check")
    return

# ── analyse ──
# 缺少 free_memory 字段检查，当 details 只有 name 时会 KeyError
logger.warning("Cannot get VRAM details, skipping VRAM check")
return
```

**影响**: 某些硬件环境（如虚拟 GPU）的 details 只返回 `name` 字段不返回 `free_memory`，analyse 会 KeyError 崩溃。

**建议**: → **同步至 analyse**

---

#### 3b. WINGS_DEVICE_MEMORY 环境变量 (L279) — [BUG FIX] analyse ✅（需合并）

```python
# ── unified ──
total_memory = ctx["device_details"][0].get("total_memory", 12)
if total_memory is None:
    total_memory = 12
    logger.warning("total_memory is None in device details, defaulting to 12G")

# ── analyse（三级回退）──
if ctx["device_details"] and ctx["device_details"][0]:
    total_memory = ctx["device_details"][0]["total_memory"]   # ⚠️ 直接访问，可能 KeyError
else:
    mem_env = os.getenv("WINGS_DEVICE_MEMORY", "").strip()
    if mem_env:
        total_memory = float(mem_env)  # 带异常处理
    else:
        total_memory = 12
```

**分析**:
- analyse 增加了 `WINGS_DEVICE_MEMORY` 环境变量回退，K8s 部署可注入设备显存值 → **有价值**
- 但 analyse 用 `["total_memory"]` 直接访问而非 `.get()`，device_details 存在但缺少该字段时会 KeyError → **有风险**
- unified 用 `.get("total_memory", 12)` 更安全

**建议**: → **合并两者优点**：使用 `.get()` 安全访问 + `WINGS_DEVICE_MEMORY` 环境变量回退

---

#### 3c. model_deploy_config 遗留配置补充 (L677) — [BUG FIX] analyse ✅

```python
# ── unified ──
return load_json_config(default_config_path)

# ── analyse（补充逻辑）──
config = load_json_config(default_config_path)
if "model_deploy_config" not in config and default_file == "vllm_default.json":
    # vllm_default.json 缺少 model_deploy_config 时，从旧版 {device_type}_default.json 加载
    legacy_path = os.path.join(os.path.dirname(default_config_path),
                               f"{device_type}_default.json")
    if os.path.exists(legacy_path):
        legacy_config = load_json_config(legacy_path)
        if "model_deploy_config" in legacy_config:
            config["model_deploy_config"] = legacy_config["model_deploy_config"]
return config
```

**影响**: 当 `vllm_default.json` 缺少 `model_deploy_config` 段时，旧版按设备类型的配置文件可补充。兼容历史配置。

**建议**: → **同步至 unified**

---

#### 3d. embedding/rerank 引擎选择 (L1003) — [BUG] analyse ❌

```python
# ── unified（正确）──
elif model_type in ["embedding", "rerank"]:
    logger.warning(f"model type is {model_type}, automatically switched to VLLM_Ascend engine")
    return "vllm_ascend"    # ← 正确：embedding/rerank 在昇腾上应走 vllm_ascend

# ── analyse（错误）──
elif model_type in ["embedding", "rerank"]:
    logger.warning(f"model type is {model_type}, automatically switched to VLLM_Ascend engine")
    return vllm              # ← 错误：返回 'vllm' 而非 "vllm_ascend"
```

**影响**: 日志说 "switched to VLLM_Ascend engine" 但实际返回 `vllm`，导致 embedding/rerank 模型在昇腾设备上错误地使用 vllm 引擎。

**建议**: → **修复 analyse**，使用 `return "vllm_ascend"`

---

#### 3e. operator_acceleration 引擎选择 (L1013) — [BUG] analyse ❌

```python
# ── unified（正确）──
elif get_operator_acceleration_env():
    logger.warning("operator_acceleration is enabled, automatically switched to VLLM_Ascend engine")
    return "vllm_ascend"    # ← 正确

# ── analyse（错误）──
elif get_operator_acceleration_env():
    logger.warning("operator_acceleration is enabled, automatically switched to VLLM_Ascend engine")
    return vllm              # ← 错误：返回 'vllm' 而非 "vllm_ascend"
```

**影响**: 同 3d，日志与实际行为不一致。算子加速需要 vllm_ascend 引擎，但 analyse 错误地走了 vllm。

**建议**: → **修复 analyse**，使用 `return "vllm_ascend"`

---

#### 3f. nodes_count 解析 (L1523) — [BUG] analyse ❌

```python
# ── unified（正确）──
if cmd_known_params.get("nnodes"):        # ← 键名 "nnodes" 匹配 argparse 定义
    nodes_count = cmd_known_params.get("nnodes")  # int 类型直接使用

# ── analyse（错误）──
if cmd_known_params.get("nodes"):         # ← 键名 "nodes" 不存在！argparse 定义的是 "nnodes"
    nodes_count = len(cmd_known_params.get("nodes").split(','))  # 永远不会执行
```

**根因**: `start_args_compat.py` 中定义 `--nnodes` → 存储为 `nnodes`（两个版本一致）。analyse 使用 `"nodes"` 键名不匹配，`get("nodes")` 永远返回 `None`，`nodes_count` 始终为 `1`。多节点场景下 VRAM 检查失效。

另外 `split(',')` 对 `int` 类型也无法调用，即使键名正确也会 AttributeError。

**建议**: → **修复 analyse**，使用 `cmd_known_params.get("nnodes")`

---

### 4. backend/app/core/start_args_compat.py — [FEATURE] unified ✅

```python
# ── unified ──
SUPPORTED_ENGINES = {"vllm", "vllm_ascend", "sglang", "mindie", "wings"}

# ── analyse ──
SUPPORTED_ENGINES = {"vllm", "vllm_ascend", "sglang", "mindie"}
```

**影响**: unified 支持 `wings` 引擎（MMGM 多模态网关），analyse 不支持。

**建议**: → **同步至 analyse**（如果 analyse 也需要 wings 引擎支持）

---

### 5. backend/app/core/wings_entry.py — [REFACTOR] unified ✅

**差异**: 仅文档注释增强，无功能代码变更。

unified 增加了以下函数的 docstring：
- `_shell_escape_single_quote()` — 添加了转义逻辑说明
- `_build_engine_patch_options_export()` — 添加了参数/返回值文档
- Accel 相关段落的注释（已在 Accel 同步范围内）

**建议**: → **可选同步**（纯文档改善）

---

### 6. backend/app/engines/vllm_adapter.py — [BUG FIX] unified ✅

**差异**: NV (NVIDIA) 分布式节点 IP 检测

```bash
# ── unified（正确）── Head 节点
VLLM_HOST_IP=${POD_IP:-$(python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print(s.getsockname()[0]); s.close()")}

# ── analyse ── Head 节点
VLLM_HOST_IP=$(hostname -i)
```

```bash
# ── unified（多出）── Worker 节点
VLLM_HOST_IP=${POD_IP:-$(python3 -c "import socket; ...")}
export VLLM_HOST_IP

# ── analyse ── Worker 节点
# 无对应 VLLM_HOST_IP 设置
```

**影响**:
- `hostname -i` 在 K8s Pod 内可能返回 bridge IP（172.17.x.x），不适合 Ray 跨节点通信
- `POD_IP` 由 K8s `status.podIP` 注入，保证是 Pod 网络 IP
- Python socket fallback 处理无 `POD_IP` 的场景
- Worker 节点同样需要正确的 `VLLM_HOST_IP`，analyse 缺失

**建议**: → **同步至 analyse**

---

### 7. backend/app/engines/mindie_adapter.py — [FEATURE] unified ✅

**差异 A**: NPU 环境设置脚本 (L149)

```bash
# ── unified（多出）──
source /usr/local/Ascend/atb-models/set_env.sh 2>/dev/null || true
source /usr/local/Ascend/nnal/atb/set_env.sh 2>/dev/null || true
export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/driver:$LD_LIBRARY_PATH
export GRPC_POLL_STRATEGY=poll
```

**差异 B**: 守护进程启动模式 (L614)

```bash
# ── unified（后台+等待模式）──
./bin/mindieservice_daemon &
MINDIE_PID=$!
wait $MINDIE_PID
EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ]; then
    echo "[wings] MindIE daemon exited with code $EXIT_CODE" >&2
fi
exit $EXIT_CODE

# ── analyse（简单 exec）──
exec ./bin/mindieservice_daemon
```

**影响**:
- 差异 A: NPU 硬件需要加载 atb/nnal 环境，否则 MindIE 无法访问 NPU 设备
- 差异 B: background+wait 模式可捕获退出码并输出诊断信息，`exec` 模式下 exit code 丢失

**建议**: → **同步至 analyse**

---

### 8. backend/app/proxy/gateway.py — [FEATURE] analyse ✅

```python
# ── analyse（多出）──
@app.on_event("startup")
async def _startup():
    from ..utils.noise_filter import install_noise_filters
    install_noise_filters()
```

**影响**: proxy 子进程中安装日志噪声过滤器，抑制无用警告。

**建议**: → **同步至 unified**（与 #2 main.py 的 noise_filter 一同）

---

### 9. backend/app/proxy/health_service.py — [FEATURE] analyse ✅

```python
# ── analyse（多出）──
@app.on_event("startup")
async def _health_startup():
    from ..utils.noise_filter import install_noise_filters
    install_noise_filters()
```

**影响**: health 子进程中安装日志噪声过滤器。

**建议**: → **同步至 unified**（与 #2, #8 一同）

---

### 10. backend/app/proxy/queueing.py — [BUG] analyse ❌

```python
# ── unified（正确）──
@staticmethod
def _queue_disabled_raise(rid: str | None):

# ── analyse（错误）──
@staticmethod
def _queue_disabled_raise(self, rid: str | None):   # ← self 不应出现在 @staticmethod 中
```

**影响**: `@staticmethod` 方法不能有 `self` 参数。调用 `_queue_disabled_raise("some_rid")` 时，`self` 绑定到 `"some_rid"`，`rid` 缺参数 → **TypeError 运行时崩溃**。

**建议**: → **修复 analyse**，删除 `self` 参数

---

### 11. backend/app/proxy/__init__.py — [BUG] analyse ❌

```python
# ── unified ──
__all__ = [..., "settings", "tags"]

# ── analyse ──
__all__ = [..., "settings", "tags", "warmup"]
```

`warmup.py` 在两个版本中均**不存在**。

**影响**: `from app.proxy import *` 或显式 `from app.proxy import warmup` 时 → **ImportError**。

**建议**: → **修复 analyse**，删除 `"warmup"` 或创建 `warmup.py` 模块

---

### 12. Dockerfile — [BUG FIX] unified ✅

```dockerfile
# ── unified ──
RUN sed -i 's/\r//' ./wings_start.sh && chmod +x ./wings_start.sh

# ── analyse ──
RUN chmod +x ./wings_start.sh
```

**影响**: Windows 环境编辑的 shell 脚本可能含 `\r`（CRLF），不去除会导致 bash 执行报错 `/bin/bash^M: bad interpreter`。

**建议**: → **同步至 analyse**

---

### 13. backend/app/distributed/ — [FEATURE] 仅 unified

**仅 unified 包含**的分布式编排模块：

| 文件 | 行数 | 功能 |
|------|------|------|
| `__init__.py` | 空 | 包初始化 |
| `master.py` | 314 | 主节点编排器（调度、监控、心跳） |
| `worker.py` | 279 | 工作节点代理（注册、任务执行、健康上报） |
| `monitor.py` | 139 | 集群监控（状态聚合、异常检测） |
| `scheduler.py` | 150 | 任务调度器（负载均衡、节点选择） |

**影响**: 完整的多节点分布式推理编排能力。analyse 版本不具备此功能。

**建议**: → **按需同步至 analyse**

---

### 14. k8s/ — [CONFIG] 仅 unified

**仅 unified 包含**的验证/测试 YAML（非生产部署）：

- `statefulset-nv-verify.yaml` — NV GPU 单节点验证
- `statefulset-nv-verify-dp.yaml` — NV GPU 数据并行验证
- `statefulset-nv-single-148.yaml` — 148 节点单卡测试
- `statefulset-nv-single-150.yaml` — 150 节点单卡测试
- `mindie-single-deploy.yaml` — MindIE 单节点部署
- `vllm-ascend-dist-deploy.yaml` — vLLM Ascend 分布式部署

**建议**: → **可选同步**（开发调试用途）

---

### 15. 已确认内容一致的文件（17 个）

以下文件两个版本内容完全一致，无需操作：

- `backend/app/config/settings.py`
- `backend/app/core/engine_manager.py`
- `backend/app/core/hardware_detect.py`
- `backend/app/core/port_plan.py`
- `backend/app/engines/sglang_adapter.py`
- `backend/app/proxy/simple_proxy.py`
- `backend/app/proxy/speaker_logging.py`
- `backend/app/proxy/tags.py`
- `backend/app/proxy/settings.py`
- `backend/app/proxy/http_client.py`
- `backend/app/proxy/health.py`
- `backend/app/utils/device_utils.py`
- `backend/app/utils/env_utils.py`
- `backend/app/utils/file_utils.py`
- `backend/app/utils/model_utils.py`
- `backend/app/utils/noise_filter.py`
- `backend/app/utils/process_utils.py`

---

## 三、同步优先级建议

### 🔴 高优先级（必须修复的 Bug）

| # | 方向 | 内容 |
|---|------|------|
| 3d | analyse → 修复 | `return vllm` → `return "vllm_ascend"` (embedding/rerank) |
| 3e | analyse → 修复 | `return vllm` → `return "vllm_ascend"` (operator_acceleration) |
| 3f | analyse → 修复 | `"nodes"` → `"nnodes"` + 删除 `.split(',')` |
| 10 | analyse → 修复 | 删除 `@staticmethod` 方法中的 `self` 参数 |
| 11 | analyse → 修复 | 删除 `__all__` 中不存在的 `"warmup"` |

### 🟠 高优先级（Bug Fix 功能同步）

| # | 方向 | 内容 |
|---|------|------|
| 1 | unified → analyse | PORT/PROXY_PORT 端口冲突修复 |
| 3a | unified → analyse | VRAM free_memory 字段保护 |
| 6 | unified → analyse | NV 分布式 POD_IP 检测 |
| 12 | unified → analyse | Dockerfile sed 去 CR |

### 🟡 中优先级（功能增强同步）

| # | 方向 | 内容 |
|---|------|------|
| 3b | 合并两者 | WINGS_DEVICE_MEMORY 环境变量 + `.get()` 安全访问 |
| 3c | analyse → unified | model_deploy_config 遗留配置补充 |
| 2,8,9 | analyse → unified | noise_filter 安装（main.py + gateway.py + health_service.py） |
| 7 | unified → analyse | MindIE NPU 环境 + 守护进程模式 |

### 🟢 低优先级（可选同步）

| # | 方向 | 内容 |
|---|------|------|
| 4 | unified → analyse | SUPPORTED_ENGINES 添加 "wings" |
| 5 | unified → analyse | wings_entry.py 文档注释 |
| 13 | unified → analyse | distributed/ 分布式模块（按需） |
| 14 | unified → analyse | k8s 验证 YAML（按需） |
