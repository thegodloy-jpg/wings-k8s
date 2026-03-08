# 设计方案 v2：wings 架构移植到 infer-control-sidecar（基于源码分析）

> 本文档基于对 `wings/` 和 `infer-control-sidecar/` 两个项目源码的完整阅读，  
> 明确标注每个模块的**复用策略（直接复用 / 薄改造 / 新写）**，  
> MVP 范围：单机非分布式 + 仅 vLLM 引擎。

---

## 核心差异：一句话

wings 的 `vllm_adapter.start_engine()` 做了两件事：**拼命令** + **`subprocess.Popen` 启动进程**。  
sidecar 场景只需要**拼命令**，把命令字符串写入共享卷，由引擎容器自行 `bash` 执行。  
因此 `vllm_adapter.py` 只需剥离进程启动部分，保留命令构建逻辑即可。

---

## 各组件复用策略

### 层次一览

```
wings 原模块                      sidecar 对应模块          复用策略

wings/core/hardware_detect.py   app/core/hardware_detect.py  直接复用（去掉 wings.utils 依赖）
wings/utils/device_utils.py     app/utils/device_utils.py    直接复用（去掉 torch_npu 硬依赖）
wings/utils/file_utils.py       app/utils/file_utils.py      直接复用 load_json_config 函数
wings/config/engine_parameter_mapping.json
                                app/config/engine_parameter_mapping.json  直接复用（仅保留 vllm 段）
wings/config/nvidia_default.json (llm.default.vllm 部分)
                                app/config/vllm_default.json  裁剪复用
wings/core/config_loader.py     app/core/config_loader.py    薄改造（去掉与 argparse / 分布式 / Ascend 相关逻辑）
wings/engines/vllm_adapter.py   app/engines/vllm_adapter.py  薄改造（只保留命令构建，去掉进程启动）
wings/wings.py (main 流程)      app/core/wings.py             薄改造（用 settings 替代 argparse，去掉分布式分支）

infer-control-sidecar/api/routes.py        保持不变
infer-control-sidecar/services/proxy_service.py  保持不变
infer-control-sidecar/utils/http_client.py       保持不变
infer-control-sidecar/utils/file_utils.py        保持不变（write_command_to_volume 已有）
infer-control-sidecar/services/engine_manager.py  改造（调用 wings 流程替代 CommandBuilder）
infer-control-sidecar/config/settings.py         改造（扩展 env 字段）
infer-control-sidecar/services/command_builder.py  删除（被 vllm_adapter 替代）
```

---

## 目标目录结构

```
backend/app/
 main.py                                # 不变（lifespan 调 engine_manager.start()）
 api/
    routes.py                          # 不变
 config/
    settings.py                        # 改：扩展 env 字段
    vllm_default.json                  # 新（裁剪自 nvidia_default.json，仅 llm.default.vllm 段）
    engine_parameter_mapping.json      # 新（直接复用 wings 版，仅保留 vllm 映射段）
 core/                                  # 全新目录
    __init__.py
    wings.py                           # 新（薄改造自 wings/wings.py main 流程）
    config_loader.py                   # 新（薄改造自 wings/core/config_loader.py）
    hardware_detect.py                 # 新（直接复用 wings/core/hardware_detect.py，解耦依赖）
 engines/                               # 全新目录
    __init__.py
    vllm_adapter.py                    # 新（薄改造自 wings/engines/vllm_adapter.py）
 services/
    engine_manager.py                  # 改：接收 final_params  调 adapter  写共享卷
    proxy_service.py                   # 不变
 utils/
     device_utils.py                    # 新（直接复用 wings/utils/device_utils.py，解耦依赖）
     file_utils.py                      # 不变（已有 write_command_to_volume）
     http_client.py                     # 不变
```

删除：`services/command_builder.py`

---

## 启动流程

