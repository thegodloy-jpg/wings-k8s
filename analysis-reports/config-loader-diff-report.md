# config_loader.py 逐行 Diff 分析报告

> **A（原版）**: `wings/wings/core/config_loader.py` — 1163 行  
> **B（统一版）**: `infer-control-sidecar-unified/backend/app/core/config_loader.py` — 1564 行  
> **增量**: +401 行（+34.5%），主要来自 5 个新函数 + 大量 docstring 补全

---

## 一、变更总览

| 变更类型 | 数量 | 重要程度 |
|---------|------|---------|
| Bug 修复 | 1 | 🔴 关键 |
| 新增函数 | 5 | 🟠 重要 |
| 健壮性增强 | 4 | 🟡 中等 |
| 环境变量化 | 3 | 🟡 中等 |
| 导入路径迁移 | 全部 | 🟢 机械 |
| Docstring 补全 | ~30 函数 | 🟢 规范 |
| 逻辑完全一致 | ~20 函数 | — 无变化 |

---

## 二、模块头部 & 导入

### 2.1 Module Docstring

```diff
- # -*- coding: utf-8 -*-
- """config loader"""
+ # -*- coding: utf-8 -*-
+ """config_loader — 推理引擎配置加载与合并
+ ...（22行架构级文档）...
+ """
```

B 增加了完整的模块级文档，描述了配置合并优先级、新增函数清单、K8s Sidecar 适配说明。

### 2.2 Import 路径

```diff
- from wings.core.model_identifier import ModelIdentifier
- from wings.core.hardware_detect import is_h20_gpu
- from wings.utils.file_utils import load_json_config
- from wings.utils.env_utils import (...)
+ from app.core.model_identifier import ModelIdentifier
+ from app.utils.file_utils import load_json_config
+ from app.utils.env_utils import (...)
```

- 所有 `wings.*` → `app.*`（Sidecar 包结构）
- **移除** `from wings.core.hardware_detect import is_h20_gpu`（被新函数 `_get_h20_model_hint()` 替代）

---

## 三、常量定义

### 3.1 `DEFAULT_CONFIG_DIR`

```diff
- DEFAULT_CONFIG_DIR = os.path.join(os.path.dirname(__file__), '..', 'config')
+ DEFAULT_CONFIG_DIR = _resolve_default_config_dir()
```

B 新增 `_resolve_default_config_dir()` 函数：

```python
def _resolve_default_config_dir() -> str:
    env_dir = os.getenv("APP_CONFIG_DIR")
    if env_dir and os.path.isdir(env_dir):
        return env_dir
    return os.path.join(os.path.dirname(__file__), '..', 'config')
```

**意义**：K8s 部署中配置目录可通过 `APP_CONFIG_DIR` 环境变量注入（如 ConfigMap 挂载路径），不再依赖 `__file__` 相对路径。

### 3.2 `DEFAULT_CONFIG_FILES["nvidia"]`

```diff
- "nvidia": "nvidia_default.json",
+ "nvidia": "vllm_default.json",
```

**意义**：标准化配置文件命名，`nvidia_default.json` 作为 legacy 回退（在 `_load_default_config` 中处理）。

### 3.3 新增常量

```python
+ SUPPORTED_DEVICE_TYPES = {"nvidia", "ascend"}  # 仅 B
```

用于 `_load_default_config` 中的设备类型校验。

---

## 四、新增函数（仅 B 中存在）

### 4.1 `_load_mapping(path, key)` — 防御性 JSON 读取

```python
def _load_mapping(path: str, key: str) -> dict:
    raw = load_json_config(path)
    if not isinstance(raw, dict) or key not in raw:
        logger.warning("mapping file '%s' missing key '%s'", path, key)
        return {}
    mapping = raw[key]
    if not isinstance(mapping, dict):
        logger.warning("key '%s' is not a dict in '%s'", key, path)
        return {}
    return mapping
```

替代 A 中的裸 `load_json_config(path)[key]`，避免 KeyError / TypeError 崩溃。

### 4.2 `_get_h20_model_hint()` — 环境变量替代 SDK 调用

