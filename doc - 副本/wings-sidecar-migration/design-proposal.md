# 设计方案：将 wings 架构移植到 infer-control-sidecar

## 核心思路

保持 **K8s Sidecar 部署模型**（共享卷传命令）不变，把 `wings/` 的三层职责搬进来：

```
当前：settings.py(env) → command_builder.py(硬编码6参数) → 写共享卷 → 转发
目标：wings.py(决策+配置) → vllm_adapter.py(拼命令) → 写共享卷 → 转发
```

关键差异：`wings/` 用 `subprocess.Popen` 直拉进程，sidecar 用 **写文件到共享卷** → 引擎容器自行 `bash` 执行。所以 adapter 只需 **产出命令字符串**，不需要启动进程。

---

## MVP 范围

| 维度 | 范围 |
|---|---|
| 引擎 | 仅 vLLM |
| 部署 | 单机单 Pod（非分布式） |
| 硬件 | 仅 NVIDIA GPU（sidecar 容器内无 GPU 驱动，通过 env 注入或探测 `/dev/nvidia*`） |
| 配置合并 | 默认 JSON + K8s env 覆盖 + 参数映射 |
| 代理 | 保持现有 httpx 转发不变 |

---

## 目标目录结构

```
backend/app/
├── main.py                          # FastAPI 入口（改：lifespan 调用 wings 流程）
├── api/
│   └── routes.py                    # 保持不变
├── config/
│   ├── settings.py                  # 改：扩展环境变量字段
│   ├── vllm_default.json            # 新：vLLM 默认参数模板
│   └── engine_parameter_mapping.json # 新：通用参数 → vLLM CLI 参数名映射
├── core/                            # 新目录
│   ├── __init__.py
│   ├── wings.py                     # 新：决策入口（检测硬件 → 合并配置 → 返回 final_params）
│   ├── config_loader.py             # 新：加载默认 JSON + env 覆盖 + 参数映射
│   └── hardware_detect.py           # 新：硬件检测（简化版）
├── engines/                         # 新目录（替代 services/command_builder.py）
│   ├── __init__.py
│   └── vllm_adapter.py              # 新：拼 vLLM 命令字符串
├── services/
│   ├── engine_manager.py            # 改：接收 final_params → 调 adapter → 写共享卷
│   └── proxy_service.py             # 保持不变
└── utils/
    ├── file_utils.py                # 保持不变
    └── http_client.py               # 保持不变
```

删除：`services/command_builder.py`（被 `engines/vllm_adapter.py` 替代）

---

## 启动流程

```
main.py  lifespan()
  │
  ▼
core/wings.py  resolve_engine_params()          ← "决策+配置"
  ├─ 1. hardware_detect.detect_hardware()
  │     └─ 返回 {"device": "nvidia", "count": 1, "details": [...]}
  │        实现：检查 env DEVICE_TYPE / 探测 /dev/nvidia* / 默认 nvidia
  │
  ├─ 2. config_loader.load_and_merge_configs(hardware_env, settings)
  │     ├─ 加载 vllm_default.json（默认参数模板）
  │     ├─ 用 settings.py 中的 env 值覆盖默认值
  │     ├─ 通过 engine_parameter_mapping.json 做参数名转换
  │     │    model_path → --model
  │     │    model_name → --served-model-name
  │     │    gpu_memory_utilization → --gpu-memory-utilization
  │     │    ...
  │     └─ 返回 final_params: Dict[str, Any]
  │          含 engine_config（已映射为 vLLM CLI 参数名）
  │
  └─ 3. 返回 final_params
  │
  ▼
services/engine_manager.py  start(final_params)  ← "编排"
  ├─ 4. vllm_adapter.build_command(final_params)
  │     └─ 返回完整 bash 命令字符串（含 env 设置）
  │
  ├─ 5. file_utils.write_command_to_volume(command)
  │     └─ 写入 /shared-volume/start_command.sh
  │
  └─ 6. wait_for_engine_ready()
        └─ 轮询 GET http://127.0.0.1:{ENGINE_PORT}/health
```

---

## 各模块职责与关键接口

### 1. `core/hardware_detect.py`

```python
def detect_hardware() -> Dict[str, Any]:
    """
    返回 {"device": "nvidia", "count": N, "details": [...]}
    MVP: 优先读 env DEVICE_TYPE，否则探测 /dev/nvidia*，兜底 "nvidia"
    """
```

### 2. `config/vllm_default.json`（从 wings 移植简化）

```json
{
  "host": "127.0.0.1",
  "port": 8000,
  "trust_remote_code": true,
  "dtype": "auto",
  "gpu_memory_utilization": 0.9,
  "max_num_seqs": 256,
  "tensor_parallel_size": 1,
  "max_model_len": 4096,
  "enforce_eager": false
}
```

### 3. `config/engine_parameter_mapping.json`（从 wings 移植）

```json
{
  "model_path": "model",
  "model_name": "served_model_name",
  "gpu_memory_utilization": "gpu_memory_utilization",
  "max_num_seqs": "max_num_seqs",
  "tensor_parallel_size": "tensor_parallel_size",
  "max_model_len": "max_model_len",
  ...
}
```