```
FastAPI lifespan()
  
  ▶ engine_manager.start()
        
         1. resolve_engine_params(settings)       core/wings.py  "决策+配置"
               
                hardware_detect.detect_hardware()
                    优先级：env DEVICE_TYPE  /dev/nvidia*  调用 device_utils.get_device_info()
                    返回：{"device": "nvidia", "count": N, "details": [...]}
               
                config_loader.load_and_merge_configs(hardware_env, settings)
                      加载 vllm_default.json（默认参数）
                      settings 字段覆盖（env 优先级高于默认 JSON）
                      _set_common_params()   使用 engine_parameter_mapping.json 做参数名映射
                          model_path     model (--model)
                          model_name     served_model_name (--served-model-name)
                          max_num_seqs   max_num_seqs (--max-num-seqs)
                          ...
                      返回 final_params{"engine":"vllm", "engine_config":{已映射参数}}
        
         2. vllm_adapter.build_command(final_params)    engines/vllm_adapter.py  "拼命令"
                返回纯字符串，如：
                    python3 -m vllm.entrypoints.openai.api_server \
                      --model /models/DeepSeek-R1-... \
                      --served-model-name DeepSeek-R1-... \
                      --host 127.0.0.1 --port 8000 \
                      --tensor-parallel-size 1 \
                      --max-model-len 8192 \
                      --gpu-memory-utilization 0.9 \
                      --max-num-seqs 256 \
                      --trust-remote-code --dtype auto
        
         3. write_command_to_volume(command, "/shared-volume", "start_command.sh")
                写文件，引擎容器 bash 执行
        
         4. wait_for_engine_ready()
                 轮询 GET http://127.0.0.1:{ENGINE_PORT}/health
```

---

## 各模块改造细节

### 1. `utils/device_utils.py`（直接复用）

**来源**：`wings/utils/device_utils.py` 完整复制  
**改动**：
- 将 `from wings.utils.xxx import` 替换为相对 import 或标准库
- `torch_npu` / NPU 相关代码用 `try/except ImportError` 保护（MVP 仅 NVIDIA，不能因缺少 `torch_npu` 而崩溃）
- 删除 `is_h20_gpu`（MVP 不需要）

**保留函数**：`get_device_info()`, `get_nvidia_gpu_info()`, `get_available_device()`

---

### 2. `core/hardware_detect.py`（直接复用）

**来源**：`wings/core/hardware_detect.py` 完整复制  
**改动**：
- `from wings.utils.device_utils import get_device_info`  `from app.utils.device_utils import get_device_info`
- 在 `detect_hardware()` 入口处增加一行：优先读 `os.getenv("DEVICE_TYPE")` 作为 fallback（sidecar 容器可能无法调用 `nvidia-smi`，env 注入更可靠）

```python
def detect_hardware() -> Dict[str, Any]:
    # sidecar 场景：env 强制指定时直接返回，不做实际设备探测
    forced = os.getenv("DEVICE_TYPE", "").lower()
    if forced in ("nvidia", "ascend", "cpu"):
        count = int(os.getenv("DEVICE_COUNT", "1"))
        return {"device": forced, "count": count, "details": [], "units": "GB"}
    
    # 走原 wings 逻辑（调用 get_device_info）
    ...
```

---

### 3. `config/vllm_default.json`（裁剪复用）

**来源**：`wings/config/nvidia_default.json` 中 `model_deploy_config.llm.default.vllm` 段  
**内容**（MVP 最小集，不含 host/port/TP运行时注入）：

```json
{
  "trust_remote_code": true,
  "max_model_len": 4096,
  "gpu_memory_utilization": 0.9,
  "max_num_seqs": 256,
  "dtype": "auto",
  "enforce_eager": false
}
```

---

### 4. `config/engine_parameter_mapping.json`（直接复用）

**来源**：`wings/config/engine_parameter_mapping.json`，仅保留 `default_to_vllm_parameter_mapping` 段。

```json
{
  "default_to_vllm_parameter_mapping": {
    "host": "host",
    "port": "port",
    "model_name": "served_model_name",
    "model_path": "model",
    "input_length": "",
    "output_length": "",
    "trust_remote_code": "trust_remote_code",
    "dtype": "dtype",
    "kv_cache_dtype": "kv_cache_dtype",
    "quantization": "quantization",
    "gpu_memory_utilization": "gpu_memory_utilization",
    "enable_chunked_prefill": "enable_chunked_prefill",
    "max_num_batched_tokens": "max_num_batched_tokens",
    "block_size": "block_size",
    "max_num_seqs": "max_num_seqs",
    "seed": "seed",
    "enable_prefix_caching": "enable_prefix_caching",
    "tensor_parallel_size": "tensor_parallel_size",
    "max_model_len": "max_model_len"
  }
}
```

---

### 5. `core/config_loader.py`（薄改造）