```python
def _get_h20_model_hint() -> str:
    return os.getenv("H20_MODEL_HINT", "")
```

A 中使用 `is_h20_gpu(main_gpu.get("total_memory", 0), 10.0)` 依赖 pynvml SDK；B 改为读取环境变量，因为 Sidecar 容器内没有 GPU 驱动。

### 4.3 `_load_engine_fallback_defaults(engine)` — 引擎级兜底配置

```python
def _load_engine_fallback_defaults(engine: str) -> Dict[str, Any]:
    fallback_file = DEFAULT_CONFIG_FILES.get(engine)
    if not fallback_file:
        return {}
    path = os.path.join(DEFAULT_CONFIG_DIR, fallback_file)
    if not os.path.exists(path):
        return {}
    return load_json_config(path)
```

当 `model_deploy_config` 中完全没有该 model_type 的条目时，不再崩溃，而是回退到引擎级默认配置。

### 4.4 `_handle_mindie_distributed(distributed_config, cmd_params)` — MindIE 分布式支持

```python
def _handle_mindie_distributed(distributed_config, cmd_params):
    mindie_cfg = distributed_config.get('mindie_distributed', {})
    master_port = mindie_cfg.get('master_port', 27070)
    cmd_params.update({
        'mindie_master_addr': get_master_ip(),
        'mindie_master_port': master_port,
    })
```

A 的 `_handle_distributed` 只处理 vllm 和 sglang。B 新增 `mindie` 分支。

### 4.5 `_resolve_default_config_dir()` — 动态配置目录

已在 3.1 节描述。

---

## 五、Bug 修复

### 5.1 `_set_soft_fp8()` — JSON 嵌套错误 🔴

**A（有 Bug）**：

```python
additional_config = json.dumps({
    "ascend_scheduler_config": {
        "prefill_timeslice": prefill_timeslice,
        "decode_timeslice": decode_timeslice,
    "torchair_graph_config": {           # ← 错误！嵌套到了 ascend_scheduler_config 内部
        "enable_single_stream": True
    }
    }
})
```

`torchair_graph_config` 被错误地放在了 `ascend_scheduler_config` 字典**内部**（JSON 缩进/花括号对齐错误）。

**B（已修复）**：

```python
additional_config = json.dumps({
    "ascend_scheduler_config": {
        "prefill_timeslice": prefill_timeslice,
        "decode_timeslice": decode_timeslice,
    },                                    # ← 正确闭合
    "torchair_graph_config": {            # ← 独立顶级键
        "enable_single_stream": True
    }
})
```

**影响**：在 Ascend 设备上启用 soft_fp8 时，A 生成错误的 additional_config JSON，导致 vllm_ascend 引擎解析配置异常。

---

## 六、健壮性增强

### 6.1 `_write_engine_second_line(path, engine)`

```diff
  # A — 假设文件存在
- with open(path, "r+", encoding="utf-8") as f:
-     lines = f.read().splitlines()
-     ...
-     f.seek(0)
-     f.write("\n".join(lines) + "\n")
-     f.truncate()

  # B — 容错：自动创建目录和文件
+ try:
+     parent = os.path.dirname(path)
+     if parent:
+         os.makedirs(parent, exist_ok=True)
+     lines = []
+     if os.path.exists(path):
+         with open(path, "r", encoding="utf-8") as f:
+             lines = f.read().splitlines()
+     ...
+     with open(path, "w", encoding="utf-8") as f:
+         f.write("\n".join(lines) + "\n")
+ except Exception as e:
+     logger.warning("Write engine marker file failed (%s): %s", path, e)
```

**差异点**：
| 项目 | A | B |
|------|---|---|
| 文件不存在 | `FileNotFoundError` 崩溃 | 自动创建 |
| 目录不存在 | 崩溃 | `os.makedirs` 自动创建 |
| 写入异常 | 上抛异常终止进程 | `logger.warning` 降级 |
| 文件打开模式 | `r+`（读写） | 先 `r` 读再 `w` 写 |

### 6.2 `_get_model_specific_config()`