### 4. `core/config_loader.py`

```python
def load_and_merge_configs(hardware_env: Dict, settings: Settings) -> Dict[str, Any]:
    """
    合并顺序：vllm_default.json → settings(env) 覆盖 → 参数名映射
    返回 {"engine": "vllm", "engine_config": {...已映射参数...}, ...}
    """
```

### 5. `core/wings.py`

```python
def resolve_engine_params(settings: Settings) -> Dict[str, Any]:
    """
    决策入口：detect → merge → 返回 final_params
    唯一对外接口，供 engine_manager 调用
    """
```

### 6. `engines/vllm_adapter.py`

```python
def build_command(params: Dict[str, Any]) -> str:
    """
    输入: final_params（含 engine_config）
    输出: 完整 bash 命令字符串，如:
      python3 -m vllm.entrypoints.openai.api_server \
        --model /models/xxx --host 127.0.0.1 --port 8000 ...
    """
```

### 7. `services/engine_manager.py`（改造）

```python
class EngineManager:
    async def start(self):
        # 1. 调 wings 决策
        final_params = resolve_engine_params(settings)
        # 2. 调 adapter 拼命令
        command = vllm_adapter.build_command(final_params)
        # 3. 写共享卷
        await write_command_to_volume(command, ...)
        # 4. 等待就绪
        await self.wait_for_engine_ready()
```

---

## 配置项扩展（settings.py 新增 env）

在现有基础上新增以下 K8s env 支持（全部可选，有默认值）：

| 新增 env | 默认值 | 说明 |
|---|---|---|
| `DEVICE_TYPE` | `nvidia` | 硬件类型 |
| `DTYPE` | `auto` | 数据类型 |
| `GPU_MEMORY_UTILIZATION` | `0.9` | GPU 显存利用率 |
| `TRUST_REMOTE_CODE` | `true` | 信任远端代码 |
| `ENFORCE_EAGER` | `false` | 是否禁用 CUDA Graph |
| `QUANTIZATION` | `""` | 量化方式 |
| `ENABLE_CHUNKED_PREFILL` | `false` | Chunked Prefill |
| `BLOCK_SIZE` | `""` | Block 大小 |
| `SEED` | `""` | 随机种子 |
| `MAX_NUM_SEQS` | `256` | 最大并发序列数 |

---

## 变更文件清单

| 文件 | 动作 | 说明 |
|---|---|---|
| `config/settings.py` | **改** | 扩展 env 字段 |
| `main.py` | **改** | lifespan 改调新流程 |
| `services/engine_manager.py` | **改** | 用 wings + adapter 替代 CommandBuilder |
| `services/command_builder.py` | **删** | 被 adapter 替代 |
| `core/__init__.py` | **新** | |
| `core/wings.py` | **新** | 决策+配置入口 |
| `core/config_loader.py` | **新** | 配置合并 |
| `core/hardware_detect.py` | **新** | 硬件检测 |
| `engines/__init__.py` | **新** | |
| `engines/vllm_adapter.py` | **新** | vLLM 命令构建 |
| `config/vllm_default.json` | **新** | 默认参数模板 |
| `config/engine_parameter_mapping.json` | **新** | 参数名映射 |
| `api/routes.py` | 不变 | |
| `services/proxy_service.py` | 不变 | |
| `utils/*` | 不变 | |

共 **3 改 + 1 删 + 8 新** = 12 个文件操作。

---

## 验证方式

MVP 完成后可通过以下方式验证整个链路：

1. **单元验证**：直接调用 `resolve_engine_params()` 检查输出参数是否正确
2. **命令验证**：调用 `build_command()` 检查生成的命令字符串格式
3. **集成验证**：在 K8s 中部署，检查 `/shared-volume/start_command.sh` 内容是否包含完整正确的 vLLM 命令
4. **端到端**：`curl http://<service>:9000/health` → engine_ready=true

---

## 与原 wings 项目的精简对照

| wings 原有能力 | MVP 是否包含 | 说明 |
|---|---|---|
| 硬件检测（NVIDIA/Ascend/CPU） | ✅ 简化版 | 仅 NVIDIA，通过 env 或 /dev 探测 |
| 多引擎支持（vLLM/SGLang/MindIE/Wings） | ❌ 仅 vLLM | 后续按需加 adapter |
| 配置合并（默认 JSON + CLI + unknown args） | ✅ 简化版 | 默认 JSON + env 覆盖 |
| 参数映射（engine_parameter_mapping.json） | ✅ | 直接移植 |
| 分布式（Master/Worker/Ray/dp_deployment） | ❌ | 不在 MVP 范围 |
| 代理层（Gateway 全功能） | ❌ 保持现有 | 保持 httpx 简单转发 |
| env 脚本 source（set_vllm_env.sh） | ❌ | sidecar 场景不需要 |
| subprocess.Popen 拉进程 | ❌ | 改为写共享卷文件 |