**来源**：`wings/core/config_loader.py` 中以下函数**直接复用**：
- `_merge_configs()`  纯 dict 深合并，无外部依赖
- `_set_common_params()`  使用 engine_parameter_mapping.json 做参数名转换
- `_set_sequence_length()`  纯逻辑
- `_set_parallelism_params()`  只保留单机分支 `_adjust_tensor_parallelism()`
- `_merge_vllm_params()`  只保留非分布式、非 Ascend 的核心路径
- `_merge_final_config()`  trivial

**改造点**：`load_and_merge_configs()` 入参从 `argparse.Namespace` 改为 `Settings` 对象：

```python
def load_and_merge_configs(hardware_env: Dict, settings: "Settings") -> Dict[str, Any]:
    """
    合并顺序（优先级由低到高）：
    1. vllm_default.json
    2. settings 字段（K8s env）
    3. 参数名映射（通用名  vLLM CLI 名）
    """
    defaults = _load_vllm_defaults()          # 读 config/vllm_default.json
    cmd_params = _settings_to_cmd_params(settings)   # Settings  通用名 dict
    merged = _merge_configs(defaults, {k: v for k, v in cmd_params.items() if v is not None})
    engine_config = _apply_vllm_param_mapping(merged, hardware_env)
    return {
        "engine": "vllm",
        "host": settings.ENGINE_HOST,
        "port": settings.ENGINE_PORT,
        "model_name": settings.MODEL_NAME,
        "model_path": settings.MODEL_PATH,
        "engine_config": engine_config
    }
```

**`_settings_to_cmd_params()` 是核心适配点**（对标 wings 的 `_process_cmd_args()`）：

```python
def _settings_to_cmd_params(settings: "Settings") -> Dict[str, Any]:
    """把 Settings（pydantic env）转为 wings _process_cmd_args 同格式 dict"""
    return {
        "engine":                  "vllm",
        "model_name":              settings.MODEL_NAME,
        "model_path":              settings.MODEL_PATH,
        "host":                    settings.ENGINE_HOST,
        "port":                    settings.ENGINE_PORT,
        "dtype":                   settings.DTYPE,
        "kv_cache_dtype":          settings.KV_CACHE_DTYPE,
        "gpu_memory_utilization":  settings.GPU_MEMORY_UTILIZATION,
        "trust_remote_code":       settings.TRUST_REMOTE_CODE,
        "enforce_eager":           settings.ENFORCE_EAGER,
        "quantization":            settings.QUANTIZATION,
        "max_num_seqs":            settings.MAX_NUM_SEQS,
        "tensor_parallel_size":    settings.TP_SIZE,
        "max_model_len":           settings.MAX_MODEL_LEN,
        "block_size":              settings.BLOCK_SIZE,
        "seed":                    settings.SEED,
        "distributed":             False,
        "gpu_usage_mode":          "full",
    }
```

**删除**（不进入 MVP）：
- `_load_user_config()`  外部 config-file 支持，后续可加
- `_auto_select_engine()`  引擎固定为 vllm
- `_merge_mindie_params()` / `_merge_sglang_params()`
- `_set_kv_cache_config()` / `_set_router_config()` / `_set_soft_fp8()`
- wings 自研模型相关（mmgm 等）

---

### 6. `engines/vllm_adapter.py`（薄改造）

**来源**：`wings/engines/vllm_adapter.py` 中以下函数**直接复用**：
- `_build_vllm_cmd_parts(params)`  核心，遍历 `engine_config` 拼 `--key value` 对
- `_build_vllm_command(params)`  组合完整命令

**删除**：
- `_start_vllm_single()` 及全部 `subprocess.Popen`
- `_build_base_env_commands()`（sidecar 不需要 `source set_vllm_env.sh`）
- 分布式函数（`start_vllm_distributed`, `_start_ray_*`, `_build_distributed_env_commands`）

**对外暴露** `build_command(params) -> str` 作为唯一入口（包装 `_build_vllm_cmd_parts`）：

```python
def build_command(params: Dict[str, Any]) -> str:
    """
    构建 vLLM 启动命令字符串（不启动进程，只返回字符串）。
    供 engine_manager 写入 /shared-volume/start_command.sh。
    """
    engine_config = params.get("engine_config", {}).copy()
    cmd_parts = ["python3", "-m", "vllm.entrypoints.openai.api_server"]
    for arg, value in engine_config.items():
        if value is None:
            continue
        arg_name = f"--{arg.replace('_', '-')}"
        if isinstance(value, bool):
            if value:
                cmd_parts.append(arg_name)
        elif isinstance(value, str) and value.strip().startswith('{'):
            cmd_parts.extend([arg_name, f"'{value}'"])
        else:
            cmd_parts.extend([arg_name, str(value)])
    return " ".join(cmd_parts)
```