```diff
  # A — 裸字典索引
- models_dict = default_config[config_model_key][model_type]
- ...
- engine_specific_defaults = config[engine_key]

  # B — 防御性 .get() + 兜底
+ model_deploy_config = default_config.get(config_model_key, {})
+ if not isinstance(model_deploy_config, dict):
+     model_deploy_config = {}
+ models_dict = model_deploy_config.get(model_type, {})
+ if not models_dict:
+     engine_specific_defaults = _load_engine_fallback_defaults(engine)
+     return _merge_cmd_params(...)
+ ...
+ engine_specific_defaults = config.get(engine_key, {})
```

A 中如果 default_config 结构不完整（缺少 model_deploy_config 或 model_type 键），直接 KeyError 崩溃。B 添加了 4 层防御：
1. `.get()` 返回空字典
2. `isinstance()` 类型检查
3. 空值 warning 日志
4. 回退到 `_load_engine_fallback_defaults()`

### 6.3 `_load_default_config()`

```diff
  # B 新增
+ if device_type not in SUPPORTED_DEVICE_TYPES:
+     logger.warning("Unsupported device type '%s', fallback to 'nvidia'", device_type)
+     device_type = "nvidia"
+ ...
+ if not os.path.exists(default_config_path) and default_file == "vllm_default.json":
+     legacy_path = os.path.join(DEFAULT_CONFIG_DIR, f"{device_type}_default.json")
+     if os.path.exists(legacy_path):
+         logger.warning("Fallback to legacy default config: %s", legacy_path)
+         default_config_path = legacy_path
```

B 增加设备类型校验和 legacy 配置文件回退逻辑。

### 6.4 `_auto_select_engine()`

```diff
  # A
- _write_engine_second_line("/var/log/wings/wings.txt", engine)
  # B
+ _write_engine_second_line(os.getenv("BACKEND_PID_FILE", "/var/log/wings/wings.txt"), engine)
```

路径可通过环境变量配置，适配不同的容器挂载路径。

---

## 七、环境变量化（SDK ↛ ENV）

| 配置项 | A（原版） | B（统一版） |
|--------|---------|-----------|
| 配置目录 | `os.path.dirname(__file__)/../config` | `os.getenv("APP_CONFIG_DIR", ...)` |
| H20 卡型 | `is_h20_gpu(gpu_memory, threshold)` SDK 调用 | `os.getenv("H20_MODEL_HINT", "")` |
| PD KV 端口 | `"20001"` 硬编码 | `os.getenv("PD_KV_PORT", "20001")` |
| PID 文件路径 | `"/var/log/wings/wings.txt"` 硬编码 | `os.getenv("BACKEND_PID_FILE", "...")` |

**设计原则**：Sidecar 容器无 GPU 驱动，所有硬件信息只能通过环境变量注入（由 K8s Pod spec 或 ConfigMap 传入）。

---

## 八、`_merge_mindie_params()` — 分布式分支新增

```diff
  # A — 无分布式处理
  def _merge_mindie_params(params, ctx, engine_cmd_parameter):
      ...
      _adjust_tensor_parallelism(params, ctx["device_count"], 'worldSize')
      params['npuDeviceIds'] = [[i for i in range(ctx["device_count"])]]
      return params

  # B — 新增 distributed 分支
+ if ctx.get("distributed"):
+     node_ips = get_node_ips()
+     n_nodes = len(...)
+     params['worldSize'] = int(ctx["device_count"]) * n_nodes
+     params['multiNodesInferEnabled'] = False
+     params['node_ips'] = node_ips
+     params['npuDeviceIds'] = [[i for i in range(ctx["device_count"])]]
+ else:
      _adjust_tensor_parallelism(params, ctx["device_count"], 'worldSize')
      params['npuDeviceIds'] = [[i for i in range(ctx["device_count"])]]
```

支持 MindIE 多节点分布式推理，设置全局 `worldSize = device_count × n_nodes`。

---

## 九、`_handle_distributed()` — 新增 MindIE 分支