---

### 7. `core/wings.py`（薄改造）

**来源**：`wings/wings.py` 中 `main()` 函数的单机非分布式路径  
**改造**：去掉 argparse、去掉分布式分支，参数来源改为 `Settings` 对象

```python
def resolve_engine_params(settings: "Settings") -> Dict[str, Any]:
    """
    决策 + 配置入口，对标 wings.py 的 main() 单机路径。
    供 engine_manager.start() 调用。
    """
    # 1. 硬件检测
    hardware_env = detect_hardware()
    logger.info(f"Detected hardware: {hardware_env}")

    # 2. 配置合并
    final_params = load_and_merge_configs(hardware_env, settings)
    logger.info(f"Final params engine_config keys: {list(final_params['engine_config'].keys())}")

    return final_params
```

---

### 8. `config/settings.py`（改造）

在现有字段基础上追加，保持向后兼容：

```python
class Settings(BaseSettings):
    #  现有字段（保持不变）
    ENGINE_TYPE: str = "vllm"
    ENGINE_HOST: str = "127.0.0.1"
    ENGINE_PORT: int = 8000
    WINGS_PORT: int = 9000
    MODEL_NAME: str = ""
    MODEL_PATH: str = "/models"
    TP_SIZE: int = 1
    MAX_MODEL_LEN: int = 4096
    SHARED_VOLUME_PATH: str = "/shared-volume"
    HEALTH_CHECK_INTERVAL: int = 5
    HEALTH_CHECK_TIMEOUT: int = 300

    #  新增（wings 参数映射用）
    DEVICE_TYPE: str = "nvidia"
    DEVICE_COUNT: int = 1
    DTYPE: Optional[str] = None
    KV_CACHE_DTYPE: Optional[str] = None
    GPU_MEMORY_UTILIZATION: float = 0.9
    TRUST_REMOTE_CODE: bool = True
    ENFORCE_EAGER: bool = False
    QUANTIZATION: Optional[str] = None
    BLOCK_SIZE: Optional[int] = None
    SEED: Optional[int] = None
    MAX_NUM_SEQS: int = 256
    ENABLE_PREFIX_CACHING: Optional[bool] = None
    ENABLE_CHUNKED_PREFILL: Optional[bool] = None
```

---

### 9. `services/engine_manager.py`（改造）

去掉 `CommandBuilder` 依赖，接入 wings 流程：

```python
from app.core.wings import resolve_engine_params
from app.engines.vllm_adapter import build_command
from app.utils.file_utils import write_command_to_volume

class EngineManager:
    async def start_engine(self) -> bool:
        # 1. 决策 + 配置（wings 核心流程）
        final_params = resolve_engine_params(settings)

        # 2. 拼命令（adapter 职责）
        command = build_command(final_params)
        logger.info(f"Built engine command: {command}")

        # 3. 写共享卷（不变）
        return await write_command_to_volume(
            command=command,
            shared_path=settings.SHARED_VOLUME_PATH,
            filename="start_command.sh"
        )

    # wait_for_engine_ready() 不变
```

---

## 参数流向图（完整）

```
K8s env
  
  
Settings
   MODEL_PATH="/models/DeepSeek..."        
   MODEL_NAME="DeepSeek-R1-..."               _settings_to_cmd_params()
   TP_SIZE=1                                   
   MAX_MODEL_LEN=8192                         cmd_params (通用名)
   GPU_MEMORY_UTILIZATION=0.9               
   MAX_NUM_SEQS=256                         
   TRUST_REMOTE_CODE=true                
                                               |
  vllm_default.json  _merge_configs() ▶ merged (通用名)
                                               |
  engine_parameter_mapping.json  _set_common_params() ▶ engine_config (vLLM 参数名)
    "model_path"  "model"
    "model_name"  "served_model_name"         |
    "max_num_seqs"  "max_num_seqs"            
                                         final_params
                                          {
                                            "engine": "vllm",
                                            "engine_config": {
                                              "model": "/models/...",
                                              "served-model-name": "DeepSeek-...",
                                              "host": "127.0.0.1",
                                              "port": 8000,
                                              "tensor-parallel-size": 1,
                                              "max-model-len": 8192,
                                              "gpu-memory-utilization": 0.9,
                                              "max-num-seqs": 256,
                                              "trust-remote-code": true,
                                              "dtype": "auto"
                                            }
                                          }
                                               
                                         build_command()
                                               
                                               
  python3 -m vllm.entrypoints.openai.api_server \
    --model /models/DeepSeek-R1-... \
    --served-model-name DeepSeek-R1-... \
    --host 127.0.0.1 --port 8000 \
    --tensor-parallel-size 1 \
    --max-model-len 8192 \
    --gpu-memory-utilization 0.9 \
    --max-num-seqs 256 \
    --trust-remote-code --dtype auto
                                               
                                  write_command_to_volume()
                                               
                                               
                         /shared-volume/start_command.sh
                                               
                                  引擎容器 bash 执行
```

---

## 变更文件清单（完整）

| 文件 | 动作 | 来源 | 说明 |
|---|---|---|---|
| `config/settings.py` | **改** | 原有 | 新增 wings 参数字段 |
| `services/engine_manager.py` | **改** | 原有 | 接入 wings 流程，去掉 CommandBuilder |
| `services/command_builder.py` | **删** | 原有 | 被 vllm_adapter 替代 |
| `core/__init__.py` | **新** |  | 空文件 |
| `core/wings.py` | **新** | 薄改造自 `wings/wings.py` main() | 决策入口 |
| `core/config_loader.py` | **新** | 薄改造自 `wings/core/config_loader.py` | 配置合并，去分布式/Ascend/特殊功能 |
| `core/hardware_detect.py` | **新** | 直接复用 `wings/core/hardware_detect.py` | 解耦 wings.utils 依赖，加 env 快速路径 |
| `engines/__init__.py` | **新** |  | 空文件 |
| `engines/vllm_adapter.py` | **新** | 薄改造自 `wings/engines/vllm_adapter.py` | 只保留命令构建，去掉进程启动 |
| `utils/device_utils.py` | **新** | 直接复用 `wings/utils/device_utils.py` | 保护 torch_npu 依赖 |
| `config/vllm_default.json` | **新** | 裁剪自 `wings/config/nvidia_default.json` | llm.default.vllm 段 |
| `config/engine_parameter_mapping.json` | **新** | 直接复用 `wings/config/engine_parameter_mapping.json` | 仅保留 vllm 段 |
| `api/routes.py` | **不变** |  | |
| `services/proxy_service.py` | **不变** |  | |
| `utils/file_utils.py` | **不变** |  | write_command_to_volume 已有 |
| `utils/http_client.py` | **不变** |  | |
| `main.py` | **不变** |  | lifespan 调 engine_manager.start() 不变 |

共 **2 改 + 1 删 + 10 新**（新文件均来自 wings 直接复用或薄改造）。

---

## v1  v2 主要变化

| 项目 | v1 设计 | v2 修正 |
|---|---|---|
| `hardware_detect.py` | 新写简化版 | **直接复用** wings 原版 + 增加 env DEVICE_TYPE 快速路径 |
| `device_utils.py` | 未提及 | **需要从 wings 复制**（hardware_detect 的依赖），保护 torch_npu |
| `config_loader.py` 签名 | 笼统描述 | 明确：`_settings_to_cmd_params()` 把 Settings  通用参数名 dict，再走原 wings 的 `_set_common_params()` |
| `vllm_adapter.py` 逻辑 | build_command() 新写 | 明确：直接复用 `_build_vllm_cmd_parts()` 原有逻辑，只加 wrapper |
| `vllm_default.json` 内容 | 含 host/port | **去掉** host/port/tensor_parallel_size（运行时注入），避免与 settings 重复 |
| `main.py` | 改动 | **不变**：lifespan 调 engine_manager.start() 链路不变，改动在 engine_manager 内部 |

---

## 验证方式

1. **单元**：`python -c "from app.core.wings import resolve_engine_params; ..."`
2. **命令验证**：检查 `build_command({...})` 输出是否包含期望的 `--arg value` 对
3. **集成**：`kubectl exec -it <pod> -c wings-infer -- cat /shared-volume/start_command.sh`
4. **端到端**：`curl http://<svc>:9000/health` 返回 `{"engine_ready": true}`