```diff
  def _handle_distributed(engine, cmd_params, model_info):
      ...
      if engine in ['vllm', 'vllm_ascend']:
          _handle_vllm_distributed(...)
      elif engine == 'sglang':
          _handle_sglang_distributed(...)
+     elif engine == 'mindie':                    # B 新增
+         _handle_mindie_distributed(...)
```

---

## 十、完全一致的函数清单（≈20 个）

以下函数 A/B 之间业务逻辑完全相同（仅 import 路径和 docstring 有差异）：

| 函数名 | 行为 |
|--------|------|
| `_check_vram_requirements()` | VRAM 检查逻辑 |
| `_merge_cmd_params()` | 参数合并入口分发 |
| `_merge_vllm_params()` | vLLM 参数合并 |
| `_set_cuda_graph_sizes()` | CUDA Graph 大小设置 |
| `_set_lmcache()` | LMCache 配置 |
| `_get_pd_config()` | PD 配置（仅 kv_port 差异） |
| `_merge_configs()` | 递归深度合并字典 |
| `_load_user_config()` | 用户配置加载 |
| `_process_cmd_args()` | CLI 参数转字典 |
| `_select_nvidia_engine()` | NVIDIA 引擎选择 |
| `_select_ascend_engine()` | Ascend 引擎选择 |
| `_validate_user_engine()` | 用户引擎校验 |
| `_handle_vllm_distributed()` | vLLM 分布式配置 |
| `_handle_sglang_distributed()` | SGLang 分布式配置 |
| `_handle_ascend_vllm()` | Ascend vLLM 升级 |
| `_autodiscover_hunyuan_paths()` | HunyuanVideo 路径探测 |
| `_find_variant_directory()` | 变体目录查找 |
| `_find_dit_weight()` | DIT 权重查找 |
| `_find_vae_path()` | VAE 路径查找 |
| `_find_text_encoder_path()` | 编码器 1 查找 |
| `_find_text_encoder_2_path()` | 编码器 2 查找 |
| `_build_mmgm_engine_defaults()` | MMGM 引擎配置构建 |
| `_build_llm_engine_defaults()` | LLM 引擎配置构建 |
| `load_and_merge_configs()` | 主入口函数 |
| `_merge_final_config()` | 最终配置封装 |
| `_select_engine_automatically()` | 引擎自动选择分发 |

---

## 十一、Pathlib 内联导入清理

```diff
  # A — _find_dit_weight / _find_vae_path / _find_text_encoder_path 内部
- from pathlib import Path as _P
- return str((_P(root) / fn).resolve())

  # B — 顶层已导入 Path，函数内直接使用
+ return str((Path(root) / fn).resolve())
```

A 中 4 个函数各自内联 `from pathlib import Path as _P`，B 统一使用顶层 `from pathlib import Path`。

---

## 十二、结论

### 变更质量评估

| 维度 | 评分 | 说明 |
|------|------|------|
| 功能完整性 | `A+` | 所有 A 功能 100% 保留，新增 MindIE 分布式 |
| Bug 修复 | `A` | `_set_soft_fp8` JSON 嵌套 Bug 已修 |
| 健壮性 | `A` | 4 处显著增强（空值保护、文件创建、类型校验、兜底配置） |
| K8s 适配 | `A` | 3 处硬编码路径/SDK 调用改为环境变量 |
| 代码规范 | `A` | ~30 个函数补全了详细的 docstring |
| 向后兼容 | `A` | legacy 配置文件回退、默认值保持一致 |

### 风险点

1. **`DEFAULT_CONFIG_FILES["nvidia"]` 更名**：`nvidia_default.json` → `vllm_default.json`，需确保部署时配置文件已重命名。B 已有 legacy 回退逻辑，但仍需在 CI 检查。
2. **`H20_MODEL_HINT` 环境变量**：依赖外部注入，若部署清单遗漏此变量，DeepSeek 在 H20 卡上不会匹配到专属配置（会回退到默认配置，不会崩溃）。
3. **`multiNodesInferEnabled = False`**：MindIE 分布式分支中此值硬编码为 `False`，但 worldSize 已设为 `device_count * n_nodes`。需确认 MindIE SDK 是否允许此组合或是否需要设为 `True`。
